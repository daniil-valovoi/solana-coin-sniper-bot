"""Shared trigger keywords used by Discord and Telegram triggers."""

CA_KEYWORDS = [
    "ca",
    "contract",
    "address",
    "mint",
    "pump",
    "pump.fun",
]


def message_has_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in CA_KEYWORDS)
