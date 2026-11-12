/* 前端只做四件事：发问题、接 SSE、渲染 Markdown/表格/图表、展示来源。
   不引框架，是因为这个项目要讲清"为什么 EventSource 用不了"——
   EventSource 只能 GET、不能带 header、不能发 body，而我们是 POST + JSON。 */

const state = {
  sessionId: null,
  busy: false,
  charts: [],
};

const $ = (sel) => document.querySelector(sel);
const messagesEl = $('#messages');

/* ---------------- Markdown ---------------- */
function renderMarkdown(text, target) {
  if (window.marked && window.DOMPurify) {
    const raw = marked.parse(text, { breaks: true, gfm: true });
    // 模型输出属于不可信内容，必须消毒后再进 innerHTML
    target.innerHTML = DOMPurify.sanitize(raw);
  } else {
    target.textContent = text;
  }
}

/* ---------------- 文档列表 ---------------- */
const STATUS_TEXT = {
  pending: '排队中', parsing: '解析中', indexing: '向量化', ready: '就绪', failed: '失败',
};

async function loadDocs() {
  try {
    const [docs, stats] = await Promise.all([
      fetch('/api/documents').then((r) => r.json()),
      fetch('/api/documents/stats').then((r) => r.json()),
    ]);
    $('#stat-docs').textContent = stats.documents;
    $('#stat-chunks').textContent = stats.chunks;

    const ul = $('#docs');
    ul.innerHTML = '';
    if (!docs.items.length) {
      ul.innerHTML = '<li class="tip">还没有文档，先上传一份试试</li>';
      return;
    }
    for (const d of docs.items) {
      const li = document.createElement('li');
      li.className = 'doc-item';
      li.innerHTML = `
        <div class="doc-name" title="${d.title}">${d.title}</div>
        <div class="doc-meta">
          <span>${d.chunks} 块 · ${d.type.toUpperCase()}</span>
          <span class="badge ${d.status}">${STATUS_TEXT[d.status] || d.status}</span>
        </div>`;
      if (d.error) li.title = d.error;
      ul.appendChild(li);
    }
  } catch (e) {
    console.warn('加载文档失败', e);
  }
}

$('#file').addEventListener('change', async (ev) => {
  const file = ev.target.files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  const badge = ev.target.closest('.upload');
  badge.querySelector('span').textContent = '上传中…';
  try {
    const resp = await fetch('/api/documents/upload', { method: 'POST', body: form });
    if (!resp.ok) throw new Error((await resp.json()).detail || '上传失败');
    await loadDocs();
  } catch (e) {
    alert(e.message);
  } finally {
    badge.querySelector('span').textContent = '＋ 上传文档';
    ev.target.value = '';
  }
});

/* ---------------- 消息骨架 ---------------- */
function addUserMessage(text) {
  $('#empty')?.remove();
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = `<div class="avatar">你</div><div class="bubble"></div>`;
  el.querySelector('.bubble').textContent = text;
  messagesEl.appendChild(el);
  scrollToBottom();
}

function addAssistantMessage() {
  const el = document.createElement('div');
  el.className = 'msg assistant';
  el.innerHTML = `
    <div class="avatar">AI</div>
    <div class="bubble">
      <div class="steps"></div>
      <div class="content loading-dots"></div>
      <div class="sql-slot"></div>
      <div class="data-slot"></div>
      <div class="cites"></div>
      <div class="meta"></div>
    </div>`;
  messagesEl.appendChild(el);
  scrollToBottom();
  return el;
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

/* ---------------- SSE ---------------- */
async function send(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  $('#send').disabled = true;
  addUserMessage(question);

  const msgEl = addAssistantMessage();
  const contentEl = msgEl.querySelector('.content');
  const stepsEl = msgEl.querySelector('.steps');
  const sqlSlot = msgEl.querySelector('.sql-slot');
  const dataSlot = msgEl.querySelector('.data-slot');
  const citesEl = msgEl.querySelector('.cites');
  const metaEl = msgEl.querySelector('.meta');

  let buffer = '';
  let pending = false;

  const paint = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      contentEl.classList.remove('loading-dots');
      renderMarkdown(buffer, contentEl);
      pending = false;
      scrollToBottom();
    });
  };

  const handle = (event) => {
    switch (event.type) {
      case 'session':
        state.sessionId = event.session_id;
        break;
      case 'token':
        buffer += event.text;
        paint();
        break;
      case 'route': {
        const chip = document.createElement('span');
        chip.className = 'step';
        chip.textContent = { kb: '知识库检索', sql: '数据查询', both: '检索 + 查询', direct: '直接回答' }[event.route] || event.route;
        stepsEl.appendChild(chip);
        break;
      }
      case 'step':
        if (event.name === 'grade' && event.result === 'retry') {
          const chip = document.createElement('span');
          chip.className = 'step retry';
          chip.textContent = `召回不足，重新检索（${event.score}）`;
          stepsEl.appendChild(chip);
        }
        break;
      case 'sql':
        sqlSlot.innerHTML = `
          <details class="sql">
            <summary>查看生成的 SQL · 指标 ${event.metric?.name || ''}</summary>
            <pre><code></code></pre>
          </details>`;
        sqlSlot.querySelector('code').textContent = event.sql;
        break;
      case 'data':
        renderData(dataSlot, event);
        break;
      case 'sql_error':
        dataSlot.innerHTML = `<div class="meta">数据查询未命中：${event.message}</div>`;
        break;
      case 'sources':
        // 中间态先不渲染，等最终 citations（可能经过 verify 过滤）
        break;
      case 'citations':
        renderCitations(citesEl, event.items);
        break;
      case 'warn':
        metaEl.textContent = event.message;
        break;
      case 'error':
        buffer += `\n\n> ${event.message}`;
        paint();
        break;
      case 'done':
        contentEl.classList.remove('loading-dots');
        renderMarkdown(buffer || '（无内容）', contentEl);
        if (event.usage) {
          const parts = [`${(event.latency_ms / 1000).toFixed(1)}s`];
          if (event.route) parts.push(`路线 ${event.route}`);
          parts.push(`in ${event.usage.token_in} / out ${event.usage.token_out}`);
          metaEl.textContent = parts.join(' · ');
        }
        break;
      default:
        break;
    }
  };

  try {
    const resp = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, session_id: state.sessionId }),
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');

      // 按空行切帧：一帧可能有多个 data: 行，按 \n 切会切坏
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of frame.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          try {
            handle(JSON.parse(line.slice(6)));
          } catch (e) {
            console.warn('无法解析事件', line, e);
          }
        }
      }
    }
  } catch (e) {
    contentEl.classList.remove('loading-dots');
    contentEl.textContent = `请求失败：${e.message}`;
  } finally {
    state.busy = false;
    $('#send').disabled = false;
    scrollToBottom();
  }
}

/* ---------------- 数据表格 + 图表 ---------------- */
function renderData(slot, evt) {
  const { columns = [], rows = [], chart } = evt;
  if (!rows.length) {
    slot.innerHTML = '<div class="meta">查询成功，但没有符合条件的数据。</div>';
    return;
  }

  const table = document.createElement('div');
  table.className = 'table-wrap';
  const thead = `<thead><tr>${columns.map((c) => `<th>${c}</th>`).join('')}</tr></thead>`;
  const tbody = rows
    .slice(0, 20)
    .map(
      (r) =>
        `<tr>${columns
          .map((c) => {
            let v = r[c];
            if (c === '指标值' && v !== null && !Number.isNaN(Number(v))) {
              v = Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 });
            }
            return `<td>${v ?? ''}</td>`;
          })
          .join('')}</tr>`
    )
    .join('');
  table.innerHTML = `<table>${thead}<tbody>${tbody}</tbody></table>`;
  slot.appendChild(table);

  if (chart === 'bar' || chart === 'line') {
    const div = document.createElement('div');
    div.className = 'chart';
    slot.appendChild(div);
    const inst = echarts.init(div);
    const labels = rows.map((r) => r[columns[0]]);
    const values = rows.map((r) => Number(r['指标值'] ?? 0));
    inst.setOption({
      grid: { left: 10, right: 16, top: 24, bottom: 10, containLabel: true },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: labels,
        axisLabel: { interval: 0, rotate: labels.length > 8 ? 30 : 0, fontSize: 11 },
        axisLine: { lineStyle: { color: '#e4e7ec' } },
      },
      yAxis: {
        type: 'value',
        splitLine: { lineStyle: { color: '#f0f2f5' } },
        axisLabel: { fontSize: 11 },
      },
      series: [
        {
          type: chart,
          data: values,
          smooth: chart === 'line',
          barMaxWidth: 36,
          itemStyle: { color: '#2f6feb', borderRadius: chart === 'bar' ? [4, 4, 0, 0] : 0 },
          areaStyle: chart === 'line' ? { opacity: 0.08 } : undefined,
        },
      ],
    });
    state.charts.push(inst);
  }
}

/* ---------------- 引用卡片 ---------------- */
function renderCitations(el, items) {
  if (!items?.length) return;
  el.innerHTML = '';
  items.forEach((c, i) => {
    const card = document.createElement('div');
    card.className = 'cite';
    const loc = [c.page_no ? `第 ${c.page_no} 页` : null, c.section_path].filter(Boolean).join(' · ');
    card.innerHTML = `
      <div class="cite-head">[${i + 1}] ${c.doc_name || '文档'}</div>
      <div class="cite-body">${(c.snippet || '').replace(/</g, '&lt;')}</div>`;
    card.title = loc || '';
    el.appendChild(card);
  });
}

/* ---------------- 交互 ---------------- */
$('#composer').addEventListener('submit', (e) => {
  e.preventDefault();
  const input = $('#input');
  const q = input.value.trim();
  if (!q) return;
  input.value = '';
  input.style.height = 'auto';
  send(q);
});

$('#input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    $('#composer').requestSubmit();
  }
});

$('#input').addEventListener('input', (e) => {
  e.target.style.height = 'auto';
  e.target.style.height = `${Math.min(e.target.scrollHeight, 140)}px`;
});

$('#new-chat').addEventListener('click', () => {
  state.sessionId = null;
  messagesEl.innerHTML = `<div class="empty" id="empty"><h2>新会话</h2>
    <div class="chips">
      <button class="chip">退货窗口是多少天？</button>
      <button class="chip">上月销售额最高的 5 个品类</button>
    </div></div>`;
  bindChips();
});

function bindChips() {
  document.querySelectorAll('.chip').forEach((btn) => {
    btn.onclick = () => send(btn.textContent.trim());
  });
}

window.addEventListener('resize', () => state.charts.forEach((c) => c.resize()));

bindChips();
loadDocs();
setInterval(loadDocs, 4000);
