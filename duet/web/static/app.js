"use strict";
// duet web client. Reads the same event log the CLI does (SPECS §6.0); the only
// writes are starting a debate and answering a gate.

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

let active = null;   // session id being viewed
let stream = null;   // EventSource
const seen = new Set();  // event seqs rendered, so history + live never double up

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

// -- session list ----------------------------------------------------------

async function loadSessions() {
  const list = $("#session-list");
  const sessions = await api("/api/sessions");
  list.replaceChildren();
  for (const s of sessions) {
    const item = el("div", "session-item" + (s.id === active ? " active" : ""));
    item.append(el("span", "lbl", s.label));
    const running = s.running ? " · ⏳ running" : "";
    item.append(el("span", "meta", `${s.state} · $${s.cost_usd.toFixed(4)}${running}`));
    item.onclick = () => openSession(s.id);
    list.append(item);
  }
}

// -- one session -----------------------------------------------------------

async function openSession(id) {
  active = id;
  $("#new-form").hidden = true;
  $("#empty").hidden = true;
  $("#session").hidden = false;
  $("#timeline").replaceChildren();
  $("#summary").replaceChildren();
  $("#gate").replaceChildren();
  seen.clear();
  await refresh();
  loadSessions();
  connect(id);
}

async function refresh() {
  if (!active) return;
  const snap = await api(`/api/sessions/${active}`);
  renderHead(snap);
  renderSummary(snap);
  renderGate(snap);
  $("#document").textContent = snap.document || "(no document yet)";
}

function renderHead(snap) {
  const head = $("#session-head");
  head.replaceChildren();
  head.append(el("h2", null, snap.label || "(untitled)"));
  head.append(el("span", "badge " + snap.state, snap.state));
  head.append(el("span", "badge", snap.template));
  if (snap.running) head.append(el("span", "spin", "⏳ debating…"));
  head.append(el("span", "cost", `$${snap.cost_usd.toFixed(4)}`));
}

function renderSummary(snap) {
  const box = $("#summary");
  box.replaceChildren();
  if (!snap.summary) return;
  box.append(el("h4", null, "In short"));
  box.append(el("div", null, snap.summary));
}

// -- gate ------------------------------------------------------------------

function renderGate(snap) {
  const wrap = $("#gate");
  wrap.replaceChildren();
  const gate = snap.gate;
  if (!gate) return;

  const box = el("div", "gate-box");
  const isArb = gate.gate === "arbitration";
  box.append(el("h3", null, isArb ? "The models could not agree" : "Your decision"));

  if (gate.facts) {
    const f = gate.facts;
    box.append(el("div", "facts",
      `${f.rounds} rounds · $${(f.cost_usd || 0).toFixed(4)} · ` +
      `${f.objections_closed}/${f.objections_raised} objections settled` +
      (f.objections_open ? `, ${f.objections_open} still open` : "")));
  }

  for (const o of gate.objections || []) {
    const row = el("div", "objection");
    row.append(el("span", "sev " + o.severity, o.severity));
    row.append(document.createTextNode(o.headline || o.claim.slice(0, 90)));
    const held = (o.held_by || []).join(", ");
    if (held) row.append(el("span", "cost", `  · held by ${held}`));
    if (o.claim && o.claim !== o.headline) {
      const d = el("details");
      d.append(el("summary", null, "detail"));
      d.append(el("p", null, o.claim));
      row.append(d);
    }
    box.append(row);
  }

  const actions = el("div", "row");
  actions.style.marginTop = ".8rem";
  for (const opt of gate.options) {
    const b = el("button", opt === "approve" ? "primary" : "", opt);
    b.onclick = () => act(opt, gate);
    actions.append(b);
  }
  box.append(actions);
  wrap.append(box);
}

async function act(action, gate) {
  let feedback = "";
  if (action === "revise" || action === "resolve") {
    const label = action === "resolve"
      ? "Your ruling (becomes a FIXED: item the models must accept):"
      : "What should change?";
    feedback = prompt(label) || "";
    if (!feedback.trim()) return;
  }
  await api(`/api/sessions/${active}/gate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, actor: "web", feedback }),
  });
  await refresh();
  loadSessions();
}

// -- live stream -----------------------------------------------------------

function connect(id) {
  if (stream) stream.close();
  stream = new EventSource(`/api/sessions/${id}/events`);
  stream.onmessage = (msg) => {
    if (id !== active) return;
    const ev = JSON.parse(msg.data);
    if (ev.type === "_eof") { refresh(); loadSessions(); return; }
    renderEvent(ev);
  };
  stream.onerror = () => { /* browser auto-reconnects */ };
}

function renderEvent(ev) {
  if (ev.seq != null) {
    if (seen.has(ev.seq)) return;
    seen.add(ev.seq);
  }
  const p = ev.payload || {};
  const tl = $("#timeline");
  if (ev.type === "draft") {
    const t = el("div", "turn draft");
    t.append(el("span", "who", p.human ? (p.participant + " (you)") : p.participant));
    t.append(document.createTextNode(`  drafted round ${p.round}` +
      ((p.addressed && p.addressed.length) ? ` · addresses ${p.addressed.join(", ")}` : "")));
    tl.append(t);
  } else if (ev.type === "verdict") {
    const v = p.verdict || {};
    const t = el("div", "turn");
    t.append(el("span", "who", p.participant));
    t.append(document.createTextNode("  "));
    t.append(el("span", "v-" + v.verdict, v.verdict));
    if (p.coerced) t.append(el("span", "cost", `  (coerced: ${p.coerced})`));
    if ((v.objections || []).length) {
      const ul = el("ul");
      for (const o of v.objections) {
        ul.append(el("li", null, `${o.severity}: ${o.headline || o.claim.slice(0, 80)}`));
      }
      t.append(ul);
    }
    tl.append(t);
  } else if (ev.type === "state_change" && p.to) {
    tl.append(el("div", "state-line", `— ${p.from || "?"} → ${p.to} —`));
    // A gate opening or the run ending changes the header/gate panel.
    if (["GATE_1", "ARBITRATION", "DONE", "FAILED", "BUDGET_STOP"].includes(p.to)) refresh();
  } else if (ev.type === "error") {
    tl.append(el("div", "state-line", `⚠ ${p.error || "error"}`));
  }
  tl.scrollIntoView({ block: "end" });
}

// -- new debate ------------------------------------------------------------

async function showNewForm() {
  active = null;
  if (stream) stream.close();
  $("#session").hidden = true;
  $("#empty").hidden = true;
  const sel = $("#template");
  sel.replaceChildren();
  for (const t of await api("/api/templates")) {
    const opt = el("option", null, `${t.label} — ${t.when}`);
    opt.value = t.name;
    sel.append(opt);
  }
  $("#new-form").hidden = false;
  loadSessions();
}

$("#new-btn").onclick = showNewForm;
$("#cancel-new").onclick = () => { $("#new-form").hidden = true; $("#empty").hidden = false; };
$("#new-form").onsubmit = async (e) => {
  e.preventDefault();
  const idea = $("#idea").value.trim();
  if (!idea) return;
  const { id } = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idea, template: $("#template").value, context: $("#context").value }),
  });
  $("#idea").value = ""; $("#context").value = "";
  openSession(id);
};

loadSessions();
setInterval(() => { if (!active || !document.hidden) loadSessions(); }, 5000);
