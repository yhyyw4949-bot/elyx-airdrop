# Telegram Airdrop Campaign Bot + Mini App

Runs an airdrop campaign on Telegram: users open your Mini App, join your channel, follow you on X, connect their TON wallet, and earn points for referrals. Python, aiogram 3.x (bot) + FastAPI (Mini App backend), SQLite storage (no database server needed).

## Features

- Telegram Mini App (web app inside Telegram) with task checklist and TON Connect wallet linking
- `/start` welcome message with referral support (`t.me/<yourbot>?start=ref_<user_id>`)
- Tasks:
  - Join your Telegram channel (verified automatically via Telegram's API; optional second group)
  - Follow your X/Twitter account (user confirms with a tap)
  - Connect a TON wallet via TON Connect (address validated with a configurable regex)
- Personal referral link per user, points per referral
- Signed Telegram initData validation on every API call, so users can't fake their identity
- Admin commands in the bot chat: `/stats`, `/broadcast`, `/export` (CSV of users, wallets and referral counts)
- All campaign settings (channel, X link, token name, points, wallet format) read from environment variables

## How it's organized

- `app.py` - bot handlers + FastAPI backend (one process runs both)
- `config.py` - environment-based settings
- `db.py` - SQLite storage
- `webapp/` - the Mini App frontend (HTML/JS/CSS, uses the TON Connect UI from a CDN)
- `requirements.txt`, `.env.example`

## Setup

### 1. Create the bot with BotFather

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts: pick a name, then a username ending in `bot`.
3. BotFather replies with a token that looks like `1234567890:AAxx...`. Keep it secret.

### 2. Prepare your channel

1. Create your public channel and choose a username (e.g. `@your_channel`).
2. Add your bot to the channel as an **admin** (it needs this to check whether users joined).

### 3. Get a public HTTPS URL

Telegram Mini Apps only open over HTTPS. Options:

- A VPS with a domain and a reverse proxy (Caddy handles HTTPS automatically - see below)
- A hosting service that gives you HTTPS out of the box (Railway, Render, Fly.io)
- For local testing only, a tunnel: `cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`

### 4. Register the Mini App with BotFather

1. Message [@BotFather](https://t.me/BotFather), send `/newapp` (or `/myapps` -> your bot -> Edit App).
2. Choose your bot, give the app a title, and set the **URL to your public HTTPS address** from step 3 (e.g. `https://airdrop.example.com`).
3. Upload an icon when asked (any square PNG works; the app also references `/icon.png` in the TON Connect manifest).

### 5. Install and configure

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | What it is |
|---|---|
| `BOT_TOKEN` | Token from BotFather |
| `WEBAPP_URL` | Your public HTTPS URL, e.g. `https://airdrop.example.com` |
| `ADMIN_IDS` | Your Telegram user ID(s), comma-separated. Get yours from [@userinfobot](https://t.me/userinfobot). |
| `CHANNEL_USERNAME` | Your public channel, e.g. `@your_channel` |
| `GROUP_USERNAME` | Optional second group to require; leave empty to skip |
| `X_PROFILE_URL` | Your X/Twitter profile URL |
| `TOKEN_NAME` | Your token's name, shown in the app |
| `POINTS_PER_REFERRAL` | Points per successful referral |
| `WALLET_PATTERN` | Regex for wallet validation. TON (`UQ...`/`EQ...`) by default. For EVM use `^0x[a-fA-F0-9]{40}$`, for Solana use `^[1-9A-HJ-NP-Za-km-z]{32,44}$` |
| `DB_PATH` | SQLite file location (default `airdrop.db`) |
| `PORT` | Port the web app listens on (default `8000`) |
| `WHITELIST_AUTO_APPROVE` | `true` auto-joins everyone to the whitelist in phase 1; `false` means you approve with `/approve` |
| `POINTS_PER_TOKEN` | How many points convert to 1 ELYX in phase 3 |

### 6. Run it

```bash
python app.py
```

The bot starts polling and the Mini App is served on your port. Open the bot in Telegram, send `/start`, and tap "Open the airdrop app".

## Admin commands

Only for IDs in `ADMIN_IDS`, sent to the bot in DM:

- `/stats` - total users, whitelisted members, wallets linked, referral joins
- `/phase` - show the current phase; `/phase 1-4` moves the campaign between phases
- `/whitelist` - list pending whitelist requests (when auto-approve is off)
- `/approve <user_id>` or `/approve all` - approve whitelist members
- `/broadcast <text>` - message every user (or reply to any message with `/broadcast`)
- `/export` - CSV of all users (ID, username, wallet, whitelist status, points, ELYX allocation, referrer)

All of these are also in the admin panel (see below).

## Admin panel

Admins (IDs in `ADMIN_IDS`) get an "Admin panel" link inside the Mini App, at `/admin.html`. It shows campaign stats, phase control buttons, the pending whitelist with approve buttons, the member list with points and ELYX allocations, and a CSV export. It is gated by the same signed Telegram initData check, so only your admin accounts can open it. The bot chat commands (`/phase`, `/whitelist`, `/approve`, `/stats`, `/export`) keep working too.

## Campaign phases

The app runs in four phases, moved by the admin with `/phase` in the bot chat:

1. **Whitelist** - users register in the app and get whitelisted (automatically, or after your approval when `WHITELIST_AUTO_APPROVE=false`; review with `/whitelist` and `/approve`)
2. **Points collection** - whitelisted members do the tasks and invite friends for points
3. **Token conversion** - the app shows each member their ELYX at `POINTS_PER_TOKEN` points per token
4. **Airdrop** - final allocations are shown in the app; `/export` gives you the distribution CSV

The app home always shows the current phase and the member's status in it.

## Hosting on Render

The repo includes a `render.yaml` blueprint:

1. Push this folder to a GitHub repo.
2. In Render: New -> Blueprint, pick the repo. Render reads `render.yaml` and creates the service.
3. In the service's Environment settings, fill in `BOT_TOKEN`, `ADMIN_IDS`, and set `WEBAPP_URL` to your Render URL (`https://<your-service>.onrender.com`).
4. Register that same URL with BotFather via `/newapp`.

The blueprint mounts a 1 GB disk at `/var/data` so the SQLite database survives redeploys. The free plan sleeps when idle; the first open after sleep takes a few seconds.

## Hosting on a VPS (production)

**systemd** (keeps it running and restarts on crash), `/etc/systemd/system/airdrop-bot.service`:

```ini
[Unit]
Description=Telegram airdrop bot
After=network.target

[Service]
WorkingDirectory=/opt/airdrop-bot
ExecStart=/usr/bin/python3 -m uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Caddy** (automatic HTTPS), `/etc/caddy/Caddyfile`:

```
airdrop.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Then:

```bash
sudo systemctl enable --now airdrop-bot caddy
```

Set `WEBAPP_URL=https://airdrop.example.com` in `.env` and register that URL with BotFather.

## Notes

- The X follow can't be verified from Telegram's side, so users confirm it with a tap. Channel membership is verified for real through the Bot API.
- TON Connect needs `icon.png` in the `webapp/` folder for the wallet popup; add any square PNG.
- If you switch chains, change `WALLET_PATTERN` in `.env` and replace the TON Connect button in `webapp/index.html` / `app.js` with a plain address input.
