"""
Sniper core module — trigger-agnostic trading engine.

Usage:
    from sniper import Sniper, SniperConfig

    sniper = Sniper(SniperConfig())
    await sniper.start()
    tx_hash = await sniper.buy(ca)
"""
import asyncio
import re
import base58
import aiohttp
from dataclasses import dataclass, field

from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.signature import Signature
from solana.rpc.async_api import AsyncClient
from solana.rpc.models import TxOpts

import os
from dotenv import load_dotenv

load_dotenv()

import time as _time

def log(tag: str, msg: str):
    ts = _time.strftime("%H:%M:%S")
    print(f"[{ts}] [{tag}] {msg}")


SOLANA_CA_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')


@dataclass
class SniperConfig:
    private_key_b58: str = field(default_factory=lambda: os.environ.get("PRIVATE_KEY_B58"))
    rpc_url: str = field(default_factory=lambda: os.environ.get("RPC_URL"))
    buy_amount_sol: float = 0.22
    slippage_pct: int = 40
    buy_priority_fee_sol: float = 0.004
    sell_priority_fee_sol: float = 0.001
    exit_timeout_s: float = 12.0  # auto-sell 50% after this many seconds


class Sniper:
    """Trigger-agnostic Solana sniper. Call buy()/sell() from any trigger source."""

    def __init__(self, config: SniperConfig | None = None):
        self.cfg = config or SniperConfig()
        self.cached_blockhash: str | None = None
        self._refresher_task: asyncio.Task | None = None
        # Optional callback invoked when the keyboard 'r' command is received.
        # Signature: async def on_refetch() -> None. Set by the trigger layer.
        self.on_refetch = None

    # ==================== SETUP ====================
    def get_keypair(self) -> Keypair:
        return Keypair.from_bytes(base58.b58decode(self.cfg.private_key_b58))

    async def start(self):
        """Start background blockhash refresher and keyboard controls."""
        self._refresher_task = asyncio.create_task(self._blockhash_refresher())
        import threading
        threading.Thread(target=self._keyboard_listener, daemon=True).start()

    async def _blockhash_refresher(self):
        client = AsyncClient(self.cfg.rpc_url)
        try:
            while True:
                try:
                    res = await client.get_latest_blockhash()
                    if res and res.value:
                        self.cached_blockhash = str(res.value.blockhash)
                except Exception:
                    pass
                await asyncio.sleep(0.4)
        finally:
            await client.close()

    def _keyboard_listener(self):
        log("KBD", "Controls: [s] sell all  [ss] sell 50% only  [r] refetch trigger")
        while True:
            try:
                line = input().strip().lower()
            except EOFError:
                break
            if line in ("s", "ss", "r"):
                self._kbd_command = line
                log("KBD", f"Command received: '{line}'")
            elif line:
                log("KBD", f"Unknown command '{line}' — use s / ss / r")

    _kbd_command: str | None = None

    # ==================== VALIDATION ====================
    async def is_valid_solana_address(self, address: str, client: AsyncClient) -> bool:
        try:
            from solders.pubkey import Pubkey
            pubkey = Pubkey.from_string(address)
            resp = await client.get_account_info(pubkey)
            return resp.value is not None
        except Exception:
            return False

    async def find_valid_ca(self, matches: list[str]) -> str | None:
        client = AsyncClient(self.cfg.rpc_url)
        try:
            results = await asyncio.gather(
                *[self.is_valid_solana_address(m, client) for m in matches],
                return_exceptions=True
            )
            for match, valid in zip(matches, results):
                if valid is True:
                    return match
            return None
        finally:
            await client.close()

    # ==================== TX BUILD & SUBMISSION ====================
    async def build_pump_tx(self, action: str, mint: str, amount, priority_fee: float, denom_in_sol: bool = True) -> bytes | None:
        keypair = self.get_keypair()
        payload = {
            "publicKey": str(keypair.pubkey()),
            "action": action,
            "mint": mint,
            "denominatedInSol": "true" if denom_in_sol else "false",
            "amount": amount,
            "slippage": self.cfg.slippage_pct,
            "priorityFee": priority_fee,
            "pool": "auto"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://pumpportal.fun/api/trade-local",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    log("PUMPPORTAL", f"HTTP {resp.status} for {action} {mint[:8]}...")
                    return None
                log("PUMPPORTAL", f"TX built for {action} {mint[:8]}...")
                return await resp.read()

    async def sign_tx(self, tx_bytes: bytes) -> bytes:
        keypair = self.get_keypair()
        tx = VersionedTransaction.from_bytes(tx_bytes)
        return bytes(VersionedTransaction(tx.message, [keypair]))

    async def _send_jito_tx(self, session: aiohttp.ClientSession, signed_tx_bytes: bytes) -> bool:
        tx_b58 = base58.b58encode(signed_tx_bytes).decode("ascii")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [tx_b58, {"encoding": "base58"}]
        }
        jito_endpoints = [
            "https://mainnet.block-engine.jito.wtf/api/v1/transactions",
            "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/transactions",
            "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/transactions",
            "https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions",
        ]

        async def _send_one(url):
            try:
                async with session.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    return resp.status == 200
            except Exception:
                return False

        results = await asyncio.gather(*[_send_one(url) for url in jito_endpoints], return_exceptions=True)
        return any(r is True for r in results)

    async def _send_rpc_tx(self, signed_tx_bytes: bytes) -> str:
        tx = VersionedTransaction.from_bytes(signed_tx_bytes)
        tx_hash = str(tx.signatures[0])
        client = AsyncClient(self.cfg.rpc_url)
        try:
            await client.send_raw_transaction(
                signed_tx_bytes,
                opts=TxOpts(skip_preflight=True, skip_confirmation=True, max_retries=5)
            )
        except Exception as e:
            log("RPC", f"RPC Send notice: {e}")
        finally:
            await client.close()
        return tx_hash

    async def broadcast_tx(self, signed_tx_bytes: bytes) -> str:
        tx = VersionedTransaction.from_bytes(signed_tx_bytes)
        tx_hash = str(tx.signatures[0])
        log("BROADCAST", f"Submitting tx {tx_hash[:8]}... (Helius RPC + Jito Engines)")
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(
                self._send_rpc_tx(signed_tx_bytes),
                self._send_jito_tx(session, signed_tx_bytes),
                return_exceptions=True
            )
        log("BROADCAST", f"Submitted: https://solscan.io/tx/{tx_hash}")
        return tx_hash

    async def _monitor_tx_confirmation(self, tx_hash: str):
        client = AsyncClient(self.cfg.rpc_url)
        try:
            sig = Signature.from_string(tx_hash)
            for _ in range(20):
                await asyncio.sleep(0.5)
                sig_resp = await client.get_signature_statuses([sig])
                if sig_resp and sig_resp.value and sig_resp.value[0] is not None:
                    status = sig_resp.value[0]
                    if status.err:
                        log("RPC", f"TX {tx_hash[:8]}... failed on-chain: {status.err}")
                        return False
                    else:
                        log("RPC", f"TX {tx_hash[:8]}... CONFIRMED on-chain ({status.confirmation_status})")
                        return True
            log("RPC", f"TX {tx_hash[:8]}... monitoring timed out (may still land)")
            return False
        except Exception as e:
            log("RPC", f"Monitor notice: {e}")
            return False
        finally:
            await client.close()

    # ==================== PUBLIC TRADE API ====================
    async def execute_trade(self, action: str, mint: str, amount, denom_in_sol: bool = True) -> str | None:
        log(action.upper(), f"mint={mint[:8]}... amount={amount} sol={denom_in_sol}")
        tx_bytes = await self.build_pump_tx(
            action,
            mint,
            amount,
            self.cfg.buy_priority_fee_sol if action == "buy" else self.cfg.sell_priority_fee_sol,
            denom_in_sol
        )
        if not tx_bytes:
            log(action.upper(), "Build failed")
            return None

        signed = await self.sign_tx(tx_bytes)
        tx_hash = await self.broadcast_tx(signed)
        asyncio.create_task(self._monitor_tx_confirmation(tx_hash))
        return tx_hash

    async def buy(self, ca: str) -> str | None:
        """Buy a token, then start the automated partial-exit manager."""
        tx_hash = await self.execute_trade("buy", ca, self.cfg.buy_amount_sol, denom_in_sol=True)
        if tx_hash:
            asyncio.create_task(self._partial_exit(ca))
        else:
            log("BUY", "No position opened")
        return tx_hash

    async def sell(self, mint: str, amount="100%") -> str | None:
        return await self.execute_trade("sell", mint, amount=amount, denom_in_sol=False)

    # ==================== AUTOMATED PARTIAL EXIT ====================
    async def _partial_exit(self, mint: str):
        cfg = self.cfg

        log("EXIT", "Pre-building sell TXs...")
        sell100_bytes, sell50_bytes = await asyncio.gather(
            self.build_pump_tx("sell", mint, "100%", cfg.sell_priority_fee_sol, denom_in_sol=False),
            self.build_pump_tx("sell", mint, "50%", cfg.sell_priority_fee_sol, denom_in_sol=False),
        )
        sell100_signed = await self.sign_tx(sell100_bytes) if sell100_bytes else None
        sell50_signed = await self.sign_tx(sell50_bytes) if sell50_bytes else None

        if sell100_signed:
            log("EXIT", "Sell TXs ready — s=sell all  ss=sell half  r=refetch trigger")
        else:
            log("EXIT", "Pre-build failed — will build on demand")

        async def do_sell(signed: bytes | None, amount: str, label: str):
            if signed:
                tx_hash = await self.broadcast_tx(signed)
                asyncio.create_task(self._monitor_tx_confirmation(tx_hash))
                ok = True
            else:
                result = await self.execute_trade("sell", mint, amount=amount, denom_in_sol=False)
                ok = bool(result)
            log("EXIT", label if ok else "SELL FAILED — go to pump.fun NOW")

        for _ in range(int(cfg.exit_timeout_s / 0.2)):
            await asyncio.sleep(0.2)
            cmd = self._kbd_command
            if cmd is not None:
                self._kbd_command = None
                if cmd == "s":
                    log("EXIT", "Manual: selling 100%...")
                    await do_sell(sell100_signed, "100%", "100% sell submitted")
                    return
                elif cmd == "ss":
                    log("EXIT", "Manual: selling 50%...")
                    await do_sell(sell50_signed, "50%", "50% sell submitted — manage rest manually")
                    return
                elif cmd == "r":
                    if self.on_refetch:
                        log("EXIT", "Refetch requested — handing off to trigger")
                        await self.on_refetch()
                    else:
                        log("EXIT", "No refetch handler registered by trigger")
                    return

        log("EXIT", "Selling 50% (auto)...")
        await do_sell(sell50_signed, "50%", "50% sold — manage remaining 50% manually on pump.fun")
