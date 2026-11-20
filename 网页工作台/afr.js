(() => {
'use strict';

const form = document.querySelector('#afr-search');
const status = document.querySelector('#afr-status');
const hits = document.querySelector('#afr-results');
const source = document.querySelector('#afr-source');
const title = document.querySelector('#afr-title');
const text = document.querySelector('#afr-text');
const field = form.elements.query;
let csrf, view = 0;

async function request(action, body) {
  const response = await fetch('/api/afr/' + action, {
    method: 'POST', cache: 'no-store',
    headers: {'Content-Type': 'application/json', 'X-Odyssey-CSRF': csrf},
    body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '本机查询未完成');
  return data;
}

function clearSource() {
  source.hidden = true;
  title.textContent = '';
  text.textContent = '';
}

async function read(hit) {
  const current = ++view;
  clearSource();
  status.textContent = '正在读取原文';
  try {
    const item = await request('read', {ref: hit.ref, revision: hit.revision});
    if (current !== view) return;
    title.textContent = item.heading || item.path;
    text.textContent = item.text;
    source.hidden = false;
    status.textContent = '';
  } catch (error) {
    if (current === view) status.textContent = error.message;
  }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (!csrf) return;
  const current = ++view;
  clearSource();
  hits.replaceChildren();
  status.textContent = '正在搜索';
  try {
    const result = await request('search', {query: field.value.trim()});
    if (current !== view) return;
    for (const hit of result.hits) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = hit.heading ? hit.heading + ' · ' + hit.path : hit.path;
      button.addEventListener('click', () => read(hit));
      item.append(button);
      hits.append(item);
    }
    status.textContent = result.hits.length ? '' : '没有找到匹配材料';
  } catch (error) {
    if (current === view) status.textContent = error.message;
  }
});

fetch('/api/session', {cache: 'no-store'}).then(response => response.json()).then(session => {
  if (typeof session.csrf !== 'string' || !session.csrf) throw new Error('session');
  csrf = session.csrf;
  form.querySelector('button').disabled = false;
}).catch(() => {status.textContent = '本机工作台会话不可用，请刷新。';});
})();
