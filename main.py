"""
Entry point — wires the Discord trigger to the sniper engine.
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
    args = parser.parse_args()

    sniper = Sniper(SniperConfig())
    await sniper.start()

    trigger = DiscordTrigger(sniper, ca_only=args.with_parser)
    await trigger.run()


if __name__ == "__main__":
    asyncio.run(main())
