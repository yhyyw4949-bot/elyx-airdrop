const tg = (window.Telegram && window.Telegram.WebApp) ||
  { ready() {}, expand() {}, initData: "", initDataUnsafe: {} };
tg.ready();
tg.expand();

const initData = tg.initData;
const headers = { "X-Telegram-Init-Data": initData, "Content-Type": "application/json" };

let profile = null;
let tonConnectUI = null;

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

function renderPhaseSteps(phase) {
  const el = document.getElementById("phase-steps");
  el.innerHTML = "";
  for (let i = 1; i <= 4; i++) {
    const d = document.createElement("span");
    d.className = "dot" + (i <= phase ? " on" : "");
    el.appendChild(d);
  }
}

function renderTask(t) {
  const li = document.createElement("li");
  li.id = `task-${t.key}`;
  if (t.done) li.classList.add("done");
  const label = t.points > 0 ? `+${t.points} pts` : "";
  let controls = "";
  if (t.kind === "wallet") {
    controls = `<div id="ton-connect"></div><p id="wallet-address" class="muted mono" hidden></p>`;
  } else {
    const openBtn = t.url ? `<a class="btn small" target="_blank" href="${t.url}">Open</a>` : "";
    if (t.verification === "channel" || t.verification === "confirm" || t.verification === "x_api") {
      const claimLabel = t.verification === "channel" ? "Verify" : "I followed";
      const xInput = t.x_verify
        ? `<input class="mono x-input" placeholder="Your X username" />` : "";
      controls = `<div class="row">${openBtn}${xInput}<button class="btn small primary claim-btn" data-id="${t.id}">${claimLabel}</button></div>`;
    } else {
      controls = `<div class="row">${openBtn}</div>`;
    }
  }
  li.innerHTML =
    `<span class="check">${t.done ? "✅" : "⬜"}</span>` +
    `<div class="task-body"><strong>${t.title}</strong> <span class="muted">${label}</span>${controls}</div>`;
  return li;
}

function renderTasks() {
  const list = document.getElementById("tasks-list");
  list.innerHTML = "";
  for (const t of profile.tasks || []) list.appendChild(renderTask(t));
  if (!profile.whitelisted) {
    document.getElementById("tasks-locked").hidden = false;
    list.style.opacity = "0.4";
    list.style.pointerEvents = "none";
  } else {
    list.style.opacity = "";
    list.style.pointerEvents = "";
  }

  list.querySelectorAll(".claim-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const li = btn.closest("li");
        const xInput = li.querySelector(".x-input");
        const body = xInput ? { x_username: xInput.value } : {};
        const r = await api(`/api/tasks/${btn.dataset.id}/claim`, "POST", body);
        profile.points = r.points;
        const task = profile.tasks.find((t) => t.id === Number(btn.dataset.id));
        if (task) task.done = true;
        renderTasks();
        renderPoints();
        toast("Task completed ✅");
      } catch (e) {
        toast(e.message);
        btn.disabled = false;
      }
    });
  });

  // TON Connect: connecting a wallet sends its address to the backend.
  const root = document.getElementById("ton-connect");
  if (root && !tonConnectUI && typeof TON_CONNECT_UI !== "undefined") {
    tonConnectUI = new TON_CONNECT_UI.TonConnectUI({
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
        const task = (profile.tasks || []).find((t) => t.kind === "wallet");
        if (task && !task.done) {
          await api(`/api/tasks/${task.id}/claim`, "POST", {}).catch(() => {});
          task.done = true;
        }
        renderTasks();
        renderPoints();
        const w = document.getElementById("wallet-address");
        if (w) { w.textContent = r.wallet; w.hidden = false; }
        toast("Wallet linked ✅");
      } catch (e) {
        toast(e.message);
      }
    });
  }
  const walletTask = (profile.tasks || []).find((t) => t.kind === "wallet" && t.done);
  if (walletTask && profile.wallet) {
    const w = document.getElementById("wallet-address");
    if (w) { w.textContent = profile.wallet; w.hidden = false; }
  }
}

function renderPoints() {
  document.getElementById("points").textContent = profile.points;
  document.getElementById("referrals").textContent = profile.referrals;
}

function renderWithdrawals(list) {
  const el = document.getElementById("wd-list");
  el.innerHTML = "";
  for (const w of list) {
    const div = document.createElement("div");
    div.className = "wd-item";
    const cancel = w.status === "pending"
      ? `<button class="btn small wd-cancel" data-id="${w.id}">Cancel</button>` : "";
    div.innerHTML =
      `<span class="mono">${w.tokens} ELYX</span> ` +
      `<span class="badge ${w.status}">${w.status}</span> ${cancel}`;
    el.appendChild(div);
  }
  el.querySelectorAll(".wd-cancel").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api(`/api/withdraw/${btn.dataset.id}/cancel`, "POST");
        toast("Withdrawal cancelled");
        await loadWithdrawals();
        await refreshProfile();
      } catch (e) {
        toast(e.message);
        btn.disabled = false;
      }
    });
  });
}

async function loadWithdrawals() {
  const r = await api("/api/withdrawals");
  renderWithdrawals(r.withdrawals || []);
}

async function refreshProfile() {
  profile = await api("/api/profile");
  document.getElementById("wd-available").textContent = profile.tokens;
  renderPoints();
}

function render() {
  document.getElementById("title").textContent = `${profile.token_name} Airdrop`;
  renderPhaseSteps(profile.phase);
  if (profile.is_admin) setupAdminToggle();
  document.getElementById("phase-banner").textContent =
    `Phase ${profile.phase} of 4 · ${profile.phase_name}`;

  // Phase 1: whitelist status (registering already joined us).
  if (profile.phase === 1) {
    document.getElementById("card-whitelist").hidden = false;
    document.getElementById("whitelist-status").textContent = profile.whitelisted
      ? "You're on the whitelist ✅ Points collection opens soon."
      : "You're registered. The team reviews whitelist requests shortly.";
  }

  // Phase 2+: tasks and referrals.
  if (profile.phase >= 2) {
    document.getElementById("card-tasks").hidden = false;
    renderTasks();
    document.getElementById("card-referrals").hidden = false;
    renderPoints();
    const user = tg.initDataUnsafe && tg.initDataUnsafe.user;
    if (profile.bot_username && user) {
      document.getElementById("ref-link").value =
        `https://t.me/${profile.bot_username}?start=ref_${user.id}`;
      document.getElementById("ref-hint").textContent =
        `Share your link: +${profile.points_per_referral} points per friend who joins.`;
    }
    api("/api/leaderboard").then((r) => {
      if (!r.referrals || !r.referrals.length) return;
      document.getElementById("leaderboard").hidden = false;
      const ol = document.getElementById("leaderboard-list");
      ol.innerHTML = "";
      for (const row of r.referrals) {
        const li = document.createElement("li");
        li.textContent = `${row.first_name || row.username || "User"} · ${row.c} referrals`;
        ol.appendChild(li);
      }
    }).catch(() => {});
  }

  // Phase 3: conversion.
  if (profile.phase === 3) {
    document.getElementById("card-conversion").hidden = false;
    document.getElementById("conv-points").textContent = profile.points;
    document.getElementById("conv-tokens").textContent = `${profile.tokens} ${profile.token_name}`;
    document.getElementById("conv-rate").textContent =
      `Rate: ${profile.points_per_token} point${profile.points_per_token === 1 ? "" : "s"} = 1 ${profile.token_name}`;
  }

  // Phase 4: airdrop allocation + withdrawals.
  if (profile.phase >= 4) {
    document.getElementById("card-airdrop").hidden = false;
    document.getElementById("airdrop-tokens").textContent = `${profile.tokens} ${profile.token_name}`;
    document.getElementById("airdrop-status").textContent = profile.wallet
      ? "Your allocation is locked to the wallet above. Distribution is starting."
      : "Connect your TON wallet so we can send your allocation.";
  }
  if (profile.withdrawals_open) {
    document.getElementById("card-withdraw").hidden = false;
    document.getElementById("wd-min").textContent = profile.min_withdrawal_tokens;
    document.getElementById("wd-available").textContent = profile.tokens;
    if (profile.wallet) document.getElementById("wd-wallet").value = profile.wallet;
    loadWithdrawals().catch(() => {});
  }
}

document.getElementById("wd-submit").addEventListener("click", async () => {
  const btn = document.getElementById("wd-submit");
  btn.disabled = true;
  try {
    const tokens = Number(document.getElementById("wd-amount").value);
    const wallet = document.getElementById("wd-wallet").value.trim();
    if (!tokens || tokens <= 0) throw new Error("Enter an amount");
    if (!wallet) throw new Error("Enter your wallet address");
    await api("/api/withdraw", "POST", { tokens, wallet, network: "TON" });
    toast("Withdrawal requested ✅ The team reviews it shortly.");
    document.getElementById("wd-amount").value = "";
    await loadWithdrawals();
    await refreshProfile();
  } catch (e) {
    toast(e.message);
  } finally {
    btn.disabled = false;
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

const DEMO = new URLSearchParams(location.search).get("demo") === "1";

let adminReady = false;
function setupAdminToggle() {
  const toggle = document.getElementById("view-toggle");
  toggle.hidden = false;
  toggle.querySelectorAll(".view-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      toggle.querySelectorAll(".view-btn").forEach((b) => b.classList.toggle("active", b === btn));
      const admin = btn.dataset.view === "admin";
      document.getElementById("view-app").hidden = admin;
      document.getElementById("view-admin").hidden = !admin;
      document.getElementById("phase-steps").style.display = admin ? "none" : "";
      if (admin && !adminReady) {
        adminReady = true;
        try {
          await window.initAdmin({ api, toast, demo: DEMO });
        } catch (e) {
          toast(e.message);
        }
      }
    });
  });
}

(async () => {
  if (DEMO) {
    profile = {
      token_name: "ELYX", phase: 2, phase_name: "Points collection",
      points_per_token: 1, whitelisted: true, tokens: 320, is_admin: true,
      channel: "@elyx", channel_url: "https://t.me/elyx", group: null, group_url: null,
      x_url: "https://x.com/your_handle", points_per_referral: 10,
      joined_channel: true, followed_x: false, wallet: null, points: 320,
      referrals: 7, bot_username: "Elyvxbot",
      min_withdrawal_tokens: 100, withdrawals_open: false,
      tasks: [
        { id: 1, key: "channel", title: "Join the Telegram channel", kind: "channel",
          url: "https://t.me/elyx", points: 10, verification: "channel", done: true },
        { id: 2, key: "x", title: "Follow us on X", kind: "x",
          url: "https://x.com/your_handle", points: 10, verification: "confirm", done: false },
        { id: 3, key: "wallet", title: "Connect your TON wallet", kind: "wallet",
          url: null, points: 10, verification: "wallet", done: false },
      ],
    };
    render();
    document.getElementById("ref-link").value = "https://t.me/Elyvxbot?start=ref_7082457308";
    document.getElementById("leaderboard").hidden = false;
    document.getElementById("leaderboard-list").innerHTML =
      "<li>Omar · 12 referrals</li><li>Mariam · 9 referrals</li><li>You · 7 referrals</li>";
    return;
  }
  try {
    profile = await api("/api/register", "POST");
    render();
  } catch (e) {
    toast(e.message);
  }
})();
