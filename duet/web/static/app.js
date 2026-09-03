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
  $("#docview").hidden = true;
  $("#session").hidden = false;
  $("#timeline").replaceChildren();
  $("#summary").replaceChildren();
  $("#gate").replaceChildren();
  seen.clear();
  switchTab("debate");
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
  renderDocument(snap.document || "");
}

// -- document page: render the agreed document, with a section jump-list ----

function renderMarkdownInto(docEl, navEl, md, emptyText) {
  docEl.replaceChildren(); navEl.replaceChildren();
  if (!md) { docEl.append(el("p", "empty", emptyText)); return; }
  const { nodes, sections } = markdownToNodes(md);
  nodes.forEach((n) => docEl.append(n));
  if (sections.length > 2) {
    for (const sec of sections) {
      const a = el("a", "doc-link", sec.text);
      a.href = "#" + sec.id;
      a.onclick = (e) => { e.preventDefault(); document.getElementById(sec.id).scrollIntoView({ behavior: "smooth" }); };
      navEl.append(a);
    }
  }
}

function renderDocument(md) {
  renderMarkdownInto($("#doc"), $("#doc-nav"), md,
    "No document yet — it appears once the models draft one.");
}

// -- project documentation -------------------------------------------------

async function loadDocsList() {
  const list = $("#docs-list");
  list.replaceChildren();
  let docs = [];
  try { docs = await api("/api/docs"); } catch { /* docs not shipped */ }
  if (!docs.length) { $("#docs-section").hidden = true; return; }
  $("#docs-section").hidden = false;
  for (const d of docs) {
    const item = el("div", "session-item doc-item");
    item.append(el("span", "lbl", d.title));
    item.onclick = () => openDoc(d.id);
    list.append(item);
  }
}

async function openDoc(id) {
  active = null;
  if (stream) stream.close();
  $("#session").hidden = true;
  $("#new-form").hidden = true;
  $("#empty").hidden = true;
  $("#docview").hidden = false;
  const d = await api(`/api/docs/${id}`);
  $("#docview-title").textContent = d.title;
  renderMarkdownInto($("#docview-body"), $("#docview-nav"), d.markdown, "");
  document.querySelectorAll("#docs-list .doc-item").forEach((n, i) => n.classList.toggle("active", false));
  loadSessions();
  window.scrollTo({ top: 0 });
}

// A small, safe markdown renderer — enough for these documents (headings,
// paragraphs, lists, inline `code` and **bold**). Text is set via textContent,
// so nothing in the document can inject markup.
function markdownToNodes(md) {
  const nodes = [], sections = [];
  const lines = md.replace(/\r/g, "").split("\n");
  let list = null, para = [], fence = null, table = null;
  const flushPara = () => { if (para.length) { nodes.push(inline(el("p"), para.join(" "))); para = []; } };
  const flushList = () => { if (list) { nodes.push(list); list = null; } };
  const flushTable = () => { if (table) { nodes.push(table); table = null; } };
  const flushAll = () => { flushPara(); flushList(); flushTable(); };
  let secN = 0;
  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;

    // Fenced code: everything inside is literal, including markdown syntax.
    if (line.startsWith("```")) {
      if (fence) { nodes.push(fence); fence = null; }
      else { flushAll(); fence = el("pre"); }
      continue;
    }
    if (fence) {
      fence.append(document.createTextNode(raw + "\n"));
      continue;
    }

    // Tables: | a | b | with a |---|---| separator row.
    if (line.startsWith("|") && line.endsWith("|")) {
      const cells = line.slice(1, -1).split("|").map((c) => c.trim());
      if (cells.every((c) => /^:?-{2,}:?$/.test(c))) continue;  // separator row
      flushPara(); flushList();
      if (!table) { table = el("table"); table.dataset.head = "1"; }
      const tr = el("tr");
      const tag = table.dataset.head === "1" ? "th" : "td";
      for (const c of cells) tr.append(inline(el(tag), c));
      table.dataset.head = "0";
      table.append(tr);
      continue;
    }
    flushTable();

    if (line === "---" || line === "***") { flushAll(); nodes.push(el("hr")); continue; }
    if ((m = line.match(/^>\s?(.*)$/))) {
      flushAll();
      nodes.push(inline(el("blockquote"), m[1]));
      continue;
    }

    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara(); flushList();
      const level = m[1].length, text = m[2];
      const h = el("h" + Math.min(level + 1, 5), null, text);
      if (level <= 2) { const id = "sec-" + (secN++); h.id = id; sections.push({ id, text }); }
      nodes.push(h);
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      flushPara();
      if (!list || list.tagName !== "UL") { flushList(); list = el("ul"); }
      list.append(inline(el("li"), m[1]));
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      flushPara();
      if (!list || list.tagName !== "OL") { flushList(); list = el("ol"); }
      list.append(inline(el("li"), m[1]));
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList(); para.push(line.trim());
    }
  }
  flushPara(); flushList(); flushTable();
  if (fence) nodes.push(fence);
  return { nodes, sections };
}

// Render inline **bold**, *italic* and `code` into a node without ever using
// innerHTML. Bold is listed before italic so ** is never eaten by the * rule.
function inline(node, text) {
  const re = /(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) node.append(document.createTextNode(text.slice(last, m.index)));
    const tok = m[0];
    if (tok.startsWith("**")) node.append(el("strong", null, tok.slice(2, -2)));
    else if (tok.startsWith("*")) node.append(el("em", null, tok.slice(1, -1)));
    else node.append(el("code", null, tok.slice(1, -1)));
    last = m.index + tok.length;
  }
  if (last < text.length) node.append(document.createTextNode(text.slice(last)));
  return node;
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $("#tab-debate").hidden = name !== "debate";
  $("#tab-document").hidden = name !== "document";
}
document.querySelectorAll(".tab").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));

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

  if (snap.state === "BUDGET_STOP") {
    const box = el("div", "gate-box");
    box.append(el("h3", null, "Budget reached"));
    box.append(el("div", "facts",
      `Spent $${snap.cost_usd.toFixed(4)} of the $${snap.budget_usd.toFixed(2)} cap. ` +
      "Raise it to continue from where it stopped."));
    const row = el("div", "row");
    const input = el("input"); input.type = "number"; input.step = "1";
    input.value = Math.ceil(snap.budget_usd * 2); input.style.maxWidth = "8rem";
    const b = el("button", "primary", "Raise & resume");
    b.onclick = async () => {
      await api(`/api/sessions/${active}/budget`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ budget: parseFloat(input.value) }),
      });
      await refresh(); loadSessions();
    };
    row.append(input, b); box.append(row); wrap.append(box);
    return;
  }

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
  $("#docview").hidden = true;
  const sel = $("#template");
  sel.replaceChildren();
  for (const t of await api("/api/templates")) {
    const opt = el("option", null, `${t.label} — ${t.when}`);
    opt.value = t.name;
    sel.append(opt);
  }
  const psel = $("#profile");
  psel.replaceChildren();
  const { profiles, default_budget } = await api("/api/profiles");
  const def = el("option", null, "default"); def.value = ""; psel.append(def);
  for (const name of profiles) { const o = el("option", null, name); o.value = name; psel.append(o); }
  $("#budget").placeholder = `default $${default_budget}`;
  $("#new-form").hidden = false;
  loadSessions();
}

$("#new-btn").onclick = showNewForm;
$("#cancel-new").onclick = () => { $("#new-form").hidden = true; $("#empty").hidden = false; };
$("#new-form").onsubmit = async (e) => {
  e.preventDefault();
  const idea = $("#idea").value.trim();
  if (!idea) return;
  const budgetVal = $("#budget").value.trim();
  const { id } = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      idea, template: $("#template").value, context: $("#context").value,
      profile: $("#profile").value, budget: budgetVal ? parseFloat(budgetVal) : null,
    }),
  });
  $("#idea").value = ""; $("#context").value = ""; $("#budget").value = "";
  openSession(id);
};

loadSessions();
loadDocsList();
setInterval(() => { if (!active || !document.hidden) loadSessions(); }, 5000);
