const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const initData = tg.initData;
const headers = { "X-Telegram-Init-Data": initData, "Content-Type": "application/json" };

let profile = null;

function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.hidden = false;
  setTimeout(() => (el.hidden = true), 2500);
}

async function api(path, method = "GET", body = null) {
  const res = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : null,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "Request failed");
  }
  return res.json();
}

function markDone(id) {
  const li = document.getElementById(id);
  li.querySelector(".check").textContent = "✅";
  li.classList.add("done");
}

function renderPhaseSteps(phase) {
  const el = document.getElementById("phase-steps");
  el.innerHTML = "";
  for (let i = 1; i <= 4; i++) {
    const d = document.createElement("span");
    d.className = "dot" + (i <= phase ? " on" : "");
    el.appendChild(d);
  }
}

function render() {
  document.getElementById("title").textContent = `${profile.token_name} Airdrop`;
  renderPhaseSteps(profile.phase);
  if (profile.is_admin) document.getElementById("admin-link").hidden = false;
  document.getElementById("phase-banner").textContent =
    `Phase ${profile.phase} of 4 · ${profile.phase_name}`;

  // Phase 1: whitelist status (registering already joined us).
  if (profile.phase === 1) {
    const card = document.getElementById("card-whitelist");
    card.hidden = false;
    document.getElementById("whitelist-status").textContent = profile.whitelisted
      ? "You're on the whitelist ✅ Points collection opens soon."
      : "You're registered. The team reviews whitelist requests shortly.";
  }

  // Phase 2+: tasks and referrals.
  if (profile.phase >= 2) {
    const card = document.getElementById("card-tasks");
    card.hidden = false;
    if (profile.channel_url) {
      document.getElementById("channel-link").href = profile.channel_url;
    } else {
      document.getElementById("channel-link").hidden = true;
    }
    if (profile.group) {
      document.getElementById("task-group").hidden = false;
      if (profile.group_url) {
        document.getElementById("group-link").href = profile.group_url;
      } else {
        document.getElementById("group-link").hidden = true;
      }
    }
    document.getElementById("x-link").href = profile.x_url;

    if (!profile.whitelisted) {
      document.getElementById("tasks-locked").hidden = false;
      document.getElementById("tasks-list").style.opacity = "0.4";
      document.getElementById("tasks-list").style.pointerEvents = "none";
    } else {
      if (profile.joined_channel) {
        markDone("task-channel");
        if (profile.group) markDone("task-group");
      }
      if (profile.followed_x) markDone("task-x");
      if (profile.wallet) {
        markDone("task-wallet");
        const w = document.getElementById("wallet-address");
        w.textContent = profile.wallet;
        w.hidden = false;
      }
    }

    document.getElementById("card-referrals").hidden = false;
    document.getElementById("points").textContent = profile.points;
    document.getElementById("referrals").textContent = profile.referrals;

    const user = tg.initDataUnsafe.user;
    if (profile.bot_username && user) {
      document.getElementById("ref-link").value =
        `https://t.me/${profile.bot_username}?start=ref_${user.id}`;
      document.getElementById("ref-hint").textContent =
        `Share your link: +${profile.points_per_referral} points per friend who joins.`;
    }
  }

  // Phase 3: conversion.
  if (profile.phase === 3) {
    document.getElementById("card-conversion").hidden = false;
    document.getElementById("conv-points").textContent = profile.points;
    document.getElementById("conv-tokens").textContent = `${profile.tokens} ${profile.token_name}`;
    document.getElementById("conv-rate").textContent =
      `Rate: ${profile.points_per_token} point${profile.points_per_token === 1 ? "" : "s"} = 1 ${profile.token_name}`;
  }

  // Phase 4: airdrop allocation.
  if (profile.phase === 4) {
    document.getElementById("card-airdrop").hidden = false;
    document.getElementById("airdrop-tokens").textContent = `${profile.tokens} ${profile.token_name}`;
    document.getElementById("airdrop-status").textContent = profile.wallet
      ? "Your allocation is locked to the wallet above. Distribution is starting."
      : "Connect your TON wallet so we can send your allocation.";
  }
}

document.getElementById("verify-channel").addEventListener("click", async () => {
  try {
    const r = await api("/api/verify_channel", "POST");
    if (r.joined_channel) {
      markDone("task-channel");
      if (profile.group) markDone("task-group");
      toast("Channel verified ✅");
    } else {
      toast("You're not in the channel yet. Join it, then tap verify again.");
    }
  } catch (e) {
    toast(e.message);
  }
});

document.getElementById("confirm-x").addEventListener("click", async () => {
  try {
    await api("/api/confirm_x", "POST");
    markDone("task-x");
    toast("X follow confirmed ✅");
  } catch (e) {
    toast(e.message);
  }
});

document.getElementById("copy-ref").addEventListener("click", async () => {
  const input = document.getElementById("ref-link");
  try {
    await navigator.clipboard.writeText(input.value);
    toast("Link copied!");
  } catch {
    input.select();
    document.execCommand("copy");
    toast("Link copied!");
  }
});

// TON Connect: connecting a wallet sends its address to the backend.
const tonConnectUI = new TON_CONNECT_UI.TonConnectUI({
  manifestUrl: `${location.origin}/tonconnect-manifest.json`,
  buttonRootId: "ton-connect",
});

tonConnectUI.onStatusChange(async (wallet) => {
  if (!wallet) return;
  const raw = wallet.account.address; // raw form, e.g. 0:abcd...
  let friendly = raw;
  try {
    friendly = TON_CONNECT_UI.toUserFriendlyAddress
      ? TON_CONNECT_UI.toUserFriendlyAddress(raw)
      : raw;
  } catch {}
  try {
    const r = await api("/api/link_wallet", "POST", { address: friendly });
    profile.wallet = r.wallet;
    markDone("task-wallet");
    const w = document.getElementById("wallet-address");
    w.textContent = r.wallet;
    w.hidden = false;
    toast("Wallet linked ✅");
  } catch (e) {
    toast(e.message);
  }
});

const DEMO = new URLSearchParams(location.search).get("demo") === "1";

(async () => {
  if (DEMO) {
    profile = {
      token_name: "ELYX", phase: 2, phase_name: "Points collection",
      points_per_token: 1, whitelisted: true, tokens: 320, is_admin: true,
      channel: "@elyx", channel_url: "https://t.me/elyx", group: null, group_url: null,
      x_url: "https://x.com/your_handle", points_per_referral: 10,
      joined_channel: true, followed_x: false, wallet: null, points: 320,
      referrals: 7, bot_username: "Elyvxbot",
    };
    render();
    document.getElementById("ref-link").value = "https://t.me/Elyvxbot?start=ref_7082457308";
    return;
  }
  try {
    profile = await api("/api/register", "POST");
    render();
  } catch (e) {
    toast(e.message);
  }
})();
