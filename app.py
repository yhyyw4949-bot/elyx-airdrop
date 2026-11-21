"""
Airdrop campaign: Telegram bot (aiogram 3.x) + Mini App backend (FastAPI).

One process runs both:
  - the bot: /start with referral support, admin commands (/stats, /broadcast, /export)
  - the web app: serves the Mini App frontend and its JSON API
    (Telegram initData validation, task verification, TON wallet linking)

Requires: Python 3.10+, pip install -r requirements.txt
Run:      python app.py
"""

import asyncio
import csv
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db as store
from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    CHANNEL_USERNAME,
    GROUP_USERNAME,
    POINTS_PER_REFERRAL,
    POINTS_PER_TOKEN,
    PORT,
    TOKEN_NAME,
    WALLET_PATTERN,
    WEBAPP_URL,
    WHITELIST_AUTO_APPROVE,
    X_PROFILE_URL,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("airdrop")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

WALLET_RE = re.compile(WALLET_PATTERN)

PHASE_NAMES = {
    1: "Whitelist",
    2: "Points collection",
    3: "Token conversion",
    4: "Airdrop",
}


# --------------------------------------------------------------------------
# Referral + task helpers (shared by the bot and the web API)
# --------------------------------------------------------------------------

def parse_ref(value: str | None) -> int | None:
    """Extract the referrer id from a start payload like 'ref_123456'."""
    if value and value.startswith("ref_") and value[4:].isdigit():
        return int(value[4:])
    return None


async def apply_referral(user_id: int, referrer_id: int | None) -> None:
    """Credit the referrer once, only when this user is genuinely new."""
    if not referrer_id or referrer_id == user_id:
        return
    if store.get_user(referrer_id):
        store.add_points(referrer_id, POINTS_PER_REFERRAL)
        try:
            await bot.send_message(
                referrer_id,
                f"🎉 Someone joined with your link! +{POINTS_PER_REFERRAL} points. "
                f"You now have {store.referral_count(referrer_id)} referrals.",
            )
        except Exception:
            pass  # referrer blocked the bot; ignore


async def check_channel_membership(user_id: int) -> bool:
    def as_chat(v: str):
        return int(v) if v.lstrip("-").isdigit() else v
    targets = [as_chat(CHANNEL_USERNAME)] + ([as_chat(GROUP_USERNAME)] if GROUP_USERNAME else [])
    ok = {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}
    for chat in targets:
        try:
            member = await bot.get_chat_member(chat, user_id)
            if member.status not in ok:
                return False
        except Exception as exc:
            log.warning("getChatMember failed for %s: %s", chat, exc)
            return False
    return True


BOT_USERNAME: str | None = None  # filled from getMe() at startup


def profile_payload(user_id: int) -> dict:
    row = store.get_user(user_id)
    points = row["points"] if row else 0
    return {
        "user_id": user_id,
        "bot_username": BOT_USERNAME,
        "is_admin": user_id in ADMIN_IDS,
        "token_name": TOKEN_NAME,
        "phase": store.get_phase(),
        "phase_name": PHASE_NAMES[store.get_phase()],
        "points_per_token": POINTS_PER_TOKEN,
        "whitelisted": bool(row and row["whitelisted"]),
        "tokens": store.tokens_for(points),
        "channel": CHANNEL_USERNAME,
        "channel_url": (f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
                        if CHANNEL_USERNAME.startswith("@") else None),
        "group": GROUP_USERNAME or None,
        "group_url": (f"https://t.me/{GROUP_USERNAME.lstrip('@')}"
                      if GROUP_USERNAME.startswith("@") else None),
        "x_url": X_PROFILE_URL,
        "points_per_referral": POINTS_PER_REFERRAL,
        "joined_channel": bool(row and row["joined_channel"]),
        "followed_x": bool(row and row["followed_x"]),
        "wallet": row["wallet"] if row else None,
        "points": points,
        "referrals": store.referral_count(user_id),
    }


# --------------------------------------------------------------------------
# Bot handlers
# --------------------------------------------------------------------------

def app_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Open the airdrop app", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])


PHASE_INTROS = {
    1: "We're in the whitelist phase. Join the whitelist in the app to reserve your spot.",
    2: "The points phase is live. Complete the tasks and invite friends to earn points.",
    3: "We're converting points to ELYX. Check your allocation in the app.",
    4: "The airdrop is live! Check your ELYX allocation in the app.",
}


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    user = message.from_user
    referrer_id = parse_ref(command.args)
    phase = store.get_phase()
    is_new = store.register_user(
        user.id, user.username, user.first_name, referrer_id,
        whitelisted=phase == 1 and WHITELIST_AUTO_APPROVE,
    )
    if is_new:
        await apply_referral(user.id, referrer_id)

    # Menu button so the app is always one tap away.
    try:
        await bot.set_chat_menu_button(message.chat.id, MenuButtonWebApp(
            text="Airdrop", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception:
        pass

    await message.answer(
        f"Hey {user.first_name or 'there'}! Welcome to the <b>{TOKEN_NAME}</b> airdrop. "
        f"{PHASE_INTROS[phase]}",
        reply_markup=app_keyboard(),
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    s = store.stats()
    phase = store.get_phase()
    await message.answer(
        f"📊 Campaign stats (phase {phase}: {PHASE_NAMES[phase]})\n\n"
        f"Total users: <b>{s['total']}</b>\n"
        f"Whitelisted: {s['whitelisted']}\n"
        f"Verified in channel: {s['joined']}\n"
        f"Wallets linked: {s['wallets']}\n"
        f"Joined via referral: {s['referred']}"
    )


@dp.message(Command("phase"))
async def cmd_phase(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    if command.args and command.args.strip() in {"1", "2", "3", "4"}:
        phase = int(command.args.strip())
        store.set_phase(phase)
        await message.answer(f"✅ Campaign is now in phase {phase}: {PHASE_NAMES[phase]}.")
    else:
        phase = store.get_phase()
        await message.answer(
            f"Current phase: <b>{phase} ({PHASE_NAMES[phase]})</b>.\n"
            "Use /phase 1-4 to move phases:\n"
            "1 - Whitelist\n2 - Points collection\n3 - Token conversion\n4 - Airdrop"
        )


@dp.message(Command("whitelist"))
async def cmd_whitelist(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    pending = store.pending_whitelist()
    if not pending:
        await message.answer("No pending whitelist requests.")
        return
    lines = [
        f"<code>{r['user_id']}</code> @{r['username'] or '-'} ({r['first_name'] or '-'})"
        for r in pending[:50]
    ]
    await message.answer(
        "Pending whitelist requests:\n" + "\n".join(lines)
        + "\n\nApprove with /approve <user_id> or /approve all"
    )


@dp.message(Command("approve"))
async def cmd_approve(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if arg == "all":
        pending = store.pending_whitelist()
        for r in pending:
            store.set_flag(r["user_id"], "whitelisted", 1)
        await message.answer(f"✅ Approved {len(pending)} users.")
    elif arg.isdigit():
        uid = int(arg)
        if store.get_user(uid):
            store.set_flag(uid, "whitelisted", 1)
            await message.answer(f"✅ Approved <code>{uid}</code>.")
        else:
            await message.answer("No such user.")
    else:
        await message.answer("Use /approve <user_id> or /approve all.")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return

    reply_source = message.reply_to_message
    broadcast_text = None
    if not reply_source and message.text and len(message.text.split(maxsplit=1)) > 1:
        broadcast_text = message.text.split(maxsplit=1)[1]  # /broadcast <text>

    if not reply_source and broadcast_text is None:
        await message.answer("Reply to a message with /broadcast, or use /broadcast <text>.")
        return

    ids = [r["user_id"] for r in store.all_users()]
    sent, failed = 0, 0
    for uid in ids:
        try:
            if broadcast_text is not None:
                await bot.send_message(uid, broadcast_text)
            else:
                await reply_source.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # stay under Telegram rate limits

    await message.answer(f"Broadcast done. Sent: {sent}, failed: {failed}.")


@dp.message(Command("export"))
async def cmd_export(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = store.all_users()

    fd, path = tempfile.mkstemp(suffix=".csv", prefix="airdrop_export_")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["user_id", "username", "first_name", "wallet", "whitelisted",
                         "points", "tokens", "referrer_id", "joined_channel",
                         "followed_x", "created_at"])
        for r in rows:
            writer.writerow([r["user_id"], r["username"], r["first_name"], r["wallet"],
                             r["whitelisted"], r["points"], store.tokens_for(r["points"]),
                             r["referrer_id"], r["joined_channel"],
                             r["followed_x"], r["created_at"]])

    with open(path, "rb") as fh:
        await message.answer_document(
            BufferedInputFile(fh.read(), filename=f"airdrop_users_{datetime.now():%Y%m%d_%H%M}.csv"),
            caption=f"{len(rows)} users exported.",
        )
    os.unlink(path)


# --------------------------------------------------------------------------
# Web API (Mini App backend)
# --------------------------------------------------------------------------

def validate_init_data(init_data: str) -> dict:
    """Verify Telegram WebApp initData and return the parsed fields.

    Raises HTTPException(401) when the signature is wrong or the data is stale.
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash")

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=401, detail="Bad initData signature")

    if "user" not in pairs:
        raise HTTPException(status_code=401, detail="No user in initData")
    return pairs


def authed_user_id(init_data: str) -> int:
    pairs = validate_init_data(init_data)
    return int(json.loads(pairs["user"])["id"])


class WalletBody(BaseModel):
    address: str


class PhaseBody(BaseModel):
    phase: int


class ApproveBody(BaseModel):
    user_id: int | None = None
    all: bool = False


def make_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        global BOT_USERNAME
        store.init_db()
        me = await bot.get_me()
        BOT_USERNAME = me.username
        polling = asyncio.create_task(dp.start_polling(bot))
        log.info("Bot polling started; web app on port %s", PORT)
        yield
        polling.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.get("/tonconnect-manifest.json")
    async def tonconnect_manifest():
        return {
            "url": WEBAPP_URL,
            "name": f"{TOKEN_NAME} Airdrop",
            "iconUrl": f"{WEBAPP_URL}/icon.png",
        }

    @app.post("/api/register")
    async def api_register(x_telegram_init_data: str = Header(default="")):
        """Register (or refresh) the user, apply a referral once, return the profile.

        During phase 1 this is also the whitelist signup: auto-approved when
        WHITELIST_AUTO_APPROVE is on, otherwise pending admin review.
        """
        pairs = validate_init_data(x_telegram_init_data)
        user = json.loads(pairs["user"])
        user_id = int(user["id"])
        referrer_id = parse_ref(pairs.get("start_param"))
        phase = store.get_phase()
        is_new = store.register_user(
            user_id, user.get("username"), user.get("first_name"), referrer_id,
            whitelisted=phase == 1 and WHITELIST_AUTO_APPROVE,
        )
        if is_new:
            await apply_referral(user_id, referrer_id)
        return profile_payload(user_id)

    def require_whitelisted(user_id: int) -> None:
        row = store.get_user(user_id)
        if not row:
            raise HTTPException(status_code=400, detail="Register first")
        if not row["whitelisted"]:
            raise HTTPException(status_code=403, detail="Whitelist members only")

    @app.post("/api/verify_channel")
    async def api_verify_channel(x_telegram_init_data: str = Header(default="")):
        user_id = authed_user_id(x_telegram_init_data)
        require_whitelisted(user_id)
        if store.get_phase() != 2:
            raise HTTPException(status_code=400, detail="Tasks open in the points phase")
        if await check_channel_membership(user_id):
            store.set_flag(user_id, "joined_channel", 1)
            return {"joined_channel": True}
        return {"joined_channel": False}

    @app.post("/api/confirm_x")
    async def api_confirm_x(x_telegram_init_data: str = Header(default="")):
        user_id = authed_user_id(x_telegram_init_data)
        require_whitelisted(user_id)
        if store.get_phase() != 2:
            raise HTTPException(status_code=400, detail="Tasks open in the points phase")
        # X follow can't be verified from Telegram; the user confirms with a tap.
        store.set_flag(user_id, "followed_x", 1)
        return {"followed_x": True}

    @app.post("/api/link_wallet")
    async def api_link_wallet(body: WalletBody,
                              x_telegram_init_data: str = Header(default="")):
        user_id = authed_user_id(x_telegram_init_data)
        require_whitelisted(user_id)
        address = body.address.strip()
        if not WALLET_RE.match(address):
            raise HTTPException(status_code=400, detail="Invalid wallet address")
        store.set_flag(user_id, "wallet", address)
        return {"wallet": address}

    @app.get("/api/profile")
    async def api_profile(x_telegram_init_data: str = Header(default="")):
        return profile_payload(authed_user_id(x_telegram_init_data))

    # --- Admin API (gated to ADMIN_IDS, same initData validation) ----------------

    def require_admin(init_data: str) -> int:
        user_id = authed_user_id(init_data)
        if user_id not in ADMIN_IDS:
            raise HTTPException(status_code=403, detail="Admins only")
        return user_id

    @app.get("/api/admin/overview")
    async def admin_overview(x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        s = store.stats()
        phase = store.get_phase()
        return {**s, "phase": phase, "phase_name": PHASE_NAMES[phase]}

    @app.post("/api/admin/phase")
    async def admin_set_phase(body: PhaseBody,
                              x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        if body.phase not in PHASE_NAMES:
            raise HTTPException(status_code=400, detail="Phase must be 1-4")
        store.set_phase(body.phase)
        return {"phase": body.phase, "phase_name": PHASE_NAMES[body.phase]}

    @app.get("/api/admin/whitelist")
    async def admin_whitelist(x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        rows = store.pending_whitelist()
        return {"pending": [
            {"user_id": r["user_id"], "username": r["username"],
             "first_name": r["first_name"], "created_at": r["created_at"]}
            for r in rows
        ]}

    @app.post("/api/admin/approve")
    async def admin_approve(body: ApproveBody,
                            x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        if body.all:
            pending = store.pending_whitelist()
            for r in pending:
                store.set_flag(r["user_id"], "whitelisted", 1)
            return {"approved": len(pending)}
        if body.user_id and store.get_user(body.user_id):
            store.set_flag(body.user_id, "whitelisted", 1)
            return {"approved": 1}
        raise HTTPException(status_code=400, detail="Pass user_id or all=true")

    @app.get("/api/admin/users")
    async def admin_users(x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        rows = store.all_users()[:500]
        return {"users": [
            {"user_id": r["user_id"], "username": r["username"],
             "first_name": r["first_name"], "wallet": r["wallet"],
             "whitelisted": bool(r["whitelisted"]), "points": r["points"],
             "tokens": store.tokens_for(r["points"]), "referrer_id": r["referrer_id"]}
            for r in rows
        ]}

    @app.get("/api/admin/export")
    async def admin_export(x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["user_id", "username", "first_name", "wallet", "whitelisted",
                         "points", "tokens", "referrer_id", "joined_channel",
                         "followed_x", "created_at"])
        for r in store.all_users():
            writer.writerow([r["user_id"], r["username"], r["first_name"], r["wallet"],
                             r["whitelisted"], r["points"], store.tokens_for(r["points"]),
                             r["referrer_id"], r["joined_channel"],
                             r["followed_x"], r["created_at"]])
        from fastapi.responses import Response
        return Response(
            content=buf.getvalue(), media_type="text/csv",
            headers={"Content-Disposition":
                     f"attachment; filename=airdrop_users_{datetime.now():%Y%m%d_%H%M}.csv"},
        )

    # Serve the Mini App frontend (index.html at /, assets alongside).
    webapp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")
    app.mount("/", StaticFiles(directory=webapp_dir, html=True), name="webapp")

    return app


app = make_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
