import asyncio
import re
import base58
import aiohttp
from telethon import TelegramClient, events
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.models import TxOpts

import os
from dotenv import load_dotenv

load_dotenv()

# ==================== CONFIGURATION ====================
TG_API_ID = os.environ.get("TG_API_ID")
TG_API_HASH = os.environ.get("TG_API_HASH")
TG_CHANNEL = os.environ.get("TG_CHANNEL")

CA_KEYWORDS = ["ca", "CA", "contract", "Contract", "address", "Address", "mint","Mint", "$david", "$DAVID", "DAVID", "david"]

PRIVATE_KEY_B58 = os.environ.get("PRIVATE_KEY_B58")
RPC_URL = os.environ.get("RPC_URL")

BUY_AMOUNT_SOL = 0.85
SLIPPAGE_PCT = 40
BUY_PRIORITY_FEE_SOL = 0.004
SELL_PRIORITY_FEE_SOL = 0.001

already_triggered = False
SOLANA_CA_PATTERN = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')


# ==================== LOGGING ====================
import time as _time

def log(tag: str, msg: str):
    ts = _time.strftime("%H:%M:%S")
    print(f"[{ts}] [{tag}] {msg}")


# ==================== KEYBOARD CONTROLS ====================
kbd_command: str | None = None

def _keyboard_listener():
    global kbd_command
    log("KBD", "Controls: [s] sell all  [ss] sell 50% only  [r] fetch & buy last TG message")
    while True:
        try:
            line = input().strip().lower()
        except EOFError:
            break
        if line in ("s", "ss", "r"):
            kbd_command = line
            log("KBD", f"Command received: '{line}'")
        elif line:
            log("KBD", f"Unknown command '{line}' — use s / ss / r")


# ==================== HELPER FUNCTIONS ====================
def get_keypair() -> Keypair:
    return Keypair.from_bytes(base58.b58decode(PRIVATE_KEY_B58))

def message_has_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in CA_KEYWORDS)

async def is_valid_solana_address(address: str, client: AsyncClient) -> bool:
    try:
        from solders.pubkey import Pubkey
        pubkey = Pubkey.from_string(address)
        resp = await client.get_account_info(pubkey)
        return resp.value is not None
    except Exception:
        return False

async def find_valid_ca(matches: list[str]) -> str | None:
    client = AsyncClient(RPC_URL)
    try:
        results = await asyncio.gather(
            *[is_valid_solana_address(m, client) for m in matches],
            return_exceptions=True
        )
        for match, valid in zip(matches, results):
            if valid is True:
                return match
        return None
    finally:
        await client.close()


# ==================== PUMPORTAL EXECUTION ====================
async def build_pump_tx(action: str, mint: str, amount, priority_fee: float, denom_in_sol: bool = True) -> bytes | None:
    keypair = get_keypair()
    payload = {
        "publicKey": str(keypair.pubkey()),
        "action": action,
        "mint": mint,
        "denominatedInSol": "true" if denom_in_sol else "false",
        "amount": amount,
        "slippage": SLIPPAGE_PCT,
        "priorityFee": priority_fee,
        "pool": "auto"
    }
    headers = {"Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://pumpportal.fun/api/trade-local",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                log("PUMPPORTAL", f"HTTP {resp.status} for {action} {mint[:8]}...")
                return None
            log("PUMPPORTAL", f"TX built for {action} {mint[:8]}...")
            return await resp.read()

async def sign_tx(tx_bytes: bytes) -> bytes:
    keypair = get_keypair()
    tx = VersionedTransaction.from_bytes(tx_bytes)
    return bytes(VersionedTransaction(tx.message, [keypair]))

async def send_via_rpc(signed_tx_bytes: bytes) -> str | None:
    log("RPC", "Submitting...")
    client = AsyncClient(RPC_URL)
    try:
        res = await client.send_raw_transaction(
            signed_tx_bytes,
            opts=TxOpts(skip_preflight=True, skip_confirmation=False, max_retries=3)
        )
        tx_hash = str(res.value)
        log("RPC", f"Submitted: https://solscan.io/tx/{tx_hash}")
        await asyncio.sleep(3)
        sig_resp = await client.get_signature_statuses([res.value])
        status = sig_resp.value[0]
        if status is None:
            log("RPC", "TX not found after 3s — likely dropped")
        elif status.err:
            log("RPC", f"TX failed on-chain: {status.err}")
            return None
        else:
            log("RPC", f"TX confirmed ({status.confirmation_status})")
        return tx_hash
    except Exception as e:
        log("RPC", f"Error: {e}")
        return None
    finally:
        await client.close()

async def execute_trade(action: str, mint: str, amount, denom_in_sol: bool = True) -> str | None:
    log(action.upper(), f"mint={mint[:8]}... amount={amount} sol={denom_in_sol}")
    for attempt in range(2):
        tx_bytes = await build_pump_tx(action, mint, amount, BUY_PRIORITY_FEE_SOL if action == "buy" else SELL_PRIORITY_FEE_SOL, denom_in_sol)
        if not tx_bytes:
            log(action.upper(), f"Build failed (attempt {attempt + 1})")
            await asyncio.sleep(1)
            continue
        signed = await sign_tx(tx_bytes)
        tx_hash = await send_via_rpc(signed)
        if tx_hash:
            return tx_hash
        log(action.upper(), f"Failed (attempt {attempt + 1})")
        await asyncio.sleep(1)
    log(action.upper(), "All attempts exhausted — act manually")
    return None


# ==================== AUTOMATED PARTIAL EXIT ====================
async def partial_exit(mint: str, tg_client):
    global kbd_command, already_triggered

    # Pre-build both sell TXs in parallel immediately after buy confirms
    log("EXIT", "Pre-building sell TXs...")
    sell100_bytes, sell50_bytes = await asyncio.gather(
        build_pump_tx("sell", mint, "100%", SELL_PRIORITY_FEE_SOL, denom_in_sol=False),
        build_pump_tx("sell", mint, "50%", SELL_PRIORITY_FEE_SOL, denom_in_sol=False),
    )
    sell100_signed = await sign_tx(sell100_bytes) if sell100_bytes else None
    sell50_signed = await sign_tx(sell50_bytes) if sell50_bytes else None

    if sell100_signed:
        log("EXIT", "Sell TXs ready — s=sell all  ss=sell half  r=fetch & re-buy last TG msg")
    else:
        log("EXIT", "Pre-build failed — will build on demand")

    async def _rpc_nowait(signed: bytes) -> bool:
        """Submit pre-signed TX without waiting for confirmation."""
        client = AsyncClient(RPC_URL)
        try:
            res = await client.send_raw_transaction(
                signed,
                opts=TxOpts(skip_preflight=True, skip_confirmation=True, max_retries=3)
            )
            log("RPC", f"Sell submitted: https://solscan.io/tx/{res.value}")
            return True
        except Exception as e:
            log("RPC", f"Sell error: {e}")
            return False
        finally:
            await client.close()

    async def do_sell(signed: bytes | None, amount: str, label: str):
        if signed:
            ok = await _rpc_nowait(signed)
        else:
            result = await execute_trade("sell", mint, amount=amount, denom_in_sol=False)
            ok = bool(result)
        log("EXIT", label if ok else "SELL FAILED — go to pump.fun NOW")

    for _ in range(60):  # 60 * 0.2s = 12s
        await asyncio.sleep(0.2)
        cmd = kbd_command
        if cmd is not None:
            kbd_command = None
            if cmd == "s":
                log("EXIT", "Manual: selling 100%...")
                await do_sell(sell100_signed, "100%", "100% sell submitted")
                return
            elif cmd == "ss":
                log("EXIT", "Manual: selling 50%...")
                await do_sell(sell50_signed, "50%", "50% sell submitted — manage rest manually")
                return
            elif cmd == "r":
                log("EXIT", "Fetching last message from channel...")
                already_triggered = False
                messages = await tg_client.get_messages(TG_CHANNEL, limit=1)
                if not messages:
                    log("EXIT", "No messages found in channel")
                    return
                text = messages[0].message
                log("EXIT", f"Got: {text[:80]}")
                if not message_has_keyword(text):
                    log("EXIT", "Last message has no CA keyword — nothing to do")
                    return
                matches = SOLANA_CA_PATTERN.findall(text)
                ca = await find_valid_ca(matches) if matches else None
                if not ca:
                    log("EXIT", "No valid CA found in last message")
                    return
                log("EXIT", f"CA found: {ca} — triggering buy")
                already_triggered = True
                tx_hash = await execute_trade("buy", ca, BUY_AMOUNT_SOL, denom_in_sol=True)
                if tx_hash:
                    asyncio.create_task(partial_exit(ca, tg_client))
                else:
                    log("BUY", "No position opened")
                    already_triggered = False
                return

    # Timer elapsed — auto-sell 50%
    log("EXIT", "Selling 50% (auto)...")
    await do_sell(sell50_signed, "50%", "50% sold — manage remaining 50% manually on pump.fun")


# ==================== TELEGRAM LISTENER LOOP ====================
async def main():
    tg_client = TelegramClient('session_sniper', TG_API_ID, TG_API_HASH)

    @tg_client.on(events.NewMessage(chats=TG_CHANNEL))
    async def handler(event):
        global already_triggered
        if already_triggered:
            return

        text = event.message.message

        if not message_has_keyword(text):
            log("TG", f"Skip (no keyword): {text[:60]}")
            return

        matches = SOLANA_CA_PATTERN.findall(text)
        if not matches:
            log("TG", "Keyword matched but no base58 address found")
            return

        already_triggered = True
        log("TG", f"Validating {len(matches)} address(es) on-chain...")

        ca = await find_valid_ca(matches)
        if not ca:
            log("TG", "No valid on-chain address found — skipping")
            already_triggered = False
            return

        log("TG", f"CA confirmed on-chain: {ca}")

        tx_hash = await execute_trade("buy", ca, BUY_AMOUNT_SOL, denom_in_sol=True)
        if tx_hash:
            asyncio.create_task(partial_exit(ca, tg_client))
        else:
            log("BUY", "No position opened")
            already_triggered = False

    import threading
    threading.Thread(target=_keyboard_listener, daemon=True).start()

    log("BOT", f"Active — monitoring {TG_CHANNEL}")
    await tg_client.start()
    await tg_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())