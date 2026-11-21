const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const initData = tg.initData;
const headers = { "X-Telegram-Init-Data": initData, "Content-Type": "application/json" };

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

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function loadOverview() {
  const o = await api("/api/admin/overview");
  document.getElementById("phase-banner").textContent =
    `Phase ${o.phase} of 4 · ${o.phase_name}`;
  document.getElementById("st-total").textContent = o.total;
  document.getElementById("st-whitelisted").textContent = o.whitelisted;
  document.getElementById("st-wallets").textContent = o.wallets;
  document.getElementById("st-referred").textContent = o.referred;
  document.querySelectorAll(".phase-btn").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.phase) === o.phase);
  });
}

async function loadWhitelist() {
  const { pending } = await api("/api/admin/whitelist");
  const tbody = document.querySelector("#whitelist-table tbody");
  tbody.innerHTML = "";
  document.getElementById("whitelist-empty").hidden = pending.length > 0;
  for (const u of pending) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${esc(u.first_name || "-")} <span class="muted">@${esc(u.username || "-")}</span></td>` +
      `<td><button class="btn small approve-btn" data-id="${u.user_id}">Approve</button></td>`;
    tbody.appendChild(tr);
  }
}

async function loadUsers() {
  const { users } = await api("/api/admin/users");
  const tbody = document.querySelector("#users-table tbody");
  tbody.innerHTML = "";
  for (const u of users) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${esc(u.first_name || "-")} <span class="muted">@${esc(u.username || "-")}</span></td>` +
      `<td>${u.points}</td><td>${u.tokens}</td>` +
      `<td class="mono">${u.wallet ? esc(u.wallet.slice(0, 8) + "…") : "—"}</td>`;
    tbody.appendChild(tr);
  }
}

document.querySelectorAll(".phase-btn").forEach((b) => {
  b.addEventListener("click", async () => {
    try {
      await api("/api/admin/phase", "POST", { phase: Number(b.dataset.phase) });
      toast(`Phase set to ${b.dataset.phase}`);
      await loadOverview();
    } catch (e) {
      toast(e.message);
    }
  });
});

document.getElementById("approve-all").addEventListener("click", async () => {
  try {
    const r = await api("/api/admin/approve", "POST", { all: true });
    toast(`Approved ${r.approved}`);
    await Promise.all([loadWhitelist(), loadOverview(), loadUsers()]);
  } catch (e) {
    toast(e.message);
  }
});

document.getElementById("whitelist-table").addEventListener("click", async (e) => {
  const btn = e.target.closest(".approve-btn");
  if (!btn) return;
  try {
    await api("/api/admin/approve", "POST", { user_id: Number(btn.dataset.id) });
    toast("Approved");
    await Promise.all([loadWhitelist(), loadOverview(), loadUsers()]);
  } catch (err) {
    toast(err.message);
  }
});

document.getElementById("refresh-wl").addEventListener("click", loadWhitelist);
document.getElementById("refresh-users").addEventListener("click", loadUsers);

document.getElementById("export").addEventListener("click", async () => {
  try {
    const res = await fetch("/api/admin/export", { headers });
    if (!res.ok) throw new Error("Export failed");
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "airdrop_users.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) {
    toast(e.message);
  }
});

(async () => {
  try {
    await loadOverview();
    document.getElementById("panel").hidden = false;
    await Promise.all([loadWhitelist(), loadUsers()]);
  } catch (e) {
    document.getElementById("locked").hidden = false;
  }
})();
