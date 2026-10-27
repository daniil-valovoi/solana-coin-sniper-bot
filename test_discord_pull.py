"""Minimal one-shot test: pull the last few messages from a Discord channel
using the user token. Single HTTP fetch, no websocket, no gateway.

Usage: python test_discord_pull.py
Env: DISCORD_USER_TOKEN, DISCORD_CHANNEL_ID
"""
import os

import requests

TOKEN = os.environ["DISCORD_USER_TOKEN"]
CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]

resp = requests.get(
    f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages",
    params={"limit": 5},
    headers={"Authorization": TOKEN},  # user token: no "Bot " prefix
    timeout=10,
)
resp.raise_for_status()
for m in reversed(resp.json()):  # oldest first
    print(f"[{m['timestamp']}] {m['author']['username']}: {m['content'][:200]}")
