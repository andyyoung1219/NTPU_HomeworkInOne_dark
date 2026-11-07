import {esc,id,icon,button,empty,badge,date,fullDate,heading,panel,table,option,externalLink,taskStatus,errorText} from './ui.js';
import {createWorkflow} from './workflow.js';

const $ = selector => document.querySelector(selector);
const content = $('#content');
let courses = [], dashboard = null, loggedIn = false, booted = false, routeVersion = 0, mutationBusy = false;
let route = 'home', routeRef = '', assignmentFilter = 'pending', hubTab = 'timetable', hubData = null, discovered = [], searchResults = null, deleteSnapshot = null;
let searchFields = {q:'',course_id:'',due_from:'',due_to:''};
const labels = {home:'學習總覽',courses:'我的課程',course:'我的課程',work:'作業工作台',hub:'學習資料',history:'歷史任務',search:'搜尋'};

async function api(path,method = 'GET',body,headers = {}) {
  let response;
  try {
    response = await fetch('/api' + path,{method,credentials:'same-origin',cache:'no-store',
      headers:{Accept:'application/json',...(body !== undefined ? {'Content-Type':'application/json'} : {}),...headers},
      ...(body !== undefined ? {body:JSON.stringify(body)} : {})});
  } catch { const error = new Error('無法連上本機服務。'); error.code = 'NETWORK_ERROR'; throw error; }
  let result;
  try { result = await response.json(); } catch { throw new Error('伺服器回應無法讀取，請確認本機服務。'); }
  if (!response.ok || result.error) {
    const code = typeof result.error === 'string' ? result.error : result.error?.code || `HTTP_${response.status}`;
    const error = new Error('操作未完成，已保存的資料仍保留。'); error.code = code; throw error;
  }
  return result.data;
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
  if (focus) content.focus();
}});
function navigate(page,ref = '') { const hash = '#' + page + (ref ? '/' + id(ref) : ''); if (location.hash === hash) void render(); else location.hash = hash; }
function loginEmpty() { return empty('連結帳號，開始你的學習空間','登入 Moodle 後同步課程與作業。你已保存的資料會留在本機。',button('連結學校帳號','connections','primary')); }
function assignmentRows(items) {
  return items.map(a => {
    const state = a.submission_label || (a.submission_status === 'submitted' ? '已提交' : '待確認');
    const tone = state === '逾期未提交' ? 'red' : state === '已提交' ? '' : 'amber';
    return `<button class="assignment-row" data-action="open-assignment" data-id="${esc(a.id)}"><span class="row-icon">${icon('file')}</span><span class="row-copy"><strong>${esc(a.title)}</strong><small>${esc(a.course_name || courses.find(c => c.id === a.course_id)?.name || '課程作業')}${a.stale ? ' · 快取／待更新' : ''}</small></span><span class="row-meta">${badge(state,tone)}<small>${date(a.due_at)}</small></span></button>`;
  }).join('') || empty('目前沒有這類作業','同步課程後，作業與截止時間會整理在這裡。');
}
function courseCards(items) {
  return `<div class="course-grid">${items.map(c => `<button class="course-card" data-action="open-course" data-id="${esc(c.id)}"><span class="course-symbol">${icon('book')}</span><h3>${esc(c.name)}</h3><p>${esc(c.semester || '學期未提供')}${c.code ? ' · ' + esc(c.code) : ''}</p><span class="card-bottom"><span>${c.stale ? '快取／待更新' : 'Moodle 課程'}</span><span>查看課程 ↗</span></span></button>`).join('')}</div>`;
}
function homeView() {
  const d = dashboard || {courses:[],assignments:[],upcoming:[],overdue:[],review:[],today:[],recent:[]};
  let items = d.assignments;
  if (assignmentFilter === 'pending') items = items.filter(a => a.submission_label !== '已提交');
  if (assignmentFilter === 'week') items = d.upcoming;
  const stats = [ ['本週截止',d.upcoming.length,'份作業','先完成最接近截止的作業','calendar','accent'],['待審閱草稿',d.review.length,'份草稿','確認內容後即可匯出','edit',''],['進行中課程',d.courses.filter(c => c.scope !== 'inactive').length,'門課程','課程資料集中管理','book',''],['逾期未提交',d.overdue.length,'份作業','以 Moodle 提交狀態為準','clock','warm'] ];
  return `<div class="page-heading"><div><div class="date-label">${icon('calendar')}${esc(fullDate())}</div><h1>讓今天的學習，更有條理。</h1><p>課程、作業與進度，一眼掌握。</p></div>${button(`${icon('refresh')} 同步課程`,'sync-courses')}</div>
    ${!loggedIn ? `<section class="onboarding"><span>${icon('link')}</span><div><h2>歡迎來到你的學習工作台</h2><p>先連結 Moodle，將課程與作業帶進來。</p></div>${button('連結學校帳號 →','connections','primary')}</section>` : ''}
    <div class="stats-grid">${stats.map(([title,count,unit,note,glyph,tone]) => `<div class="stat-card ${tone}"><div class="stat-top"><span>${title}</span>${icon(glyph)}</div><strong>${loggedIn ? count : '—'}</strong><span class="stat-unit">${unit}</span><small>${note}</small></div>`).join('')}</div>
    <div class="dashboard-columns"><div>${panel(`作業待辦 <span class="count">${items.length}</span>`,
      `<div class="segment" aria-label="作業篩選">${[['pending','待處理'],['week','本週截止'],['all','全部作業']].map(([k,v]) => `<button class="${assignmentFilter === k ? 'active' : ''}" data-action="filter-assignments" data-filter="${k}" aria-pressed="${assignmentFilter === k}">${v}</button>`).join('')}</div>${assignmentRows(items.slice(0,6))}`,
      '<a class="text-link" href="#work">前往工作台 ↗</a>')}
      ${panel('最近的草稿',d.review.length ? d.review.slice(0,3).map(t => `<button class="assignment-row" data-action="open-task" data-id="${esc(t.id)}"><span class="row-icon">${icon('edit')}</span><span class="row-copy"><strong>${esc(t.title)}</strong><small>${date(t.created_at)}</small></span>${badge(taskStatus(t),'amber')}</button>`).join('') : empty('留一個位置，給你的下一份成果','完成 AI 生成後，在這裡繼續審閱與編輯。'),'<a class="text-link" href="#history">查看全部 ↗</a>')}</div>
      <div class="dashboard-side"><section class="feature-card"><span class="feature-orbit">${icon('spark')}</span><p class="eyebrow">HOMEWORK, SIMPLIFIED</p><h2>從題目到草稿，<br>一步一步完成。</h2><p>整理資料、釐清需求，讓 AI 協助你完成第一份草稿。</p>${button(`開始處理作業 ${icon('arrow')}`,'work','primary')}</section>
      ${panel('今天的課程',`<div class="panel-body">${d.today.length ? d.today.map(c => `<div class="schedule-row"><span class="schedule-time">第 ${esc(c.period || '?')} 節</span><span class="row-copy"><strong>${esc(c.name)}</strong><small>${esc(c.classroom || '教室未提供')} · ${esc(c.teacher || '教師未提供')}</small></span></div>`).join('') : empty('今天沒有已同步的課程','連結校務系統並同步課表，讓一天的安排更清楚。','<a class="text-link" href="#hub">查看學習資料 ↗</a>')}</div>`)}
      </div></div>
      <div class="section-heading"><h2>我的課程 <span class="count">${d.courses.length}</span></h2><a class="text-link" href="#courses">所有課程 ↗</a></div>${d.courses.length ? courseCards(d.courses.slice(0,3)) : `<section class="panel">${empty('課程將顯示在這裡','連結後按「同步課程」，就能開始整理學習資料。')}</section>`}`;
}
function snapshot(s) {
  if (!s) return '';
  return `<p class="snapshot">來源：${s.source === 'sis' ? '官方校務' : s.source === 'moodle' ? 'Moodle' : '尚未同步'} · 最後成功同步：${s.last_synced_at ? date(s.last_synced_at) : '尚無紀錄'}${s.stale ? ' · 快取／待更新' : ''}${s.error ? ' · ' + esc(errorText({code:s.error})) : ''}</p>`;
}
function courseView(data) {
  const c = data.course, s = data.snapshots;
  return heading(c.name,`${c.semester || '學期未提供'} · 作業、教材與公告`,button(`${icon('refresh')} 完整同步本課`,'sync-one','',`data-id="${esc(c.id)}"`)) +
    panel('課程作業',assignmentRows(data.assignments)) +
    panel('章節與教材',`<div class="panel-body">${snapshot(s.contents)}${(s.contents.data || []).map(section => `<details><summary>${esc(section.name)}</summary><p class="pre-wrap muted">${esc(section.summary || '')}</p>${(section.modules || []).map(m => `<div class="file-item"><div><h3>${esc(m.name)}</h3><p class="muted pre-wrap">${esc(m.description || '')}</p>${externalLink(m.url,'官方頁面')}</div></div>`).join('')}</details>`).join('') || empty('尚無教材章節','按「完整同步本課」讀取 Moodle 教材。')}
      ${data.materials.length ? `<h3 class="section-heading">教材原檔</h3>${data.materials.map(m => `<div class="file-item"><span>${esc(m.name)}</span>${m.path ? `<a class="text-link" href="/api/materials/${id(m.id)}/file" download>下載原檔 ↗</a>` : button('核准下載到本機','download-material','small',`data-id="${esc(m.id)}"`)}</div>`).join('')}` : ''}</div>`) +
    panel('課程公告',`<div class="panel-body">${snapshot(s.announcements)}${(s.announcements.data || []).map(p => `<details><summary>${esc(p.title)}</summary><p class="pre-wrap muted">${esc(p.message)}</p>${externalLink(p.url,'查看原公告')}</details>`).join('') || empty('尚無公告')}</div>`) +
    panel('Moodle 評分',`<div class="panel-body">${snapshot(s.grades)}<p class="snapshot">正式校務成績另列於學習資料，Moodle 評分不代表正式學期成績。</p></div>${table(['評分項目','分數','範圍','百分比'],(s.grades.data || []).map(g => [g.name,g.grade ?? '未提供',g.range,g.percentage]))}`) +
    panel('本課成果',taskRows(data.tasks)) + `<a class="text-link" href="#courses">← 返回所有課程</a>`;
}
function taskRows(tasks) {
  return tasks.map(t => `<button class="assignment-row" data-action="open-task" data-id="${esc(t.id)}"><span class="row-icon">${icon('file')}</span><span class="row-copy"><strong>${esc(t.title)}</strong><small>${esc(t.course_name || '')} · ${date(t.created_at)}</small></span>${badge(taskStatus(t),t.status === 'completed' ? '' : 'amber')}</button>`).join('') || empty('還沒有任務紀錄','從作業工作台開始，成果與中斷的任務都會保留在這裡。',button('開始處理作業','work','primary'));
}
function reportRows(s) {
  return (s?.data?.sections || []).flatMap(section => [
    ...(section.fields || []).map(f => [section.title,f.label,f.value,f.detail || '']),
    ...(section.subjects || []).map(subject => [section.title,subject.title,subject.grade || '',subject.credits ? `學分：${subject.credits}；類別：${subject.category || ''}` : (subject.details || []).join(' / ')]),
    ...(section.notes || []).map(note => [section.title,'官方說明',note,''])]);
}
function hubView() {
  const data = hubData, s = data.sis[hubTab];
  const rows = hubTab === 'timetable' ? (s.data?.courses || []).flatMap(c => (c.meetings?.length ? c.meetings : [{}]).map(m => [c.name,c.teacher || '未提供',m.weekday ? '星期' + '一二三四五六日'[m.weekday - 1] : '未提供',m.period || '未提供',m.classroom || '未提供',c.semester])) : reportRows(s);
  const names = {timetable:'正式課表',grades:'正式成績',rank:'官方排名',graduation:'畢業進度'};
  return heading('我的學習資料','課表、成績與學習進度，集中在這裡。',button(`${icon('refresh')} 同步校務資料`,'sync-sis')) +
    `<p class="notice">校務資料保存在本機，不會自動傳送給 AI。請使用與 Moodle 相同的本人帳號登入。</p>
    <div class="tabs" aria-label="校務資料類別">${Object.entries(names).map(([k,v]) => `<button data-action="hub-tab" data-tab="${k}" class="${hubTab === k ? 'active' : ''}" aria-pressed="${hubTab === k}">${v}</button>`).join('')}</div>
    ${panel(names[hubTab],`<div class="panel-body">${snapshot(s)}${s.data?.summary_issues?.length ? `<p class="notice warning">未讀取的學分摘要：${esc(s.data.summary_issues.join('、'))}</p>` : ''}</div>${table(hubTab === 'timetable' ? ['課程','教師','星期','節次','教室','學期'] : ['學期／分類','科目／項目','官方數值','說明'],rows)}`)}
    ${panel('同步 Moodle 學習資料',`<div class="panel-body"><p class="muted">選擇課程，一次同步作業、教材、公告與評分。</p><div class="form-grid"><label>指定學期（選填）<input id="semester" maxlength="20" placeholder="例如：115-1；留白為目前修課"></label><div class="button-row">${button('列出可同步課程','discover-courses')}</div></div><div id="sync-course-list">${(discovered.length ? discovered : courses).map(c => `<label class="check"><input type="checkbox" name="sync-course" value="${esc(c.id)}"><span>${esc(c.name)}<small>${esc(c.semester || '學期未提供')}</small></span></label>`).join('') || '<p class="muted">尚無課程，請先列出可同步課程。</p>'}</div>${button('同步選定課程','sync-selected','primary')}</div>`)}
    <section class="panel form-panel"><details><summary>校務與 Moodle 課程對應</summary><p class="muted">有歧義時，請核對同學期課程後確認。</p>${data.mappings.map((m,i) => `<div class="file-item"><div><strong>${esc(m.name)}</strong><small class="muted"> · ${esc(m.semester)}</small><p class="snapshot">${esc({matched:'已自動比對',confirmed:'已人工確認',needs_confirmation:'需人工確認',unmatched:'沒有對應課程'}[m.status] || m.status)}</p></div><div>${m.candidates.length ? `<label>對應 Moodle 課程<select id="mapping-${i}">${option('','請選擇',m.moodle_id)}${m.candidates.map(c => option(c.id,c.name,m.moodle_id)).join('')}</select></label>${button('確認對應','confirm-mapping','small',`data-id="${esc(m.sis_id)}" data-index="${i}"`)}` : ''}</div></div>`).join('') || empty('尚無課程對應','同步校務課表後即可核對。')}</details></section>`;
}
function searchView() {
  const r = searchResults;
  return heading('找到需要的資料','搜尋本機課程、作業、教材與已下載文件，保留來源頁／節。') +
    `<form id="search-form" class="search-form"><label>關鍵字<input id="search-query" name="q" maxlength="200" placeholder="搜尋作業名稱、教材內容…" value="${esc(searchFields.q)}"></label><label>課程<select name="course_id">${option('','全部課程',searchFields.course_id)}${courses.map(c => option(c.id,c.name,searchFields.course_id)).join('')}</select></label><label>截止日起<input name="due_from" type="date" value="${esc(searchFields.due_from)}"></label><label>截止日迄<input name="due_to" type="date" value="${esc(searchFields.due_to)}"></label><button class="button primary" type="submit">${icon('search')} 搜尋</button></form>
    ${r ? panel(`搜尋結果 <span class="count">${r.results.length}</span>`,r.results.map((hit,i) => `<article class="result-card"><h3>${esc(hit.title)} ${badge({course:'課程',assignment:'作業',asset:'附件',material:'教材',task:'任務'}[hit.kind] || hit.kind,'gray')}</h3><p>${esc(hit.snippet)}</p><p class="snapshot">來源頁／節：${esc(hit.page || hit.section || '—')} · 截止：${date(hit.due_at)}</p>${button('開啟來源','open-search-result','small',`data-index="${i}"`)}</article>`).join('') || empty('沒有找到符合的資料','試試其他關鍵字，或縮小課程與日期範圍。')) + `<p class="snapshot">索引更新：${date(r.index.indexed_at)}${r.truncated ? ' · 僅顯示前 100 筆，請縮小範圍。' : ''}</p>${r.index.issues.length ? `<details><summary>搜尋範圍與解析限制（${r.index.issues.length}）</summary>${r.index.issues.map(i => `<p class="snapshot">${esc(i.name)}：${esc(i.code)}</p>`).join('')}</details>` : ''}` : `<section class="panel">${empty('輸入關鍵字，從這裡開始','未下載文件僅搜尋檔名；CSV 內容搜尋涵蓋欄位與前五筆。')}</section>`}`;
}
async function refreshData() {
  try { dashboard = await api('/dashboard'); courses = dashboard.courses; loggedIn = true; }
  catch (error) { if (error.code !== 'MOODLE_LOGIN_REQUIRED') throw error; loggedIn = false; courses = []; dashboard = null; }
  $('#connection-summary').textContent = loggedIn ? 'Moodle 本機資料可用' : '管理學校帳號與 AI';
}
async function render() {
  if (!booted) return;
  const version = ++routeVersion;
  const parts = location.hash.slice(1).split('/'); route = labels[parts[0]] ? parts[0] : 'home';
  try { routeRef = decodeURIComponent(parts.slice(1).join('/')); } catch { routeRef = ''; }
  $('#page-label').textContent = labels[route]; document.title = `${labels[route]} · NTPU 學習工作台`;
  document.querySelectorAll('[data-page]').forEach(el => { const selected = el.dataset.page === (route === 'course' ? 'courses' : route); el.classList.toggle('active',selected); if (selected) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current'); });
  content.setAttribute('aria-busy','true'); content.innerHTML = '<div class="loading-state"><span class="spinner"></span><p>正在讀取本機資料…</p></div>';
  try {
    let html;
    if (route === 'home') { await refreshData(); html = homeView(); }
    else if (!loggedIn) html = heading(labels[route],'你的資料保存在本機。') + `<section class="panel">${loginEmpty()}</section>`;
    else if (route === 'courses') html = heading('我的課程','課程內容、作業與成果，都有自己的位置。',button(`${icon('refresh')} 同步課程`,'sync-courses')) + (courses.length ? courseCards(courses) : `<section class="panel">${empty('尚未同步課程','按右上角「同步課程」取得目前修課清單。')}</section>`);
    else if (route === 'course') html = courseView(await api(`/learning/courses/${id(routeRef)}`));
    else if (route === 'work') { await workflow.load(); html = workflow.html(); }
    else if (route === 'history') html = heading('歷史任務','繼續未完成的工作，或回顧已保存的成果。',button('開始新作業 →','work','primary')) + panel('所有任務',taskRows(await api('/tasks')));
    else if (route === 'hub') { hubData = await api('/hub'); html = hubView(); }
    else if (route === 'search') html = searchView();
    if (version !== routeVersion) return;
    content.innerHTML = html; content.focus({preventScroll:true}); window.scrollTo({top:0,behavior:'instant'});
  } catch (error) {
    if (version !== routeVersion) return;
    content.innerHTML = heading('暫時無法讀取','已保存的資料仍保留，請確認連線後再試一次。') + `<div class="notice error">${esc(errorText(error))}</div><div class="button-row">${button('重新讀取','refresh')}${button('管理連線','connections')}</div>`;
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
  workflow.reset(); searchResults = null; searchFields = {q:'',course_id:'',due_from:'',due_to:''}; discovered = []; deleteSnapshot = null; $('#delete-preview').hidden = true;
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
    case 'refresh': await render(); break;
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
    case 'ai-login': await api('/connections/ntpu-ai/start','POST'); connectionMessage('已開啟專用登入視窗。完成後關閉該視窗，按「恢復所選連線」。'); break;
    case 'logout':
      if (await confirmAction('登出所選服務？','已保存的本機資料會保留。','登出')) {
        await workflow.flush(); await api(`/connections/${id($('#provider').value)}`,'DELETE'); await identityChanged(); await checkConnections(); connectionMessage('已登出所選服務。');
      } break;
    case 'sync-courses': await api('/sync/moodle','POST'); await refreshData(); await render(); notify('課程清單已更新。進入課程可同步作業與教材。'); break;
    case 'filter-assignments': assignmentFilter = el.dataset.filter; content.innerHTML = homeView(); break;
    case 'open-course': navigate('course',el.dataset.id); break;
    case 'open-assignment': await workflow.openAssignment(el.dataset.id); navigate('work'); break;
    case 'open-task': await workflow.openTask(el.dataset.id); navigate('work'); break;
    case 'sync-one': syncResult(await api('/sync/moodle/full','POST',{course_ids:[el.dataset.id]})); await refreshData(); await render(); break;
    case 'download-material':
      await api(`/materials/${id(el.dataset.id)}/download`,'POST',{approved:true}); await render(); notify('教材已下載至本機，可使用原檔連結。'); break;
    case 'hub-tab': hubTab = el.dataset.tab; content.innerHTML = hubView(); break;
    case 'sync-sis': syncResult(await api('/sync/sis','POST')); await render(); break;
    case 'discover-courses': {
      const semester = $('#semester').value.trim(); discovered = await api('/hub/courses/discover' + (semester ? '?semester=' + id(semester) : ''),'POST');
      await refreshData(); content.innerHTML = hubView(); $('#semester').value = semester;
      // An empty discovery result must not silently restore unrelated courses.
      if (!discovered.length) $('#sync-course-list').innerHTML = '<p class="muted">此學期沒有找到可同步的課程。</p>';
      notify(`找到 ${discovered.length} 門課程，請勾選同步範圍。`); break;
    }
    case 'sync-selected': {
      const selected = [...document.querySelectorAll('[name="sync-course"]:checked')].map(c => c.value);
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
document.addEventListener('input',event => workflow.input(event.target));
document.addEventListener('change',event => {
  if (event.target.id === 'provider') providerFields();
  else void run(event.target,() => workflow.change(event.target));
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
window.addEventListener('hashchange',() => { void workflow.flush().then(render).catch(error => notify(errorText(error),true)); });
document.addEventListener('visibilitychange',() => { if (document.visibilityState === 'hidden') void workflow.flush().catch(() => {}); });
async function boot() {
  try { await refreshData(); }
  catch (error) { booted = true; await render(); notify(errorText(error),true); return; }
  booted = true; await render();
}
void boot();
