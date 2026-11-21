"""
Configuration for the airdrop campaign bot + Mini App.

All values come from environment variables. Copy .env.example to .env and fill it in.
"""

import os


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so no extra dependency is needed."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

# Public HTTPS URL where this app is hosted, e.g. https://airdrop.example.com
# Required: Telegram Mini Apps only open over HTTPS.
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.example")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@your_channel")  # channel to join
GROUP_USERNAME = os.getenv("GROUP_USERNAME", "")                    # optional group, "" to skip
X_PROFILE_URL = os.getenv("X_PROFILE_URL", "https://x.com/your_handle")
TOKEN_NAME = os.getenv("TOKEN_NAME", "ELYX")
POINTS_PER_REFERRAL = int(os.getenv("POINTS_PER_REFERRAL", "10"))

# --- Wallet address validation --------------------------------------------------
# Default: TON (user-friendly mainnet addresses, UQ.../EQ...).
# For EVM (Ethereum, BSC, Polygon, ...), use:  ^0x[a-fA-F0-9]{40}$
# For Solana, use:                             ^[1-9A-HJ-NP-Za-km-z]{32,44}$
WALLET_PATTERN = os.getenv("WALLET_PATTERN", r"^[EU]Q[A-Za-z0-9_-]{46}$")

DB_PATH = os.getenv("DB_PATH", "airdrop.db")
PORT = int(os.getenv("PORT", "8000"))

# --- Campaign phases ------------------------------------------------------------
# 1: whitelist registration, 2: points collection, 3: points -> ELYX conversion,
# 4: airdrop distribution. The admin moves phases with /phase in the bot chat.
# WHITELIST_AUTO_APPROVE=true joins everyone to the whitelist automatically in
# phase 1; set it to false to review each member yourself with /whitelist and /approve.
WHITELIST_AUTO_APPROVE = os.getenv("WHITELIST_AUTO_APPROVE", "true").lower() in {"1", "true", "yes"}
POINTS_PER_TOKEN = int(os.getenv("POINTS_PER_TOKEN", "1"))  # how many points make 1 ELYX
