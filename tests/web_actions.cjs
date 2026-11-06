// Exercise the shipped click handlers without launching a browser process.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');
const root = path.join(__dirname, '..');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    dataset: {}, value: '', checked: false, hidden: false, disabled: false,
    querySelectorAll: () => [], addEventListener(name, handler) { this[name] = handler; }
  });
  return elements.get(id);
}
const actions = ['learn', 'exam', 'links', 'login'].map(kind => {
  const el = element(kind); el.dataset.kind = kind; return el;
});
const requests = [];
let release;
let delay = false;
const ctx = vm.createContext({
  document: {getElementById: element, querySelectorAll: () => actions},
  sessionStorage: {getItem: () => '', setItem: () => {}},
  location: {hash: '', pathname: '/', origin: 'http://127.0.0.1'},
  history: {replaceState: () => {}}, URLSearchParams,
  crypto: {randomUUID}, setTimeout,
  fetch: async (url, options) => {
    if (url === '/api/state') return new Promise(() => {});
    requests.push({url, ...JSON.parse(options.body)});
    if (delay) await new Promise(resolve => { release = resolve; });
    return {ok: true, json: async () => ({})};
  }
});
vm.runInContext(fs.readFileSync(path.join(root, 'core/web/static/app.js'), 'utf8'), ctx);
function state(recommended, overrides = {}, controller = true) {
  ctx.snapshot = {dashboard: {recommended}, control: {}, ...overrides};
  ctx.controller = controller;
  vm.runInContext('state = snapshot; canControl = controller; availability();', ctx);
}
(async () => {
  assert.match(fs.readFileSync(path.join(root, 'core/web/static/index.html'), 'utf8'), /<button\b[^>]*id="recommended"/);
  for (const [label, kind] of [['仅挂课', 'learn'], ['AI 自动考试', 'exam'],
      ['手动选择课程 / 录入课程或考试链接', 'links'], ['切换账号 / 更新登录凭证', 'login']]) {
    state(label);
    element('workbench-input').value = '  https://example.invalid/course\nhttps://example.invalid/subject  ';
    element('auto-submit').checked = true;
    assert.equal(element('recommended').disabled, false);
    await element('recommended').onclick();
    assert.equal(requests.at(-1).kind, kind);
    if (kind === 'exam') assert.equal(requests.at(-1).auto_submit, true);
    if (kind === 'links') assert.equal(requests.at(-1).input_text, element('workbench-input').value.trim());
    assert.ok(element('workbench-input').value.startsWith('  '));
  }
  state('AI 自动考试');
  element('auto-submit').checked = false;
  await element('recommended').onclick();
  assert.equal(requests.at(-1).auto_submit, false);
  const before = requests.length;
  for (const [label, overrides, controller] of [
    ['unknown', {}, true], ['仅挂课', {}, false],
    ['仅挂课', {operation_id: 'running'}, true], ['仅挂课', {control: {exit_requested: true}}, true]
  ]) {
    state(label, overrides, controller);
    assert.equal(element('recommended').disabled, true);
    await element('recommended').onclick();
    assert.equal(requests.length, before);
  }
  state('仅挂课'); delay = true;
  const pending = element('recommended').onclick();
  assert.equal(element('recommended').disabled, true);
  await element('recommended').onclick();
  assert.equal(requests.length, before + 1);
  release(); await pending;
  console.log('Recommendation actions: mapping, options, drafts, authority and duplicate-click guards passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
