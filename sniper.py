"""
Sniper core module — trigger-agnostic trading engine.

Usage:
    from sniper import Sniper, SniperConfig

    sniper = Sniper(SniperConfig())
    await sniper.start()
    tx_hash = await sniper.buy(ca)
"""
import asyncio
import base64
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

LOG_DIR = "runs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, _time.strftime("%Y-%m-%d_%H-%M-%S") + ".txt")

def log(tag: str, msg: str, console: bool = True):
    ts = _time.strftime("%H:%M:%S")
    ms = int((_time.time() % 1) * 1000)
    line = f"[{ts}.{ms:03d}] [{tag}] {msg}"
    if console:
        print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


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
    jito_tip_lamports: int = 1_000_000  # 0.001 SOL tip — 0 disables Jito path
    skip_ca_check: bool = True  # skip on-chain CA validation before buying (faster)
    simulate_before_send: bool = field(default_factory=lambda: os.environ.get("SIMULATE_BEFORE_SEND", "").lower() in ("1", "true", "yes"))  # sim tx before sending (adds ~100-400ms RPC round trip)


class Sniper:
    """Trigger-agnostic Solana sniper. Call buy()/sell() from any trigger source."""

    def __init__(self, config: SniperConfig | None = None):
        self.cfg = config or SniperConfig()
        self.cached_blockhash: str | None = None
        self._refresher_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        # Persistent RPC client — avoids TCP+TLS handshake on every send/poll
        self._rpc: AsyncClient | None = None
        # Optional callback invoked when the keyboard 'r' command is received.
        # Signature: async def on_refetch() -> None. Set by the trigger layer.
        self.on_refetch = None

    # ==================== SETUP ====================
    def _get_session(self) -> aiohttp.ClientSession:
        """Persistent HTTP session — avoids TLS handshake on every trade."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ttl_dns_cache=300, limit=20)
            )
        return self._session
    def get_keypair(self) -> Keypair:
        return Keypair.from_bytes(base58.b58decode(self.cfg.private_key_b58))

    async def start(self):
        """Start background blockhash refresher and keyboard controls."""
        self._refresher_task = asyncio.create_task(self._blockhash_refresher())
        import threading
        threading.Thread(target=self._keyboard_listener, daemon=True).start()

    def get_rpc(self) -> AsyncClient:
        """Persistent RPC client (lazy-created, reused across sends/polls)."""
        if self._rpc is None:
            self._rpc = AsyncClient(self.cfg.rpc_url)
        return self._rpc

    async def stop(self):
        if self._refresher_task:
            self._refresher_task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()
        if self._rpc:
            await self._rpc.close()
            self._rpc = None

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

    @staticmethod
    def pick_ca_fast(text: str) -> str | None:
        """First base58-looking candidate, no RPC validation (fast path)."""
        matches = SOLANA_CA_PATTERN.findall(text)
        return matches[0] if matches else None

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
        base_payload = {
            "publicKey": str(keypair.pubkey()),
            "action": action,
            "mint": mint,
            "denominatedInSol": "true" if denom_in_sol else "false",
            "amount": amount,
            "slippage": self.cfg.slippage_pct,
            "priorityFee": priority_fee,
        }
        # PumpPortal inserts the Jito tip transfer itself — no manual tx surgery.
        tip_sol = self.cfg.jito_tip_lamports / 1_000_000_000
        if tip_sol > 0:
            base_payload["jitoTip"] = tip_sol
        session = self._get_session()
        # Try bonding curve first, then PumpSwap (post-graduation), then auto.
        for pool in ("pump", "pump-amm", "auto"):
            payload = {**base_payload, "pool": pool}
            log("PUMPPORTAL", f"build {action} mint={mint[:8]}... pool={pool} payload={payload}", console=False)
            try:
                async with session.post(
                    "https://pumpportal.fun/api/trade-local",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        raw = await resp.read()
                        if not self.cfg.simulate_before_send:
                            log("PUMPPORTAL", f"TX built for {action} {mint[:8]}... (pool={pool}) — sim disabled, sending blind")
                            return raw
                        err, logs = await self._simulate_tx(raw)
                        if err is not None:
                            tail = [l for l in logs if "Error" in l or "failed" in l][:2]
                            log("PUMPPORTAL", f"pool={pool} SIM FAILED: {err} {' | '.join(tail)} — trying next pool", console=False)
                            if "UnsupportedQuoteMint" in (err or "") or any("UnsupportedQuoteMint" in l for l in tail):
                                log("PUMPPORTAL", f"SKIP {mint[:8]}... — token is not paired with SOL; direct buy impossible")
                                return None
                            continue
                        log("PUMPPORTAL", f"TX built for {action} {mint[:8]}... (pool={pool}) — sim OK")
                        return raw
                    body = await resp.text()
                    log("PUMPPORTAL", f"HTTP {resp.status} ({resp.reason}) pool={pool} for {action} {mint[:8]}... body={body[:200]}")
            except Exception as e:
                log("PUMPPORTAL", f"Request error pool={pool}: {e}")
        return None

    async def _simulate_tx(self, tx_bytes: bytes):
        """Simulate a signed tx. Returns (err, logs) — err None means it will land and execute."""
        try:
            tx = VersionedTransaction.from_bytes(tx_bytes)
            res = await self.get_rpc().simulate_transaction(tx)
            v = res.value
            return v.err, (v.logs or [])
        except Exception as e:
            log("RPC", f"simulate error: {e!r}")
            return "EXCEPTION", []

    async def sign_tx(self, tx_bytes: bytes) -> bytes:
        keypair = self.get_keypair()
        tx = VersionedTransaction.from_bytes(tx_bytes)
        return bytes(VersionedTransaction(tx.message, [keypair]))

    async def _send_jito_tx(self, signed_tx_bytes: bytes) -> bool:
        session = self._get_session()
        # base64 per Jito docs — base58 is deprecated ("slow")
        tx_b64 = base64.b64encode(signed_tx_bytes).decode("ascii")
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [tx_b64, {"encoding": "base64"}]
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
                    body = await resp.text()
                    log("JITO", f"{url.split('//')[1].split('.')[0]}: HTTP {resp.status} body={body[:400]}")
                    return resp.status == 200
            except Exception as e:
                log("JITO", f"send error {url}: {e!r}")
                return False

        results = await asyncio.gather(*[_send_one(url) for url in jito_endpoints], return_exceptions=True)
        return any(r is True for r in results)

    async def _send_rpc_tx(self, signed_tx_bytes: bytes) -> str:
        tx = VersionedTransaction.from_bytes(signed_tx_bytes)
        tx_hash = str(tx.signatures[0])
        try:
            resp = await self.get_rpc().send_raw_transaction(
                signed_tx_bytes,
                opts=TxOpts(skip_preflight=True, skip_confirmation=True, max_retries=5)
            )
            log("RPC", f"sendRawTransaction resp={resp!r}")
        except Exception as e:
            log("RPC", f"sendRawTransaction ERROR: {e!r}")
        return tx_hash

    async def broadcast_tx(self, signed_tx_bytes: bytes) -> str:
        tx = VersionedTransaction.from_bytes(signed_tx_bytes)
        tx_hash = str(tx.signatures[0])
        log("BROADCAST", f"Submitting tx {tx_hash[:8]}... (RPC + Jito Engines)")
        await asyncio.gather(
            self._send_rpc_tx(signed_tx_bytes),
            self._send_jito_tx(signed_tx_bytes),
            return_exceptions=True
        )
        log("BROADCAST", f"Submitted: https://solscan.io/tx/{tx_hash}")
        return tx_hash

    async def _monitor_tx_confirmation(self, tx_hash: str) -> bool:
        client = self.get_rpc()
        try:
            sig = Signature.from_string(tx_hash)
            for i in range(60):
                await asyncio.sleep(0.5)
                sig_resp = await client.get_signature_statuses([sig])
                status = sig_resp.value[0] if sig_resp and sig_resp.value else None
                if status is None:
                    log("RPC", f"poll {i}: TX {tx_hash[:8]}... not seen yet", console=False)
                    continue
                if status.err:
                    log("RPC", f"TX {tx_hash[:8]}... failed on-chain: {status.err}")
                    return False
                log("RPC", f"TX {tx_hash[:8]}... CONFIRMED on-chain ({status.confirmation_status})")
                return True
            # Final fallback: fetchTransaction (catches txs that landed late)
            final = await client.get_transaction(Signature.from_string(tx_hash), max_supported_transaction_version=0)
            if final and final.value:
                meta = getattr(final.value, "meta", None)
                log("RPC", f"TX {tx_hash[:8]}... found in history after timeout (meta.err={getattr(meta, 'err', None)})")
                return bool(meta and not meta.err)
            log("RPC", f"TX {tx_hash[:8]}... NEVER LANDED — not in status, not in history")
            return False
        except Exception as e:
            log("RPC", f"Monitor notice: {e!r}")
            return False

    # ==================== PUBLIC TRADE API ====================
    async def execute_trade(self, action: str, mint: str, amount, denom_in_sol: bool = True) -> tuple[str, bool] | None:
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
        if action == "buy":
            # Buys: await confirmation inline — sells depend on the position existing.
            confirmed = await self._monitor_tx_confirmation(tx_hash)
            log("BUY", f"buy {'CONFIRMED' if confirmed else 'NOT CONFIRMED'} ({tx_hash})")
            return (tx_hash, confirmed)
        asyncio.create_task(self._monitor_tx_confirmation(tx_hash))
        return (tx_hash, False)

    async def buy(self, ca: str) -> str | None:
        """Buy a token, then start the automated partial-exit manager."""
        result = await self.execute_trade("buy", ca, self.cfg.buy_amount_sol, denom_in_sol=True)
        if result:
            tx_hash, confirmed = result
            if confirmed:
                asyncio.create_task(self._partial_exit(ca))
            else:
                log("BUY", "Buy NOT confirmed — no exit manager started; check logs")
        else:
            log("BUY", "No position opened")
        return result[0] if result else None

    async def sell(self, mint: str, amount="100%") -> str | None:
        result = await self.execute_trade("sell", mint, amount=amount, denom_in_sol=False)
        return result[0] if result else None

    # ==================== AUTOMATED PARTIAL EXIT ====================
    async def _partial_exit(self, mint: str):
        cfg = self.cfg

        log("EXIT", "Pre-building sell TXs...")
        async def _prep(b):
            if not b:
                return None
            signed = await self.sign_tx(b)
            err, _logs = await self._simulate_tx(signed)
            if err is not None:
                log("EXIT", f"pre-built sell sim failed: {err} — will build on demand")
                return None
            return signed

        sell100_bytes, sell50_bytes = await asyncio.gather(
            self.build_pump_tx("sell", mint, "100%", cfg.sell_priority_fee_sol, denom_in_sol=False),
            self.build_pump_tx("sell", mint, "50%", cfg.sell_priority_fee_sol, denom_in_sol=False),
        )
        sell100_signed, sell50_signed = await asyncio.gather(_prep(sell100_bytes), _prep(sell50_bytes))

        if sell100_signed:
            log("EXIT", "Sell TXs ready — s=sell all  ss=sell half  r=refetch trigger")
        else:
            log("EXIT", "Pre-build failed — will build on demand")

        async def do_sell(signed: bytes | None, amount: str, label: str):
            if signed:
                tx_hash = await self.broadcast_tx(signed)
                ok = await self._monitor_tx_confirmation(tx_hash)
            else:
                result = await self.execute_trade("sell", mint, amount=amount, denom_in_sol=False)
                ok = bool(result and result[1])
            log("EXIT", (label + " CONFIRMED") if ok else "SELL FAILED — go to pump.fun NOW")

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
