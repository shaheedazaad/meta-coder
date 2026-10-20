const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

const python = process.env.PYTHON || (fs.existsSync('.venv/bin/python') ? '.venv/bin/python' : 'python');
const rendered = spawnSync(python, ['tests/render_frontend.py'], { encoding: 'utf8', env: { ...process.env, PYTHONPATH: '.' } });
assert.equal(rendered.status, 0, rendered.stderr);
const fixtures = JSON.parse(rendered.stdout);
const script = new vm.Script(fs.readFileSync('meta_coder/static/app.js', 'utf8'), { filename: path.resolve('meta_coder/static/app.js') });

function setup(t, page = 'project', configure = () => {}) {
  const dom = new JSDOM(fixtures[page].html, { url: fixtures[page].url, runScripts: 'outside-only' });
  const w = dom.window;
  const media = new Map();
  w.matchMedia = query => {
    if (!media.has(query)) media.set(query, { matches: false, listeners: [], addEventListener(_, fn) { this.listeners.push(fn); } });
    return media.get(query);
  };
  w.fetch = () => Promise.reject(new Error('unexpected fetch'));
  w.confirm = () => true;
  configure(w, media);
  script.runInContext(dom.getInternalVMContext());
  t.after(() => dom.window.close());
  return { w, d: w.document, media, dom };
}
function change(w, input, value, event = 'input') {
  input.value = value;
  input.dispatchEvent(new w.Event(event, { bubbles: true }));
}
function submit(w, form) {
  form.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
}
function files(w, input, names) {
  Object.defineProperty(input, 'files', { configurable: true, writable: true, value: names.map(name => new w.File(['text'], name)) });
}
const flush = () => new Promise(resolve => setImmediate(resolve));

for (const page of ['home', 'project', 'settings']) {
  test(`full frontend initializes on rendered ${page} page`, t => {
    const { d, w } = setup(t, page);
    assert.equal(d.documentElement.dataset.themePreference, 'system');
    assert.equal(w.projectEdits.dirty(), false);
  });
}

test('theme selection persists, follows system changes, and updates accessibility state', t => {
  const { d, media } = setup(t, 'home');
  const dark = d.querySelector('[data-theme-value="dark"]');
  dark.click();
  assert.equal(d.documentElement.classList.contains('dark'), true);
  assert.equal(d.defaultView.localStorage.getItem('meta-coder-theme'), 'dark');
  assert.equal(dark.getAttribute('aria-checked'), 'true');
  const system = media.get('(prefers-color-scheme: dark)');
  system.listeners.forEach(fn => fn());
  assert.equal(d.documentElement.classList.contains('dark'), true);
  d.querySelector('[data-theme-value="system"]').click();
  assert.equal(d.documentElement.classList.contains('dark'), false);
  system.matches = true;
  system.listeners.forEach(fn => fn());
  assert.equal(d.documentElement.classList.contains('dark'), true);
  d.querySelector('[data-theme-value="light"]').click();
  assert.equal(d.documentElement.style.colorScheme, 'light');
});

test('invalid or unavailable storage does not break theme controls', t => {
  const { d } = setup(t, 'home', w => {
    w.localStorage.setItem('meta-coder-theme', 'invalid');
    w.Storage.prototype.setItem = () => { throw new Error('disabled'); };
  });
  assert.equal(d.documentElement.dataset.themePreference, 'system');
  d.querySelector('[data-theme-value="dark"]').click();
  assert.equal(d.documentElement.classList.contains('dark'), true);
});

test('manual editor adds, edits, serializes, and removes fields and levels', async t => {
  const { w, d } = setup(t);
  const list = d.getElementById('effect-fields-list');
  const originalCount = list.children.length;
  const notes = [...list.children].find(block => block.querySelector('[data-field="name"]').value === 'notes');
  assert.equal(notes.querySelector('[data-field="name"]').disabled, true);
  assert.equal(notes.querySelector('[data-remove-row]'), null);
  d.getElementById('add-effect-field').click();
  const block = list.lastElementChild;
  assert.equal(block.open, true);
  assert.equal(d.activeElement, block.querySelector('[data-field="name"]'));
  change(w, block.querySelector('[data-field="name"]'), 'condition');
  change(w, block.querySelector('[data-field="description"]'), 'Assigned group');
  assert.equal(block.querySelector('[data-summary-name]').textContent, 'condition');
  block.querySelector('[data-add-level]').click();
  const level = block.querySelector('[data-levels-list]').firstElementChild;
  change(w, level.querySelector('[data-field="value"]'), 'control');
  change(w, level.querySelector('[data-field="description"]'), 'Baseline');
  await flush();
  assert.equal(w.projectEdits.dirty(), true);
  const form = d.getElementById('manual-form');
  form.addEventListener('submit', e => e.preventDefault());
  submit(w, form);
  let payload = JSON.parse(d.getElementById('manual-json-input').value);
  assert.deepEqual(payload.effects.at(-1).levels, [{ value: 'control', description: 'Baseline' }]);
  level.querySelector('[data-remove-row]').click();
  block.querySelector('[data-add-level]').click();
  change(w, block.querySelector('[data-field="type"]'), 'number', 'change');
  assert.equal(block.querySelector('[data-levels-section]').classList.contains('hidden'), true);
  const evidence = block.querySelector('[data-field="evidence_required"]');
  evidence.checked = false;
  evidence.dispatchEvent(new w.Event('change', { bubbles: true }));
  change(w, d.getElementById('manual-effect-definition'), 'New comparison');
  submit(w, form);
  payload = JSON.parse(d.getElementById('manual-json-input').value);
  assert.equal(payload.effect_definition, 'New comparison');
  assert.deepEqual(payload.effects.at(-1).levels, []);
  assert.equal(payload.effects.at(-1).evidence_required, false);
  block.querySelector('[data-remove-row]').click();
  assert.equal(list.children.length, originalCount);
});

test('tabs remember selection, support keyboard navigation, and respond to compact layouts', t => {
  const { w, d, media } = setup(t);
  const tabs = [...d.querySelectorAll('[data-tab-link]')];
  tabs[1].click();
  assert.equal(tabs[1].getAttribute('aria-selected'), 'true');
  tabs[1].dispatchEvent(new w.KeyboardEvent('keydown', { key: 'End', bubbles: true }));
  assert.equal(d.activeElement, tabs.at(-1));
  tabs.at(-1).dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
  assert.equal(d.activeElement, tabs[0]);
  const compact = media.get('(max-width: 48rem)');
  compact.matches = true;
  compact.listeners.forEach(fn => fn());
  assert.equal(d.querySelector('.project-tabs').getAttribute('aria-orientation'), 'horizontal');
  tabs[0].dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
  assert.equal(d.activeElement, tabs.at(-1));
  tabs.at(-1).dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
  assert.equal(d.activeElement, tabs[0]);
  assert.equal(w.sessionStorage.getItem('metaCoderActiveTab:' + d.getElementById('tab-root').dataset.projectId), tabs[0].dataset.tabLink);
});

test('provider controls update defaults, notices, and unsaved settings state', async t => {
  const { w, d } = setup(t);
  const provider = d.getElementById('run-provider');
  change(w, provider, 'openai_compatible', 'change');
  assert.equal(d.getElementById('run-model').value, '');
  assert.match(d.getElementById('run-key-status').textContent, /API key optional/);
  change(w, provider, 'openrouter', 'change');
  assert.match(d.getElementById('run-model').value, /\//);
  assert.match(d.getElementById('run-key-status').textContent, /No OpenRouter API key/);
  await flush();
  assert.equal(w.projectEdits.dirty(), true);
});

test('drafting provider remembers independently edited model IDs', t => {
  const { w, d } = setup(t, 'settings');
  const provider = d.getElementById('manual-generator-provider');
  const model = d.getElementById('manual-generator-model');
  change(w, model, 'custom-local');
  change(w, provider, 'gemini', 'change');
  assert.match(model.value, /gemini/);
  change(w, model, 'custom-gemini');
  change(w, provider, 'openai_compatible', 'change');
  assert.equal(model.value, 'custom-local');
  assert.match(model.placeholder, /served by your endpoint/);
  change(w, provider, 'gemini', 'change');
  assert.equal(model.value, 'custom-gemini');
});

test('manual drafting applies an unsaved candidate and releases busy controls', async t => {
  const { w, d } = setup(t, 'project', w => {
    w.fetch = async () => ({ ok: true, text: async () => JSON.stringify({ manual: { name: 'draft', effect_definition: 'Draft comparison', effects: [{ name: 'age', type: 'number', levels: [] }] }, filename: 'manual.md', yaml: 'name: draft' }) });
  });
  const form = d.getElementById('manual-draft-form');
  const input = form.querySelector('input[type=file]');
  files(w, input, ['manual.md']);
  submit(w, form);
  assert.equal(input.disabled, true);
  await flush();
  assert.equal(input.disabled, false);
  assert.equal(d.getElementById('manual-effect-definition').value, 'Draft comparison');
  assert.equal(d.getElementById('manual-status-badge').textContent, 'Draft · not saved');
  assert.equal(w.projectEdits.dirty(), true);
  assert.match(d.getElementById('manual-draft-status').textContent, /Draft ready/);
  assert.equal(d.getElementById('manual-yaml-preview').textContent, 'name: draft');
});

for (const response of [{ ok: false, text: async () => JSON.stringify({ error: 'Provider unavailable' }) }, { ok: false, text: async () => 'bad response' }]) {
  test('manual drafting reports failure and preserves the editor', async t => {
    const { w, d } = setup(t, 'project', w => { w.fetch = async () => response; });
    const form = d.getElementById('manual-draft-form');
    const input = form.querySelector('input[type=file]');
    files(w, input, ['manual.md']);
    const previous = d.getElementById('manual-effect-definition').value;
    submit(w, form);
    await flush();
    assert.equal(d.getElementById('manual-effect-definition').value, previous);
    assert.equal(d.getElementById('manual-draft-status').getAttribute('data-variant'), 'destructive');
    assert.equal(input.disabled, false);
    assert.equal(w.projectEdits.dirty(), false);
  });
}

function fakeTimers(w) {
  const pending = new Map();
  let id = 0;
  w.setTimeout = (fn, delay) => { pending.set(++id, { fn, delay }); return id; };
  w.clearTimeout = id => pending.delete(id);
  return {
    pending,
    run() { const [key, timer] = pending.entries().next().value; pending.delete(key); timer.fn(); return timer.delay; },
  };
}

test('dropzones preview files, submit changes and drops, and respect disabled inputs', t => {
  const { w, d } = setup(t);
  const zone = d.querySelector('[data-dropzone]');
  const input = zone.querySelector('input[type=file]');
  const preview = zone.querySelector('[data-dropzone-filename]');
  let submissions = 0;
  zone.closest('form').requestSubmit = () => submissions++;
  files(w, input, ['a.pdf']);
  input.dispatchEvent(new w.Event('change'));
  assert.equal(preview.textContent, 'a.pdf');
  assert.equal(submissions, 1);
  files(w, input, ['a.pdf', 'b.pdf']);
  const drop = new w.Event('drop', { bubbles: true, cancelable: true });
  drop.dataTransfer = { files: input.files };
  zone.dispatchEvent(new w.Event('dragenter', { bubbles: true, cancelable: true }));
  assert.equal(zone.classList.contains('dragging'), true);
  zone.dispatchEvent(drop);
  assert.equal(zone.classList.contains('dragging'), false);
  assert.equal(preview.textContent, '2 files selected');
  assert.equal(submissions, 2);
  input.disabled = true;
  zone.dispatchEvent(drop);
  input.dispatchEvent(new w.Event('change'));
  assert.equal(submissions, 2);
  input.disabled = false;
  files(w, input, []);
  input.dispatchEvent(new w.Event('change'));
  assert.equal(preview.textContent, '');
  assert.equal(submissions, 2);
});

test('sheet draft preview paginates, renders text safely, and supports discard and save', async t => {
  const rows = Array.from({ length: 11 }, (_, i) => ({ row_id: 'r' + i, source_pdf: '', locator: '', authors: '<img src=x>', year: '2024', title: '', doi: '' }));
  let resolve;
  const { w, d } = setup(t, 'project', w => {
    w.fetch = () => new Promise(done => { resolve = done; });
  });
  const form = d.getElementById('sheet-draft-form');
  const save = d.getElementById('sheet-draft-save-form');
  submit(w, form);
  assert.equal(form.getAttribute('aria-busy'), 'true');
  const firstResolve = resolve;
  submit(w, form);
  submit(w, save);
  assert.equal(resolve, firstResolve);
  resolve({ ok: true, text: async () => JSON.stringify({ csv: 'row_id\nr0', rows, warnings: ['<script>unsafe</script>'], source_row_count: 11, row_count: 11 }) });
  await flush();
  assert.equal(form.getAttribute('aria-busy'), 'false');
  assert.equal(w.projectEdits.dirty(), true);
  assert.equal(d.querySelectorAll('#sheet-draft-table tbody tr').length, 10);
  assert.equal(d.querySelector('#sheet-draft-table img'), null);
  assert.equal(d.querySelector('#sheet-draft-warnings script'), null);
  d.getElementById('sheet-draft-next').click();
  assert.equal(d.querySelectorAll('#sheet-draft-table tbody tr').length, 1);
  assert.match(d.getElementById('sheet-draft-page-summary').textContent, /Page 2 of 2/);
  d.getElementById('sheet-draft-previous').click();
  assert.equal(d.querySelectorAll('#sheet-draft-table tbody tr').length, 10);
  submit(w, save);
  resolve({ ok: false, text: async () => JSON.stringify({ error: 'Missing authors' }) });
  await flush();
  assert.equal(w.projectEdits.dirty(), true);
  assert.equal(d.getElementById('sheet-draft-status').textContent, 'Missing authors');
  submit(w, save);
  resolve({ ok: true, text: async () => JSON.stringify({ redirect: '#saved' }) });
  await flush();
  assert.equal(w.projectEdits.dirty(), false);
  assert.equal(w.location.hash, '#saved');
  d.getElementById('sheet-draft-discard').click();
  assert.equal(d.getElementById('sheet-draft-csv').value, '');
  assert.equal(d.getElementById('sheet-draft-preview').classList.contains('hidden'), true);
});

test('failed sheet conversion releases controls and preserves server-disabled buttons', async t => {
  const { w, d } = setup(t, 'project', w => {
    w.fetch = async () => ({ ok: false, text: async () => 'not JSON' });
    w.document.querySelector('#sheet-draft-save-form button').disabled = true;
  });
  submit(w, d.getElementById('sheet-draft-form'));
  await flush();
  assert.equal(d.getElementById('sheet-draft-status').dataset.variant, 'destructive');
  assert.match(d.getElementById('sheet-draft-status').textContent, /request failed/);
  assert.equal(d.querySelector('#sheet-draft-save-form button').disabled, true);
  assert.equal(w.projectEdits.dirty(), false);
});

for (const kind of ['run', 'pdf-scan']) {
  test(`${kind} polling handles progress, transient errors, and terminal refresh with unsaved edits`, async t => {
    let timers;
    let current = { status: 'running', processed: 1, total: 2, pdfs: [{ source_pdf: 'paper.pdf', status: 'ok', json_repaired: true, input_tokens: 12, output_tokens: 4, error: 'Check', missing_ids: ['r2'], extra_ids: ['r3'] }, { source_pdf: 'off-page.pdf' }] };
    let fail = false;
    const { w, d } = setup(t, 'project', w => {
      timers = fakeTimers(w);
      w.document.body.insertAdjacentHTML('beforeend', `<div id="${kind}-progress" data-running="true" data-status-url="/status"><div data-progress-bar><span></span></div><p data-progress-summary></p></div><table><tr data-run-row="paper.pdf"><td data-run-status-cell></td><td data-run-input-tokens></td><td data-run-output-tokens></td><td data-run-detail></td></tr></table>`);
      w.fetch = async () => { if (fail) throw new Error('offline'); return { json: async () => current }; };
    });
    w.projectEdits.register({}, () => true);
    assert.equal(timers.run(), 1000);
    await flush();
    const root = d.getElementById(kind + '-progress');
    assert.equal(root.querySelector('[data-progress-bar] span').style.width, '50%');
    if (kind === 'run') {
      assert.match(d.querySelector('[data-run-status-cell]').textContent, /ok · repaired/);
      assert.equal(d.querySelector('[data-run-input-tokens]').textContent, '12');
      assert.equal(d.querySelector('[data-run-output-tokens]').textContent, '4');
      assert.match(d.querySelector('[data-run-detail]').textContent, /missing: r2 unexpected: r3/);
      current = { status: 'cancelling', total: 0, processed: 0, pdfs: [{ source_pdf: 'paper.pdf', status: 'pending' }] };
      assert.equal(timers.run(), 1500);
      await flush();
      assert.equal(d.querySelector('[data-run-input-tokens]').textContent, '—');
      assert.equal(d.querySelector('[data-run-detail]').textContent, '');
    }
    fail = true;
    assert.equal(timers.run(), 1500);
    await flush();
    fail = false;
    current = { status: 'complete', total: 2, processed: 2 };
    assert.equal(timers.run(), 3000);
    await flush();
    assert.equal(timers.pending.size, 0);
    assert.equal(d.getElementById('background-update').classList.contains('hidden'), false);
  });
}

test('run polling shows cells to check only for PDFs that have some', async t => {
  let timers;
  const current = { status: 'running', processed: 2, total: 3, pdfs: [{ source_pdf: 'paper.pdf', status: 'ok', cells_to_check: 2 }, { source_pdf: 'other.pdf', status: 'ok', cells_to_check: 0 }] };
  const { d } = setup(t, 'project', w => {
    timers = fakeTimers(w);
    w.document.body.insertAdjacentHTML('beforeend', `<div id="run-progress" data-running="true" data-status-url="/status"><div data-progress-bar><span></span></div><p data-progress-summary></p></div><table><tr data-run-row="paper.pdf"><td data-run-status-cell></td><td data-run-input-tokens></td><td data-run-output-tokens></td><td data-run-detail></td></tr><tr data-run-row="other.pdf"><td data-run-status-cell></td><td data-run-input-tokens></td><td data-run-output-tokens></td><td data-run-detail></td></tr></table>`);
    w.fetch = async () => ({ json: async () => current });
  });
  assert.equal(timers.run(), 1000);
  await flush();
  assert.match(d.querySelector('[data-run-row="paper.pdf"] [data-run-detail]').textContent, /^cells to check: 2$/);
  assert.equal(d.querySelector('[data-run-row="other.pdf"] [data-run-detail]').textContent, '');
});

test('live search replaces only results, handles stale responses, and permits retry', async t => {
  let timers;
  const requests = [];
  const { w, d } = setup(t, 'home', w => {
    timers = fakeTimers(w);
    w.fetch = (url, options) => new Promise(resolve => requests.push({ url, options, resolve }));
  });
  const input = d.getElementById('project-search');
  change(w, input, 'first');
  assert.equal(timers.run(), 250);
  change(w, input, 'second');
  assert.equal(requests[0].options.signal.aborted, true);
  timers.run();
  requests[0].resolve({ ok: true, text: async () => '<div id="project-search-results">STALE</div>' });
  requests[1].resolve({ ok: true, text: async () => '<div id="project-search-results" data-result-count="2"><p>New results</p></div>' });
  await flush();
  assert.equal(d.getElementById('project-search-results').textContent, 'New results');
  assert.equal(d.getElementById('project-search'), input);
  assert.equal(new URL(w.location.href).searchParams.get('q'), 'second');
  assert.equal(d.getElementById('project-search-status').textContent, '2 matching projects');
  change(w, d.getElementById('project-sort'), 'name', 'change');
  assert.equal(timers.run(), 0);
  requests[2].resolve({ ok: false });
  await flush();
  assert.match(d.getElementById('project-search-status').textContent, /Try typing again/);
  submit(w, d.querySelector('.project-search'));
  timers.run();
  requests[3].resolve({ ok: true, text: async () => '<div>missing results</div>' });
  await flush();
  assert.equal(d.getElementById('project-search-results').textContent, 'New results');
  input.dispatchEvent(new w.InputEvent('input', { bubbles: true, isComposing: true }));
  assert.equal(timers.pending.size, 0);
  input.dispatchEvent(new w.Event('compositionend'));
  assert.equal(timers.pending.size, 1);
});

test('invalid manual controls reveal the correct tab and collapsed ancestors', t => {
  const { w, d } = setup(t);
  d.querySelector('[data-tab-link="run"]').click();
  const field = d.querySelector('#effect-fields-list [data-field="name"]');
  field.value = '';
  field.closest('details').open = false;
  field.dispatchEvent(new w.Event('invalid', { cancelable: true }));
  assert.equal(field.closest('details').open, true);
  assert.equal(d.querySelector('[data-tab-link="manual"]').getAttribute('aria-selected'), 'true');
  assert.equal(d.activeElement, field);
  const second = d.querySelectorAll('#effect-fields-list [data-field="name"]')[1];
  second.value = '';
  second.dispatchEvent(new w.Event('invalid', { cancelable: true }));
  assert.equal(d.activeElement, field);
  const input = d.getElementById('sheet-draft-file');
  input.dispatchEvent(new w.Event('invalid', { cancelable: true }));
  assert.equal(d.activeElement, field);
});

test('cross-tab links and nested tab groups activate the requested panel', t => {
  const { w, d } = setup(t, 'project', w => {
    w.document.body.insertAdjacentHTML('beforeend', '<div id="identify-subtabs"><div role="tablist"><button data-subtab-link="one">One</button><button data-subtab-link="two">Two</button></div><section data-subtab-panel="one"></section><section data-subtab-panel="two"></section></div><a data-tab-jump="run"><span id="jump">Run</span></a><a id="missing-jump" data-tab-jump="missing">Missing</a>');
  });
  d.getElementById('jump').click();
  assert.equal(d.querySelector('[data-tab-link="run"]').getAttribute('aria-selected'), 'true');
  d.getElementById('missing-jump').click();
  d.querySelector('[data-subtab-link="two"]').click();
  assert.equal(d.querySelector('[data-subtab-panel="one"]').classList.contains('hidden'), true);
  d.querySelector('[data-subtab-link="two"]').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  assert.equal(d.querySelector('[data-subtab-link="one"]').getAttribute('aria-selected'), 'true');
  d.querySelector('[data-subtab-link="one"]').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(d.querySelector('[data-subtab-link="one"]').getAttribute('aria-selected'), 'true');
});

test('tabs tolerate disabled session storage and migrate the legacy metadata tab', t => {
  const { d } = setup(t, 'project', w => {
    w.document.getElementById('tab-root').dataset.forceTab = 'metadata';
    w.Storage.prototype.getItem = () => { throw new Error('storage disabled'); };
    w.Storage.prototype.setItem = () => { throw new Error('storage disabled'); };
  });
  assert.equal(d.querySelector('[data-tab-link="analysis"]').getAttribute('aria-selected'), 'true');
});

test('malformed provider metadata falls back to readable status', t => {
  const { w, d } = setup(t, 'project', w => {
    w.document.getElementById('run-key-status').dataset.providerStatus = '{';
  });
  change(w, d.getElementById('run-provider'), 'openrouter', 'change');
  assert.match(d.getElementById('run-key-status').textContent, /No openrouter API key/);
});

test('navigation protection ignores only the form currently being saved', async t => {
  const { w, d } = setup(t);
  change(w, d.getElementById('manual-effect-definition'), 'Changed');
  await flush();
  const blocked = new w.Event('beforeunload', { cancelable: true });
  w.dispatchEvent(blocked);
  assert.equal(blocked.defaultPrevented, true);
  submit(w, d.getElementById('manual-form'));
  await flush();
  const allowed = new w.Event('beforeunload', { cancelable: true });
  w.dispatchEvent(allowed);
  assert.equal(allowed.defaultPrevented, false);
  change(w, d.getElementById('run-model'), 'changed model');
  await flush();
  const protectedOtherForm = new w.Event('beforeunload', { cancelable: true });
  w.dispatchEvent(protectedOtherForm);
  assert.equal(protectedOtherForm.defaultPrevented, true);
  assert.equal(w.projectEdits.dirty(), true);
});

test('a malformed successful manual response preserves existing edits', async t => {
  const { w, d } = setup(t, 'project', w => {
    w.fetch = async () => ({ ok: true, text: async () => '{}' });
  });
  change(w, d.getElementById('manual-effect-definition'), 'Unsaved comparison');
  const form = d.getElementById('manual-draft-form');
  files(w, form.querySelector('input[type=file]'), ['manual.md']);
  submit(w, form);
  await flush();
  assert.equal(d.getElementById('manual-effect-definition').value, 'Unsaved comparison');
  assert.equal(d.getElementById('manual-draft-status').getAttribute('data-variant'), 'destructive');
});

test('a malformed successful sheet response preserves the previous draft', async t => {
  let response = { csv: 'row_id,source_pdf,locator\nr1,paper.pdf,trial', rows: [{ row_id: 'r1', source_pdf: 'paper.pdf', locator: 'trial', authors: 'Smith', year: '2020', title: '', doi: '' }], warnings: [], source_row_count: 1, row_count: 1 };
  const { w, d } = setup(t, 'project', w => {
    w.fetch = async () => ({ ok: true, text: async () => JSON.stringify(response) });
  });
  const form = d.getElementById('sheet-draft-form');
  submit(w, form);
  await flush();
  const previousCsv = d.getElementById('sheet-draft-csv').value;
  const previousSummary = d.getElementById('sheet-draft-summary').textContent;
  response = {};
  submit(w, form);
  await flush();
  assert.equal(d.getElementById('sheet-draft-csv').value, previousCsv);
  assert.equal(d.getElementById('sheet-draft-summary').textContent, previousSummary);
  assert.equal(d.querySelector('#sheet-draft-table tbody tr td').textContent, 'r1');
  assert.equal(d.getElementById('sheet-draft-status').dataset.variant, 'destructive');
  assert.match(d.getElementById('sheet-draft-status').textContent, /previous draft was preserved/);
});

test('clean background refresh requests navigation while dirty edits restore run-button state', async t => {
  const { w, d, dom } = setup(t);
  const messages = [];
  dom.virtualConsole.removeAllListeners('jsdomError');
  dom.virtualConsole.on('jsdomError', error => messages.push(error.message));
  w.projectEdits.refresh();
  assert.equal(messages.length, 1);
  assert.match(messages[0], /navigation/); // JSDOM reports attempted full-page navigation.
  const button = d.createElement('button');
  button.dataset.requiresSavedManual = '';
  d.body.appendChild(button);
  let dirty = false;
  w.projectEdits.register({}, () => dirty);
  assert.equal(button.disabled, false);
  dirty = true;
  w.projectEdits.update();
  assert.equal(button.disabled, true);
  dirty = false;
  w.projectEdits.update();
  assert.equal(button.disabled, false);
});

test('optional upload and editor controls can be absent without breaking the page', t => {
  const { w, d } = setup(t, 'project', w => {
    const d = w.document;
    d.getElementById('manual-editor-data').textContent = JSON.stringify({ effects: [{ name: 'sparse' }] });
    d.getElementById('manual-description').remove();
    const template = d.getElementById('tmpl-effect-field').content;
    template.querySelector('[data-field="description"]').remove();
    template.querySelector('[data-field="evidence_required"]').remove();
    d.querySelector('[data-dropzone-filename]').remove();
    d.body.insertAdjacentHTML('beforeend', '<label data-dropzone>No input</label>');
  });
  const block = d.querySelector('#effect-fields-list details');
  assert.equal(block.querySelector('[data-field="type"]').value, 'string');
  change(w, block.querySelector('[data-field="type"]'), '', 'change');
  assert.equal(block.querySelector('[data-summary-type]').textContent, 'string');
  assert.equal(w.projectEdits.dirty(), true);
});

test('ticking several categories is saved, and switching to a number resets it', t => {
  const { w, d } = setup(t, 'project', w => {
    w.document.getElementById('manual-editor-data').textContent = JSON.stringify({
      effects: [{ name: 'outcome', type: 'string', levels: [{ value: 'accuracy', description: '' }, { value: 'speed', description: '' }] }],
    });
  });
  const block = [...d.querySelectorAll('#effect-fields-list details')]
    .find(el => el.querySelector('[data-field="name"]').value === 'outcome');
  const box = block.querySelector('[data-field="multiple"]');
  const form = d.getElementById('manual-form');
  form.addEventListener('submit', e => e.preventDefault());
  assert.equal(box.checked, false);
  box.checked = true;
  box.dispatchEvent(new w.Event('change', { bubbles: true }));
  submit(w, form);
  let field = JSON.parse(d.getElementById('manual-json-input').value).effects.find(f => f.name === 'outcome');
  assert.equal(field.multiple, true);
  change(w, block.querySelector('[data-field="type"]'), 'number', 'change');
  assert.equal(box.checked, false);
  submit(w, form);
  field = JSON.parse(d.getElementById('manual-json-input').value).effects.find(f => f.name === 'outcome');
  assert.equal(field.multiple, false);
});

test('empty editor payload still permits adding a coding field', t => {
  const { d } = setup(t, 'project', w => {
    w.document.getElementById('manual-editor-data').textContent = '{}';
  });
  assert.equal(d.getElementById('effect-fields-list').children.length, 0);
  d.getElementById('add-effect-field').click();
  assert.equal(d.getElementById('effect-fields-list').children.length, 1);
});

test('draft submit ignores missing files, duplicate submits, and disabled controls', async t => {
  let calls = 0;
  let resolve;
  const { w, d } = setup(t, 'project', w => {
    w.fetch = () => { calls++; return new Promise(done => { resolve = done; }); };
    w.document.getElementById('manual-draft-status').remove();
    w.document.body.insertAdjacentHTML('beforeend', '<div id="manual-incomplete-warning">Incomplete</div>');
  });
  const form = d.getElementById('manual-draft-form');
  const input = form.querySelector('input[type=file]');
  submit(w, form);
  assert.equal(calls, 0);
  files(w, input, ['manual.md']);
  input.disabled = true;
  submit(w, form);
  assert.equal(calls, 0);
  input.disabled = false;
  submit(w, form);
  submit(w, form);
  assert.equal(calls, 1);
  resolve({ ok: true, text: async () => JSON.stringify({ manual: { effects: [] }, yaml: '' }) });
  await flush();
  assert.equal(d.getElementById('manual-incomplete-warning').classList.contains('hidden'), true);
  assert.equal(d.querySelector('[data-manual-draft-filename]').textContent, 'manual document');
  assert.equal(d.getElementById('manual-yaml-preview').textContent, '');
});

test('draft request failures without a message show the fallback diagnostic', async t => {
  const { w, d } = setup(t, 'project', w => { w.fetch = async () => { throw {}; }; });
  const form = d.getElementById('manual-draft-form');
  files(w, form.querySelector('input[type=file]'), ['manual.md']);
  submit(w, form);
  await flush();
  assert.equal(d.getElementById('manual-draft-status').textContent, 'The coding-manual draft failed.');
});

test('tab initialization supports absent groups, stale saved tabs, and unnamed projects', t => {
  const { w, d } = setup(t, 'project', w => {
    delete w.document.getElementById('tab-root').dataset.projectId;
    w.sessionStorage.setItem('metaCoderActiveTab:default', 'deleted-tab');
  });
  assert.equal(d.querySelector('[data-tab-link]').getAttribute('aria-selected'), 'true');
  assert.doesNotThrow(() => w.initTabGroup(null, {}));
  assert.doesNotThrow(() => w.initTabGroup(d.createElement('div'), { panelAttr: 'data-panel', linkAttr: 'data-link' }));
});

test('identify tabs initialize without the surrounding project navigation', t => {
  const { w, d } = setup(t, 'home', w => {
    w.document.body.insertAdjacentHTML('beforeend', '<div id="identify-subtabs"><div role="tablist"><button data-subtab-link="one">One</button></div><div data-subtab-panel="one">Panel</div></div>');
  });
  assert.equal(w.sessionStorage.getItem('metaCoderIdentifySubtab:default'), 'one');
  assert.equal(d.querySelector('[data-subtab-link]').getAttribute('aria-selected'), 'true');
});

for (const removeStatus of [false, true]) {
  test(`provider selection tolerates absent metadata (status removed: ${removeStatus})`, t => {
    const { w, d } = setup(t, 'project', w => {
      const status = w.document.getElementById('run-key-status');
      delete status.dataset.providerStatus;
      delete status.dataset.providerLabels;
      w.document.getElementById('run-provider-defaults').textContent = '';
      if (removeStatus) status.remove();
    });
    change(w, d.getElementById('run-provider'), 'openrouter', 'change');
    assert.equal(d.getElementById('run-model').value, '');
    if (!removeStatus) assert.match(d.getElementById('run-key-status').textContent, /No openrouter API key/);
  });
}

test('provider status distinguishes saved keys from unconfigured endpoints', t => {
  const { w, d } = setup(t, 'project', w => {
    w.document.getElementById('run-key-status').dataset.providerStatus = JSON.stringify({ gemini: 'saved', openai_compatible: 'unconfigured' });
  });
  assert.match(d.getElementById('run-key-status').textContent, /API key is saved/);
  change(w, d.getElementById('run-provider'), 'openai_compatible', 'change');
  assert.match(d.getElementById('run-key-status').textContent, /Set the OpenAI-compatible API base URL/);
});

test('manual endpoint model defaults to blank when no model has been chosen', t => {
  const { w, d } = setup(t, 'settings', w => {
    w.document.getElementById('manual-generator-provider').value = 'gemini';
    w.document.getElementById('manual-generator-model').value = 'gemini-test';
  });
  change(w, d.getElementById('manual-generator-provider'), 'openai_compatible', 'change');
  assert.equal(d.getElementById('manual-generator-model').value, '');
  assert.match(d.getElementById('manual-generator-model').placeholder, /served by your endpoint/);
});

for (const kind of ['run', 'pdf-scan']) {
  test(`${kind} progress tolerates empty totals and a bare progress bar`, async t => {
    let timers;
    const { w, d } = setup(t, 'project', w => {
      timers = fakeTimers(w);
      w.document.body.insertAdjacentHTML('beforeend', `<div id="${kind}-progress" data-running="true" data-status-url="/status"><div data-progress-bar></div><p data-progress-summary></p></div><table><tr data-run-row="p.pdf"><td data-run-status-cell></td></tr></table>`);
      w.fetch = async () => ({ json: async () => ({ status: 'running', pdfs: [{ source_pdf: 'p.pdf', status: 'waiting' }] }) });
    });
    timers.run();
    await flush();
    const bar = d.getElementById(kind + '-progress').querySelector('[data-progress-bar]');
    assert.equal(bar.style.width, '0%');
    assert.equal(bar.getAttribute('aria-valuemax'), '1');
    assert.equal(bar.getAttribute('aria-valuenow'), '0');
    if (kind === 'run') assert.equal(d.querySelector('[data-run-status-cell]').textContent, 'waiting');
    else assert.match(d.getElementById(kind + '-progress').textContent, /0 \/ 0/);
  });
}

test('a busy sheet conversion cannot discard the existing draft', async t => {
  let resolve;
  const { w, d } = setup(t, 'project', w => {
    w.fetch = () => new Promise(done => { resolve = done; });
  });
  d.getElementById('sheet-draft-csv').value = 'unsaved csv';
  submit(w, d.getElementById('sheet-draft-form'));
  d.getElementById('sheet-draft-discard').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  assert.equal(d.getElementById('sheet-draft-csv').value, 'unsaved csv');
  resolve({ ok: false, text: async () => '{"error":"offline"}' });
  await flush();
  assert.equal(d.getElementById('sheet-draft-csv').value, 'unsaved csv');
});

test('aborted live search keeps the previous results without an error announcement', async t => {
  let timers;
  const { w, d } = setup(t, 'home', w => {
    timers = fakeTimers(w);
    w.fetch = async () => { throw new w.DOMException('aborted', 'AbortError'); };
  });
  const before = d.getElementById('project-search-results').innerHTML;
  change(w, d.getElementById('project-search'), 'search');
  timers.run();
  await flush();
  assert.equal(d.getElementById('project-search-results').innerHTML, before);
  assert.equal(d.getElementById('project-search-results').getAttribute('aria-busy'), 'false');
  assert.equal(d.getElementById('project-search-status').textContent, '');
});

for (const preference of ['system', 'light', 'dark', 'invalid', null, 'storage-error']) {
  for (const systemDark of [true, false]) {
    test(`pre-paint theme agrees with app controls: ${preference}, system dark ${systemDark}`, t => {
      let initial;
      const { d } = setup(t, 'home', (w, media) => {
        w.matchMedia('(prefers-color-scheme: dark)').matches = systemDark;
        if (preference === 'storage-error') w.Storage.prototype.getItem = () => { throw new Error('disabled'); };
        else if (preference !== null) w.localStorage.setItem('meta-coder-theme', preference);
        const inline = w.document.querySelector('head script:not([src])').textContent;
        w.eval(inline);
        initial = { dark: w.document.documentElement.classList.contains('dark'), scheme: w.document.documentElement.style.colorScheme, preference: w.document.documentElement.dataset.themePreference };
      });
      assert.deepEqual({ dark: d.documentElement.classList.contains('dark'), scheme: d.documentElement.style.colorScheme, preference: d.documentElement.dataset.themePreference }, initial);
      const expected = ['light', 'dark'].includes(preference) ? preference : systemDark ? 'dark' : 'light';
      assert.equal(initial.scheme, expected);
    });
  }
}
