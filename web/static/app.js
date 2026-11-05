"use strict";
const $ = (s) => document.querySelector(s);
const log = $("#log");
let sessionId = null;
try { sessionId = sessionStorage.getItem("allowance_sid"); } catch (_) {}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// Escape first, then turn [text](https://...) and bare https links into anchors.
function rich(text) {
  let h = esc(text);
  h = h.replace(/\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/(^|[\s(])(https:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return h.split(/\n{2,}/).map((p) => `<p>${p}</p>`).join("");
}
const money = (v) => "$" + Number(v || 0).toFixed(2);
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast"; t.textContent = msg; document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function addMsg(role, text) {
  const d = document.createElement("div");
  d.className = `msg ${role}`;
  d.innerHTML = role === "agent" ? rich(text) : `<p>${esc(text)}</p>`;
  log.appendChild(d); log.scrollTop = log.scrollHeight;
}

function describe(ev) {
  const o = ev.output || {}, i = ev.input || {};
  const p = o.purchase;
  switch (ev.tool) {
    case "allowance_search_products":
      return [`🔎 search “${i.query}”: ${o.ok ? (o.results || []).length + " results" : o.error}`, ""];
    case "allowance_propose_purchase":
      if (!p) return [`🧾 propose: ${o.error}`, "deny"];
      return [`🧾 policy says ${p.decision} for ${p.title} ×${p.quantity} (${money(p.total)})` +
        (p.decision === "DENY" ? `: ${p.decision_reasons.join("; ")}` : ""), p.decision.toLowerCase()];
    case "allowance_buy":
    case "allowance_confirm_payment":
      if (!p) return [`💳 ${o.error}`, "deny"];
      return [`💳 ${p.title}: ${p.status}` + (p.paypal_order_id ? ` (PayPal order ${p.paypal_order_id})` : ""),
        p.status === "PAID" ? "auto" : p.status === "DENIED" || p.status === "FAILED" ? "deny" : "approval"];
    case "allowance_refund": return [`↩️ refund: ${p ? p.status : o.error}`, ""];
    case "allowance_cancel": return [`✖️ cancel: ${p ? p.status : o.error}`, ""];
    case "allowance_wallet_status": return [`👛 wallet: ${money(o.remaining)} remaining`, ""];
    default: return [`🔧 ${ev.tool}`, ""];
  }
}
function addTrace(ev) {
  const [label, cls] = describe(ev);
  const d = document.createElement("details");
  d.className = `trace ${cls}`;
  d.innerHTML = `<summary>${esc(label)}</summary><pre>${esc(JSON.stringify({ input: ev.input, output: ev.output }, null, 2))}</pre>`;
  log.appendChild(d); log.scrollTop = log.scrollHeight;
}

async function send(text) {
  text = text.trim(); if (!text) return;
  addMsg("user", text);
  $("#input").value = ""; $("#send").disabled = true;
  const typing = document.createElement("div");
  typing.className = "typing"; typing.textContent = "Agent is working…"; log.appendChild(typing);
  try {
    const res = await api("/api/chat", { method: "POST", body: JSON.stringify({ session_id: sessionId, message: text }) });
    sessionId = res.session_id;
    try { sessionStorage.setItem("allowance_sid", sessionId); } catch (_) {}
    typing.remove();
    (res.events || []).forEach(addTrace);
    addMsg("agent", res.reply || "(no reply)");
    if (res.status) renderStatus(res.status);
    loadPurchases();
  } catch (e) {
    typing.remove(); addMsg("agent", "Error: " + e.message);
  } finally { $("#send").disabled = false; $("#input").focus(); }
}

function renderStatus(s) {
  const pol = s.policy, budget = Number(pol.budget) || 0;
  const spent = Number(s.spent), held = Number(s.reserved_for_pending_approvals);
  $("#spent").textContent = money(spent);
  $("#held").textContent = money(held);
  $("#remaining").textContent = money(s.remaining);
  const pct = (v) => budget ? Math.min(100, (v / budget) * 100) + "%" : "0%";
  $("#meter .spent").style.width = pct(spent);
  $("#meter .held").style.width = pct(held);
  $("#envBadge").textContent = s.paypal_env === "live" ? "PayPal LIVE" : "PayPal sandbox";
  const ap = $("#autopay");
  if (s.auto_pay_connected) {
    ap.innerHTML = `<span class="on">● Auto-pay on</span><span>${esc(s.auto_pay_account || "PayPal account saved")}</span><button class="ghost" id="disc" type="button">Disconnect</button>`;
    $("#disc").onclick = async () => { await api("/api/vault/disconnect", { method: "POST" }); refresh(); };
  } else {
    ap.innerHTML = `<span class="off">○ Auto-pay off: every purchase needs your PayPal approval</span><button id="conn" type="button">Connect PayPal</button>`;
    $("#conn").onclick = async () => {
      try { const r = await api("/api/vault/connect", { method: "POST" }); location.href = r.approve_url; }
      catch (e) { toast(e.message); }
    };
  }
  const f = $("#policy");
  if (!f.contains(document.activeElement)) {
    f.budget.value = pol.budget; f.auto_approve_limit.value = pol.auto_approve_limit;
    f.max_per_purchase.value = pol.max_per_purchase; f.max_quantity.value = pol.max_quantity;
    f.allowed_domains.value = (pol.allowed_domains || []).join(", ");
  }
}

async function loadPurchases(flashId) {
  const { purchases } = await api("/api/purchases?limit=15");
  const ul = $("#purchases");
  if (!purchases.length) { ul.innerHTML = '<li class="empty">No purchases yet.</li>'; return; }
  ul.innerHTML = "";
  for (const p of purchases) {
    const li = document.createElement("li");
    li.className = "p" + (p.purchase_id === flashId ? " flash" : "");
    const acts = [];
    if (p.status === "AWAITING_APPROVAL" && p.approve_url) {
      acts.push(`<a href="${esc(p.approve_url)}">Approve in PayPal</a>`);
      acts.push(`<button class="ghost" data-act="confirm" data-id="${p.purchase_id}">I approved: check</button>`);
    }
    if (p.status === "AWAITING_APPROVAL" || p.status === "PROPOSED") acts.push(`<button class="ghost" data-act="cancel" data-id="${p.purchase_id}">Cancel</button>`);
    if (p.status === "PAID") acts.push(`<button class="ghost" data-act="refund" data-id="${p.purchase_id}">Refund</button>`);
    const showReasons = ["DENIED", "AWAITING_APPROVAL", "PROPOSED"].includes(p.status);
    li.innerHTML = `
      <div class="p-row"><span class="p-title">${esc(p.title)}</span><span class="p-total">${money(p.total)}</span></div>
      <div class="p-row"><span class="p-meta">${esc(p.merchant)} · ×${p.quantity}${p.paypal_capture_id ? " · capture " + esc(p.paypal_capture_id) : p.paypal_order_id ? " · order " + esc(p.paypal_order_id) : ""}</span><span class="st ${p.status}">${p.status.replace("_", " ")}</span></div>
      ${showReasons && p.decision_reasons.length ? `<ul class="p-reasons">${p.decision_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      ${p.error ? `<div class="p-meta">${esc(p.error)}</div>` : ""}
      ${acts.length ? `<div class="p-actions">${acts.join("")}</div>` : ""}`;
    ul.appendChild(li);
  }
}

async function refresh(flashId) {
  try { renderStatus(await api("/api/status")); await loadPurchases(flashId); } catch (e) { toast(e.message); }
}

$("#purchases").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  b.disabled = true;
  try {
    const r = await api(`/api/purchases/${b.dataset.id}/${b.dataset.act}`, { method: "POST" });
    if (r.note) toast(r.note); else if (r.error) toast(r.error);
  } catch (err) { toast(err.message); }
  refresh(b.dataset.id);
});
$("#form").addEventListener("submit", (e) => { e.preventDefault(); send($("#input").value); });
$("#chips").addEventListener("click", (e) => { const b = e.target.closest("button[data-q]"); if (b) send(b.dataset.q); });
$("#newChat").onclick = () => {
  sessionId = null; try { sessionStorage.removeItem("allowance_sid"); } catch (_) {}
  log.innerHTML = ""; addMsg("agent", "New chat. What should I buy?");
};
$("#reset").onclick = async () => { await api("/api/reset", { method: "POST" }); refresh(); toast("New budget window started."); };
$("#policy").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    budget: f.budget.value, auto_approve_limit: f.auto_approve_limit.value,
    max_per_purchase: f.max_per_purchase.value, max_quantity: Number(f.max_quantity.value),
    allowed_domains: f.allowed_domains.value.split(",").map((s) => s.trim()).filter(Boolean),
  };
  try { await api("/api/policy", { method: "POST", body: JSON.stringify(body) }); toast("Limits saved."); refresh(); }
  catch (err) { toast(err.message); }
});

const params = new URLSearchParams(location.search);
if (params.get("vault") === "connected") toast("Auto-pay connected.");
if (params.get("vault") === "error") toast("Auto-pay setup failed.");
const flash = params.get("purchase");
if (flash) addMsg("agent", "Back from PayPal. The purchase status is updated on the right.");
if (params.toString()) history.replaceState(null, "", "/");
refresh(flash);
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
