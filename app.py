"""
Airdrop campaign: Telegram bot (aiogram 3.x) + Mini App backend (FastAPI).

One process runs both:
  - the bot: /start with referral support, admin commands (/stats, /phase,
    /whitelist, /approve, /broadcast, /export)
  - the web app: serves the Mini App frontend and its JSON API
    (Telegram initData validation, tasks, referrals, withdrawals, admin panel)

Requires: Python 3.10+, pip install -r requirements.txt
Run:      python app.py
"""

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import parse_qsl

import httpx
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
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db as store
from config import (
    ADMIN_IDS,
    BOT_TOKEN,
    CHANNEL_USERNAME,
    GROUP_USERNAME,
    INIT_DATA_MAX_AGE,
    LEADERBOARD_SIZE,
    MIN_WITHDRAWAL_TOKENS,
    POINTS_PER_REFERRAL,
    POINTS_PER_TOKEN,
    PORT,
    REFERRAL_REQUIRES_VERIFIED,
    TOKEN_NAME,
    WALLET_PATTERN,
    WEBAPP_URL,
    WHITELIST_AUTO_APPROVE,
    WITHDRAWALS_OPEN_PHASE,
    X_BEARER_TOKEN,
    X_PROFILE_URL,
    X_TARGET_USERNAME,
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


async def maybe_credit_referral(referee_id: int) -> None:
    """Credit the referrer when the referral is valid; notify them."""
    try:
        referrer_id, points = store.credit_referral_if_due(referee_id, POINTS_PER_REFERRAL)
    except Exception:
        log.exception("Failed crediting referral for %s", referee_id)
        return
    if not referrer_id:
        return
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 A friend qualified through your link! +{points} points. "
            f"You now have {store.referral_count(referrer_id, credited_only=True)} "
            "confirmed referrals.",
        )
    except Exception:
        pass  # referrer blocked the bot; ignore


async def register_and_refer(user_id: int, username: str | None, first_name: str | None,
                             referrer_id: int | None) -> bool:
    """Register the user and credit the referral when it doesn't need verification."""
    is_new = store.register_user(
        user_id, username, first_name, referrer_id,
        whitelisted=store.get_phase() == 1 and WHITELIST_AUTO_APPROVE,
    )
    if is_new and not REFERRAL_REQUIRES_VERIFIED:
        await maybe_credit_referral(user_id)
    return is_new


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


async def verify_x_follow(x_username: str) -> bool:
    """Verify an X follow through the X API. Requires X_BEARER_TOKEN and
    X_TARGET_USERNAME; without them the caller falls back to manual confirm."""
    if not X_BEARER_TOKEN or not X_TARGET_USERNAME:
        return False
    username = x_username.strip().lstrip("@")
    if not username:
        return False
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.get(
                f"https://api.twitter.com/2/users/by/username/{username}",
                headers=headers)
            if r.status_code != 200:
                return False
            source_id = r.json()["data"]["id"]
            r = await client.get(
                f"https://api.twitter.com/2/users/{source_id}/following",
                params={"max_results": 1000}, headers=headers)
            if r.status_code != 200:
                return False
            return any(u.get("username", "").lower() == X_TARGET_USERNAME.lower()
                       for u in r.json().get("data", []))
        except Exception:
            log.exception("X follow verification failed")
            return False


BOT_USERNAME: str | None = None  # filled from getMe() at startup


def profile_payload(user_id: int) -> dict:
    row = store.get_user(user_id)
    points = row["points"] if row else 0
    phase = store.get_phase()
    completions = store.task_completions_for(user_id)
    tasks = []
    for t in store.list_tasks(active_only=True):
        done = t["id"] in completions
        participants = store.task_completion_count(t["id"])
        tasks.append({
            "id": t["id"], "key": t["key"], "title": t["title"], "kind": t["kind"],
            "url": t["url"], "points": t["points"], "verification": t["verification"],
            "x_verify": t["verification"] == "x_api" and bool(X_BEARER_TOKEN and X_TARGET_USERNAME),
            "max_participants": t["max_participants"], "participants": participants,
            "full": t["max_participants"] is not None and participants >= t["max_participants"],
            "done": done,
        })
    return {
        "user_id": user_id,
        "bot_username": BOT_USERNAME,
        "is_admin": user_id in ADMIN_IDS,
        "token_name": TOKEN_NAME,
        "phase": phase,
        "phase_name": PHASE_NAMES[phase],
        "points_per_token": POINTS_PER_TOKEN,
        "whitelisted": bool(row and row["whitelisted"]),
        "banned": bool(row and row["banned"]),
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
        "tasks": tasks,
        "min_withdrawal_tokens": MIN_WITHDRAWAL_TOKENS,
        "withdrawals_open": phase >= WITHDRAWALS_OPEN_PHASE,
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
    if store.is_banned(user.id):
        await message.answer("Your access to this airdrop has been suspended.")
        return
    await register_and_refer(user.id, user.username, user.first_name,
                             parse_ref(command.args))
    phase = store.get_phase()

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


@dp.message(Command("balance"))
async def cmd_balance(message: Message) -> None:
    row = store.get_user(message.from_user.id)
    if not row:
        await message.answer("Send /start first to join the airdrop.")
        return
    await message.answer(
        f"Balance: <b>{row['points']}</b> points "
        f"(<b>{store.tokens_for(row['points'])}</b> {TOKEN_NAME})\n"
        f"Referrals: {store.referral_count(message.from_user.id)}",
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
        f"Banned: {s['banned']}\n"
        f"Wallets linked: {s['wallets']}\n"
        f"Joined via referral: {s['referred']}\n"
        f"Pending withdrawals: {s['pending_withdrawals']}\n"
        f"Completed withdrawals: {s['completed_withdrawals']}"
    )


@dp.message(Command("phase"))
async def cmd_phase(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        return
    if command.args and command.args.strip() in {"1", "2", "3", "4"}:
        phase = int(command.args.strip())
        store.set_phase(phase)
        store.log_admin_action(message.from_user.id, "phase", str(phase))
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
        store.log_admin_action(message.from_user.id, "approve_all", str(len(pending)))
        await message.answer(f"✅ Approved {len(pending)} users.")
    elif arg.isdigit():
        uid = int(arg)
        if store.get_user(uid):
            store.set_flag(uid, "whitelisted", 1)
            store.log_admin_action(message.from_user.id, "approve", str(uid))
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

    ids = [r["user_id"] for r in store.all_users() if not r["banned"]]
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

    store.log_admin_action(message.from_user.id, "broadcast", f"sent={sent} failed={failed}")
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
                         "banned", "points", "tokens", "referrer_id", "joined_channel",
                         "followed_x", "created_at"])
        for r in rows:
            writer.writerow([r["user_id"], r["username"], r["first_name"], r["wallet"],
                             r["whitelisted"], r["banned"], r["points"],
                             store.tokens_for(r["points"]),
                             r["referrer_id"], r["joined_channel"],
                             r["followed_x"], r["created_at"]])

    with open(path, "rb") as fh:
        await message.answer_document(
            BufferedInputFile(fh.read(), filename=f"airdrop_users_{datetime.now():%Y%m%d_%H%M}.csv"),
            caption=f"{len(rows)} users exported.",
        )
    os.unlink(path)


@dp.message(F.text)
async def cmd_fallback(message: Message) -> None:
    """Any unexpected text gets a friendly pointer instead of silence or errors."""
    if message.from_user and store.is_banned(message.from_user.id):
        return
    await message.answer(
        "I didn't understand that. Use /start to open the airdrop app "
        "or /balance to check your points.",
        reply_markup=app_keyboard(),
    )


@dp.error()
async def on_error(event, exception) -> None:
    log.exception("Update handling failed: %s", exception)


# --------------------------------------------------------------------------
# Web API (Mini App backend)
# --------------------------------------------------------------------------

def validate_init_data(init_data: str) -> dict:
    """Verify Telegram WebApp initData and return the parsed fields.

    Raises HTTPException(401) when the signature is wrong, the user field is
    missing or malformed, or the data is older than INIT_DATA_MAX_AGE seconds.
    """
    if not init_data:
        raise HTTPException(status_code=401, detail="Missing initData")

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise HTTPException(status_code=401, detail="Malformed initData")
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash")

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(status_code=401, detail="Bad initData signature")

    if INIT_DATA_MAX_AGE > 0:
        try:
            age = time.time() - int(pairs.get("auth_date", "0"))
        except ValueError:
            age = INIT_DATA_MAX_AGE + 1
        if age > INIT_DATA_MAX_AGE:
            raise HTTPException(status_code=401, detail="Session expired, reopen the app")

    if "user" not in pairs:
        raise HTTPException(status_code=401, detail="No user in initData")
    return pairs


def authed_user_id(init_data: str) -> int:
    pairs = validate_init_data(init_data)
    try:
        return int(json.loads(pairs["user"])["id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Malformed user in initData")


def require_user(init_data: str) -> int:
    """Validated, registered, non-banned user id."""
    user_id = authed_user_id(init_data)
    row = store.get_user(user_id)
    if not row:
        raise HTTPException(status_code=400, detail="Register first")
    if row["banned"]:
        raise HTTPException(status_code=403, detail="Account is banned")
    return user_id


def require_whitelisted(user_id: int) -> None:
    row = store.get_user(user_id)
    if not row:
        raise HTTPException(status_code=400, detail="Register first")
    if not row["whitelisted"]:
        raise HTTPException(status_code=403, detail="Whitelist members only")


class WalletBody(BaseModel):
    address: str = Field(min_length=1, max_length=128)


class PhaseBody(BaseModel):
    phase: int


class ApproveBody(BaseModel):
    user_id: int | None = None
    all: bool = False


class ClaimBody(BaseModel):
    x_username: str | None = Field(default=None, max_length=64)


class WithdrawBody(BaseModel):
    tokens: int = Field(gt=0)
    wallet: str = Field(min_length=1, max_length=128)
    network: str = Field(default="TON", max_length=32)


class AdjustBody(BaseModel):
    user_id: int
    delta: int
    reason: str = Field(min_length=1, max_length=200)


class BanBody(BaseModel):
    user_id: int
    banned: bool


class BroadcastBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class TaskBody(BaseModel):
    id: int | None = None
    key: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1, max_length=200)
    kind: str = Field(pattern="^(channel|x|wallet|custom)$")
    url: str | None = Field(default=None, max_length=300)
    points: int = Field(ge=0, le=1_000_000)
    verification: str = Field(pattern="^(channel|x_api|confirm|wallet)$")
    max_participants: int | None = Field(default=None, ge=1)
    active: bool = True
    sort_order: int = 0


class WithdrawDecisionBody(BaseModel):
    action: str = Field(pattern="^(approve|reject|complete)$")
    reason: str | None = Field(default=None, max_length=200)


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

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

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
        try:
            user = json.loads(pairs["user"])
            user_id = int(user["id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            raise HTTPException(status_code=401, detail="Malformed user in initData")
        if store.is_banned(user_id):
            raise HTTPException(status_code=403, detail="Account is banned")
        await register_and_refer(user_id, user.get("username"), user.get("first_name"),
                                 parse_ref(pairs.get("start_param")))
        return profile_payload(user_id)

    @app.get("/api/profile")
    async def api_profile(x_telegram_init_data: str = Header(default="")):
        return profile_payload(require_user(x_telegram_init_data))

    # --- Tasks ------------------------------------------------------------------

    @app.get("/api/tasks")
    async def api_tasks(x_telegram_init_data: str = Header(default="")):
        return {"tasks": profile_payload(require_user(x_telegram_init_data))["tasks"]}

    @app.post("/api/tasks/{task_id}/claim")
    async def api_claim_task(task_id: int, body: ClaimBody | None = None,
                             x_telegram_init_data: str = Header(default="")):
        """Verify and complete a task. Verification depends on the task kind:
        channel -> Telegram getChatMember, x -> X API when configured else a
        manual confirmation, wallet -> a linked wallet. Completion is atomic
        and duplicate claims are rejected.
        """
        user_id = require_user(x_telegram_init_data)
        require_whitelisted(user_id)
        if store.get_phase() != 2:
            raise HTTPException(status_code=400, detail="Tasks open in the points phase")
        task = store.get_task(task_id)
        if not task or not task["active"]:
            raise HTTPException(status_code=404, detail="Unknown task")

        if task["verification"] == "channel":
            if not await check_channel_membership(user_id):
                raise HTTPException(status_code=400,
                                    detail="You're not in the channel/group yet")
        elif task["verification"] == "x_api":
            if X_BEARER_TOKEN and X_TARGET_USERNAME:
                x_username = (body.x_username if body else None) or ""
                if not await verify_x_follow(x_username):
                    raise HTTPException(
                        status_code=400,
                        detail="We couldn't verify the follow. Check your X username "
                               "and that you follow us, then try again.")
            # Without X API credentials the admin relies on the manual confirm.
        elif task["verification"] == "wallet":
            row = store.get_user(user_id)
            if not row["wallet"]:
                raise HTTPException(status_code=400, detail="Connect your wallet first")

        try:
            balance = store.complete_task(user_id, task_id)
        except ValueError as exc:
            detail = {"already completed": "Task already completed",
                      "task full": "This task is full",
                      "unknown task": "Unknown task"}.get(str(exc), "Cannot complete task")
            raise HTTPException(status_code=409 if str(exc) == "already completed" else 400,
                                detail=detail)

        # Legacy flags kept in sync for exports and older dashboards.
        if task["key"] in ("channel", "group"):
            store.set_flag(user_id, "joined_channel", 1)
            if REFERRAL_REQUIRES_VERIFIED:
                await maybe_credit_referral(user_id)
        if task["key"] == "x":
            store.set_flag(user_id, "followed_x", 1)
        return {"ok": True, "points": balance, "task_id": task_id}

    # Legacy endpoints kept so older cached app versions keep working.
    @app.post("/api/verify_channel")
    async def api_verify_channel(x_telegram_init_data: str = Header(default="")):
        task = store.get_task_by_key("channel")
        if not task:
            raise HTTPException(status_code=404, detail="Unknown task")
        return await api_claim_task(task["id"], None, x_telegram_init_data)

    @app.post("/api/confirm_x")
    async def api_confirm_x(x_telegram_init_data: str = Header(default="")):
        task = store.get_task_by_key("x")
        if not task:
            raise HTTPException(status_code=404, detail="Unknown task")
        return await api_claim_task(task["id"], None, x_telegram_init_data)

    @app.post("/api/link_wallet")
    async def api_link_wallet(body: WalletBody,
                              x_telegram_init_data: str = Header(default="")):
        user_id = require_user(x_telegram_init_data)
        require_whitelisted(user_id)
        address = body.address.strip()
        if not WALLET_RE.match(address):
            raise HTTPException(status_code=400, detail="Invalid wallet address")
        store.set_flag(user_id, "wallet", address)
        return {"wallet": address}

    # --- Referrals ---------------------------------------------------------------

    @app.get("/api/leaderboard")
    async def api_leaderboard(x_telegram_init_data: str = Header(default="")):
        require_user(x_telegram_init_data)
        def fmt(rows, key):
            return [{"name": (r["first_name"] or "") + (f" @{r['username']}" if r["username"] else ""),
                     "value": r[key]} for r in rows]
        return {
            "points": fmt(store.points_leaderboard(LEADERBOARD_SIZE), "points"),
            "referrals": fmt(store.referral_leaderboard(LEADERBOARD_SIZE), "c"),
        }

    @app.get("/api/referrals")
    async def api_referrals(x_telegram_init_data: str = Header(default="")):
        user_id = require_user(x_telegram_init_data)
        return {
            "count": store.referral_count(user_id),
            "credited": store.referral_count(user_id, credited_only=True),
            "history": [dict(r) for r in store.referral_history(user_id)],
        }

    # --- Withdrawals -------------------------------------------------------------

    @app.post("/api/withdraw")
    async def api_withdraw(body: WithdrawBody,
                           x_telegram_init_data: str = Header(default="")):
        user_id = require_user(x_telegram_init_data)
        require_whitelisted(user_id)
        if store.get_phase() < WITHDRAWALS_OPEN_PHASE:
            raise HTTPException(status_code=400,
                                detail="Withdrawals open in the airdrop phase")
        wallet = body.wallet.strip()
        if not WALLET_RE.match(wallet):
            raise HTTPException(status_code=400, detail="Invalid wallet address")
        try:
            w = store.request_withdrawal(user_id, body.tokens, wallet,
                                         body.network or "TON", MIN_WITHDRAWAL_TOKENS)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"withdrawal": dict(w)}

    @app.post("/api/withdraw/{withdrawal_id}/cancel")
    async def api_withdraw_cancel(withdrawal_id: int,
                                  x_telegram_init_data: str = Header(default="")):
        user_id = require_user(x_telegram_init_data)
        try:
            w = store.cancel_withdrawal(withdrawal_id, user_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"withdrawal": dict(w)}

    @app.get("/api/withdrawals")
    async def api_withdrawals(x_telegram_init_data: str = Header(default="")):
        user_id = require_user(x_telegram_init_data)
        return {"withdrawals": [dict(w) for w in store.withdrawals_for(user_id)]}

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
        admin_id = require_admin(x_telegram_init_data)
        if body.phase not in PHASE_NAMES:
            raise HTTPException(status_code=400, detail="Phase must be 1-4")
        store.set_phase(body.phase)
        store.log_admin_action(admin_id, "phase", str(body.phase))
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
        admin_id = require_admin(x_telegram_init_data)
        if body.all:
            pending = store.pending_whitelist()
            for r in pending:
                store.set_flag(r["user_id"], "whitelisted", 1)
            store.log_admin_action(admin_id, "approve_all", str(len(pending)))
            return {"approved": len(pending)}
        if body.user_id and store.get_user(body.user_id):
            store.set_flag(body.user_id, "whitelisted", 1)
            store.log_admin_action(admin_id, "approve", str(body.user_id))
            return {"approved": 1}
        raise HTTPException(status_code=400, detail="Pass user_id or all=true")

    @app.get("/api/admin/users")
    async def admin_users(q: str = "", x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        rows = store.search_users(q.strip(), 100) if q.strip() else store.all_users()[:500]
        return {"users": [
            {"user_id": r["user_id"], "username": r["username"],
             "first_name": r["first_name"], "wallet": r["wallet"],
             "whitelisted": bool(r["whitelisted"]), "banned": bool(r["banned"]),
             "points": r["points"], "tokens": store.tokens_for(r["points"]),
             "referrer_id": r["referrer_id"],
             "referrals": store.referral_count(r["user_id"])}
            for r in rows
        ]}

    @app.get("/api/admin/users/{user_id}")
    async def admin_user_detail(user_id: int,
                                x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        row = store.get_user(user_id)
        if not row:
            raise HTTPException(status_code=404, detail="No such user")
        return {
            "user": dict(row),
            "tokens": store.tokens_for(row["points"]),
            "referrals": store.referral_count(user_id),
            "ledger": [dict(r) for r in store.ledger_for(user_id, 50)],
            "withdrawals": [dict(w) for w in store.withdrawals_for(user_id)],
            "tasks_completed": len(store.task_completions_for(user_id)),
        }

    @app.post("/api/admin/adjust")
    async def admin_adjust(body: AdjustBody,
                           x_telegram_init_data: str = Header(default="")):
        admin_id = require_admin(x_telegram_init_data)
        if body.delta == 0:
            raise HTTPException(status_code=400, detail="delta must not be 0")
        if not store.get_user(body.user_id):
            raise HTTPException(status_code=404, detail="No such user")
        try:
            balance = store.add_points(body.user_id, body.delta,
                                       f"admin:{body.reason}", ref=str(admin_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Balance cannot go negative")
        store.log_admin_action(admin_id, "adjust",
                               f"user={body.user_id} delta={body.delta} {body.reason}")
        return {"user_id": body.user_id, "balance": balance}

    @app.post("/api/admin/ban")
    async def admin_ban(body: BanBody,
                        x_telegram_init_data: str = Header(default="")):
        admin_id = require_admin(x_telegram_init_data)
        if not store.get_user(body.user_id):
            raise HTTPException(status_code=404, detail="No such user")
        if body.user_id in ADMIN_IDS:
            raise HTTPException(status_code=400, detail="Cannot ban an admin")
        store.set_flag(body.user_id, "banned", int(body.banned))
        store.log_admin_action(admin_id, "ban" if body.banned else "unban",
                               str(body.user_id))
        return {"user_id": body.user_id, "banned": body.banned}

    @app.get("/api/admin/tasks")
    async def admin_tasks(x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        return {"tasks": [
            {**dict(t), "participants": store.task_completion_count(t["id"])}
            for t in store.list_tasks(active_only=False)
        ]}

    @app.post("/api/admin/tasks")
    async def admin_upsert_task(body: TaskBody,
                                x_telegram_init_data: str = Header(default="")):
        admin_id = require_admin(x_telegram_init_data)
        if body.verification == "channel" and body.kind != "channel":
            raise HTTPException(status_code=400,
                                detail="channel verification needs kind=channel")
        if body.verification == "wallet" and body.kind != "wallet":
            raise HTTPException(status_code=400,
                                detail="wallet verification needs kind=wallet")
        if body.verification == "x_api" and body.kind != "x":
            raise HTTPException(status_code=400,
                                detail="x_api verification needs kind=x")
        task_id = store.upsert_task(body.id, body.key, body.title, body.kind, body.url,
                                    body.points, body.verification,
                                    body.max_participants, body.active, body.sort_order)
        store.log_admin_action(admin_id, "task_upsert", f"{body.key} ({task_id})")
        return {"id": task_id}

    @app.delete("/api/admin/tasks/{task_id}")
    async def admin_delete_task(task_id: int,
                                x_telegram_init_data: str = Header(default="")):
        admin_id = require_admin(x_telegram_init_data)
        if not store.get_task(task_id):
            raise HTTPException(status_code=404, detail="Unknown task")
        deleted = store.delete_task(task_id)
        store.log_admin_action(admin_id, "task_delete" if deleted else "task_disable",
                               str(task_id))
        return {"deleted": deleted}

    @app.get("/api/admin/withdrawals")
    async def admin_withdrawals(status: str = "",
                                x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        rows = store.list_withdrawals(status or None)
        return {"withdrawals": [dict(r) for r in rows]}

    @app.post("/api/admin/withdrawals/{withdrawal_id}/decision")
    async def admin_withdrawal_decision(withdrawal_id: int, body: WithdrawDecisionBody,
                                        x_telegram_init_data: str = Header(default="")):
        admin_id = require_admin(x_telegram_init_data)
        try:
            w = store.decide_withdrawal(withdrawal_id, body.action, admin_id, body.reason)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        store.log_admin_action(admin_id, f"withdrawal_{body.action}",
                               f"id={withdrawal_id} {body.reason or ''}")
        return {"withdrawal": dict(w)}

    @app.post("/api/admin/broadcast")
    async def admin_broadcast(body: BroadcastBody,
                              x_telegram_init_data: str = Header(default="")):
        admin_id = require_admin(x_telegram_init_data)
        ids = [r["user_id"] for r in store.all_users() if not r["banned"]]
        sent, failed = 0, 0
        for uid in ids:
            try:
                await bot.send_message(uid, body.text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)  # stay under Telegram rate limits
        store.log_admin_action(admin_id, "broadcast", f"sent={sent} failed={failed}")
        return {"sent": sent, "failed": failed}

    @app.get("/api/admin/audit")
    async def admin_audit(x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        return {"actions": [dict(r) for r in store.admin_actions(50)]}

    @app.get("/api/admin/export")
    async def admin_export(x_telegram_init_data: str = Header(default="")):
        require_admin(x_telegram_init_data)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["user_id", "username", "first_name", "wallet", "whitelisted",
                         "banned", "points", "tokens", "referrer_id", "joined_channel",
                         "followed_x", "created_at"])
        for r in store.all_users():
            writer.writerow([r["user_id"], r["username"], r["first_name"], r["wallet"],
                             r["whitelisted"], r["banned"], r["points"],
                             store.tokens_for(r["points"]),
                             r["referrer_id"], r["joined_channel"],
                             r["followed_x"], r["created_at"]])
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
