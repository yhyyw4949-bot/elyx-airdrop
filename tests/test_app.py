"""Backend tests for the ELYX airdrop bot + Mini App API.

Run:  python -m pytest tests/ -v
Requires: pip install -r requirements.txt pytest httpx
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

import pytest

TEST_TOKEN = "123456:TEST_TOKEN_FOR_TESTS"

os.environ["BOT_TOKEN"] = TEST_TOKEN
os.environ["DB_PATH"] = ""  # set per-test in fixture
os.environ["ADMIN_IDS"] = "1"
os.environ["CHANNEL_USERNAME"] = "@testchannel"
os.environ["X_PROFILE_URL"] = "https://x.com/test"
os.environ["WHITELIST_AUTO_APPROVE"] = "true"
os.environ["MIN_WITHDRAWAL_TOKENS"] = "100"
os.environ["POINTS_PER_TOKEN"] = "1"
os.environ["POINTS_PER_REFERRAL"] = "10"

import app as app_module  # noqa: E402
import db as store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ADMIN_ID = 1
USER_ID = 100
REFERER_ID = 200


def sign_init_data(user_id: int, auth_date: int | None = None,
                   start_param: str | None = None) -> str:
    data = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAtest",
        "user": json.dumps({"id": user_id, "first_name": "T", "username": f"u{user_id}"}),
    }
    if start_param:
        data["start_param"] = start_param
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def hdr(user_id: int, **kw) -> dict:
    return {"X-Telegram-Init-Data": sign_init_data(user_id, **kw)}


class FakeBot:
    username = "testbot"

    def __init__(self):
        self.sent = []

    async def get_me(self):
        class Me:
            username = "testbot"
        return Me()

    async def send_message(self, *a, **kw):
        self.sent.append((a, kw))

    async def set_chat_menu_button(self, *a, **kw):
        pass


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("app.BOT_USERNAME", "testbot")
    fake = FakeBot()
    monkeypatch.setattr("app.bot", fake)

    async def _noop(*a, **kw):
        await asyncio.sleep(3600)
    monkeypatch.setattr("app.dp.start_polling", _noop, raising=True)

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "test.db"))
    store.init_db()
    with TestClient(app_module.app) as c:
        c.fake_bot = fake
        yield c


# --------------------------------------------------------------------------
# initData validation
# --------------------------------------------------------------------------

def test_missing_init_data_rejected(client):
    assert client.post("/api/register").status_code == 401


def test_bad_signature_rejected(client):
    bad = sign_init_data(USER_ID).replace("user=", "user=tampered", 1)
    # Recompute nothing: signature no longer matches.
    assert client.post("/api/register", headers={
        "X-Telegram-Init-Data": bad}).status_code == 401


def test_expired_init_data_rejected(client):
    old = sign_init_data(USER_ID, auth_date=int(time.time()) - 90000)
    assert client.post("/api/register", headers={
        "X-Telegram-Init-Data": old}).status_code == 401


# --------------------------------------------------------------------------
# Registration + referrals
# --------------------------------------------------------------------------

def test_register_creates_whitelisted_user(client):
    r = client.post("/api/register", headers=hdr(USER_ID))
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == USER_ID
    assert body["whitelisted"] is True
    assert body["phase"] == 1


def test_referral_credited_once(client):
    assert client.post("/api/register", headers=hdr(REFERER_ID)).status_code == 200
    for _ in range(2):  # duplicate registrations must not double-credit
        r = client.post("/api/register", headers=hdr(USER_ID, start_param=f"ref_{REFERER_ID}"))
        assert r.status_code == 200
    assert store.get_balance(REFERER_ID) == 10
    assert store.referral_count(REFERER_ID) == 1


def test_self_referral_ignored(client):
    r = client.post("/api/register", headers=hdr(USER_ID, start_param=f"ref_{USER_ID}"))
    assert r.status_code == 200
    assert store.get_balance(USER_ID) == 0
    assert store.referral_count(USER_ID) == 0


def test_referral_to_unknown_user_ignored(client):
    r = client.post("/api/register", headers=hdr(USER_ID, start_param="ref_999"))
    assert r.status_code == 200
    assert store.get_user(999) is None


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def register(client, uid):
    assert client.post("/api/register", headers=hdr(uid)).status_code == 200
    store.set_phase(2)


def test_default_tasks_seeded(client):
    register(client, USER_ID)
    tasks = client.get("/api/tasks", headers=hdr(USER_ID)).json()["tasks"]
    keys = {t["key"] for t in tasks}
    assert {"channel", "x", "wallet"} <= keys


def test_confirm_task_awards_points_once(client, monkeypatch):
    register(client, USER_ID)
    task = store.get_task_by_key("x")
    r = client.post(f"/api/tasks/{task['id']}/claim", headers=hdr(USER_ID), json={})
    assert r.status_code == 200
    assert r.json()["points"] == 10
    r = client.post(f"/api/tasks/{task['id']}/claim", headers=hdr(USER_ID), json={})
    assert r.status_code == 409
    assert store.get_balance(USER_ID) == 10


def test_channel_task_requires_membership(client, monkeypatch):
    register(client, USER_ID)
    task = store.get_task_by_key("channel")

    async def no(*a, **kw):
        return False
    monkeypatch.setattr("app.check_channel_membership", no)
    r = client.post(f"/api/tasks/{task['id']}/claim", headers=hdr(USER_ID), json={})
    assert r.status_code == 400

    async def yes(*a, **kw):
        return True
    monkeypatch.setattr("app.check_channel_membership", yes)
    r = client.post(f"/api/tasks/{task['id']}/claim", headers=hdr(USER_ID), json={})
    assert r.status_code == 200


def test_tasks_blocked_in_wrong_phase(client):
    register(client, USER_ID)
    store.set_phase(1)
    task = store.get_task_by_key("x")
    r = client.post(f"/api/tasks/{task['id']}/claim", headers=hdr(USER_ID), json={})
    assert r.status_code == 400
    store.set_phase(2)


def test_wallet_link_and_claim(client):
    register(client, USER_ID)
    r = client.post("/api/link_wallet", headers=hdr(USER_ID), json={"address": "bad"})
    assert r.status_code == 400
    addr = "UQ" + "a" * 46
    r = client.post("/api/link_wallet", headers=hdr(USER_ID), json={"address": addr})
    assert r.status_code == 200
    task = store.get_task_by_key("wallet")
    r = client.post(f"/api/tasks/{task['id']}/claim", headers=hdr(USER_ID), json={})
    assert r.status_code == 200


# --------------------------------------------------------------------------
# Withdrawals
# --------------------------------------------------------------------------

def funded_user(client, uid=USER_ID, points=150):
    register(client, uid)
    store.add_points(uid, points, "test_seed")


def test_withdrawal_flow(client):
    funded_user(client)
    store.set_phase(4)
    addr = "UQ" + "b" * 46

    # Below the minimum.
    r = client.post("/api/withdraw", headers=hdr(USER_ID),
                    json={"tokens": 50, "wallet": addr})
    assert r.status_code == 400

    # Valid request locks the points.
    r = client.post("/api/withdraw", headers=hdr(USER_ID),
                    json={"tokens": 100, "wallet": addr})
    assert r.status_code == 200
    wid = r.json()["withdrawal"]["id"]
    assert store.get_balance(USER_ID) == 50

    # One pending withdrawal per user.
    r = client.post("/api/withdraw", headers=hdr(USER_ID),
                    json={"tokens": 50, "wallet": addr})
    assert r.status_code == 400

    # Cancel releases the points.
    r = client.post(f"/api/withdraw/{wid}/cancel", headers=hdr(USER_ID))
    assert r.status_code == 200
    assert store.get_balance(USER_ID) == 150

    # New request -> approve -> complete; transitions are idempotent.
    r = client.post("/api/withdraw", headers=hdr(USER_ID),
                    json={"tokens": 100, "wallet": addr})
    wid = r.json()["withdrawal"]["id"]
    r = client.post(f"/api/admin/withdrawals/{wid}/decision",
                    headers=hdr(ADMIN_ID), json={"action": "approve"})
    assert r.status_code == 200
    r = client.post(f"/api/admin/withdrawals/{wid}/decision",
                    headers=hdr(ADMIN_ID), json={"action": "approve"})
    assert r.status_code == 400  # already approved
    r = client.post(f"/api/admin/withdrawals/{wid}/decision",
                    headers=hdr(ADMIN_ID), json={"action": "complete"})
    assert r.status_code == 200
    assert store.get_balance(USER_ID) == 50  # points stay spent


def test_withdrawal_reject_releases_points(client):
    funded_user(client)
    store.set_phase(4)
    addr = "UQ" + "c" * 46
    r = client.post("/api/withdraw", headers=hdr(USER_ID),
                    json={"tokens": 100, "wallet": addr})
    wid = r.json()["withdrawal"]["id"]
    r = client.post(f"/api/admin/withdrawals/{wid}/decision",
                    headers=hdr(ADMIN_ID), json={"action": "reject", "reason": "test"})
    assert r.status_code == 200
    assert store.get_balance(USER_ID) == 150


def test_withdrawal_blocked_before_phase(client):
    funded_user(client)
    store.set_phase(2)
    r = client.post("/api/withdraw", headers=hdr(USER_ID),
                    json={"tokens": 100, "wallet": "UQ" + "d" * 46})
    assert r.status_code == 400


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------

def test_admin_routes_require_admin(client):
    register(client, USER_ID)
    r = client.get("/api/admin/overview", headers=hdr(USER_ID))
    assert r.status_code == 403
    r = client.get("/api/admin/overview", headers=hdr(ADMIN_ID))
    assert r.status_code == 200


def test_admin_adjust_and_ledger(client):
    register(client, USER_ID)
    r = client.post("/api/admin/adjust", headers=hdr(ADMIN_ID),
                    json={"user_id": USER_ID, "delta": 25, "reason": "promo"})
    assert r.status_code == 200
    assert store.get_balance(USER_ID) == 25
    r = client.post("/api/admin/adjust", headers=hdr(ADMIN_ID),
                    json={"user_id": USER_ID, "delta": -30, "reason": "fix"})
    assert r.status_code == 400  # would go negative
    assert store.get_balance(USER_ID) == 25

    # Ledger is consistent with the balance.
    rows = store.ledger_for(USER_ID)
    assert sum(r["delta"] for r in rows) == store.get_balance(USER_ID)


def test_ban_blocks_api(client):
    register(client, USER_ID)
    r = client.post("/api/admin/ban", headers=hdr(ADMIN_ID),
                    json={"user_id": USER_ID, "banned": True})
    assert r.status_code == 200
    assert client.get("/api/profile", headers=hdr(USER_ID)).status_code == 403


def test_admin_cannot_ban_admin(client):
    register(client, ADMIN_ID)
    r = client.post("/api/admin/ban", headers=hdr(ADMIN_ID),
                    json={"user_id": ADMIN_ID, "banned": True})
    assert r.status_code == 400
