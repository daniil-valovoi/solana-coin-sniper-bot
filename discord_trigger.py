"""
Discord trigger module — watches a specific server's channel as a USER account
(user token / self-bot via discord.py-self).

WARNING: user-token automation violates Discord ToS and can get the account
banned. Use a throwaway account.

Usage:
    from discord_trigger import DiscordTrigger
    trigger = DiscordTrigger(sniper)
    await trigger.run()

Env vars:
    DISCORD_USER_TOKEN  — user token (see instructions below)
    DISCORD_CHANNEL_ID  — channel ID to monitor
"""
import asyncio
import os

import discord
from discord.ext import commands  # noqa: F401  (part of discord.py-self)

from sniper import Sniper, log, SOLANA_CA_PATTERN
from keywords import CA_KEYWORDS, message_has_keyword


class DiscordTrigger:
    def __init__(self, sniper: Sniper, channel_id: int | None = None,
                 user_token: str | None = None, ca_only: bool = False):
        self.sniper = sniper
        self.ca_only = ca_only
        self.channel_id = channel_id or int(os.environ.get("DISCORD_CHANNEL_ID", "0"))
        self.user_token = user_token or os.environ.get("DISCORD_USER_TOKEN")
        self.already_triggered = False
        self._client: discord.Client | None = None
        # Wire the sniper's 'r' keyboard command to refetch the last message
        sniper.on_refetch = self.refetch_and_buy

    async def _extract_valid_ca(self, text: str) -> str | None:
        if self.sniper.cfg.skip_ca_check:
            return self.sniper.pick_ca_fast(text)
        matches = SOLANA_CA_PATTERN.findall(text)
        if not matches:
            return None
        return await self.sniper.find_valid_ca(matches)

    async def handle_message(self, text: str):
        """Process one message; buy if a valid CA is found. Reusable for any source."""
        if self.already_triggered:
            return
        if not self.ca_only and not message_has_keyword(text):
            log("DC", f"Skip (no keyword): {text[:60]}")
            return
        log("DC", "Keyword matched, validating address(es)...")
        ca = await self._extract_valid_ca(text)
        if not ca:
            log("DC", "No valid on-chain address found — skipping")
            return
        self.already_triggered = True
        log("DC", f"CA selected: {ca}")
        tx_hash = await self.sniper.buy(ca)
        if not tx_hash:
            log("BUY", "No position opened")
            self.already_triggered = False

    async def refetch_and_buy(self):
        """'r' command: fetch last channel message and buy if it contains a CA."""
        log("DC", "Fetching last message from channel...")
        channel = self._client.get_channel(self.channel_id)
        if channel is None:
            log("DC", "Channel not found (check DISCORD_CHANNEL_ID)")
            return
        async for msg in channel.history(limit=1):
            log("DC", f"Got: {msg.content[:80]}")
            self.already_triggered = False
            await self.handle_message(msg.content)
            return
        log("DC", "No messages found in channel")

    async def run(self):
        if not self.user_token:
            raise RuntimeError("DISCORD_USER_TOKEN not set")
        if not self.channel_id:
            raise RuntimeError("DISCORD_CHANNEL_ID not set")

        # User accounts: no intents required
        self._client = discord.Client()

        @self._client.event
        async def on_ready():
            log("BOT", f"Active — monitoring channel {self.channel_id} "
                       f"as {self._client.user}")

        @self._client.event
        async def on_message(message: discord.Message):
            if message.channel.id != self.channel_id:
                return
            await self.handle_message(message.content)

        log("BOT", "Connecting to Discord (user token)...")
        await self._client.start(self.user_token)
