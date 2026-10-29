# Solana Coin Sniper Bot

## Summary
Bot that watches a Discord or Telegram channels for token contract addresses (CAs), buys the token the moment it's posted, and manages an automatic exit.
Purpose - snipe fresh memecoin launches as fast as possible; execute trades initiated by custom triggers with your own rules.

## What it does
1. Listens to Discord or Telegram messages; extracts CAs by keyword match (see `keywords.py`) or by parsing the message for a valid on-chain address (`--with-parser`).
2. Buys via PumpPortal, covering both bonding-curve tokens and migrated (PumpSwap) tokens.
3. Pre-builds and simulates sell transactions in advance for instant manual or automatic exit.
4. Auto-sells 50% after `exit_timeout_s` (default 12s) unless a manual command was given.
5. Sends transactions to both RPC and Jito block-engine simultaneously; Jito tip configurable (`jito_tip_lamports`, 0 disables it).
6. Optional pre-send simulation (`SIMULATE_BEFORE_SEND`, default off): when enabled, each tx is dry-run via RPC before sending and tokens that fail (e.g. not paired with SOL — `UnsupportedQuoteMint`) are skipped. When off, txs are sent immediately without waiting for simulation.

## Triggers (Telegram / Discord)
Both triggers share the same logic: keyword check → CA extraction → validation → `sniper.buy(ca)`. They differ only in how they receive messages:

1. **Telegram** (`telegram_trigger.py`) — uses **Telethon** (MTProto user-client, not a bot API). You log in once with `TG_API_ID` / `TG_API_HASH`; a session file (`session_sniper.session`) is created so subsequent runs don't need re-auth. New messages arrive via `events.NewMessage(chats=...)`.
2. **Discord** (`discord_trigger.py`) — uses **discord.py-self** (user-token / self-bot, not a bot account). The token is read from `DISCORD_USER_TOKEN` and messages are read from `DISCORD_CHANNEL_ID` via `channel.history`. IMPORTANT: User-token automation violates Discord ToS — use a throwaway account.
3. Both wire the `r` terminal command to refetch the last channel message and retry the buy.
4. `keygen.py` — one-time Telegram login helper. Uses `TG_API_ID` / `TG_API_HASH` to log in via QR code (scan with Telegram Mobile → Settings → Devices → Link Desktop Device) and generates the `session_sniper.session` file the Telegram trigger needs. Run it once before using `telegram_trigger.py`.

## Trade sizing
Sizing is set in `sniper.py` (`Config` dataclass) — edit the defaults or wire env vars:
1. `buy_amount_sol` (0.22) — SOL spent per buy.
2. `slippage_pct` (40) — max slippage passed to PumpPortal.
3. `buy_priority_fee_sol` (0.004) / `sell_priority_fee_sol` (0.001) — priority fees per tx.
4. `jito_tip_lamports` (1,000,000 = 0.001 SOL) — Jito tip; `0` disables the Jito path entirely.
5. `exit_timeout_s` (12) — seconds before the automatic 50% sell fires.
6. `--test` flag (`main.py`) — preset: 0.003 SOL buy, low fees, small tip.

## Environment variables (`.env`, see `example.env`)
1. `PRIVATE_KEY_B58` — wallet private key, base58-encoded. The keypair is loaded locally; txs are signed locally, only raw txs leave the machine.
2. `RPC_URL` — Solana RPC endpoint (Helius recommended). Get one free at [helius.dev](https://helius.dev) → create an API key → copy the endpoint `https://mainnet.helius-rpc.com/?api-key=<key>`.
3. `SENDER_URL` — Helius Sender endpoint (same API key, `https://sender.helius-rpc.com/?api-key=<key>`), used for the fast send path.
4. `TG_API_ID` / `TG_API_HASH` — from [my.telegram.org](https://my.telegram.org) → API development tools → create an app. `TG_API_ID` is the numeric app id, `TG_API_HASH` is the hash string.
5. `TG_CHANNEL` — channel username or ID to monitor (e.g. `@somechannel`).
6. `DISCORD_USER_TOKEN` — your Discord **user** token (not bot). Open Discord in the browser → DevTools → Network tab → filter `/api` → click any request → Request Headers → copy the `Authorization` value.
7. `DISCORD_CHANNEL_ID` — right-click the channel in Discord (with developer mode enabled in settings) → Copy Channel ID.
8. `SIMULATE_BEFORE_SEND` — `true`/`false`, see "What it does" #6.

## Limitations
1. Only tokens paired with SOL can be bought directly (SOL-pooled only).
2. Pump.fun / PumpSwap only — no Raydium, Orca, etc.
3. Running from a local PC adds network latency (a server near validators is faster).
4. Keywords in `keywords.py` are generic and may need tuning per channel (false positives / misses).

## Important functionality
1. `--test` flag (`main.py`): tiny-budget preset — 0.003 SOL buy, low fees, small tip. For cheap real-money test runs.
2. RPC + Jito race: every tx is broadcast to both paths in parallel; whichever lands first wins.
3. Analytics: every run writes timestamped logs (ms precision) to `runs/<date>.txt`.
4. Terminal commands (type + Enter, active while a position is open):
   - `s` — sell 100%
   - `ss` — sell 50%
   - `r` — refetch/re-listen for the next trigger
