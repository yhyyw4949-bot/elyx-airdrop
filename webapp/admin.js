// Admin panel logic. Loaded by index.html; initialized by app.js when an
// admin opens the Admin view. `ctx` provides { api, toast, demo } from app.js.
window.initAdmin = async function (ctx) {
  const { api, toast, demo } = ctx;
  const headers = { "X-Telegram-Init-Data": tg.initData, "Content-Type": "application/json" };

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
    document.getElementById("st-banned").textContent = o.banned;
    document.getElementById("st-pending-wd").textContent = o.pending_withdrawals;
    document.getElementById("st-completed-wd").textContent = o.completed_withdrawals;
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
  
  async function loadUsers(q = "") {
    const { users } = await api(`/api/admin/users${q ? `?q=${encodeURIComponent(q)}` : ""}`);
    const tbody = document.querySelector("#users-table tbody");
    tbody.innerHTML = "";
    for (const u of users) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${esc(u.first_name || "-")} <span class="muted">@${esc(u.username || "-")}</span>` +
        `${u.banned ? ' <span class="badge rejected">banned</span>' : ""}</td>` +
        `<td>${u.points}</td><td>${u.tokens}</td>` +
        `<td class="mono">${u.wallet ? esc(u.wallet.slice(0, 8) + "…") : "—"}</td>` +
        `<td>` +
        `<button class="btn small adjust-btn" data-id="${u.user_id}" data-name="${esc(u.username || u.first_name || u.user_id)}">±pts</button> ` +
        `<button class="btn small ${u.banned ? "" : "danger"} ban-btn" data-id="${u.user_id}" data-banned="${u.banned ? 1 : 0}">${u.banned ? "Unban" : "Ban"}</button>` +
        `</td>`;
      tbody.appendChild(tr);
    }
  }
  
  async function loadWithdrawals() {
    const status = document.getElementById("wd-filter").value;
    const { withdrawals } = await api(`/api/admin/withdrawals${status ? `?status=${status}` : ""}`);
    const tbody = document.querySelector("#wd-table tbody");
    tbody.innerHTML = "";
    document.getElementById("wd-empty").hidden = withdrawals.length > 0;
    for (const w of withdrawals) {
      const tr = document.createElement("tr");
      let actions = "";
      if (w.status === "pending") {
        actions =
          `<button class="btn small primary wd-btn" data-id="${w.id}" data-action="approve">Approve</button> ` +
          `<button class="btn small danger wd-btn" data-id="${w.id}" data-action="reject">Reject</button>`;
      } else if (w.status === "approved") {
        actions =
          `<button class="btn small primary wd-btn" data-id="${w.id}" data-action="complete">Mark paid</button> ` +
          `<button class="btn small danger wd-btn" data-id="${w.id}" data-action="reject">Reject</button>`;
      } else {
        actions = `<span class="badge ${w.status}">${w.status}</span>`;
      }
      tr.innerHTML =
        `<td>${esc(w.first_name || "-")} <span class="muted">@${esc(w.username || w.user_id)}</span><br>` +
        `<span class="muted">${esc(w.created_at || "").slice(0, 16).replace("T", " ")}</span></td>` +
        `<td>${w.tokens}</td><td class="mono">${esc((w.wallet || "").slice(0, 10))}…</td>` +
        `<td>${actions}</td>`;
      tbody.appendChild(tr);
    }
  }
  
  async function loadTasks() {
    const { tasks } = await api("/api/admin/tasks");
    const wrap = document.getElementById("tasks-admin");
    wrap.innerHTML = "";
    for (const t of tasks) {
      const div = document.createElement("div");
      div.className = "row task-admin-row";
      div.innerHTML =
        `<span class="${t.active ? "" : "muted"}">${esc(t.title)} ` +
        `<span class="muted">${esc(t.key)} · ${t.points}pts · ${t.participants} done</span></span>` +
        `<span>` +
        `<button class="btn small task-toggle" data-id="${t.id}" data-active="${t.active ? 1 : 0}">${t.active ? "Disable" : "Enable"}</button> ` +
        `<button class="btn small danger task-del" data-id="${t.id}">Delete</button>` +
        `</span>`;
      wrap.appendChild(div);
    }
  }
  
  async function loadAudit() {
    const { actions } = await api("/api/admin/audit");
    const tbody = document.querySelector("#audit-table tbody");
    tbody.innerHTML = "";
    for (const a of actions) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td class="muted">${esc((a.created_at || "").slice(0, 16).replace("T", " "))}</td>` +
        `<td>${esc(a.action)}</td><td class="muted">${esc(a.details || "")}</td>`;
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
  
  document.getElementById("refresh-wl").addEventListener("click", () =>
    loadWhitelist().catch((e) => toast(e.message)));
  document.getElementById("refresh-users").addEventListener("click", () =>
    loadUsers().catch((e) => toast(e.message)));
  document.getElementById("refresh-wd").addEventListener("click", () =>
    loadWithdrawals().catch((e) => toast(e.message)));
  document.getElementById("wd-filter").addEventListener("change", () =>
    loadWithdrawals().catch((e) => toast(e.message)));
  document.getElementById("search-btn").addEventListener("click", () =>
    loadUsers(document.getElementById("user-search").value).catch((e) => toast(e.message)));
  
  document.getElementById("users-table").addEventListener("click", async (e) => {
    const banBtn = e.target.closest(".ban-btn");
    const adjBtn = e.target.closest(".adjust-btn");
    try {
      if (banBtn) {
        const banned = banBtn.dataset.banned === "1";
        await api("/api/admin/ban", "POST",
          { user_id: Number(banBtn.dataset.id), banned: !banned });
        toast(banned ? "Unbanned" : "Banned");
        await Promise.all([loadUsers(document.getElementById("user-search").value), loadOverview()]);
      } else if (adjBtn) {
        const delta = Number(prompt(`Points delta for ${adjBtn.dataset.name} (e.g. 50 or -20):`));
        if (!delta) return;
        const reason = prompt("Reason (shown in the ledger):") || "adjustment";
        await api("/api/admin/adjust", "POST",
          { user_id: Number(adjBtn.dataset.id), delta, reason });
        toast("Balance updated");
        await loadUsers(document.getElementById("user-search").value);
      }
    } catch (err) {
      toast(err.message);
    }
  });
  
  document.getElementById("wd-table").addEventListener("click", async (e) => {
    const btn = e.target.closest(".wd-btn");
    if (!btn) return;
    let reason = null;
    if (btn.dataset.action === "reject") {
      reason = prompt("Rejection reason (optional):") || null;
    }
    btn.disabled = true;
    try {
      await api(`/api/admin/withdrawals/${btn.dataset.id}/decision`, "POST",
        { action: btn.dataset.action, reason });
      toast(`Withdrawal ${btn.dataset.action}d`);
      await Promise.all([loadWithdrawals(), loadOverview()]);
    } catch (err) {
      toast(err.message);
      btn.disabled = false;
    }
  });
  
  document.getElementById("task-save").addEventListener("click", async () => {
    try {
      const body = {
        id: document.getElementById("task-id").value
          ? Number(document.getElementById("task-id").value) : null,
        key: document.getElementById("task-key").value.trim(),
        title: document.getElementById("task-title").value.trim(),
        kind: document.getElementById("task-kind").value,
        url: document.getElementById("task-url").value.trim() || null,
        points: Number(document.getElementById("task-points").value) || 0,
        verification: document.getElementById("task-verification").value,
        max_participants: document.getElementById("task-max").value
          ? Number(document.getElementById("task-max").value) : null,
        active: true,
        sort_order: Number(document.getElementById("task-order").value) || 0,
      };
      if (!body.key || !body.title) throw new Error("Key and title are required");
      await api("/api/admin/tasks", "POST", body);
      toast("Task saved");
      document.getElementById("task-id").value = "";
      await loadTasks();
    } catch (e) {
      toast(e.message);
    }
  });
  
  document.getElementById("tasks-admin").addEventListener("click", async (e) => {
    const toggle = e.target.closest(".task-toggle");
    const del = e.target.closest(".task-del");
    try {
      if (toggle) {
        // Re-save with flipped active flag: fetch current task list for the row.
        const { tasks } = await api("/api/admin/tasks");
        const t = tasks.find((x) => x.id === Number(toggle.dataset.id));
        if (!t) return;
        await api("/api/admin/tasks", "POST", {
          id: t.id, key: t.key, title: t.title, kind: t.kind, url: t.url,
          points: t.points, verification: t.verification,
          max_participants: t.max_participants, active: !t.active,
          sort_order: t.sort_order,
        });
        toast(t.active ? "Task disabled" : "Task enabled");
        await loadTasks();
      } else if (del) {
        if (!confirm("Delete this task? (Tasks with completions are disabled instead.)")) return;
        await api(`/api/admin/tasks/${del.dataset.id}`, "DELETE");
        toast("Task removed");
        await loadTasks();
      }
    } catch (err) {
      toast(err.message);
    }
  });
  
  document.getElementById("broadcast-send").addEventListener("click", async () => {
    const btn = document.getElementById("broadcast-send");
    const text = document.getElementById("broadcast-text").value.trim();
    if (!text) { toast("Write the announcement first"); return; }
    if (!confirm("Send this to all users?")) return;
    btn.disabled = true;
    try {
      const r = await api("/api/admin/broadcast", "POST", { text });
      toast(`Broadcast sent to ${r.sent} users (${r.failed} failed)`);
      document.getElementById("broadcast-text").value = "";
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
    }
  });
  
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
  
  

  if (demo) {

    document.getElementById("panel").hidden = false;
    document.getElementById("phase-banner").textContent = "Phase 2 of 4 · Points collection";
    document.getElementById("st-total").textContent = "1,284";
    document.getElementById("st-whitelisted").textContent = "962";
    document.getElementById("st-wallets").textContent = "731";
    document.getElementById("st-referred").textContent = "408";
    document.getElementById("st-banned").textContent = "3";
    document.getElementById("st-pending-wd").textContent = "2";
    document.getElementById("st-completed-wd").textContent = "15";
    const wl = document.querySelector("#whitelist-table tbody");
    [["Omar", "omar_x"], ["Mariam", "mariam.eg"]].forEach(([n, u]) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${n} <span class="muted">@${u}</span></td><td><button class="btn small approve-btn" data-id="0">Approve</button></td>`;
      wl.appendChild(tr);
    });
    const wt = document.querySelector("#wd-table tbody");
    [["Yehia", "elyxrex", 250, "UQDMxfo…"], ["Omar", "omar_x", 120, "EQB7kLm…"]].forEach(([n, u, amt, w]) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${n} <span class="muted">@${u}</span></td><td>${amt}</td>` +
        `<td class="mono">${w}</td>` +
        `<td><button class="btn small primary">Approve</button> <button class="btn small danger">Reject</button></td>`;
      wt.appendChild(tr);
    });
    const ut = document.querySelector("#users-table tbody");
    [["Yehia", "elyxrex", 320, 320, "UQDMxfo…"], ["Omar", "omar_x", 240, 240, "—"], ["Mariam", "mariam.eg", 185, 185, "EQB7kLm…"]].forEach(([n, u, p, t, w]) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${n} <span class="muted">@${u}</span></td><td>${p}</td><td>${t}</td>` +
        `<td class="mono">${w}</td><td><button class="btn small">±pts</button> <button class="btn small danger">Ban</button></td>`;
      ut.appendChild(tr);
    });
    const ta = document.getElementById("tasks-admin");
    [["Join the Telegram channel", "channel · 10pts · 962 done"], ["Follow us on X", "x · 10pts · 844 done"], ["Connect your TON wallet", "wallet · 10pts · 731 done"]].forEach(([t, meta]) => {
      const div = document.createElement("div");
      div.className = "row task-admin-row";
      div.innerHTML = `<span>${t} <span class="muted">${meta}</span></span>` +
        `<span><button class="btn small">Disable</button> <button class="btn small danger">Delete</button></span>`;
      ta.appendChild(div);
    });
    return;
  }
  await loadOverview();
  await Promise.all([loadWhitelist(), loadUsers(), loadWithdrawals(), loadTasks(), loadAudit()]);
};
