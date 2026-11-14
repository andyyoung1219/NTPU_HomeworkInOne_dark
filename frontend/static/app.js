import {esc,id,icon,button,empty,badge,date,fullDate,heading,panel,table,option,groupCoursesBySemester,externalLink,taskStatus,errorText} from './ui.js';
import {createWorkflow} from './workflow.js';

const $ = selector => document.querySelector(selector);
const content = $('#content');
let courses = [], dashboard = null, loggedIn = false, booted = false, routeVersion = 0, mutationBusy = false;
let submissionRefreshVersion = null;
let route = 'home', routeRef = '', assignmentFilter = 'pending', hubTab = 'timetable', hubData = null, searchResults = null, deleteSnapshot = null;
let hubSemester = '', discovered = null;
let searchFields = {q:'',course_id:'',due_from:'',due_to:''};
const labels = {home:'學習總覽',courses:'我的課程',course:'我的課程',work:'作業工作台',hub:'學習資料',history:'歷史任務',search:'搜尋'};
const accountHeader = 'X-Workbench-Account';
const downloadSelector = 'a[download][href^="/api/"],a[download][href^="' + location.origin + '/api/"]';
const accountChannel = typeof window.BroadcastChannel === 'function' ? new window.BroadcastChannel('workbench-account') : null;
let accountScope = null, accountVersion = 0;

async function api(path,method = 'GET',body,headers = {}) {
  const version = accountVersion;
  let response;
  try {
    response = await fetch('/api' + path,{method,credentials:'same-origin',cache:'no-store',
      headers:{Accept:'application/json',...(body !== undefined ? {'Content-Type':'application/json'} : {}),
        ...(accountScope ? {[accountHeader]:accountScope} : {}),...headers},
      ...(body !== undefined ? {body:JSON.stringify(body)} : {})});
  } catch { const error = new Error('無法連上本機服務。'); error.code = 'NETWORK_ERROR'; throw error; }
  let result;
  try { result = await response.json(); } catch { throw new Error('伺服器回應無法讀取，請確認本機服務。'); }
  if (version !== accountVersion) throw Object.assign(new Error(),{code:'ACCOUNT_CHANGED_RECONNECT'});
  const scope = response.headers?.get(accountHeader);
  if (scope && scope !== accountScope) {
    if (accountScope) {
      clearAccountView(scope);
      accountChannel?.postMessage(scope);
    } else {
      accountScope = scope;
      syncDownloadScopes();
    }
  }
  if (!response.ok || result.error) {
    const code = typeof result.error === 'string' ? result.error : result.error?.code || `HTTP_${response.status}`;
    if (code === 'ACCOUNT_CHANGED_RECONNECT') clearAccountView();
    const error = new Error('操作未完成，已保存的資料仍保留。'); error.code = code; throw error;
  }
  return result.data;
}
function clearAccountView(scope = null) {
  accountScope = scope;
  accountVersion++; routeVersion++;
  workflow.reset(); courses = []; dashboard = null; hubData = null; searchResults = null;
  searchFields = {q:'',course_id:'',due_from:'',due_to:''}; hubSemester = ''; discovered = null; deleteSnapshot = null; loggedIn = false;
  $('#delete-preview').hidden = true;
  $('#delete-preview').innerHTML = '';
  $('#connection-states').innerHTML = '';
  $('#connection-message').hidden = true;
  $('#connection-summary').textContent = '管理學校帳號與 AI';
  syncDownloadScopes();
  content.innerHTML = viewReadFailure({code:'ACCOUNT_CHANGED_RECONNECT'});
  content.setAttribute('aria-busy','false');
}
function scopeDownload(link) {
  const url = new URL(link.href);
  if (accountScope) url.searchParams.set('account_scope',accountScope);
  else url.searchParams.delete('account_scope');
  link.href = url.href;
}
function syncDownloadScopes() {
  for (const link of document.querySelectorAll(downloadSelector)) {
    scopeDownload(link);
  }
}
function notify(message,error = false) {
  const node = document.createElement('div'); node.className = 'toast' + (error ? ' error' : ''); node.textContent = message;
  $('#notifications').replaceChildren(node); setTimeout(() => node.remove(),error ? 12000 : 6000);
}
function confirmAction(title,description,accept = '確認') {
  return new Promise(resolve => {
    const dialog = $('#confirm-dialog'); $('#confirm-title').textContent = title; $('#confirm-description').textContent = description; $('#confirm-accept').textContent = accept;
    dialog.returnValue = ''; $('#confirm-accept').onclick = () => dialog.close('accept');
    dialog.addEventListener('close',() => resolve(dialog.returnValue === 'accept'),{once:true}); dialog.showModal();
  });
}
const workflow = createWorkflow({api,notify,confirm:confirmAction,courses:() => courses,repaintWork:focus => {
  if (route !== 'work') return;
  content.innerHTML = workflow.html();
  syncDownloadScopes();
  if (focus) content.focus();
}});
function navigate(page,ref = '') { const hash = '#' + page + (ref ? '/' + id(ref) : ''); if (location.hash === hash) void render(); else location.hash = hash; }
function loginEmpty() { return empty('連結帳號，開始你的學習空間','登入 Moodle 後同步課程與作業。你已保存的資料會留在本機。',button('連結學校帳號','connections','primary')); }
function submissionBadge(assignment) {
  const state = assignment.submission_label || (assignment.submission_status === 'submitted' ? '已提交' : '待確認');
  const tone = state === '逾期未提交' ? 'red' : state === '已提交' ? '' : state === '更新中' ? 'gray' : 'amber';
  return badge(state,tone);
}
function assignmentRows(items) {
  return items.map(a => `<button class="assignment-row" data-action="open-assignment" data-id="${esc(a.id)}"><span class="row-icon">${icon('file')}</span><span class="row-copy"><strong>${esc(a.title)}</strong><small>${esc(a.course_name || courses.find(c => c.id === a.course_id)?.name || '課程作業')}${a.stale ? ' · 快取／待更新' : ''}</small></span><span class="row-meta"><span data-submission-id="${esc(a.id)}">${submissionBadge(a)}</span><small>${date(a.due_at)}</small></span></button>`).join('') || empty('目前沒有這類作業','同步課程後，作業與截止時間會整理在這裡。');
}
function courseCards(items) {
  return `<div class="course-grid">${items.map(c => `<button class="course-card" data-action="open-course" data-id="${esc(c.id)}"><span class="course-symbol">${icon('book')}</span><h3>${esc(c.name)}</h3><p>${esc(c.semester || '學期未提供')}${c.code ? ' · ' + esc(c.code) : ''}</p><span class="card-bottom"><span>${c.stale ? '快取／待更新' : 'Moodle 課程'}</span><span>查看課程 ↗</span></span></button>`).join('')}</div>`;
}
function coursesView() {
  const title = heading('我的課程','課程依學期由新到舊排列。',button(`${icon('refresh')} 同步課程`,'sync-courses'));
  const [latest, ...history] = groupCoursesBySemester(courses);
  if (!latest) return title + `<section class="panel">${empty('尚未同步課程','按右上角「同步課程」取得目前修課清單。')}</section>`;
  const label = latest.semester ? `最新學期 ${latest.semester}` : '學期未提供';
  const olderCourses = history.map(group => `
    <details><summary>${esc(group.semester ?? '學期未提供')} <span class="count">${group.courses.length} 門</span></summary>
      ${courseCards(group.courses)}
    </details>`).join('');
  return title + `<section><div class="section-heading"><h2>${esc(label)} <span class="count">${latest.courses.length} 門</span></h2></div>
    ${courseCards(latest.courses)}</section>${olderCourses ? `<section aria-label="其他學期">${olderCourses}</section>` : ''}`;
}
function submissionRefreshMessage(data) {
  if (data.submission_refresh_pending) return '<p class="notice">正在向 Moodle 查詢繳交狀態…</p>';
  const error = data.submission_refresh_error;
  if (error === 'SUBMISSION_STATUS_UNAVAILABLE') {
    return '<p class="notice">繳交狀態顯示「待確認」的作業，可至 Moodle 查看。</p>';
  }
  return error ? `<p class="notice warning">未取得最新繳交狀態的作業會列為「待確認」。${esc(errorText({code:error}))}</p>` : '';
}
function submissionRefreshNotice(data) {
  return `<div id="submission-refresh-status" role="status">${submissionRefreshMessage(data)}</div>`;
}
function homeAssignmentItems(data) {
  if (assignmentFilter === 'week') return data.upcoming;
  if (!data.submission_refresh_pending) {
    if (assignmentFilter === 'pending') return data.assignments.filter(a => ['未完成','逾期未提交'].includes(a.submission_label));
    if (assignmentFilter === 'unknown') return data.unknown;
  }
  return data.assignments;
}
function homeView() {
  const d = dashboard || {courses:[],assignments:[],upcoming:[],overdue:[],unknown:[],review:[],today:[],recent:[]};
  const [latest] = groupCoursesBySemester(d.courses);
  const currentCourses = latest?.semester ? latest.courses : [];
  const activeCount = currentCourses.filter(course => course.scope !== 'inactive').length;
  const courseNote = latest?.semester ? `最新學期 ${latest.semester}` : '學期未提供';
  const items = homeAssignmentItems(d);
  const liveCount = field => d.submission_refresh_pending ? '—' : d[field].length;
  const stats = [ ['本週截止',liveCount('upcoming'),'份作業','先完成最接近截止的作業','calendar','accent','upcoming'],['待審閱草稿',d.review.length,'份草稿','確認內容後即可匯出','edit',''],['進行中課程',activeCount,'門課程',courseNote,'book',''],['逾期未提交',liveCount('overdue'),'份作業','依 Moodle 即時查詢結果','clock','warm','overdue'] ];
  return `<div class="page-heading"><div><div class="date-label">${icon('calendar')}${esc(fullDate())}</div><h1>讓今天的學習，更有條理。</h1><p>課程、作業與進度，一眼掌握。</p></div>${button(`${icon('refresh')} 同步課程與作業`,'sync-overview')}</div>
    ${!loggedIn ? `<section class="onboarding"><span>${icon('link')}</span><div><h2>歡迎來到暗黑工作台</h2><p>先連結 Moodle，將課程與作業帶進來。</p></div>${button('連結學校帳號 →','connections','primary')}</section>` : ''}
    ${submissionRefreshNotice(d)}
    <div class="stats-grid">${stats.map(([title,count,unit,note,glyph,tone,field]) => `<div class="stat-card ${tone}"><div class="stat-top"><span>${title}</span>${icon(glyph)}</div><strong${field ? ` id="home-${field}-count"` : ''}>${loggedIn ? count : '—'}</strong><span class="stat-unit">${unit}</span><small>${esc(note)}</small></div>`).join('')}</div>
    <div class="dashboard-columns"><div>${panel(`作業待辦 <span class="count" id="home-assignment-count">${items.length}</span>`,
      `<div class="segment" aria-label="作業篩選">${[['pending','未完成'],['unknown','待確認'],['week','本週截止'],['all','全部作業']].map(([k,v]) => `<button class="${assignmentFilter === k ? 'active' : ''}" data-action="filter-assignments" data-filter="${k}" aria-pressed="${assignmentFilter === k}">${v}</button>`).join('')}</div><div id="home-assignment-rows">${assignmentRows(items.slice(0,6))}</div>`,
      '<a class="text-link" href="#work">前往工作台 ↗</a>')}
      ${panel('最近的草稿',d.review.length ? d.review.slice(0,3).map(t => `<button class="assignment-row" data-action="open-task" data-id="${esc(t.id)}"><span class="row-icon">${icon('edit')}</span><span class="row-copy"><strong>${esc(t.title)}</strong><small>${date(t.created_at)}</small></span>${badge(taskStatus(t),'amber')}</button>`).join('') : empty('留一個位置，給你的下一份成果','完成 AI 生成後，在這裡繼續審閱與編輯。'),'<a class="text-link" href="#history">查看全部 ↗</a>')}</div>
      <div class="dashboard-side"><section class="feature-card"><span class="feature-orbit">${icon('spark')}</span><p class="eyebrow">HOMEWORK, SIMPLIFIED</p><h2>從題目到草稿，<br>一步一步完成。</h2><p>整理資料、釐清需求，讓 AI 協助你完成第一份草稿。</p>${button(`開始處理作業 ${icon('arrow')}`,'work','primary')}</section>
      ${panel('今天的課程',`<div class="panel-body">${d.today.length ? d.today.map(c => `<div class="schedule-row"><span class="schedule-time">第 ${esc(c.period || '?')} 節</span><span class="row-copy"><strong>${esc(c.name)}</strong><small>${esc(c.classroom || '教室未提供')} · ${esc(c.teacher || '教師未提供')}</small></span></div>`).join('') : empty('今天沒有已同步的課程','連結校務系統並同步課表，讓一天的安排更清楚。','<a class="text-link" href="#hub">查看學習資料 ↗</a>')}</div>`)}
      </div></div>
      <div class="section-heading"><h2>我的課程 <span class="count">${currentCourses.length}</span></h2><a class="text-link" href="#courses">所有課程 ↗</a></div>
      ${currentCourses.length ? courseCards(currentCourses.slice(0,3)) : `<section class="panel">${d.courses.length
        ? empty('尚無已辨識學期的課程','可到「我的課程」查看已同步的課程。')
        : empty('課程將顯示在這裡','連結後按「同步課程」，就能開始整理學習資料。')}</section>`}`;
}
function snapshot(s) {
  if (!s) return '';
  return `<p class="snapshot">來源：${s.source === 'sis' ? '官方校務' : s.source === 'moodle' ? 'Moodle' : '尚未同步'} · 最後成功同步：${s.last_synced_at ? date(s.last_synced_at) : '尚無紀錄'}${s.stale ? ' · 快取／待更新' : ''}${s.error ? ' · ' + esc(errorText({code:s.error})) : ''}</p>`;
}
function courseView(data) {
  const c = data.course, s = data.snapshots;
  return heading(c.name,`${c.semester || '學期未提供'} · 作業、教材與公告`,button(`${icon('refresh')} 完整同步本課`,'sync-one','',`data-id="${esc(c.id)}"`)) +
    submissionRefreshNotice(data) +
    panel('課程作業',assignmentRows(data.assignments)) +
    panel('章節與教材',`<div class="panel-body">${snapshot(s.contents)}${(s.contents.data || []).map(section => `<details><summary>${esc(section.name)}</summary><p class="pre-wrap muted">${esc(section.summary || '')}</p>${(section.modules || []).map(m => `<div class="file-item"><div><h3>${esc(m.name)}</h3><p class="muted pre-wrap">${esc(m.description || '')}</p>${externalLink(m.url,'官方頁面')}</div></div>`).join('')}</details>`).join('') || empty('尚無教材章節','按「完整同步本課」讀取 Moodle 教材。')}
      ${data.materials.length ? `<h3 class="section-heading">教材原檔</h3>${data.materials.map(m => `<div class="file-item"><span>${esc(m.name)}</span>${m.path ? `<a class="text-link" href="/api/materials/${id(m.id)}/file" download>下載原檔 ↗</a>` : button('核准下載到本機','download-material','small',`data-id="${esc(m.id)}"`)}</div>`).join('')}` : ''}</div>`) +
    panel('課程公告',`<div class="panel-body">${snapshot(s.announcements)}${(s.announcements.data || []).map(p => `<details><summary>${esc(p.title)}</summary><p class="pre-wrap muted">${esc(p.message)}</p>${externalLink(p.url,'查看原公告')}</details>`).join('') || empty('尚無公告')}</div>`) +
    panel('Moodle 評分',`<div class="panel-body">${snapshot(s.grades)}<p class="snapshot">正式校務成績另列於學習資料，Moodle 評分不代表正式學期成績。</p></div>${table(['評分項目','分數','範圍','百分比'],(s.grades.data || []).map(g => [g.name,g.grade ?? '未提供',g.range,g.percentage]))}`) +
    panel('本課成果',taskRows(data.tasks)) + `<a class="text-link" href="#courses">← 返回所有課程</a>`;
}
function taskRows(tasks, showDelete = false) {
  return tasks.map(t => {
    const open = `<button class="assignment-row" data-action="open-task" data-id="${esc(t.id)}"><span class="row-icon">${icon('file')}</span><span class="row-copy"><strong>${esc(t.title)}</strong><small>${esc(t.course_name || '')} · ${date(t.created_at)}</small></span>${badge(taskStatus(t),t.status === 'completed' ? '' : 'amber')}</button>`;
    if (!showDelete || !t.can_delete_draft) return open;
    const remove = button('刪除草稿','delete-draft','small quiet',`data-id="${esc(t.id)}" data-title="${esc(t.title)}" aria-label="刪除草稿：${esc(t.title)}"`);
    return `<div class="task-row-with-action">${open}${remove}</div>`;
  }).join('') || empty('還沒有任務紀錄','從作業工作台開始，成果與中斷的任務都會保留在這裡。',button('開始處理作業','work','primary'));
}
function reportRows(s) {
  return (s?.data?.sections || []).flatMap(section => [
    ...(section.fields || []).map(f => [section.title,f.label,f.value,f.detail || '']),
    ...(section.subjects || []).map(subject => [section.title,subject.title,subject.grade || '',subject.credits ? `學分：${subject.credits}；類別：${subject.category || ''}` : (subject.details || []).join(' / ')]),
    ...(section.notes || []).map(note => [section.title,'官方說明',note,''])]);
}
function hubCourseList(selected = new Set()) {
  const semester = hubSemester.trim();
  const matching = discovered?.semester === semester;
  const available = matching ? discovered.courses : semester ? [] : courses.filter(c => (c.scope || 'inprogress') === 'inprogress');
  const emptyMessage = matching ? '此學期沒有找到可同步的課程。' : '請先列出指定範圍內可同步的課程。';
  const html = available.map(c => `<label class="check"><input type="checkbox" name="sync-course" value="${esc(c.id)}" ${selected.has(c.id) ? 'checked' : ''}><span>${esc(c.name)}<small>${esc(c.semester || '學期未提供')}</small></span></label>`).join('');
  return {html:html || `<p class="muted">${emptyMessage}</p>`, hasCourses:available.length > 0};
}
function checkedHubCourses() {
  return [...document.querySelectorAll('[name="sync-course"]:checked')].map(c => c.value);
}
function hubView(selected) {
  const data = hubData, s = data.sis[hubTab];
  const rows = hubTab === 'timetable' ? (s.data?.courses || []).flatMap(c => (c.meetings?.length ? c.meetings : [{}]).map(m => [c.name,c.teacher || '未提供',m.weekday ? '星期' + '一二三四五六日'[m.weekday - 1] : '未提供',m.period || '未提供',m.classroom || '未提供',c.semester])) : reportRows(s);
  const names = {timetable:'正式課表',grades:'正式成績',rank:'官方排名',graduation:'畢業進度'};
  const selection = hubCourseList(selected);
  return heading('我的學習資料','課表、成績與學習進度，集中在這裡。',button(`${icon('refresh')} 同步校務資料`,'sync-sis')) +
    `<div class="tabs" aria-label="校務資料類別">${Object.entries(names).map(([k,v]) => `<button data-action="hub-tab" data-tab="${k}" class="${hubTab === k ? 'active' : ''}" aria-pressed="${hubTab === k}">${v}</button>`).join('')}</div>
    ${panel(names[hubTab],`<div class="panel-body">${snapshot(s)}${s.data?.summary_issues?.length ? `<p class="notice warning">未讀取的學分摘要：${esc(s.data.summary_issues.join('、'))}</p>` : ''}</div>${table(hubTab === 'timetable' ? ['課程','教師','星期','節次','教室','學期'] : ['學期／分類','科目／項目','官方數值','說明'],rows)}`)}
    ${panel('同步 Moodle 學習資料',`<div class="panel-body"><p class="muted">選擇課程，一次同步作業、教材、公告與評分。</p><div class="form-grid"><label>指定學期（選填）<input id="semester" maxlength="20" value="${esc(hubSemester)}" placeholder="例如：115-1；留白為目前修課"></label><div class="button-row">${button('列出可同步課程','discover-courses')}</div></div><div id="sync-course-list">${selection.html}</div>${button('同步選定課程','sync-selected','primary',selection.hasCourses ? '' : 'disabled')}</div>`)}
    <section class="panel form-panel"><details><summary>校務與 Moodle 課程對應</summary><p class="muted">有歧義時，請核對同學期課程後確認。</p>${data.mappings.map((m,i) => `<div class="file-item"><div><strong>${esc(m.name)}</strong><small class="muted"> · ${esc(m.semester)}</small><p class="snapshot">${esc({matched:'已自動比對',confirmed:'已人工確認',needs_confirmation:'需人工確認',unmatched:'沒有對應課程'}[m.status] || m.status)}</p></div><div>${m.candidates.length ? `<label>對應 Moodle 課程<select id="mapping-${i}">${option('','請選擇',m.moodle_id)}${m.candidates.map(c => option(c.id,c.name,m.moodle_id)).join('')}</select></label>${button('確認對應','confirm-mapping','small',`data-id="${esc(m.sis_id)}" data-index="${i}"`)}` : ''}</div></div>`).join('') || empty('尚無課程對應','同步校務課表後即可核對。')}</details></section>`;
}
function searchView() {
  const r = searchResults;
  return heading('找到需要的資料','搜尋本機課程、作業、教材與已下載文件，保留來源頁／節。') +
    `<form id="search-form" class="search-form"><label>關鍵字<input id="search-query" name="q" maxlength="200" placeholder="搜尋作業名稱、教材內容…" value="${esc(searchFields.q)}"></label><label>課程<select name="course_id">${option('','全部課程',searchFields.course_id)}${courses.map(c => option(c.id,c.name,searchFields.course_id)).join('')}</select></label><label>截止日起<input name="due_from" type="date" value="${esc(searchFields.due_from)}"></label><label>截止日迄<input name="due_to" type="date" value="${esc(searchFields.due_to)}"></label><button class="button primary" type="submit">${icon('search')} 搜尋</button></form>
    ${r ? panel(`搜尋結果 <span class="count">${r.results.length}</span>`,r.results.map((hit,i) => `<article class="result-card"><h3>${esc(hit.title)} ${badge({course:'課程',assignment:'作業',asset:'附件',material:'教材',task:'任務'}[hit.kind] || hit.kind,'gray')}</h3><p>${esc(hit.snippet)}</p><p class="snapshot">來源頁／節：${esc(hit.page || hit.section || '—')} · 截止：${date(hit.due_at)}</p>${button('開啟來源','open-search-result','small',`data-index="${i}"`)}</article>`).join('') || empty('沒有找到符合的資料','試試其他關鍵字，或縮小課程與日期範圍。')) + `<p class="snapshot">索引更新：${date(r.index.indexed_at)}${r.truncated ? ' · 僅顯示前 100 筆，請縮小範圍。' : ''}</p>${r.index.issues.length ? `<details><summary>搜尋範圍與解析限制（${r.index.issues.length}）</summary>${r.index.issues.map(i => `<p class="snapshot">${esc(i.name)}：${esc(i.code)}</p>`).join('')}</details>` : ''}` : `<section class="panel">${empty('輸入關鍵字，從這裡開始','未下載文件僅搜尋檔名；CSV 內容搜尋涵蓋欄位與前五筆。')}</section>`}`;
}
async function refreshData({overview = false, version = routeVersion} = {}) {
  let next = null;
  try { next = await api(overview ? '/dashboard?pending=true' : '/courses'); }
  catch (error) { if (error.code !== 'MOODLE_LOGIN_REQUIRED') throw error; }
  if (version !== routeVersion) return;
  if (overview || !next) dashboard = next;
  courses = (overview ? next?.courses : next) || [];
  loggedIn = next !== null;
  $('#connection-summary').textContent = loggedIn ? 'Moodle 本機資料可用' : '管理學校帳號與 AI';
}
function viewReadFailure(error) {
  return heading('暫時無法讀取','已保存的資料仍保留，請確認連線後再試一次。') + `<div class="notice error">${esc(errorText(error))}</div><div class="button-row">${button('重新讀取','refresh')}${button('管理連線','connections')}</div>`;
}
function updateHomeSubmissionRows() {
  const items = homeAssignmentItems(dashboard);
  const rows = $('#home-assignment-rows');
  const focused = document.activeElement;
  const activeId = focused && rows.contains(focused) ? focused.dataset.id : null;
  $('#home-assignment-count').textContent = items.length;
  rows.innerHTML = assignmentRows(items.slice(0,6));
  if (activeId) {
    const replacement = [...rows.querySelectorAll('[data-action="open-assignment"]')].find(row => row.dataset.id === activeId);
    (replacement || $('[data-action="filter-assignments"].active'))?.focus({preventScroll:true});
  }
}
async function refreshSubmissionView(path, local, overview, version) {
  submissionRefreshVersion = version;
  let view;
  try {
    view = await api(`${path}?live=true`);
    if (view.submission_scope !== local.submission_scope) {
      if (version === routeVersion) content.innerHTML = viewReadFailure({code:'ACCOUNT_CHANGED_RECONNECT'});
      return;
    }
  } catch (error) {
    if (version !== routeVersion) return;
    if (['ACCOUNT_CHANGED_RECONNECT','MOODLE_LOGIN_REQUIRED','LOCAL_AUTH_REQUIRED'].includes(error.code)) {
      content.innerHTML = viewReadFailure(error);
      return;
    }
    const unknown = a => ({...a,submission_status:'unknown',submission_label:'待確認'});
    const assignments = local.assignments.map(unknown);
    view = {assignments, unknown:assignments, upcoming:local.upcoming?.map(unknown), overdue:[],
      submission_refresh_error:error.code || 'SUBMISSION_STATUS_UNAVAILABLE'};
  } finally {
    if (submissionRefreshVersion === version) submissionRefreshVersion = null;
  }
  if (version !== routeVersion) return;
  $('#submission-refresh-status').innerHTML = submissionRefreshMessage(view);
  if (overview) {
    const {assignments,upcoming,overdue,unknown,submission_refresh_error} = view;
    dashboard = {...dashboard,assignments,upcoming,overdue,unknown,submission_refresh_error,submission_refresh_pending:false};
    $('#home-upcoming-count').textContent = upcoming.length;
    $('#home-overdue-count').textContent = overdue.length;
    updateHomeSubmissionRows();
  } else {
    const assignments = new Map(view.assignments.map(item => [item.id,item]));
    content.querySelectorAll('[data-submission-id]').forEach(el => {
      el.innerHTML = submissionBadge(assignments.get(el.dataset.submissionId) || {});
    });
  }
}
async function render({keepView = false} = {}) {
  if (!booted) return;
  const version = ++routeVersion;
  const parts = location.hash.slice(1).split('/'); route = labels[parts[0]] ? parts[0] : 'home';
  try { routeRef = decodeURIComponent(parts.slice(1).join('/')); } catch { routeRef = ''; }
  $('#page-label').textContent = labels[route]; document.title = `${labels[route]} · NTPU 暗黑工作台`;
  document.querySelectorAll('[data-page]').forEach(el => { const selected = el.dataset.page === (route === 'course' ? 'courses' : route); el.classList.toggle('active',selected); if (selected) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current'); });
  content.setAttribute('aria-busy','true');
  if (!keepView) content.innerHTML = '<div class="loading-state"><span class="spinner"></span><p>正在讀取本機資料…</p></div>';
  try {
    let html, submissionView, submissionPath;
    const overview = route === 'home';
    if (overview) {
      await refreshData({overview:true,version});
      submissionView = dashboard;
      submissionPath = '/dashboard';
      html = homeView();
    }
    else if (!loggedIn) html = heading(labels[route],'你的資料保存在本機。') + `<section class="panel">${loginEmpty()}</section>`;
    else if (route === 'courses') html = coursesView();
    else if (route === 'course') {
      submissionPath = `/learning/courses/${id(routeRef)}`;
      submissionView = await api(`${submissionPath}?pending=true`);
      html = courseView(submissionView);
    }
    else if (route === 'work') { await workflow.load(); html = workflow.html(); }
    else if (route === 'history') html = heading('歷史任務','繼續未完成的工作，或回顧已保存的成果。',button('開始新作業 →','work','primary')) + panel('所有任務',taskRows(await api('/tasks'),true));
    else if (route === 'hub') {
      const next = await api('/hub');
      if (version !== routeVersion) return;
      hubData = next;
      html = hubView();
    }
    else if (route === 'search') html = searchView();
    if (version !== routeVersion) return;
    const scrollTop = keepView ? window.scrollY : 0;
    const details = keepView ? [...content.querySelectorAll('details')].map(el => el.open) : null;
    content.innerHTML = html;
    syncDownloadScopes();
    if (details) {
      content.querySelectorAll('details').forEach((el,index) => {
        if (index < details.length) el.open = details[index];
      });
    } else content.focus({preventScroll:true});
    window.scrollTo({top:scrollTop,behavior:'instant'});
    if (submissionView?.submission_refresh_pending) void refreshSubmissionView(submissionPath,submissionView,overview,version);
  } catch (error) {
    if (version !== routeVersion) return;
    content.innerHTML = viewReadFailure(error);
  } finally { if (version === routeVersion) content.setAttribute('aria-busy','false'); }
}
function connectionMessage(message) { $('#connection-message').hidden = false; $('#connection-message').textContent = message; }
async function checkConnections() {
  const result = await api('/connections');
  const ai = result['ntpu-ai'];
  $('#connection-states').innerHTML = [['Moodle',result.moodle === 'cached' ? '本機資料可用' : '尚未連結'],['校務系統',result.sis === 'connected' ? '已連線' : '尚未連線'],['NTPU AI',ai.ready ? '已就緒' : ai.authentication === 'authenticated' ? '登入完成，待交接' : '尚未就緒']].map(([name,state]) => `<div class="connection-state">${name}<small>${state}</small></div>`).join('');
}
function providerFields() {
  const ai = $('#provider').value === 'ntpu-ai'; $('#credential-fields').hidden = ai; $('#ai-fields').hidden = !ai;
  $('#username').required = !ai; $('#password').required = !ai; $('#password').value = ''; $('#password').type = 'password';
  const toggle = $('[data-action="toggle-password"]'); toggle.textContent = '顯示'; toggle.setAttribute('aria-pressed','false'); toggle.setAttribute('aria-label','顯示密碼');
}
async function identityChanged() {
  clearAccountView(accountScope);
  await refreshData(); await render();
}
function syncResult(result) {
  const summaries = [];
  for (const [key,value] of Object.entries(result || {})) {
    if (value && typeof value === 'object' && 'status' in value) summaries.push([key,value]);
    else for (const [kind,s] of Object.entries(value || {})) if (s && typeof s === 'object' && 'status' in s) summaries.push([`${courses.find(c => c.id === key)?.name || key} / ${kind}`,s]);
  }
  const issues = summaries.filter(([,s]) => s.error || s.status === 'partial');
  notify(issues.length ? '部分資料未更新：\n' + issues.map(([name,s]) => `${name}：${s.error || '部分資料缺漏'}`).join('\n') : '同步完成。',issues.length > 0);
}
async function handleAction(name,el) {
  if (await workflow.action(name,el)) return;
  switch (name) {
    case 'work': case 'history': navigate(name); break;
    case 'search': navigate('search'); break;
    case 'refresh': if (!accountScope) await identityChanged(); else await render(); break;
    case 'connections':
      if (el.dataset.provider === 'ntpu-ai') $('#provider').value = 'ntpu-ai';
      $('#connection-message').hidden = true; providerFields(); $('#connections-dialog').showModal();
      $('#connection-states').textContent = '按「檢查連線」查看目前狀態。'; break;
    case 'check-connections': await checkConnections(); connectionMessage('已更新連線狀態。Moodle 本機快取不代表線上連線已驗證。'); break;
    case 'restore-connection': {
      const provider = $('#provider').value;
      if (provider === 'moodle') {
        await workflow.flush(); await api('/connections/moodle/restore','POST'); await identityChanged(); connectionMessage('Moodle 連線已恢復，課程已同步。');
      } else if (provider === 'ntpu-ai') {
        const result = await api('/connections/ntpu-ai/restore','POST'); connectionMessage(result.ready ? 'AI 已連線，可直接續跑或產生草稿。' : result.authentication === 'authenticated' ? '登入已完成。請關閉登入視窗，再次恢復連線。' : 'AI 尚未就緒。若需要登入，請開啟專用登入視窗。');
      } else { syncResult(await api('/sync/sis','POST')); connectionMessage('校務連線檢查完成。若未更新，請重新登入校務。'); }
      await checkConnections(); break;
    }
    case 'ai-login': {
      const result = await api('/connections/ntpu-ai/start','POST');
      $('#ai-login-desktop').hidden = !result.login_desktop;
      connectionMessage(result.login_desktop
        ? '已開啟專用瀏覽器。請前往 Docker 登入桌面，完成後回工作台按「恢復所選連線」，系統會關閉登入視窗並接手連線。'
        : '已開啟專用登入視窗。完成後關閉該視窗，按「恢復所選連線」。');
      break;
    }
    case 'logout':
      if (await confirmAction('登出所選服務？','已保存的本機資料會保留。','登出')) {
        await workflow.flush(); await api(`/connections/${id($('#provider').value)}`,'DELETE'); await identityChanged(); await checkConnections(); connectionMessage('已登出所選服務。');
      } break;
    case 'sync-overview': {
      const enrolled = await api('/sync/moodle','POST');
      const [latest] = groupCoursesBySemester(enrolled);
      try {
        for (const course of latest?.courses || []) {
          await api('/sync/moodle?course_id=' + id(course.id),'POST');
        }
      } finally {
        await render();
      }
      notify(latest ? '課程與最新學期的作業清單已同步。' : '課程清單已更新。');
      break;
    }
    case 'sync-courses': await api('/sync/moodle','POST'); await refreshData(); await render(); notify('課程清單已更新。進入課程可同步作業與教材。'); break;
    case 'filter-assignments': assignmentFilter = el.dataset.filter; content.innerHTML = homeView(); break;
    case 'open-course': navigate('course',el.dataset.id); break;
    case 'open-assignment': await workflow.openAssignment(el.dataset.id); navigate('work'); break;
    case 'open-task': await workflow.openTask(el.dataset.id); navigate('work'); break;
    case 'delete-draft': {
      const version = accountVersion;
      if (!await confirmAction('刪除草稿？',`將刪除「${el.dataset.title}」的本機草稿與這次任務的產物。刪除後無法從工作台復原。`,'刪除草稿')) break;
      if (version !== accountVersion) break;
      await api(`/tasks/${id(el.dataset.id)}`,'DELETE');
      workflow.discardTask(el.dataset.id);
      await render();
      notify('草稿已刪除。');
      break;
    }
    case 'sync-one': syncResult(await api('/sync/moodle/full','POST',{course_ids:[el.dataset.id]})); await refreshData(); await render(); break;
    case 'download-material':
      await api(`/materials/${id(el.dataset.id)}/download`,'POST',{approved:true}); await render(); notify('教材已下載至本機，可使用原檔連結。'); break;
    case 'hub-tab': hubTab = el.dataset.tab; content.innerHTML = hubView(new Set(checkedHubCourses())); break;
    case 'sync-sis': syncResult(await api('/sync/sis','POST')); await render(); break;
    case 'discover-courses': {
      const version = accountVersion;
      const semester = $('#semester').value.trim();
      const found = await api('/hub/courses/discover' + (semester ? '?semester=' + id(semester) : ''),'POST');
      await refreshData();
      if (version !== accountVersion || route !== 'hub') break;
      hubSemester = semester;
      discovered = {semester,courses:found};
      content.innerHTML = hubView();
      if (found.length) notify(`找到 ${found.length} 門課程，請勾選同步範圍。`);
      break;
    }
    case 'sync-selected': {
      const selected = checkedHubCourses();
      if (!selected.length || selected.length > 30) throw new Error('請選擇 1 至 30 門課程。');
      syncResult(await api('/sync/moodle/full','POST',{course_ids:selected})); await refreshData(); await render(); break;
    }
    case 'confirm-mapping': {
      const cid = $(`#mapping-${el.dataset.index}`).value; if (!cid) throw new Error('請先選擇對應的 Moodle 課程。');
      await api('/hub/mappings','PUT',{sis_id:el.dataset.id,moodle_id:cid}); await render(); notify('課程對應已確認。'); break;
    }
    case 'open-search-result': {
      const hit = searchResults.results[Number(el.dataset.index)];
      if (hit.kind === 'task') { await workflow.openTask(hit.ref); navigate('work'); }
      else if (hit.assignment_id) { await workflow.openAssignment(hit.assignment_id); navigate('work'); }
      else navigate('course',hit.course_id); break;
    }
    case 'preview-delete': {
      await workflow.flush();
      const preview = await api('/maintenance/deletion-preview'); deleteSnapshot = preview.confirmation;
      $('#delete-preview').hidden = false; $('#delete-preview').innerHTML = `<div class="notice warning">${esc(preview.scope)}<br>共 ${Object.values(preview.counts).reduce((a,b) => a + b,0)} 筆資料。<br>${esc(preview.limits)}<br>${esc(preview.ai_scope)}</div><label class="check"><input id="delete-confirm" type="checkbox"><span>我確認清除上述目前帳號的本機資料，此操作無法復原</span></label><label class="check"><input id="clear-ai" type="checkbox"><span>同時清除共用 AI 專用瀏覽器資料與登入</span></label>${button('清除目前帳號資料','delete-account','danger')}`; break;
    }
    case 'delete-account':
      if (!deleteSnapshot || !$('#delete-confirm')?.checked) throw new Error('請先預覽清除範圍並勾選確認。');
      await api('/maintenance/delete-account','POST',{confirmation:deleteSnapshot,confirmed:true,clear_ai:$('#clear-ai').checked}); await identityChanged(); connectionMessage('目前帳號的本機資料已清除。'); break;
  }
}

// One mutation at a time prevents double sends and account changes during saves.
async function run(el,operation) {
  if (mutationBusy) return;
  mutationBusy = true; const wasDisabled = el.disabled;
  el.disabled = true; el.setAttribute('aria-busy','true');
  const originalNodes = [...el.childNodes]; if (el.tagName === 'BUTTON' && !el.classList.contains('icon-button')) el.textContent = '處理中…';
  // Prevent editing a form whose values are being committed while awaiting HTTP.
  const controls = [...document.querySelectorAll('#content input,#content select,#content textarea,#login-form input,#login-form select')].map(node => [node,node.disabled]);
  controls.forEach(([node]) => node.disabled = true);
  try { await operation(); }
  catch (error) { notify(errorText(error),true); if ($('#connections-dialog').open) connectionMessage(errorText(error)); }
  finally {
    controls.forEach(([node,disabled]) => { if (node.isConnected) node.disabled = disabled; });
    if (el.isConnected) { el.disabled = wasDisabled; el.removeAttribute('aria-busy'); if (el.tagName === 'BUTTON' && !el.classList.contains('icon-button')) el.replaceChildren(...originalNodes); }
    mutationBusy = false;
  }
}
document.querySelectorAll('[data-icon]').forEach(el => el.innerHTML = icon(el.dataset.icon));
document.addEventListener('click',event => {
  const download = event.target.closest(downloadSelector);
  if (download && accountScope) scopeDownload(download);
  const navLink = event.target.closest('a[href^="#"]');
  if (navLink?.getAttribute('href') === '#content') { event.preventDefault(); content.focus(); return; }
  if (navLink && mutationBusy) { event.preventDefault(); return; }
  const el = event.target.closest('[data-action]'); if (!el || el.disabled) return;
  const name = el.dataset.action;
  if (name === 'close-dialog') { el.closest('dialog').close(); return; }
  if (name === 'toggle-password') {
    const input = $('#password'), show = input.type === 'password'; input.type = show ? 'text' : 'password'; el.textContent = show ? '隱藏' : '顯示'; el.setAttribute('aria-pressed',String(show)); el.setAttribute('aria-label',show ? '隱藏密碼' : '顯示密碼'); return;
  }
  void run(el,() => handleAction(name,el));
});
document.addEventListener('input',event => {
  if (event.target.id !== 'semester') { workflow.input(event.target); return; }
  const previous = hubSemester.trim();
  hubSemester = event.target.value;
  if (hubSemester.trim() === previous) return;
  const selection = hubCourseList();
  $('#sync-course-list').innerHTML = selection.html;
  $('[data-action="sync-selected"]').disabled = !selection.hasCourses;
});
document.addEventListener('change',event => {
  if (event.target.id === 'provider') providerFields();
  else if (event.target.id !== 'semester') void run(event.target,() => workflow.change(event.target));
});
document.addEventListener('submit',event => {
  event.preventDefault(); const form = event.target;
  if (form.id === 'login-form') {
    const provider = $('#provider').value, credentials = {username:$('#username').value.trim(),password:$('#password').value};
    void run(form.querySelector('[type="submit"]'),async () => {
      try { await workflow.flush(); await api(`/connections/${id(provider)}/start`,'POST',credentials); await identityChanged(); connectionMessage('登入成功。可以關閉視窗，繼續處理課程與作業。'); }
      finally { $('#password').value = ''; credentials.password = ''; }
      await checkConnections();
    });
  } else if (form.id === 'search-form') {
    searchFields = Object.fromEntries(new FormData(form));
    void run(form.querySelector('[type="submit"]'),async () => {
      const params = new URLSearchParams(Object.entries(searchFields).filter(([,v]) => v)); searchResults = await api('/search?' + params); content.innerHTML = searchView();
    });
  }
});
$('#connections-dialog').addEventListener('close',() => { $('#password').value = ''; $('#username').value = ''; providerFields(); deleteSnapshot = null; $('#delete-preview').hidden = true; });
document.addEventListener('keydown',event => {
  if (event.key === '/' && !event.ctrlKey && !event.metaKey && !/INPUT|TEXTAREA|SELECT/.test(event.target.tagName) && !document.querySelector('dialog[open]')) { event.preventDefault(); navigate('search'); }
});
function refreshVisibleSubmissions() {
  if (!booted || !loggedIn || mutationBusy || document.visibilityState !== 'visible'
      || (route !== 'home' && route !== 'course') || content.getAttribute('aria-busy') === 'true'
      || submissionRefreshVersion === routeVersion
      || $('dialog[open]')) return;
  void render({keepView:true});
}
window.addEventListener('hashchange',() => { void workflow.flush().then(() => render()).catch(error => notify(errorText(error),true)); });
accountChannel?.addEventListener('message',event => {
  if (typeof event.data === 'string' && event.data !== accountScope) {
    clearAccountView();
    if (booted) void identityChanged().catch(error => notify(errorText(error),true));
  }
});
window.addEventListener('focus',refreshVisibleSubmissions);
document.addEventListener('visibilitychange',() => {
  if (document.visibilityState === 'hidden') void workflow.flush().catch(() => {});
  else refreshVisibleSubmissions();
});
async function boot() {
  try { if (location.hash && location.hash !== '#home') await refreshData(); }
  catch (error) { booted = true; await render(); notify(errorText(error),true); return; }
  booted = true; await render();
}
void boot();
