"use strict";
// A client of the event log: the server owns every debate and gate decision.
// Model text is always rendered with DOM nodes, never injected as HTML.
const $ = (selector) => document.querySelector(selector);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};
const stateNames = {
  INIT: "Starting", DESIGN_DEBATE: "In debate", GATE_1: "Ready for review",
  ARBITRATION: "Needs your input", BUDGET_STOP: "Budget reached", DONE: "Approved",
  FAILED: "Stopped", BUILD: "Building", REVIEW: "In review", GATE_2: "Ready for review",
};
const actionNames = {
  approve: "Approve document", edit: "Edit document", revise: "Request changes",
  reject: "Reject", resolve: "Resolve disagreement", abort: "End debate",
};
const seen = new Set();
const rounds = new Map();
let active = null;
let activeDoc = null;
let stream = null;
let snapshot = null;
let sessionCache = [];
let navigation = 0;
let refreshNumber = 0;
let sessionRequest = 0;
let renderedGate = "";
let renderedDocument = null;
let renderedSummary = null;
let activeAction = null;
let actionBusy = false;
let newBusy = false;
let optionsLoaded = false;
let optionsRequest = null;
let retryGlobal = null;
let streamConnected = false;
let lastRound = 0;
let markdownScope = 0;
let refreshTimer = null;
const sidebarMedia = window.matchMedia("(max-width: 720px)");

const friendlyState = (state) => stateNames[state] || String(state || "Starting").replaceAll("_", " ").toLowerCase();
const money = (value) => "$" + Number(value || 0).toFixed(Number(value || 0) < 1 ? 3 : 2);
const compactDate = (value) => {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};
const clockTime = (value) => {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
};

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    let detail = body.detail || response.statusText || "Request failed";
    if (Array.isArray(detail)) detail = detail.map((item) => item.msg).join("; ");
    const error = new Error(typeof detail === "string" ? detail : "The request could not be completed.");
    error.status = response.status;
    throw error;
  }
  return body;
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

function showError(error, retry) {
  $("#global-error-text").textContent = error.message || String(error);
  $("#global-error").hidden = false;
  retryGlobal = retry || null;
  $("#retry-btn").hidden = !retry;
}
function clearError() { $("#global-error").hidden = true; retryGlobal = null; }
function inlineError(node, error) { node.textContent = error.message || String(error); node.hidden = false; }
function setSidebar(open, restoreFocus = false) {
  const sidebar = $("#sidebar");
  const returnFocus = sidebarMedia.matches && !open &&
    (restoreFocus || sidebar.contains(document.activeElement));
  document.body.classList.toggle("sidebar-open", open);
  $("#sidebar-toggle").setAttribute("aria-expanded", String(open));
  sidebar.inert = sidebarMedia.matches && !open;
  $("#sidebar-scrim").hidden = !sidebarMedia.matches || !open;
  if (open && sidebarMedia.matches) $("#session-search").focus();
  else if (returnFocus) $("#sidebar-toggle").focus();
}
function disconnect() {
  if (stream) stream.close();
  stream = null;
  streamConnected = false;
}
function home() {
  navigation++;
  active = null; activeDoc = null; snapshot = null;
  disconnect();
  $("#session").hidden = true;
  $("#docview").hidden = true;
  $("#empty").hidden = false;
  $("#breadcrumb").textContent = "Overview";
  document.title = "Duet — A second perspective";
  clearError();
  renderSessionList();
  markDocs();
  setSidebar(false);
}

// -- Session navigation ---------------------------------------------------
async function loadSessions() {
  const request = ++sessionRequest;
  try {
    const sessions = await api("/api/sessions");
    if (request !== sessionRequest) return;
    sessionCache = sessions;
    renderSessionList();
    // A gate suspends the stream, but another browser or the CLI can resume
    // that session. The list's event timestamp invalidates our cached view
    // even when a whole revision finishes between polls at the same state.
    const listed = sessions.find((session) => session.id === active);
    if (listed && snapshot?.id === active) {
      const changed = listed.state !== snapshot.state || listed.running !== snapshot.running ||
        (listed.updated_at != null && listed.updated_at !== snapshot.updated_at);
      if (changed || snapshot.running) {
        const id = active, version = navigation;
        await refresh();
        if (id === active && version === navigation && !stream) {
          // Replay unseen events after external changes, including completed
          // runs. Existing sequence IDs preserve the reader's current history.
          connect(id, version);
        }
      }
    }
  } catch (error) {
    if (request === sessionRequest) showError(error, loadSessions);
  }
}
function renderSessionList() {
  const query = $("#session-search").value.trim().toLowerCase();
  const filtered = sessionCache.filter((session) =>
    `${session.label} ${friendlyState(session.state)}`.toLowerCase().includes(query));
  const list = $("#session-list");
  // Preserve keyboard focus when the five-second list refresh rebuilds rows.
  const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.session : null;
  list.replaceChildren();
  $("#session-count").textContent = sessionCache.length;
  for (const session of filtered) {
    const item = el("button", "session-item" + (session.id === active ? " active" : ""));
    item.type = "button";
    item.dataset.session = session.id;
    item.setAttribute("aria-current", session.id === active ? "page" : "false");
    const top = el("span", "session-item-top");
    const dot = el("span", "status-dot " + session.state);
    dot.setAttribute("aria-hidden", "true");
    top.append(dot, el("span", "lbl", session.label || "Untitled debate"));
    const meta = [friendlyState(session.state), compactDate(session.updated_at)].filter(Boolean);
    item.append(top, el("span", "meta", meta.join(" · ")));
    item.title = `${session.label || "Untitled debate"} — ${friendlyState(session.state)}`;
    item.onclick = () => openSession(session.id);
    list.append(item);
    if (focusedId === session.id) item.focus({ preventScroll: true });
  }
  if (!filtered.length) list.append(el("p", "sidebar-empty", query ? "No matching debates." : "Your debates will appear here."));
}
async function openSession(id) {
  const version = ++navigation;
  active = id; activeDoc = null; snapshot = null;
  renderedGate = ""; renderedDocument = null; renderedSummary = null;
  seen.clear(); rounds.clear(); lastRound = 0;
  disconnect();
  clearError();
  $("#empty").hidden = true;
  $("#docview").hidden = true;
  $("#session").hidden = false;
  $("#session-head").replaceChildren(el("p", "loading-copy", "Opening debate…"));
  for (const selector of ["#timeline", "#summary", "#gate", "#session-info", "#doc", "#doc-nav"]) $(selector).replaceChildren();
  $("#live-status").hidden = true;
  $("#export-btn").disabled = true;
  $("#timeline-status").textContent = "Loading history";
  switchTab("debate");
  renderSessionList(); markDocs(); setSidebar(false);
  if (await refresh() && version === navigation && active === id) connect(id, version);
}
async function refresh() {
  if (!active) return false;
  const id = active, version = navigation, request = ++refreshNumber;
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(id)}`);
    if (id !== active || version !== navigation || request !== refreshNumber) return false;
    snapshot = data;
    renderHead(data); renderSummary(data); renderGate(data); renderInfo(data);
    renderDocument(data.document || ""); renderLive(data);
    return true;
  } catch (error) {
    if (id === active && version === navigation && request === refreshNumber) showError(error, () => openSession(id));
    return false;
  }
}
function scheduleRefresh() {
  if (refreshTimer) return;
  const id = active, version = navigation;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    if (id === active && version === navigation) refresh();
  }, 120);
}
function participantName(id) {
  const participant = (snapshot?.participants || []).find((person) => person.id === id);
  return participant?.badge || id || "Participant";
}
function avatar(id) {
  const participants = snapshot?.participants || [];
  const index = participants.findIndex((person) => person.id === id);
  const node = el("span", "avatar tone-" + (Math.max(index, 0) % 4), participantName(id).slice(0, 1).toUpperCase());
  node.setAttribute("aria-hidden", "true");
  return node;
}
function renderHead(data) {
  const head = $("#session-head");
  head.replaceChildren();
  head.append(el("p", "eyebrow", "YOUR DEBATE"), el("h1", null, data.label || "Untitled debate"));
  const meta = el("div", "session-meta");
  meta.append(el("span", "badge " + data.state, friendlyState(data.state)));
  meta.append(el("span", "template-label", (data.template || "software").replaceAll("_", " ")));
  if (data.created_at) meta.append(el("span", "created-date", `Started ${compactDate(data.created_at)}`));
  head.append(meta);
  $("#breadcrumb").textContent = "Debate";
  document.title = `${data.label || "Debate"} — Duet`;
}
function renderSummary(data) {
  if (data.summary === renderedSummary) return;
  renderedSummary = data.summary;
  const box = $("#summary");
  box.replaceChildren();
  if (!data.summary) { box.hidden = true; return; }
  box.hidden = false;
  box.append(el("p", "eyebrow", "THE CURRENT DIRECTION"));
  const body = el("div", "summary-copy");
  markdownToNodes(data.summary, "summary").nodes.forEach((node) => body.append(node));
  box.append(body);
}
function renderInfo(data) {
  const box = $("#session-info");
  const briefOpen = Boolean(box.querySelector(".brief-details")?.open);
  box.replaceChildren();
  box.append(el("h3", "info-heading", "Debate at a glance"));
  const counts = data.counts || {};
  const stats = [
    ["Round", data.round ? String(data.round) : "Not started"],
    ["Open objections", counts.objections_open ?? "—"],
    ["Settled objections", counts.objections_closed ?? "—"],
  ];
  for (const [label, value] of stats) {
    const row = el("div", "info-row");
    row.append(el("span", null, label), el("strong", null, value)); box.append(row);
  }
  const budget = el("div", "budget-info");
  const row = el("div", "info-row");
  row.append(el("span", null, "Budget used"), el("strong", null, `${money(data.cost_usd)} / ${money(data.budget_usd)}`));
  const track = el("div", "budget-track");
  track.setAttribute("role", "meter");
  track.setAttribute("aria-label", "Budget used");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", String(data.budget_usd || 0));
  track.setAttribute("aria-valuenow", String(Math.min(data.cost_usd || 0, data.budget_usd || 0)));
  track.setAttribute("aria-valuetext", `${money(data.cost_usd)} of ${money(data.budget_usd)}`);
  const fill = el("span");
  fill.style.width = `${data.budget_usd > 0 ? Math.min(100, 100 * data.cost_usd / data.budget_usd) : 0}%`;
  track.append(fill); budget.append(row, track); box.append(budget);
  box.append(el("h3", "info-heading participants-heading", "At the table"));
  for (const person of data.participants || []) {
    const chip = el("div", "participant-chip");
    const text = el("div");
    text.append(el("strong", null, person.badge || person.id), el("span", "participant-model", person.model || person.provider));
    chip.append(avatar(person.id), text); box.append(chip);
  }
  if (data.brief) {
    const brief = el("details", "brief-details");
    brief.open = briefOpen;
    brief.append(el("summary", null, "Your original brief"), el("p", null, data.brief));
    box.append(brief);
  }
}
function renderLive(data) {
  const status = $("#live-status");
  const call = data.active_call;
  status.hidden = !data.running;
  if (data.running) {
    const action = call?.purpose === "critique" ? "is reviewing the proposal" : "is developing the proposal";
    status.textContent = !streamConnected && stream ? "Reconnecting to the live debate…" :
      call ? `${participantName(call.participant)} ${action}${call.round ? ` · Round ${call.round}` : ""}` : "The debate is moving forward…";
  }
  $("#timeline-status").textContent = data.running ? "Live updates" :
    data.round ? `${data.round} ${data.round === 1 ? "round" : "rounds"}` : "Waiting for the first draft";
}

// -- Safe document rendering ---------------------------------------------
function safeLink(target) {
  if (!target || /[\u0000-\u0020\\]/.test(target)) return null;
  try {
    const url = new URL(target, window.location.href);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}
function inline(node, text) {
  const pattern = /(\*\*[^*]+\*\*|\*[^*\n]+\*|`[^`]+`|\[[^\]\n]+\]\([^\s)]+\))/g;
  let offset = 0, match;
  while ((match = pattern.exec(text))) {
    if (match.index > offset) node.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith("**")) node.append(el("strong", null, token.slice(2, -2)));
    else if (token.startsWith("*")) node.append(el("em", null, token.slice(1, -1)));
    else if (token.startsWith("`")) node.append(el("code", null, token.slice(1, -1)));
    else {
      const link = token.match(/^\[([^\]]+)\]\((.+)\)$/);
      const href = safeLink(link[2]);
      if (href) {
        const anchor = el("a", null, link[1]); anchor.href = href;
        anchor.rel = "noopener noreferrer";
        if (new URL(href).origin !== window.location.origin) anchor.target = "_blank";
        const docId = { "README.md": "readme", "PLAN.md": "plan", "SPECS.md": "specs" }[link[2]];
        if (docId) anchor.onclick = (event) => { event.preventDefault(); openDoc(docId); };
        node.append(anchor);
      } else node.append(document.createTextNode(token));
    }
    offset = match.index + token.length;
  }
  if (offset < text.length) node.append(document.createTextNode(text.slice(offset)));
  return node;
}
function markdownToNodes(markdown, scope = `markdown-${++markdownScope}`) {
  const nodes = [], sections = [];
  const lines = String(markdown).replace(/\r/g, "").split("\n");
  let list = null, paragraph = [], fence = null, table = null, sectionNumber = 0;
  const flushParagraph = () => { if (paragraph.length) { nodes.push(inline(el("p"), paragraph.join(" "))); paragraph = []; } };
  const flushList = () => { if (list) { nodes.push(list); list = null; } };
  const flushTable = () => { if (table) { const wrap = el("div", "table-wrap"); wrap.append(table); nodes.push(wrap); table = null; } };
  const flush = () => { flushParagraph(); flushList(); flushTable(); };
  for (const raw of lines) {
    const line = raw.trimEnd();
    let match;
    if (line.startsWith("```")) {
      if (fence) { nodes.push(fence); fence = null; }
      else { flush(); fence = el("pre"); }
      continue;
    }
    if (fence) { fence.append(document.createTextNode(raw + "\n")); continue; }
    if (line.startsWith("|") && line.endsWith("|")) {
      const cells = line.slice(1, -1).split("|").map((cell) => cell.trim());
      if (cells.every((cell) => /^:?-{2,}:?$/.test(cell))) continue;
      flushParagraph(); flushList();
      const header = !table;
      if (!table) table = el("table");
      const row = el("tr");
      for (const cell of cells) row.append(inline(el(header ? "th" : "td"), cell));
      table.append(row); continue;
    }
    flushTable();
    if (line === "---" || line === "***") { flush(); nodes.push(el("hr")); continue; }
    if ((match = line.match(/^>\s?(.*)$/))) { flush(); nodes.push(inline(el("blockquote"), match[1])); continue; }
    if ((match = line.match(/^(#{1,6})\s+(.*)$/))) {
      flushParagraph(); flushList();
      const level = match[1].length, text = match[2];
      const heading = inline(el("h" + Math.min(level + 1, 6)), text);
      const id = `${scope}-sec-${sectionNumber++}`;
      heading.id = id;
      if (level <= 2) sections.push({ id, text: heading.textContent });
      nodes.push(heading);
    } else if ((match = line.match(/^\s*[-*]\s+(.*)$/))) {
      flushParagraph(); if (!list || list.tagName !== "UL") { flushList(); list = el("ul"); }
      list.append(inline(el("li"), match[1]));
    } else if ((match = line.match(/^\s*\d+\.\s+(.*)$/))) {
      flushParagraph(); if (!list || list.tagName !== "OL") { flushList(); list = el("ol"); }
      list.append(inline(el("li"), match[1]));
    } else if (!line.trim()) { flushParagraph(); flushList(); }
    else { flushList(); paragraph.push(line.trim()); }
  }
  flush(); if (fence) nodes.push(fence);
  return { nodes, sections };
}
function renderMarkdownInto(body, nav, markdown, emptyText, scope) {
  body.replaceChildren(); nav.replaceChildren();
  if (!markdown) { body.append(el("p", "empty-document", emptyText)); nav.hidden = true; return; }
  const rendered = markdownToNodes(markdown, scope);
  rendered.nodes.forEach((node) => body.append(node));
  nav.hidden = !rendered.sections.length;
  if (rendered.sections.length) nav.append(el("p", "eyebrow", "ON THIS PAGE"));
  for (const section of rendered.sections) {
    const link = el("a", "doc-link", section.text); link.href = "#" + section.id;
    link.onclick = (event) => {
      event.preventDefault();
      const target = document.getElementById(section.id);
      target?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
      if (target) { target.tabIndex = -1; target.focus({ preventScroll: true }); }
    };
    nav.append(link);
  }
}
function renderDocument(markdown) {
  if (renderedDocument !== markdown) {
    renderMarkdownInto($("#doc"), $("#doc-nav"), markdown,
      "The working document will appear here after the first draft.", "session-document");
    renderedDocument = markdown;
  }
  $("#export-btn").disabled = !markdown;
  $("#doc-info").textContent = snapshot?.state === "DONE" ? "Approved document · Ready to take forward" :
    markdown ? `Working document · Round ${snapshot?.round || 1} · Review the latest proposal` : "Waiting for the first draft";
}
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((button) => {
    const selected = button.dataset.tab === name;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  $("#tab-debate").hidden = name !== "debate";
  $("#tab-document").hidden = name !== "document";
}
function exportDocument() {
  if (!snapshot?.document) return;
  const url = URL.createObjectURL(new Blob([snapshot.document], { type: "text/markdown;charset=utf-8" }));
  const link = el("a"); link.href = url;
  link.download = (snapshot.label || "duet-document").replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "").slice(0, 80) + ".md";
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// -- Project documentation ------------------------------------------------
function markDocs() {
  document.querySelectorAll(".doc-item").forEach((button) => {
    const selected = button.dataset.doc === activeDoc;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-current", selected ? "page" : "false");
  });
}
async function loadDocsList() {
  try {
    const docs = await api("/api/docs");
    $("#docs-section").hidden = !docs.length;
    $("#welcome-docs").hidden = !docs.length;
    const list = $("#docs-list"); list.replaceChildren();
    for (const doc of docs) {
      const item = el("button", "doc-item", doc.title); item.type = "button"; item.dataset.doc = doc.id;
      item.onclick = () => openDoc(doc.id); list.append(item);
    }
    markDocs();
  } catch { $("#docs-section").hidden = true; $("#welcome-docs").hidden = true; }
}
async function openDoc(id) {
  const version = ++navigation;
  active = null; activeDoc = id; snapshot = null; disconnect();
  $("#session").hidden = true; $("#empty").hidden = true; $("#docview").hidden = false;
  $("#docview-title").textContent = "Loading documentation…";
  $("#docview-body").replaceChildren(); $("#docview-nav").replaceChildren();
  clearError(); renderSessionList(); markDocs(); setSidebar(false);
  try {
    const doc = await api(`/api/docs/${encodeURIComponent(id)}`);
    if (version !== navigation || activeDoc !== id) return;
    $("#docview-title").textContent = doc.title;
    $("#breadcrumb").textContent = doc.title;
    document.title = `${doc.title} — Duet`;
    renderMarkdownInto($("#docview-body"), $("#docview-nav"), doc.markdown, "This document is empty.", "project-document");
  } catch (error) { if (version === navigation) showError(error, () => openDoc(id)); }
}

// -- Human decisions ------------------------------------------------------
function gateIdentity(gate) { return gate ? `${gate.gate_seq}:${gate.draft_hash}` : ""; }
function objectionNode(objection) {
  const details = el("details", "objection");
  const summary = el("summary");
  summary.append(el("span", "sev " + objection.severity, objection.severity || "concern"),
    el("span", "objection-title", objection.headline || objection.claim || "Review concern"));
  details.append(summary);
  const body = el("div", "objection-detail");
  for (const [label, content] of [
    ["Concern", objection.claim], ["Section", objection.section], ["Evidence", objection.evidence],
    ["Suggested change", objection.proposed_change], ["Held by", (objection.held_by || objection.raised_by || []).join(", ")],
  ]) {
    if (!content) continue;
    body.append(el("h4", null, label), el("p", null, content));
  }
  details.append(body); return details;
}
function renderGate(data) {
  // Do not rebuild a budget field or open objection while a user is using it.
  const signature = JSON.stringify([data.id, data.state, data.gate, data.budget_usd]);
  if (signature === renderedGate) return;
  renderedGate = signature;
  const wrap = $("#gate"); wrap.replaceChildren();
  if (data.state === "BUDGET_STOP") {
    const box = el("div", "gate-box budget-stop");
    box.append(el("p", "gate-kicker", "PAUSED"), el("h3", null, "A little more room?"),
      el("p", "gate-description", `This debate used ${money(data.cost_usd)} of its ${money(data.budget_usd)} budget. Increase the cap to continue.`));
    const form = el("form", "budget-form");
    const label = el("label", null, "New total budget (USD)");
    const input = el("input"); input.type = "number"; input.step = "0.01";
    const minimum = (Math.floor(Math.max(data.budget_usd || 0, data.cost_usd || 0) * 100 + 1e-8) + 1) / 100;
    input.min = minimum.toFixed(2);
    input.value = String(Math.max(minimum, Math.ceil(Math.max(data.budget_usd || 1, data.cost_usd || 1) * 2))); input.required = true;
    label.append(input);
    const submit = el("button", "primary", "Raise budget & resume"); submit.type = "submit";
    const error = el("p", "inline-error"); error.hidden = true; error.setAttribute("role", "alert");
    form.append(label, submit, error); box.append(form); wrap.append(box);
    let busy = false;
    form.onsubmit = async (event) => {
      event.preventDefault(); if (busy || !form.reportValidity()) return;
      const id = active, version = navigation;
      busy = true; submit.disabled = true; submit.textContent = "Resuming…"; error.hidden = true;
      try {
        await post(`/api/sessions/${encodeURIComponent(id)}/budget`, { budget: Number(input.value), actor: "web" });
        if (id === active && version === navigation) { await refresh(); connect(id, version); }
        loadSessions();
      } catch (failure) { inlineError(error, failure); }
      finally { busy = false; submit.disabled = false; submit.textContent = "Raise budget & resume"; }
    };
    return;
  }
  if (!data.gate) {
    if (data.state === "DONE") {
      const box = el("div", "gate-box complete");
      box.append(el("p", "gate-kicker", "READY TO TAKE FORWARD"), el("h3", null, "A decision you can build on."),
        el("p", "gate-description", "Your document is approved. Read it in full or export a copy for the next step."));
      const button = el("button", "primary", "Read the document"); button.onclick = () => switchTab("document"); box.append(button); wrap.append(box);
    } else if (data.state === "FAILED") {
      const box = el("div", "gate-box stopped");
      box.append(el("p", "gate-kicker", "DEBATE STOPPED"), el("h3", null, "The work is still here."),
        el("p", "gate-description", "The draft and discussion remain available. Review the timeline for the last decision or error."));
      wrap.append(box);
    }
    return;
  }
  const gate = data.gate, arbitration = gate.gate === "arbitration";
  const box = el("div", "gate-box" + (arbitration ? " arbitration" : ""));
  box.append(el("p", "gate-kicker", arbitration ? "YOUR PERSPECTIVE IS NEEDED" : "OVER TO YOU"),
    el("h3", null, arbitration ? "Help find the way forward." : "A proposal, ready for your call."),
    el("p", "gate-description", arbitration ? "The models have reached a disagreement. Review the concerns and give them a direction." :
      "Review the working document, then approve it, make an edit, or send it back with direction."));
  const read = el("button", "text-button", "Read the full document →"); read.onclick = () => switchTab("document"); box.append(read);
  if (gate.facts) {
    const facts = gate.facts;
    box.append(el("p", "gate-facts", `${facts.objections_closed || 0} of ${facts.objections_raised || 0} objections settled` +
      (facts.objections_open ? ` · ${facts.objections_open} open` : "")));
  }
  for (const objection of gate.objections || []) box.append(objectionNode(objection));
  const actions = el("div", "gate-actions");
  for (const action of gate.options || []) {
    const button = el("button", action === "approve" || action === "resolve" ? "primary" : action === "reject" || action === "abort" ? "quiet danger-text" : "secondary", actionNames[action] || action);
    button.type = "button"; button.disabled = actionBusy;
    button.onclick = () => action === "approve" ? submitDecision(action, gate) : openAction(action, gate);
    actions.append(button);
  }
  const error = el("p", "inline-error"); error.id = "gate-error"; error.hidden = true; error.setAttribute("role", "alert");
  box.append(actions, error); wrap.append(box);
}
function openAction(action, gate) {
  if (actionBusy || !snapshot || gateIdentity(gate) !== gateIdentity(snapshot.gate)) return;
  activeAction = { action, gate: { ...gate }, id: active, version: navigation };
  const editing = action === "edit", feedback = action === "revise" || action === "resolve";
  $("#action-kicker").textContent = action === "resolve" ? "A HUMAN DECISION" : "YOUR REVIEW";
  $("#action-title").textContent = actionNames[action] || action;
  $("#action-description").textContent = {
    edit: "Edit the current Markdown document. Your saved version becomes the approved document.",
    revise: "Describe what needs to change. The models will return to the debate with your direction.",
    resolve: "Your ruling becomes a fixed constraint the models must accept in the next round.",
    reject: "This ends the debate without approving the document. The history and draft stay available.",
    abort: "This ends the debate. The history and current draft stay available.",
  }[action] || "";
  $("#feedback-label").hidden = !feedback; $("#feedback").required = feedback;
  $("#feedback").hidden = !feedback;
  $("#feedback-label").textContent = action === "resolve" ? "Your ruling" : "What should change?";
  $("#feedback").value = "";
  $("#edit-label").hidden = !editing; $("#edited-markdown").required = editing;
  $("#edited-markdown").hidden = !editing;
  $("#edited-markdown").value = editing ? snapshot.document || "" : "";
  $("#action-error").hidden = true;
  $("#confirm-action").textContent = editing ? "Save & approve" : actionNames[action] || action;
  $("#confirm-action").classList.toggle("danger", action === "reject" || action === "abort");
  $("#action-dialog").showModal();
  if (editing) {
    const editor = $("#edited-markdown");
    editor.focus(); editor.setSelectionRange(0, 0); editor.scrollTop = 0;
  } else if (feedback) $("#feedback").focus();
  else $("#cancel-action").focus();
}
async function submitDecision(action, gate, feedback = "", editedMarkdown = "", context = null) {
  if (actionBusy) return;
  const id = context?.id || active, version = context?.version ?? navigation;
  const errorNode = $("#action-dialog").open ? $("#action-error") : $("#gate-error");
  if (id !== active || version !== navigation || gateIdentity(gate) !== gateIdentity(snapshot?.gate)) {
    if (errorNode) inlineError(errorNode, new Error("This review has changed. Close this window and review the latest document before deciding."));
    return;
  }
  actionBusy = true;
  if (errorNode) errorNode.hidden = true;
  document.querySelectorAll("#gate button, #action-form button").forEach((button) => { button.disabled = true; });
  try {
    await post(`/api/sessions/${encodeURIComponent(id)}/gate`, {
      action, actor: "web", feedback, edited_markdown: editedMarkdown,
      draft_hash: gate.draft_hash, gate_seq: gate.gate_seq,
    });
    if ($("#action-dialog").open) $("#action-dialog").close();
    activeAction = null;
    if (id === active && version === navigation) { await refresh(); connect(id, version); }
    loadSessions();
  } catch (error) {
    if (errorNode) inlineError(errorNode, error);
    if (error.status === 409 && id === active && version === navigation) {
      await refresh();
      if (!$("#action-dialog").open && $("#gate-error")) inlineError($("#gate-error"), error);
      else if (!$("#action-dialog").open) showError(error, refresh);
    }
  } finally {
    actionBusy = false;
    document.querySelectorAll("#gate button, #action-form button").forEach((button) => { button.disabled = false; });
  }
}

// -- Event stream and round timeline -------------------------------------
function roundBody(number) {
  const round = Number(number) || lastRound || 1;
  lastRound = Math.max(lastRound, round);
  if (!rounds.has(round)) {
    const group = el("section", "round-group");
    group.setAttribute("aria-label", `Round ${round}`);
    const heading = el("div", "round-heading");
    heading.append(el("span", "round-number", String(round).padStart(2, "0")), el("h3", null, `Round ${round}`));
    const body = el("div", "round-body"); group.append(heading, body);
    $("#timeline").append(group); rounds.set(round, body);
  }
  return rounds.get(round);
}
function eventCard(event, title, kind) {
  const payload = event.payload || {};
  const card = el("article", "turn " + kind);
  const header = el("div", "turn-header"), titleBox = el("div", "turn-title");
  titleBox.append(el("strong", null, payload.human ? "You" : participantName(payload.participant)), el("span", "turn-purpose", title));
  header.append(avatar(payload.participant), titleBox);
  if (event.ts) { const time = el("time", "turn-time", clockTime(event.ts)); time.dateTime = event.ts; header.append(time); }
  card.append(header); return card;
}
function renderEvent(event) {
  if (event.seq != null) { if (seen.has(event.seq)) return; seen.add(event.seq); }
  const payload = event.payload || {};
  if (event.type === "draft") {
    const card = eventCard(event, payload.human ? "Added a revised draft" : "Put a proposal on the table", "draft");
    const body = el("div", "turn-body");
    const addressed = payload.addressed || [];
    body.append(el("p", "turn-note", addressed.length ? `Addresses ${addressed.join(", ")}.` :
      payload.round === 1 ? "The first draft is ready for a second perspective." : "A revised proposal is ready for another look."));
    if (payload.markdown) {
      const details = el("details", "draft-details"); details.append(el("summary", null, "Read this draft"));
      const preview = el("div", "draft-preview markdown");
      markdownToNodes(payload.markdown, `draft-${event.seq}`).nodes.forEach((node) => preview.append(node));
      details.append(preview); body.append(details);
    }
    card.append(body); roundBody(payload.round).append(card);
    scheduleRefresh();
  } else if (event.type === "verdict") {
    const verdict = payload.verdict || {};
    const title = { AGREE: "Agrees with the direction", REVISE: "Suggests a refinement", OBJECT: "Raised concerns to resolve" }[verdict.verdict] || "Reviewed the proposal";
    const card = eventCard(event, title, "verdict " + (verdict.verdict || ""));
    const body = el("div", "turn-body");
    if (payload.coerced) body.append(el("p", "turn-note", "The debate rules require further review before agreement."));
    for (const objection of verdict.objections || []) body.append(objectionNode(objection));
    const resolved = verdict.resolved_objections || [];
    if (resolved.length) body.append(el("p", "resolved-note", `Resolved ${resolved.join(", ")}`));
    const waived = verdict.waived_objections || [];
    for (const objection of waived) body.append(el("p", "turn-note", `Waived ${objection.id}: ${objection.reason}`));
    if (!(verdict.objections || []).length && !resolved.length) body.append(el("p", "turn-note", verdict.verdict === "AGREE" ? "No new concerns were raised in this review." : "The proposal will receive another review."));
    card.append(body); roundBody(payload.round).append(card);
    scheduleRefresh();
  } else if (event.type === "state_change" && payload.to) {
    if (payload.to !== "INIT" && payload.to !== "DESIGN_DEBATE") $("#timeline").append(el("p", "state-line " + payload.to, friendlyState(payload.to)));
    scheduleRefresh();
  } else if (event.type === "artifact_revision") {
    $("#timeline").append(el("p", "state-line human-edit", "You edited the document.")); scheduleRefresh();
  } else if (event.type === "gate_decision") {
    const decisions = { approve: "You approved the document.", edit: "You saved an edited document.", revise: "You requested another revision.", resolve: "You gave the debate a direction.", reject: "You rejected the proposal.", abort: "You ended the debate." };
    $("#timeline").append(el("p", "state-line", decisions[payload.action] || "Your review was recorded."));
  } else if (event.type === "error") {
    const error = el("p", "timeline-error", payload.error || "The debate encountered an error."); error.setAttribute("role", "alert");
    $("#timeline").append(error); scheduleRefresh();
  } else if (event.type === "model_call" || event.type === "model_response" || event.type === "cost_tick" || event.type === "gate_pending") scheduleRefresh();
  // Intentionally do not scroll: reading an earlier round must be uninterrupted.
}
function connect(id, version = navigation) {
  if (id !== active || version !== navigation) return;
  disconnect();
  const source = new EventSource(`/api/sessions/${encodeURIComponent(id)}/events`);
  stream = source;
  source.onopen = () => {
    if (id !== active || version !== navigation || source !== stream) return;
    streamConnected = true; if (snapshot) renderLive(snapshot);
  };
  source.onmessage = (message) => {
    if (id !== active || version !== navigation || source !== stream) return;
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    if (event.type === "_eof") {
      source.close(); stream = null; streamConnected = false;
      refresh(); loadSessions(); return;
    }
    renderEvent(event);
  };
  source.onerror = () => {
    if (id !== active || version !== navigation || source !== stream) return;
    streamConnected = false; if (snapshot) renderLive(snapshot);
  };
}

// -- New-debate dialog ----------------------------------------------------
async function loadOptions() {
  if (optionsLoaded) return;
  if (optionsRequest) return optionsRequest;
  optionsRequest = (async () => {
    const [templates, profileData] = await Promise.all([api("/api/templates"), api("/api/profiles")]);
    const select = $("#template"), selected = select.value || select.dataset.requested;
    select.replaceChildren();
    for (const template of templates) {
      const option = el("option", null, template.label); option.value = template.name; option.dataset.description = template.when; select.append(option);
    }
    if (selected && Array.from(select.options).some((option) => option.value === selected)) select.value = selected;
    const profile = $("#profile"), selectedProfile = profile.value;
    profile.replaceChildren();
    const defaultOption = el("option", null, "Default roster"); defaultOption.value = ""; profile.append(defaultOption);
    for (const name of profileData.profiles) { const option = el("option", null, name); option.value = name; profile.append(option); }
    if (selectedProfile) profile.value = selectedProfile;
    $("#budget").placeholder = `Default: $${profileData.default_budget}`;
    optionsLoaded = true; updateTemplateDescription();
  })();
  try { await optionsRequest; } finally { optionsRequest = null; }
}
function updateTemplateDescription() {
  $("#template-description").textContent = $("#template").selectedOptions[0]?.dataset.description || "Choose the structure for the document the models will produce.";
}
async function showNewForm(prompt, template) {
  if (typeof prompt === "string") $("#idea").value = prompt;
  if (typeof template === "string") { $("#template").dataset.requested = template; if (optionsLoaded) $("#template").value = template; }
  $("#new-error").hidden = true;
  if (!$("#new-dialog").open) $("#new-dialog").showModal();
  setSidebar(false);
  $("#start-debate").disabled = true;
  try {
    await loadOptions(); updateTemplateDescription();
    $("#start-debate").disabled = newBusy;
  } catch (error) { inlineError($("#new-error"), new Error(`${error.message} Close and reopen this form to try again.`)); }
  $("#idea").focus();
}
async function createSession(event) {
  event.preventDefault();
  if (newBusy || !optionsLoaded || !$("#new-form").reportValidity()) return;
  const idea = $("#idea").value.trim();
  if (!idea) { inlineError($("#new-error"), new Error("Add an idea or brief to start the debate.")); return; }
  const budgetValue = $("#budget").value.trim();
  const payload = { idea, template: $("#template").value, context: $("#context").value,
    profile: $("#profile").value, budget: budgetValue ? Number(budgetValue) : null };
  newBusy = true; $("#new-error").hidden = true;
  document.querySelectorAll("#new-form button").forEach((button) => { button.disabled = true; });
  $("#start-debate").textContent = "Starting debate…";
  try {
    const { id } = await post("/api/sessions", payload);
    $("#new-dialog").close();
    $("#idea").value = ""; $("#context").value = ""; $("#budget").value = "";
    await openSession(id); loadSessions();
  } catch (error) { inlineError($("#new-error"), error); }
  finally {
    newBusy = false;
    document.querySelectorAll("#new-form button").forEach((button) => { button.disabled = false; });
    $("#start-debate").textContent = "Start debate";
  }
}

// -- Controls -------------------------------------------------------------
$("#home-btn").onclick = home;
$("#new-btn").onclick = () => showNewForm();
$("#welcome-new").onclick = () => showNewForm();
$("#welcome-docs").onclick = () => openDoc("readme");
document.querySelectorAll(".starter").forEach((button) => { button.onclick = () => showNewForm(button.dataset.prompt, button.dataset.template); });
$("#session-search").oninput = renderSessionList;
$("#sidebar-toggle").onclick = () => setSidebar(!document.body.classList.contains("sidebar-open"), true);
$("#sidebar-scrim").onclick = () => setSidebar(false, true);
$("#retry-btn").onclick = () => { const retry = retryGlobal; clearError(); if (retry) retry(); };
$("#export-btn").onclick = exportDocument;
$("#template").onchange = updateTemplateDescription;
$("#new-form").onsubmit = createSession;
for (const id of ["#close-new", "#cancel-new"]) $(id).onclick = () => { if (!newBusy) $("#new-dialog").close(); };
for (const id of ["#close-action", "#cancel-action"]) $(id).onclick = () => { if (!actionBusy) $("#action-dialog").close(); };
$("#new-dialog").addEventListener("cancel", (event) => { if (newBusy) event.preventDefault(); });
$("#action-dialog").addEventListener("cancel", (event) => { if (actionBusy) event.preventDefault(); });
$("#action-form").onsubmit = (event) => {
  event.preventDefault();
  if (!activeAction || !$("#action-form").reportValidity()) return;
  const feedback = $("#feedback").value.trim(), markdown = $("#edited-markdown").value;
  if (["revise", "resolve"].includes(activeAction.action) && !feedback) { inlineError($("#action-error"), new Error("Add your direction before continuing.")); return; }
  if (activeAction.action === "edit" && !markdown.trim()) { inlineError($("#action-error"), new Error("The edited document cannot be empty.")); return; }
  submitDecision(activeAction.action, activeAction.gate, feedback, markdown, activeAction);
};
document.querySelectorAll(".tab").forEach((button) => {
  button.onclick = () => switchTab(button.dataset.tab);
  button.onkeydown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const name = event.key === "Home" ? "debate" : event.key === "End" ? "document" : button.dataset.tab === "debate" ? "document" : "debate";
    switchTab(name); document.querySelector(`.tab[data-tab="${name}"]`).focus();
  };
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) setSidebar(false, true);
});
sidebarMedia.addEventListener("change", () => setSidebar(false));
setSidebar(false);
if ($("#demo-badge")) $("#demo-badge").hidden = new URLSearchParams(window.location.search).get("demo") !== "1";
loadSessions(); loadDocsList();
setInterval(() => {
  if (document.hidden) return;
  loadSessions();
}, 5000);
