"""
Telegram trigger module — watches a channel for CA messages and fires the sniper.

Usage:
    from telegram_trigger import TelegramTrigger
    trigger = TelegramTrigger(sniper)
    await trigger.run()
"""
import asyncio

from telethon import TelegramClient, events

from sniper import Sniper, log, SOLANA_CA_PATTERN
from keywords import CA_KEYWORDS, message_has_keyword


class TelegramTrigger:
    def __init__(self, sniper: Sniper, channel: str | None = None,
                 api_id=None, api_hash: str | None = None, session: str = 'session_sniper',
                 ca_only: bool = False):
        import os
        self.sniper = sniper
        self.ca_only = ca_only
        self.channel = channel or os.environ.get("TG_CHANNEL")
        self.api_id = api_id or os.environ.get("TG_API_ID")
        self.api_hash = api_hash or os.environ.get("TG_API_HASH")
        self.session = session
        self.already_triggered = False
        # Wire the sniper's 'r' keyboard command to refetch the last TG message
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
            log("TG", f"Skip (no keyword): {text[:60]}")
            return
        log("TG", "Keyword matched, validating address(es)...")
        ca = await self._extract_valid_ca(text)
        if not ca:
            log("TG", "No valid on-chain address found — skipping")
            return
        self.already_triggered = True
        log("TG", f"CA selected: {ca}")
        tx_hash = await self.sniper.buy(ca)
        if not tx_hash:
            log("BUY", "No position opened")
            self.already_triggered = False

    async def refetch_and_buy(self):
        """'r' command: fetch last channel message and buy if it contains a CA."""
        log("TG", "Fetching last message from channel...")
        client = self._client
        messages = await client.get_messages(self.channel, limit=1)
        if not messages:
            log("TG", "No messages found in channel")
            return
        text = messages[0].message
        log("TG", f"Got: {text[:80]}")
        self.already_triggered = False
        await self.handle_message(text)

    async def run(self):
        self._client = TelegramClient(self.session, self.api_id, self.api_hash)

        @self._client.on(events.NewMessage(chats=self.channel))
        async def handler(event):
            await self.handle_message(event.message.message)

        log("BOT", f"Active — monitoring {self.channel}")
        await self._client.start()
        await self._client.run_until_disconnected()
