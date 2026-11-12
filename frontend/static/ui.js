export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const id = value => encodeURIComponent(value);
const paths = {
  grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  book:'M12 5v16 M3 3c4 0 6 0 9 2 3-2 5-2 9-2v16c-4 0-6 0-9 2-3-2-5-2-9-2z',
  edit:'M12 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-7 M16 3l5 5 M9 15l1-5L18 2l4 4-8 8z',
  layers:'m12 3 10 5-10 5L2 8z M2 12l10 5 10-5 M2 16l10 5 10-5',
  clock:'M12 8v5l3 2 M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z',
  search:'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  calendar:'M8 2v4 M16 2v4 M3 10h18 M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2',
  arrow:'M4 12h16 M14 6l6 6-6 6',
  refresh:'M20 7a9 9 0 0 0-15-2L2 8 M2 2v6h6 M4 17a9 9 0 0 0 15 2l3-3 M16 16h6v6',
  file:'M14 2H5v20h14V7z M14 2v6h5 M8 13h8 M8 17h5',
  check:'m5 12 4 4L19 6',
  spark:'m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3z',
  link:'M10 14a5 5 0 0 0 7 0l4-4a5 5 0 0 0-7-7l-2 2 M14 10a5 5 0 0 0-7 0l-4 4a5 5 0 0 0 7 7l2-2',
  download:'M12 3v12 M7 10l5 5 5-5 M4 16v5h16v-5',
};
export const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.file}"/></svg>`;
export const button = (label, action, style = '', attrs = '') => `<button class="button ${style}" data-action="${action}" ${attrs}>${label}</button>`;
export const empty = (title, description = '', action = '') => `<div class="empty-state"><div class="empty-icon">${icon('book')}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
export const badge = (text, style = '') => `<span class="badge ${style}">${esc(text)}</span>`;
export const date = value => value ? new Intl.DateTimeFormat('zh-TW', {timeZone:'Asia/Taipei',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(typeof value === 'number' ? value * 1000 : value)) : '未提供截止日';
export const fullDate = () => new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',year:'numeric',month:'long',day:'numeric',weekday:'long'}).format(new Date());
export const heading = (title, description, action = '', eyebrow = '') => `<div class="page-heading"><div>${eyebrow ? `<p class="eyebrow">${esc(eyebrow)}</p>` : ''}<h1>${esc(title)}</h1><p>${esc(description)}</p></div>${action}</div>`;
export const panel = (title, body, action = '') => `<section class="panel"><div class="panel-head"><h2>${title}</h2>${action}</div>${body}</section>`;
export const table = (headers, rows) => rows.length ? `<div class="table-wrap"><table><thead><tr>${headers.map(h => `<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(c => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : empty('尚無資料','同步後，資料會顯示在這裡。');
export const option = (value, label, selected) => `<option value="${esc(value)}" ${String(value) === String(selected) ? 'selected' : ''}>${esc(label)}</option>`;
export function safeURL(value) {
  if (typeof value !== 'string' || !value.trim()) return '';
  try { const url = new URL(value, 'http://127.0.0.1:8766'); return ['http:','https:'].includes(url.protocol) ? url.href : ''; } catch { return ''; }
}
export function externalLink(url, label) {
  const href = safeURL(url);
  return href ? `<a class="text-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>` : '';
}
// Render a small Markdown subset. Escape all source HTML; allow HTTP(S)
// source links but never fetch images from model-generated URLs.
export function markdown(text) {
  const decorate = raw => esc(raw).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  const inline = raw => {
    const pattern = /`([^`]+)`|(!?)\[([^\]]+)\]\(([^\s)]+)\)/g;
    let output = '', cursor = 0;
    for (const match of raw.matchAll(pattern)) {
      output += decorate(raw.slice(cursor,match.index));
      if (match[1] !== undefined) output += `<code>${esc(match[1])}</code>`;
      else {
        const href = safeURL(match[4]);
        output += !match[2] && href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(match[3])}</a>` : esc(match[0]);
      }
      cursor = match.index + match[0].length;
    }
    return output + decorate(raw.slice(cursor));
  };
  const lines = String(text || '').split('\n');
  const out = []; let code = null, list = false;
  const closeList = () => { if (list) { out.push('</ul>'); list = false; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) { closeList(); if (code !== null) { out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`); code = null; } else code = []; continue; }
    if (code !== null) { code.push(line); continue; }
    if (line.includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || '')) {
      closeList(); const cells = value => value.trim().replace(/^\||\|$/g,'').split('|').map(c => c.trim());
      const headers = cells(line); const rows = []; i += 2;
      while (i < lines.length && lines[i].includes('|')) rows.push(cells(lines[i++]));
      i--; out.push(table(headers,rows)); continue;
    }
    const h = /^(#{1,6})\s+(.+)$/.exec(line);
    if (h) { closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
    const item = /^\s*(?:[-*]|\d+\.)\s+(.+)$/.exec(line);
    if (item) { if (!list) { out.push('<ul>'); list = true; } out.push(`<li>${inline(item[1])}</li>`); continue; }
    closeList();
    if (/^>\s?/.test(line)) out.push(`<blockquote>${inline(line.replace(/^>\s?/,''))}</blockquote>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  closeList(); if (code !== null) out.push(`<pre><code>${esc(code.join('\n'))}</code></pre>`);
  return out.join('') || '<p class="muted">草稿內容會顯示在這裡。</p>';
}
export const statusLabels = {running:'正在產生',planning:'準備中',exporting:'匯出中',waiting_approval:'等待核准',waiting_review:'待審閱',completed:'已匯出',failed:'執行失敗',cancelled:'已取消',blocked_auth:'需要登入',partial:'保留部分成果'};
export function taskStatus(task) {
  const completion = task.completion || {};
  if (['waiting_review','completed'].includes(task.status) && (completion.status === 'needs_action' || completion.required_actions?.length)) return '需處理待辦';
  return statusLabels[task.status] || task.status;
}
export const errors = {
  MOODLE_LOGIN_REQUIRED:'請先連結 Moodle，或恢復先前的登入連線。', LOCAL_AUTH_REQUIRED:'本機存取已過期。請使用啟動終端機顯示的 unlock 網址重新開啟。',
  NETWORK_UNAVAILABLE:'目前無法連線，請稍後重試；已保存的內容仍保留。', AI_BUSY:'AI 正在執行其他任務，請等待完成或先停止任務。', TASK_RUNNING:'任務仍在執行，請等待完成或先停止任務。',
  ASSIGNMENT_TEXT_NOT_AVAILABLE:'此作業沒有可讀取的網頁文字，請選擇附件並讀取，或前往 Moodle 核對。', ASSET_NOT_DOWNLOADED:'請先核准下載所選附件，再讀取內容。',
  AI_AUTH_REQUIRED:'NTPU AI 需要登入，請開啟連線管理。', AI_CONNECTION_PENDING:'AI 背景連線尚未就緒，請稍後檢查或恢復連線。',
  TOOL_TIMEOUT:'工具等待逾時，已有成果仍保留；請嘗試續跑。', AI_TIMEOUT:'AI 回覆逾時，已有成果仍保留；請嘗試續跑。',
  TASK_DEADLINE_EXCEEDED:'原任務已超過時限，請重新產生草稿。', REVIEW_REQUIRED:'請先核對草稿並勾選審閱確認。',
  INCOMPLETE_ACCEPTANCE_REQUIRED:'成果仍有未完成項目。核對後，請勾選接受目前成果再匯出。',
  ACCOUNT_CHANGED_RECONNECT:'請重新驗證目前的 Moodle 連線。', SIS_LOGIN_REQUIRED:'請先以同一個學校帳號登入校務系統。',
  COMPLETION_APPROVAL_REQUIRED:'請先核准本次自動完成作業。', APPROVAL_REQUIRED:'請先核對內容並核准本次傳送。',
  EXTERNAL_APPROVAL_REQUIRED:'請先核准本次公開文獻查詢。', RESEARCH_QUERY_INVALID:'請輸入有效的公開主題或 DOI。',
  RESEARCH_UNAVAILABLE:'暫時無法取得文獻。', RESEARCH_RESPONSE_TOO_LARGE:'文獻來源回傳的資料超過大小上限。',
  DISK_FULL:'磁碟空間不足，請釋放空間後再保存。', DELETE_CONFIRMATION_REQUIRED:'資料範圍已變更，請重新預覽並確認。',
  INVALID_REQUEST:'欄位內容不符合要求，請核對長度與格式。', NETWORK_ERROR:'無法連上本機服務，請確認啟動程式仍在執行。',
};
export const errorText = error => `${errors[error.code] || error.message || '操作未完成，請稍後重試。'}${error.code ? `（${error.code}）` : ''}`;
