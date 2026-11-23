const assert = require('node:assert/strict');
const test = require('node:test');
class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.classList = { add() {} };
    this.isConnected = true;
    this.value = '';
  }
  append(...nodes) {
    nodes.forEach(node => { if (typeof node !== 'string') node.parent = this; });
    this.children.push(...nodes);
  }
  prepend(node) { node.parent = this; this.children.unshift(node); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  replaceWith(node) {
    this.parent.children.splice(this.parent.children.indexOf(this), 1, node);
    node.parent = this.parent;
  }
  remove() { this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  addEventListener(event, callback) { this[event] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { document.activeElement = this; }
  scrollIntoView() {}
  get childElementCount() { return this.children.filter(node => typeof node !== 'string').length; }
}
global.window = {};
global.document = { createElement: tag => new Element(tag) };
global.location = { pathname: '/' };
const ticket = 'wb-exact';
const identity = { kind: 'article', id: 'xyz', revision: 'a'.repeat(64) };
let topicReads = 0;
const movedCanvas = 'obsidian://open?path=%2Ffiled%2Fnew.canvas';
const staleCanvas = 'obsidian://open?path=%2Funfiled%2Fold.canvas';
const item = { ...identity, title: '<em>材料</em>', domain: '产业', topics: [{title: '固定话题', index: 1}], links: {
  canvas:'obsidian://open?path=%2Ffake%2Fgood.canvas',
  original:'javascript:alert(1)',
  reading:'obsidian://open?path=%2Fsafe&other=unsafe',
} };
let attached = false;
let exportState = null;
let conflict = true;
global.fetch = async (path, options) => {
  const { pathname, searchParams } = new URL(path, 'http://localhost');
  const action = pathname.split('/').at(-1);
  let result = {};
  let status = 200;
  if (action === 'session') result.csrf = 'test-csrf';
  if (action === 'library') {
    assert.equal(searchParams.get('kind'), 'article');
    assert.equal(searchParams.get('offset'), '0');
    result = { items: [item], next: null };
  }
  if (action === 'item') {
    assert.deepEqual(Object.fromEntries(searchParams), identity);
    result.item = attached ? { ...item, links: { canvas: movedCanvas } } : item;
  }
  if (action === 'topic') {
    topicReads++;
    assert.deepEqual(Object.fromEntries(searchParams), { ...identity, index: '1' });
    result.topic = {
      title: '固定话题', index: 1, revision: 'b'.repeat(64), text: '<b>预览正文</b>',
      link: 'obsidian://open?path=%2Ffiled%2Ftopic.md',
      citations: [{number: 1, text: '固定引文', link: 'javascript:alert(1)'}],
      totalCitations: 2,
    };
  }
  if (action === 'ticket') {
    assert.equal(searchParams.get('ticket'), ticket);
    result = {
      ticket: { id: ticket, name: '指定工单' }, items: attached ? [{ ...item, obsidian: staleCanvas }] : [],
      exports: exportState ? [{
        request: 'req-1',
        state: exportState,
        ...(exportState === 'ready' ? { obsidian: 'obsidian://open?path=%2Ffile' } : {}),
      }] : [],
    };
  }
  if (action === 'attach') {
    assert.equal(options.headers['X-Odyssey-CSRF'], 'test-csrf');
    assert.deepEqual(JSON.parse(options.body), {ticket, ...identity});
    if (conflict) { status = 409; conflict = false; }
    else attached = true;
  }
  if (action === 'export') {
    assert.equal(options.headers['X-Odyssey-CSRF'], 'test-csrf');
    assert.deepEqual(JSON.parse(options.body), {ticket, request:'req-1'});
    exportState = 'ready';
  }
  return { ok: status === 200, status, json: async () => result };
};
require('./afr_handoff.js');
const walk = function* (node) {
  yield node;
  for (const child of node.children) if (typeof child !== 'string') yield* walk(child);
};
const select = (root, predicate) => [...walk(root)].find(predicate);
const label = (root, text) => select(root, node => node.textContent === text);
const settle = () => new Promise(resolve => setImmediate(resolve));

test('AFR reference and report controls preserve the exact ticket, selected revision and round', async () => {
  const library = new Element('main');
  const panel = new Element('main');
  await window.afrHandoff.mountLibrary(library, { ticket });
  assert(label(library, '<em>材料</em>'));
  let row = select(library, node => node.className?.includes('afr-row'));
  await row.click();
  await settle();
  assert(label(library, '指定工单'));
  assert(label(library, '打开白板'));
  assert(!label(library, '打开原文'));
  assert(!label(library, '打开完整阅读'));
  assert.equal(topicReads, 0);
  assert(select(library, node => node.title === '完整版本指纹 ' + identity.revision));
  const topic = label(library, '固定话题');
  assert.equal(topic.attributes['aria-expanded'], 'false');
  await topic.click();
  await settle();
  assert.equal(topicReads, 1);
  assert.equal(topic.attributes['aria-expanded'], 'true');
  assert(label(library, '<b>预览正文</b>'));
  assert(label(library, '固定引文'));
  assert(label(library, '已显示 1 条引文，共 2 条。'));
  assert.equal(label(library, '在 Obsidian 打开完整话题').href, 'obsidian://open?path=%2Ffiled%2Ftopic.md');
  assert.equal(label(library, '引文 1').tag, 'strong');
  await topic.click();
  await topic.click();
  assert.equal(topicReads, 1);
  await label(library, '引用到此工单').click();
  assert(!attached);
  await label(library, '刷新成果列表').click();
  row = select(library, node => node.className?.includes('afr-row'));
  await row.click();
  await settle();
  await label(library, '引用到此工单').click();
  assert(attached);
  await window.afrHandoff.mountTicket(panel, { ticket, request:'req-1', round:2 });
  assert(label(panel, '浏览 AFR 成果').href.includes('afrTicket=' + ticket));
  assert(!select(panel, node => node.href === staleCanvas));
  await label(panel, '查看固定版本').click();
  assert.equal(label(panel, '打开白板').href, movedCanvas);
  assert(!select(panel, node => node.href === staleCanvas));
  await label(panel, '保存本轮报告到 Obsidian').click();
  assert.equal(exportState, 'ready');
  assert(label(panel, '第 2 轮报告已保存到 Obsidian。'));
  assert(label(panel, '打开已保存报告'));
  for (const [state, message] of [
    ['changed', '第 2 轮报告在 Obsidian 中发生变化，请在「Autoloop 工单交接」中核对文件。'],
    ['missing', '第 2 轮报告文件在「Autoloop 工单交接」中缺失，请在 Obsidian 中核对。'],
  ]) {
    exportState = state;
    const freshPanel = new Element('main');
    await window.afrHandoff.mountTicket(freshPanel, { ticket, request: 'req-1', round: 2 });
    assert(label(freshPanel, message));
    assert(label(freshPanel, '重新核对文件状态'));
    assert(!label(freshPanel, '打开已保存报告'));
    assert(!label(freshPanel, '保存本轮报告到 Obsidian'));
    assert.equal(exportState, state);
  }
  await window.afrHandoff.mountTicket(panel, { ticket, round:3 });
  assert(!label(panel, '保存本轮报告到 Obsidian'));
});
