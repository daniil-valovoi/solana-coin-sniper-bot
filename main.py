"""
Entry point — wires the Discord trigger to the sniper engine.

Test modes (no Discord needed):
    python main.py --ca <MINT>            # one-shot buy of a specific mint
    python main.py --ca <MINT> --test     # same, with tiny budget preset (~0.005 SOL per run)
"""
import argparse
import asyncio

from sniper import Sniper, SniperConfig
from discord_trigger import DiscordTrigger


async def main():
    parser = argparse.ArgumentParser(description="Discord-triggered sniper bot")
    parser.add_argument(
        "--with-parser",
        action="store_true",
        help="Also buy messages with no keyword if they contain a valid on-chain address "
             "(default: keywords only)",
    )
    parser.add_argument("--ca", help="One-shot: buy this mint immediately, then run the exit manager")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Tiny-budget preset: ~0.004 SOL buy, low priority fee, small Jito tip. "
             "Overrides env vars.",
    )
    args = parser.parse_args()

    cfg = SniperConfig()
    if args.test:
        # Budget preset for cheap real-money test runs (~0.0045 SOL total per shot)
        cfg.buy_amount_sol = 0.003
        cfg.buy_priority_fee_sol = 0.0003
        cfg.sell_priority_fee_sol = 0.0001
        cfg.jito_tip_lamports = 100_000 
        cfg.slippage_pct = 50
        from sniper import log
        log("TEST", "Preset: buy 0.004 SOL | priofee 0.0003 | tip 0.0001 | slip 50%")

    sniper = Sniper(cfg)
    await sniper.start()

    if args.ca:
        tx = await sniper.buy(args.ca)
        if not tx:
            from sniper import log
            log("TEST", "Buy failed — nothing to manage")
        # keep the exit-manager loop alive
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await sniper.stop()
        return

    trigger = DiscordTrigger(sniper, ca_only=args.with_parser)
    await trigger.run()


if __name__ == "__main__":
    asyncio.run(main())
