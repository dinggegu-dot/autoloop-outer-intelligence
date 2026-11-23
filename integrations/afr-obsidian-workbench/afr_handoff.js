(() => {
  'use strict';

  const mounts = new WeakMap();
  const kinds = { article: '研报', question: '研究问题' };

  function create(tag, className, label) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (label != null) node.textContent = label;
    return node;
  }

  function button(label, action, className = 'secondary-button') {
    const node = create('button', className, label);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }

  function status(label) {
    const node = create('p', 'wb-meta afr-status', label);
    node.setAttribute('role', 'status');
    return node;
  }

  function link(label, href, className = 'secondary-button') {
    const node = create('a', className, label);
    node.href = href;
    return node;
  }

  function obsidianLink(label, uri, className) {
    if (typeof uri !== 'string' || !uri.startsWith('obsidian://open?')) return null;
    try {
      const url = new URL(uri);
      const path = url.searchParams.get('path');
      if (url.host !== 'open' || url.pathname || url.username || url.password || url.hash ||
          [...url.searchParams.keys()].length !== 1 || !path?.startsWith('/') ||
          path.startsWith('//') || /[\x00-\x1f\x7f]/.test(path)) return null;
      return link(label, 'obsidian://open?path=' + encodeURIComponent(path), className);
    } catch {
      return null;
    }
  }

  function libraryURL(ticket) {
    const query = new URLSearchParams({ shelf: 'afr' });
    if (ticket) {
      query.set('task', ticket);
      query.set('afrTicket', ticket);
    }
    return location.pathname + '?' + query + '#library';
  }

  async function api(path, options) {
    const response = await fetch(path, { cache: 'no-store', ...options });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(typeof data.error === 'string' ? data.error : '请求未完成，请重试。');
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function endpoint(action, query, body) {
    const path = '/api/afr-handoff/' + action + (query ? '?' + new URLSearchParams(query) : '');
    if (!body) return api(path);
    const session = await api('/api/session');
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Odyssey-CSRF': session.csrf },
      body: JSON.stringify(body),
    });
  }

  function startMount(element, key) {
    if (mounts.get(element) === key) return false;
    mounts.set(element, key);
    element.classList.add('afr-handoff');
    element.replaceChildren();
    return true;
  }

  function actionsFor(item) {
    const actions = create('div', 'wb-actions afr-actions');
    const destinations = [
      ['canvas', '打开白板'], ['reading', '打开完整阅读'],
      ['question', '打开 Obsidian 当前笔记'], ['original', '打开原文'],
    ];
    destinations.forEach(([key, label]) => {
      const action = obsidianLink(label, item.links?.[key]);
      if (action) actions.append(action);
    });
    return actions;
  }

  async function fixedItem({ kind, id, revision }) {
    const { item } = await endpoint('item', { kind, id, revision });
    if (item?.kind !== kind || item.id !== id || item.revision !== revision) {
      throw new Error('无法确认此成果版本。');
    }
    return item;
  }

  function topicPreview(item, entry) {
    const row = create('li', 'afr-topic');
    const preview = create('div', 'afr-topic-preview');
    preview.hidden = true;
    let busy = false;
    let loaded = false;
    const summary = button(entry.title, () => {
      preview.hidden = !preview.hidden;
      summary.setAttribute('aria-expanded', String(!preview.hidden));
      if (!preview.hidden) load();
    }, 'afr-topic-toggle');
    summary.setAttribute('aria-expanded', 'false');
    summary.setAttribute('aria-label', entry.title);
    row.append(summary, preview);

    async function load() {
      if (busy || loaded) return;
      busy = true;
      preview.replaceChildren(status('正在读取固定话题…'));
      try {
        const { id, revision } = item;
        const { topic } = await endpoint('topic', { kind: 'article', id, revision, index: entry.index });
        if (!row.isConnected) return;
        if (topic?.index !== entry.index || !topic.revision || typeof topic.text !== 'string') {
          throw new Error('无法确认此话题版本。');
        }
        const text = create('div', 'afr-topic-text', topic.text);
        preview.replaceChildren(create('p', 'wb-meta', '话题预览'), text);
        const citations = topic.citations || [];
        if (citations.length) {
          const list = create('ul', 'afr-topic-citations');
          citations.forEach(citation => {
            const source = create('li');
            const label = '引文 ' + citation.number;
            const quote = create('blockquote', '', citation.text);
            source.append(obsidianLink(label, citation.link, 'afr-source-link') || create('strong', '', label), quote);
            list.append(source);
          });
          preview.append(list);
        }
        if (topic.totalCitations > citations.length) {
          preview.append(create('p', 'wb-meta', '已显示 ' + citations.length + ' 条引文，共 ' + topic.totalCitations + ' 条。'));
        }
        const action = obsidianLink('在 Obsidian 打开完整话题', topic.link);
        if (action) preview.append(action);
        loaded = true;
      } catch (error) {
        if (!row.isConnected) return;
        const message = error.status === 409 ? '话题版本已变化，请重新打开成果后核对。'
          : error.status === 413 ? '话题超出工作台的预览大小，请在 Obsidian 打开完整阅读。' : error.message;
        preview.replaceChildren(status(message));
        if (error.status !== 409 && error.status !== 413) {
          preview.append(button('重新读取话题', () => {
            summary.focus();
            return load();
          }));
        }
      } finally {
        busy = false;
      }
    }

    return row;
  }

  function itemMeta(tag, item, includeDomain = false) {
    const revision = String(item.revision);
    const label = '版本 ' + (revision.length > 12 ? revision.slice(0, 12) + '…' : revision);
    const node = create(tag, 'wb-meta', [kinds[item.kind], includeDomain && item.domain, label].filter(Boolean).join(' · '));
    node.title = '完整版本指纹 ' + revision;
    return node;
  }

  function detailBody(item) {
    const card = create('article', 'afr-detail-card');
    const heading = create('h3', '', item.title);
    heading.tabIndex = -1;
    card.append(heading, itemMeta('p', item, true));
    const actions = actionsFor(item);
    if (actions.childElementCount) card.append(actions);
    if (item.topics?.length) {
      const topics = create('ul', 'afr-index');
      item.topics.forEach(topic => {
        topics.append(item.kind === 'article' ? topicPreview(item, topic) : create('li', '', topic.title));
      });
      card.append(create('h4', '', '完整话题'), topics);
    }
    if (item.sources?.length) {
      const list = create('ul', 'afr-index');
      item.sources.forEach(source => {
        const row = create('li');
        row.append(obsidianLink(source.title, source.link, 'afr-source-link') || create('span', '', source.title));
        const metadata = [source.relation, source.sha ? '版本 ' + source.sha : ''].filter(Boolean);
        if (metadata.length) row.append(create('small', 'wb-meta', metadata.join(' · ')));
        list.append(row);
      });
      card.append(create('h4', '', '固定出处'), list);
    }
    return { card, heading };
  }

  function mountLibrary(element, { ticket } = {}) {
    const key = JSON.stringify(['library', ticket]);
    if (!startMount(element, key)) return;

    const switcher = create('div', 'view-switch afr-kind');
    switcher.setAttribute('role', 'group');
    switcher.setAttribute('aria-label', '成果类型');
    const kindButtons = Object.entries(kinds).map(([value, label]) => {
      const tab = button(label, () => {
        if (kind === value) return;
        kind = value;
        load();
      }, '');
      tab.dataset.kind = value;
      return tab;
    });
    switcher.append(...kindButtons);

    const form = create('form', 'afr-search');
    const search = create('input');
    search.type = 'search';
    search.placeholder = '搜索成果标题或关键词';
    search.maxLength = 200;
    const label = create('label', '', '搜索 AFR 成果');
    label.append(search);
    const submit = create('button', 'primary-button', '搜索');
    submit.type = 'submit';
    form.append(label, submit);
    form.addEventListener('submit', event => {
      event.preventDefault();
      load();
    });
    const toolbar = create('div', 'afr-toolbar');
    toolbar.append(switcher, form);

    const results = create('div', 'afr-list');
    const listMessage = status('');
    const pagination = create('div', 'afr-pagination');
    const list = create('div');
    list.append(listMessage, results, pagination);
    const detail = create('div', 'afr-detail');
    detail.hidden = true;
    const layout = create('div', 'afr-layout');
    layout.append(list, detail);
    element.append(toolbar, layout);

    let kind = 'article';
    let phrase = '';
    let listVersion = 0;
    let detailVersion = 0;
    let selectedRow = null;
    const mounted = () => element.isConnected && mounts.get(element) === key;

    function resetDetail() {
      detailVersion++;
      detail.hidden = true;
      detail.replaceChildren();
      selectedRow?.removeAttribute('aria-current');
      selectedRow = null;
    }

    async function load(offset) {
      const append = offset !== undefined;
      if (!append) {
        phrase = search.value.trim();
        results.replaceChildren();
        resetDetail();
      }
      kindButtons.forEach(node => node.setAttribute('aria-pressed', String(node.dataset.kind === kind)));
      pagination.replaceChildren();
      listMessage.textContent = '正在读取 AFR 成果…';
      const generation = ++listVersion;
      try {
        const { items, next } = await endpoint('library', { kind, q: phrase, offset: append ? offset : 0 });
        if (generation !== listVersion || !mounted()) return;
        items.forEach(item => {
          const row = button('', () => open(item, row), 'library-row afr-row');
          const glyph = create('span', 'library-glyph', item.kind === 'question' ? '?' : '▤');
          glyph.setAttribute('aria-hidden', 'true');
          const text = create('span');
          const metadata = [item.domain, Number.isInteger(item.sourceCount) ? item.sourceCount + ' 份出处' : ''].filter(Boolean);
          text.append(create('strong', '', item.title));
          if (metadata.length) text.append(create('small', '', metadata.join(' · ')));
          const chevron = create('span', '', '›');
          chevron.setAttribute('aria-hidden', 'true');
          row.append(glyph, text, chevron);
          results.append(row);
        });
        listMessage.textContent = results.childElementCount
          ? '已显示 ' + results.childElementCount + ' 条成果'
          : phrase ? '没有找到匹配的 AFR 成果。' : '暂无此类 AFR 成果。';
        if (next != null && next !== false && next !== '' && Number(next) > Number(offset ?? 0)) {
          pagination.append(button('加载更多', () => load(next)));
        }
      } catch (error) {
        if (generation !== listVersion || !mounted()) return;
        listMessage.textContent = error.message;
        pagination.append(button('重新读取', () => load(offset)));
      }
    }

    async function open(selection, row) {
      selectedRow?.removeAttribute('aria-current');
      selectedRow = row;
      row.setAttribute('aria-current', 'true');
      detail.hidden = false;
      detail.replaceChildren(status('正在读取成果详情…'));
      const generation = ++detailVersion;
      const current = () => generation === detailVersion && mounted();
      const identity = { kind: selection.kind, id: selection.id, revision: selection.revision };
      try {
        const item = await fixedItem(identity);
        if (!current()) return;
        const { card, heading } = detailBody(item);
        card.prepend(button('收起详情', () => {
          resetDetail();
          row.focus();
        }, 'afr-close-detail'));
        detail.replaceChildren(card);
        if (document.activeElement === row) {
          heading.focus({ preventScroll: true });
          detail.scrollIntoView({ block: 'nearest' });
        }
        if (!ticket) {
          card.append(status('从一张工单的「AFR 交接」进入，即可引用这份成果。'));
          return;
        }

        const attachment = create('div', 'afr-attach');
        card.append(attachment);
        async function loadDestination() {
          attachment.replaceChildren(status('正在确认接收工单…'));
          try {
            const destination = await endpoint('ticket', { ticket });
            if (!current()) return;
            if (destination.ticket?.id !== ticket || !destination.ticket.name) {
              throw new Error('无法确认接收工单。');
            }
            const target = create('p', 'wb-meta');
            target.append('接收工单 ', create('strong', '', destination.ticket.name));
            const message = status('');
            attachment.replaceChildren(target, message);
            if (destination.items?.some(entry =>
              entry.kind === identity.kind && entry.id === identity.id && entry.revision === identity.revision)) {
              message.textContent = '这个版本已引用到该工单。';
              return;
            }
            const save = button('引用到此工单', async () => {
              save.disabled = true;
              message.textContent = '正在引用…';
              try {
                await endpoint('attach', null, { ticket, ...identity });
                if (!current()) return;
                message.textContent = '已引用到「' + destination.ticket.name + '」。';
                save.remove();
              } catch (error) {
                if (!current()) return;
                if (error.status === 409) {
                  message.textContent = '成果版本或工单状态已变化，请刷新列表后重新选择。';
                  save.replaceWith(button('刷新成果列表', () => load()));
                } else {
                  message.textContent = error.message;
                  save.disabled = false;
                }
              }
            }, 'primary-button');
            attachment.append(save);
          } catch (error) {
            if (!current()) return;
            attachment.replaceChildren(status(error.message), button('重新确认工单', loadDestination));
          }
        }
        loadDestination();
      } catch (error) {
        if (!current()) return;
        const changed = error.status === 409;
        detail.replaceChildren(status(changed ? '成果版本已变化，请刷新列表后重新选择。' : error.message),
          changed ? button('刷新成果列表', () => load()) : button('重新读取', () => open(selection, row)));
      }
    }

    return load();
  }

  function mountTicket(element, { ticket, request, round } = {}) {
    if (!ticket) return;
    const key = JSON.stringify(['ticket', ticket, request, round]);
    if (!startMount(element, key)) return;
    const panel = create('section', 'afr-ticket');
    const header = create('header', 'afr-ticket-header');
    header.append(create('h4', '', 'AFR 交接'), link('浏览 AFR 成果', libraryURL(ticket)));
    const content = create('div', 'afr-ticket-content');
    panel.append(header, content);
    element.append(panel);
    const mounted = () => element.isConnected && mounts.get(element) === key;
    const roundLabel = round ? '第 ' + round + ' 轮' : '本轮';

    async function load() {
      content.replaceChildren(status('正在读取工单交接…'));
      try {
        const data = await endpoint('ticket', { ticket });
        if (!mounted()) return;
        if (data.ticket?.id !== ticket) throw new Error('无法确认当前工单。');
        content.replaceChildren();
        if (data.items?.length) {
          const list = create('ul', 'afr-attached');
          const preview = create('div', 'afr-ticket-preview');
          preview.hidden = true;
          let selection = 0;

          async function show(item, trigger) {
            preview.hidden = false;
            preview.replaceChildren(status('正在读取固定版本…'));
            const version = ++selection;
            const current = () => mounted() && preview.isConnected && selection === version;
            try {
              const { card, heading } = detailBody(await fixedItem(item));
              if (!current()) return;
              card.prepend(button('收起详情', () => {
                preview.hidden = true;
                preview.replaceChildren();
                trigger.focus();
              }, 'afr-close-detail'));
              preview.replaceChildren(card);
              if (document.activeElement === trigger) heading.focus();
            } catch (error) {
              if (!current()) return;
              const text = error.status === 409 || error.status === 404
                ? '暂时无法核验这个固定版本，请在 AFR 中核对出处后重试。' : error.message;
              preview.replaceChildren(status(text), button('重新读取固定版本', () => show(item, trigger)));
            }
          }

          data.items.forEach(item => {
            const row = create('li');
            const info = create('div');
            info.append(create('strong', '', item.title), itemMeta('small', item));
            const trigger = button('查看固定版本', () => show(item, trigger));
            row.append(info, trigger);
            list.append(row);
          });
          content.append(list, preview);
        } else {
          content.append(create('p', 'wb-meta', '此工单尚未引用 AFR 成果。'));
        }

        const exportPanel = create('div', 'afr-export');
        const exported = request && data.exports?.find(entry => entry.request === request);
        const savedStates = {
          ready: '报告已保存到 Obsidian。',
          changed: '报告在 Obsidian 中发生变化，请在「Autoloop 工单交接」中核对文件。',
          missing: '报告文件在「Autoloop 工单交接」中缺失，请在 Obsidian 中核对。',
        };
        const message = status(exported
          ? roundLabel + (savedStates[exported.state] || '报告的 Obsidian 状态待核对。')
          : request ? roundLabel + '报告可保存到 Obsidian 的「Autoloop 工单交接」文件夹。'
            : '研究报告归档后可保存到 Obsidian。');
        exportPanel.append(message);

        if (exported?.state === 'ready') {
          const action = obsidianLink('打开已保存报告', exported.obsidian);
          if (action) exportPanel.append(action);
        } else if (exported) {
          const action = obsidianLink('去 Obsidian 核对', exported.obsidian);
          if (action) exportPanel.append(action);
          exportPanel.append(button('重新核对文件状态', load));
        } else if (request) {
          const save = button('保存本轮报告到 Obsidian', async () => {
            save.disabled = true;
            message.textContent = '正在保存报告…';
            try {
              await endpoint('export', null, { ticket, request });
              if (mounted()) await load();
            } catch (error) {
              if (!mounted()) return;
              message.textContent = error.status === 409
                ? '报告状态已变化。请重新读取交接状态后再保存。'
                : error.message;
              if (error.status === 409) save.replaceWith(button('重新读取交接状态', load));
              else save.disabled = false;
            }
          }, 'primary-button');
          exportPanel.append(save);
        }
        content.append(exportPanel);
      } catch (error) {
        if (mounted()) content.replaceChildren(status(error.message), button('重新读取', load));
      }
    }

    return load();
  }

  window.afrHandoff = { mountLibrary, mountTicket };
})();
