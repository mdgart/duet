// Isolated client behavior tests. See README.md for the one-off jsdom setup.
// No browser, server, provider, or network connection is used.
const { JSDOM } = require('jsdom');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'duet/web/static/index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'duet/web/static/app.js'), 'utf8');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const markdown = '# Working draft\n\n## Scope\nKeep it small.\n\n' +
  '[Safe source](https://example.com) and [unsafe](javascript:alert(1)). ' +
  '<img src=x onerror=alert(1)>\n\n1. **Capture.** Save a title.\n2. **Review.** Read it.\n';

function harness(t) {
  const dom = new JSDOM(html, {
    url: 'http://localhost:8765/', runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const window = dom.window;
  const document = window.document;
  const media = { matches: false, listeners: [], addEventListener(_event, fn) { this.listeners.push(fn); } };
  window.matchMedia = () => media;
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  window.setInterval = () => 0;
  const streams = [];
  window.EventSource = class {
    constructor() { this.closed = false; streams.push(this); }
    close() { this.closed = true; }
    end() { this.onmessage({ data: JSON.stringify({ type: '_eof' }) }); }
  };
  const data = {
    id: 'first', label: 'Reading habit', state: 'GATE_1', running: false,
    updated_at: '2026-10-01T10:00:00Z', round: 2, template: 'software', document: markdown,
    summary: 'A useful plan.', brief: 'Build a reading habit.', cost_usd: .1, budget_usd: 5,
    participants: [{ id: 'sol', badge: 'SOL', model: 'test-model' }],
    counts: { objections_open: 0, objections_closed: 1 }, active_call: null,
    gate: { gate: 'gate_1', gate_seq: 12, draft_hash: 'hash-12',
      options: ['approve', 'edit', 'revise', 'reject'], facts: { objections_raised: 1, objections_closed: 1 } },
  };
  const second = { ...data, id: 'second', label: 'Second discussion', state: 'DONE', gate: null };
  const rows = () => [data, second].map(({ id, label, state, running, updated_at, cost_usd }) =>
    ({ id, label, state, running, updated_at, cost_usd }));
  const requests = { gates: [], sessions: [], snapshotBarrier: null };
  const response = (body, status = 200) => ({
    ok: status < 400, status, statusText: 'Test response',
    json: async () => JSON.parse(JSON.stringify(body)),
  });
  window.fetch = async (url, options = {}) => {
    if (url === '/api/sessions' && !options.method) return response(rows());
    if (url === '/api/docs') return response([{ id: 'readme', title: 'Getting started' }]);
    if (url === '/api/templates') return response([
      { name: 'software', label: 'Implementation spec', when: 'Build a tool' },
      { name: 'decision', label: 'Decision record', when: 'Make a choice' },
    ]);
    if (url === '/api/profiles') return response({ profiles: ['flagship'], default_budget: 5 });
    if (url.endsWith('/gate')) {
      requests.gates.push(JSON.parse(options.body));
      await tick();
      return response({ detail: 'Example failure; edit preserved.' }, 400);
    }
    if (url === '/api/sessions' && options.method === 'POST') {
      requests.sessions.push(JSON.parse(options.body)); return response({ id: 'second' });
    }
    if (url === '/api/sessions/first') {
      if (requests.snapshotBarrier) await requests.snapshotBarrier;
      return response(data);
    }
    if (url === '/api/sessions/second') return response(second);
    throw new Error('Unexpected request: ' + url);
  };
  // Expose a closure for tests without changing the production script API.
  window.eval(script + '\nwindow.testClient = code => eval(code);');
  t.after(() => window.close());
  return { window, document, data, requests, streams, media,
    $: selector => document.querySelector(selector), call: code => window.testClient(code) };
}

test('Markdown uses safe nodes, scoped headings, list items, and grouped rounds', async t => {
  const h = harness(t); await tick(); await h.call("openSession('first')");
  h.call("renderEvent({type:'draft',seq:2,payload:{round:1,participant:'sol',markdown:'# First draft\\n\\n## Scope\\nExample'}})");
  h.call("renderEvent({type:'verdict',seq:3,payload:{round:1,participant:'sol',verdict:{verdict:'OBJECT',objections:[{severity:'major',headline:'Preserve tags',claim:'Commas matter.',evidence:'The brief uses commas.',proposed_change:'Use a tag table.'}]}}})");
  assert.equal(h.document.querySelectorAll('.round-group').length, 1);
  assert.match(h.$('.objection-detail').textContent, /Use a tag table/);
  assert.equal(h.document.querySelectorAll('#doc img, #doc a[href^="javascript:"]').length, 0);
  assert.equal(h.document.querySelectorAll('#doc a[href="https://example.com/"]').length, 1);
  const ids = [...h.document.querySelectorAll('[id*="-sec-"]')].map(node => node.id);
  assert.equal(ids.length, new Set(ids).size);
  const ordered = h.$('#doc ol');
  assert.equal(ordered.children.length, 2);
  assert.ok([...ordered.children].every(node => node.tagName === 'LI'));
  assert.equal(ordered.querySelectorAll('li > strong').length, 2);
  for (const target of ['javascript:alert(1)', 'data:text/html,test', 'vbscript:msgbox(1)', 'java\nscript:alert(1)', '\\evil.test']) {
    assert.equal(h.call(`safeLink(${JSON.stringify(target)})`), null);
  }
  assert.equal(h.call("safeLink('./guide.md')"), 'http://localhost:8765/guide.md');
});

test('Gate dialogs preserve text, reject stale identity, and prevent duplicate submission', async t => {
  const h = harness(t); await tick(); await h.call("openSession('first')");
  h.call("openAction('revise',snapshot.gate)");
  assert.equal(h.$('#edited-markdown').hidden, true);
  h.$('#feedback').value = 'Keep all data local.';
  h.data.gate.gate_seq = 13; h.data.gate.draft_hash = 'hash-13';
  await h.call('refresh()');
  assert.equal(h.$('#feedback').value, 'Keep all data local.');
  h.$('#action-form').dispatchEvent(new h.window.Event('submit', { cancelable: true }));
  await tick();
  assert.match(h.$('#action-error').textContent, /review has changed/);
  assert.equal(h.requests.gates.length, 0);
  h.$('#action-dialog').close(); h.call("openAction('edit',snapshot.gate)");
  assert.equal(h.$('#edited-markdown').value, markdown);
  assert.equal(h.$('#edited-markdown').selectionStart, 0);
  assert.equal(h.$('#edited-markdown').scrollTop, 0);
  assert.equal(h.$('#feedback').hidden, true);
  h.$('#edited-markdown').value = '# Edited\n\nKeep my exact words.';
  h.$('#action-form').dispatchEvent(new h.window.Event('submit', { cancelable: true }));
  h.$('#action-form').dispatchEvent(new h.window.Event('submit', { cancelable: true }));
  await tick(); await tick();
  assert.equal(h.requests.gates.length, 1);
  assert.deepEqual(h.requests.gates[0], { action: 'edit', actor: 'web', feedback: '',
    edited_markdown: '# Edited\n\nKeep my exact words.', draft_hash: 'hash-13', gate_seq: 13 });
  assert.match(h.$('#action-error').textContent, /edit preserved/);
  assert.equal(h.$('#edited-markdown').value, '# Edited\n\nKeep my exact words.');
});

test('Polling refreshes externally changed suspended sessions and reconnects without replacing an open edit', async t => {
  const h = harness(t); await tick(); await h.call("openSession('first')");
  h.streams.at(-1).end(); await tick(); await tick();
  assert.equal(h.call('stream'), null);
  h.call("openAction('edit',snapshot.gate)");
  h.$('#edited-markdown').value = '# My unsaved edit';
  const originalStreams = h.streams.length;
  // Another tab completes a revision between polls: same state, different log.
  h.data.updated_at = '2026-10-01T10:05:00Z';
  h.data.document = '# The other tab revised this';
  h.data.gate.gate_seq = 20; h.data.gate.draft_hash = 'hash-20';
  await h.call('loadSessions()');
  assert.equal(h.streams.length, originalStreams + 1);
  assert.match(h.$('#doc').textContent, /other tab revised/);
  assert.equal(h.$('#action-dialog').open, true);
  assert.equal(h.$('#edited-markdown').value, '# My unsaved edit');
  assert.equal(h.call('activeAction.gate.gate_seq'), 12);
  h.$('#action-form').dispatchEvent(new h.window.Event('submit', { cancelable: true }));
  assert.equal(h.requests.gates.length, 0);
  assert.match(h.$('#action-error').textContent, /review has changed/);
  h.$('#action-dialog').close(); h.streams.at(-1).end(); await tick(); await tick();
  // A remote request for changes resumes a previously closed stream.
  h.data.state = 'DESIGN_DEBATE'; h.data.running = true; h.data.gate = null;
  h.data.updated_at = '2026-10-01T10:06:00Z';
  const beforeResume = h.streams.length;
  await h.call('loadSessions()');
  assert.equal(h.streams.length, beforeResume + 1);
  assert.equal(h.$('#live-status').hidden, false);
  assert.match(h.$('#session-head').textContent, /In debate/);
  // Approval elsewhere also updates the visible gate and document status.
  h.data.state = 'DONE'; h.data.running = false; h.data.updated_at = '2026-10-01T10:07:00Z';
  await h.call('loadSessions()');
  assert.match(h.$('#session-head').textContent, /Approved/);
  assert.equal(h.document.querySelectorAll('#gate .gate-actions').length, 0);
});

test('Budget resume accepts its default even after fractional-cent usage', async t => {
  const h = harness(t); await tick(); await h.call("openSession('first')");
  for (const [cap, spent] of [[.0025, .003], [5, 5.1234], [.01, .01]]) {
    h.call(`renderGate({...snapshot,state:'BUDGET_STOP',gate:null,budget_usd:${cap},cost_usd:${spent}})`);
    const input = h.$('.budget-form input');
    assert.ok(Number(input.min) > Math.max(cap, spent));
    assert.equal(input.validity.stepMismatch, false);
    assert.equal(input.checkValidity(), true);
    input.value = input.min;
    assert.equal(input.checkValidity(), true);
  }
});

test('Mobile navigation is inert when closed and restores keyboard focus', async t => {
  const h = harness(t); await tick();
  h.media.matches = true; h.media.listeners.forEach(fn => fn());
  assert.equal(h.$('#sidebar').inert, true);
  h.$('#sidebar-toggle').click();
  assert.equal(h.$('#sidebar').inert, false);
  assert.equal(h.document.activeElement, h.$('#session-search'));
  h.document.dispatchEvent(new h.window.KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(h.$('#sidebar').inert, true);
  assert.equal(h.document.activeElement, h.$('#sidebar-toggle'));
  h.$('#sidebar-toggle').click(); h.$('#sidebar-scrim').click();
  assert.equal(h.document.activeElement, h.$('#sidebar-toggle'));
  h.media.matches = false; h.media.listeners.forEach(fn => fn());
  assert.equal(h.$('#sidebar').inert, false);
});

test('Markdown export uses the canonical document, filename, and MIME type', async t => {
  const h = harness(t); await tick(); await h.call("openSession('first')");
  h.data.document = '# Approved human edit\n\nPreserve this exact sentence.\n';
  h.data.label = 'Reading / notes: final';
  await h.call('refresh()');
  let exportedBlob, download;
  h.window.Blob = Blob;
  h.window.URL.createObjectURL = blob => { exportedBlob = blob; return 'blob:test-export'; };
  h.window.URL.revokeObjectURL = () => {};
  h.window.HTMLAnchorElement.prototype.click = function () {
    download = { filename: this.download, href: this.href };
  };
  h.call('exportDocument()');
  assert.equal(exportedBlob.type, 'text/markdown;charset=utf-8');
  assert.equal(await exportedBlob.text(), h.data.document);
  assert.deepEqual(download, { filename: 'Reading-notes-final.md', href: 'blob:test-export' });
  assert.equal(h.document.querySelectorAll('a[download]').length, 0);
});

test('Search, starter templates, create payload, and late navigation responses', async t => {
  const h = harness(t); await tick();
  h.$('#session-search').value = 'second'; h.call('renderSessionList()');
  assert.equal(h.document.querySelectorAll('[data-session]').length, 1);
  h.$('#session-search').value = ''; h.call('home()');
  await h.call("showNewForm('Consider this idea.','decision')");
  assert.equal(h.$('#template').value, 'decision');
  h.$('#context').value = 'Offline only.'; h.$('#profile').value = 'flagship'; h.$('#budget').value = '3.50';
  await h.call('createSession({preventDefault(){}})');
  assert.deepEqual(h.requests.sessions[0], { idea: 'Consider this idea.', template: 'decision',
    context: 'Offline only.', profile: 'flagship', budget: 3.5 });
  assert.equal(h.$('#new-dialog').open, false);
  let release;
  h.requests.snapshotBarrier = new Promise(resolve => { release = resolve; });
  const delayed = h.call("openSession('first')"); await tick();
  await h.call("openSession('second')");
  h.requests.snapshotBarrier = null; release(); await delayed;
  assert.match(h.$('#session-head').textContent, /Second discussion/);
  assert.doesNotMatch(h.$('#session-head').textContent, /Reading habit/);
});
