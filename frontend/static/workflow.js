import {esc,id,icon,button,empty,date,heading,option,externalLink,markdown,errorText} from './ui.js';

const steps = {select:'選作業',sources:'確認資料',generate:'執行',review:'審閱與輸出'};
const guidedModes = {
  summary: '摘要題目',
  plan: '提出解題規劃',
  report_draft: '產生報告草稿',
  code_solution: '程式解題',
};
const researchProviders = {crossref:'Crossref',europepmc:'Europe PMC',combined:'Crossref 與 Europe PMC'};
const active = task => ['running','planning','exporting'].includes(task?.status);
const finished = task => Boolean(task?.draft && ['waiting_review','completed'].includes(task.status));
const recoverable = task => ['partial','failed','cancelled','blocked_auth'].includes(task?.status);
const needsAI = task => task?.status === 'blocked_auth' || task?.error === 'AI_CONNECTION_PENDING';
const canResume = task => task?.status === 'waiting_approval' ||
  (recoverable(task) && Boolean(task.plan && task.request_sha256) && !task.resume_unavailable);
const formats = {pdf:'PDF',docx:'Word',zip:'ZIP',markdown:'Markdown',manifest:'來源紀錄'};
const restartReasons = {
  TASK_DEADLINE_EXCEEDED: '原任務已超過時限，請重新執行。',
  RETRY_BUDGET_EXCEEDED: '原步驟已用完重試次數，請重新執行。',
  STEP_BUDGET_EXCEEDED: '原任務已達執行上限，請重新執行。',
  REQUEST_BUDGET_EXCEEDED: '原任務已用完請求次數，請重新執行。',
  PROMPT_BUDGET_EXCEEDED: '原任務已達資料傳送上限，請重新執行。',
  ASSET_CHANGED: '題目或附件已變更。請重新確認資料後再執行。',
  PLAN_CHANGED: '原任務的執行計畫已變更，請重新執行。',
};
const interruptionMessages = {
  AI_TIMEOUT: '等待 AI 回覆逾時。',
  TOOL_TIMEOUT: '處理作業資料逾時。',
  AI_AUTH_REQUIRED: '登入 NTPU AI 後再繼續執行。',
  AI_CONNECTION_PENDING: 'NTPU AI 連線尚未就緒，稍後可再試。',
};
const defaults = () => ({
  course_id:null,assignment_id:null,selected_asset_ids:[],task_id:null,page:'select',mode:'report_draft',
  instruction:'',excerpt:'',requirements:'',editor:'',
  research_query:'',research_provider:'crossref',research_full_text:false,
});

function researchSettings(request) {
  if (request.mode === 'complete_assignment' || !request.allow_external_research) return {};
  const {research_query = '', research_provider = 'crossref', research_full_text = false} = request.agent || {};
  return {research_query,research_provider,research_full_text};
}

function researchScope(request) {
  if (!request.allow_external_research) return '';
  if (request.mode === 'complete_assignment') return '取得公開資料集';
  const {research_query,research_provider,research_full_text} = researchSettings(request);
  const excerpts = research_provider !== 'crossref' && research_full_text ? '及可取得的 Europe PMC 開放全文節錄' : '';
  return `向 ${researchProviders[research_provider]} 查詢「${research_query.trim()}」，取得書目與摘要${excerpts}`;
}

export function createWorkflow(ctx) {
  let value = defaults(), assignment = null, assignments = [], task = null;
  let initialized = false, runStyle = 'guided', approved = false, visual = false, reviewed = false;
  let dirty = false, timer, saveTimer, epoch = 0, pollBusy = false, saveQueue = Promise.resolve(), saveError = null;
  let retryRequest = null, sourceError = '', recoveryError = '', outputError = '', pollError = false;
  let connectionRequired = false;
  const endpoint = suffix => `/tasks/${id(value.task_id)}${suffix}`;
  const repaint = (focus = false) => ctx.repaintWork(focus);
  const clearApproval = () => { approved = false; reviewed = false; };
  const clearMessages = () => {
    sourceError = ''; recoveryError = ''; outputError = ''; pollError = false; connectionRequired = false;
  };
  function reset() {
    epoch++; clearTimeout(timer); clearTimeout(saveTimer);
    value = defaults(); assignment = null; assignments = []; task = null;
    initialized = false; runStyle = 'guided'; dirty = false; visual = false; clearApproval(); retryRequest = null; saveError = null;
    clearMessages();
  }
  function payload() { return {...value,selected_asset_ids:[...value.selected_asset_ids]}; }
  function save() {
    clearTimeout(saveTimer);
    if (!value.assignment_id) return Promise.resolve();
    const data = payload(), version = epoch;
    saveQueue = saveQueue.catch(() => {}).then(async () => {
      if (version !== epoch) return;
      try { await ctx.api('/workflow/progress','PUT',data); saveError = null; }
      catch (error) { saveError = error; ctx.notify('進度尚未保存：' + errorText(error),true); }
    });
    return saveQueue;
  }
  async function flush() { await save(); if (saveError) throw saveError; }
  function scheduleSave() { clearTimeout(saveTimer); saveTimer = setTimeout(save,650); }
  async function load() {
    if (initialized) return;
    const saved = await ctx.api('/workflow/progress');
    if (saved) {
      value = {...defaults(),...Object.fromEntries(Object.keys(defaults()).map(k => [k,saved[k] ?? defaults()[k]]))};
      if (value.page === 'configure') value.page = 'sources';
      assignment = await ctx.api(`/assignments/${id(value.assignment_id)}`);
      assignments = await ctx.api(`/courses/${id(value.course_id)}/assignments`);
      if (value.task_id) {
        task = await ctx.api(endpoint(''));
        runStyle = task.request.mode === 'complete_assignment' ? 'complete' : 'guided';
        visual = Boolean(task.request.allow_visual_transfer);
        if (saved.research_query === undefined) Object.assign(value,researchSettings(task.request));
        if (active(task) || (task.status === 'waiting_approval' && saved.page !== 'sources')) {
          value.page = 'generate';
        } else if (runStyle === 'complete' && value.page === 'sources') {
          value.page = finished(task) ? 'review' : 'generate';
        }
        dirty = value.editor !== (task.draft || '');
        if (finished(task) && task.error) outputError = exportMessage(task.error);
        if (active(task)) watch();
      }
      clearApproval();
    } else if (ctx.courses().length) {
      value.course_id = ctx.courses()[0].id;
      assignments = await ctx.api(`/courses/${id(value.course_id)}/assignments`);
      if (assignments.length) {
        assignment = await ctx.api(`/assignments/${id(assignments[0].id)}`);
        value.assignment_id = assignment.id;
      }
    }
    initialized = true;
  }
  async function openAssignment(ref) {
    await flush();
    const next = await ctx.api(`/assignments/${id(ref)}`);
    const list = await ctx.api(`/courses/${id(next.course_id)}/assignments`);
    reset(); initialized = true; assignment = next; assignments = list;
    value.course_id = next.course_id; value.assignment_id = ref;
    await save();
  }
  async function openTask(ref) {
    await flush();
    const next = await ctx.api(`/tasks/${id(ref)}`);
    const a = await ctx.api(`/assignments/${id(next.assignment_id)}`);
    const list = await ctx.api(`/courses/${id(a.course_id)}/assignments`);
    reset(); initialized = true; task = next; assignment = a; assignments = list;
    const request = next.request;
    runStyle = request.mode === 'complete_assignment' ? 'complete' : 'guided';
    const selected = runStyle === 'guided'
      ? (request.selected_asset_ids || []).filter(ref => a.assets.some(asset => asset.id === ref))
      : [];
    value = {...defaults(),course_id:a.course_id,assignment_id:a.id,task_id:next.id,
      selected_asset_ids:selected,mode:Object.hasOwn(guidedModes, request.mode) ? request.mode : 'report_draft',
      instruction:request.instruction || '',excerpt:request.excerpt || '',requirements:request.requirements || '',editor:next.draft || '',
      page:finished(next) ? 'review' : 'generate',...researchSettings(request)};
    visual = Boolean(request.allow_visual_transfer);
    if (finished(next) && next.error) outputError = exportMessage(next.error);
    if (active(next)) watch();
    await save();
  }
  function summary() {
    if (!assignment) return '';
    const course = ctx.courses().find(c => c.id === value.course_id)?.name;
    return `<div class="work-summary">
      <strong>${esc(assignment.title)}</strong>
      <span class="work-status-meta">${esc(course || '')}${assignment.due_at ? ` · 截止 ${date(assignment.due_at)}` : ''}${assignment.stale ? ' · 待同步' : ''}</span>
      ${assignment.source_url ? externalLink(assignment.source_url,'Moodle 題目') : ''}
    </div>`;
  }
  function sourceFiles() {
    return (assignment?.assets || []).map(a => `<div class="file-item"><label class="check"><input type="checkbox" data-asset="${esc(a.id)}" ${value.selected_asset_ids.includes(a.id) ? 'checked' : ''}><span>${esc(a.name)}<small>${a.path ? '已下載至本機' : '尚未下載'}${a.flags?.length ? ' · ' + esc(a.flags.join('、')) : ''}</small></span></label>${a.path ? `<a class="text-link" href="/api/assets/${id(a.id)}/preview" download>原檔 ↗</a>` : ''}</div>`).join('') || '<p class="muted">這份作業沒有附件，可直接讀取題目文字。</p>';
  }
  function selectView() {
    return `<h2>選擇作業</h2>
      <div class="form-grid"><label>課程<select id="work-course">${option('','請選擇課程',value.course_id)}${ctx.courses().map(c => option(c.id,c.name,value.course_id)).join('')}</select></label><label>作業<select id="work-assignment">${option('','請選擇作業',value.assignment_id)}${assignments.map(a => option(a.id,a.title,value.assignment_id)).join('')}</select></label></div>
      ${!assignments.length ? '<p class="work-mode-note">這門課還沒有作業，可以先同步。</p>' : ''}
      ${assignment?.description ? `<details><summary>查看題目</summary><div class="assignment-description">${esc(assignment.description)}</div></details>` : ''}
      <div class="work-choice" role="group" aria-label="完成方式">
        <label class="mode-option"><input type="radio" name="run-style" value="guided" ${runStyle === 'guided' ? 'checked' : ''}><span><strong>一步一步完成</strong><small>先確認題目與附件，再開始執行。</small></span></label>
        <label class="mode-option"><input type="radio" name="run-style" value="complete" ${runStyle === 'complete' ? 'checked' : ''}><span><strong>直接完成</strong><small>自動讀取題目與附件，完成後審閱。</small></span></label>
      </div>
      <div class="button-row between">
        ${button('同步作業','work-sync','small',!value.course_id ? 'disabled' : '')}
        ${button(runStyle === 'complete' ? '開始直接完成' : '下一步：確認資料',runStyle === 'complete' ? 'work-start' : 'work-next','primary',!assignment ? 'disabled' : '')}
      </div>`;
  }
  function sourcesView() {
    const files = assignment?.assets || [];
    const hasExcerpt = Boolean(value.excerpt.trim());
    return `<h2>確認資料</h2>
      <p>${hasExcerpt ? '題目已準備好。需要附件時，勾選後讀取即可。' : '讀取題目與附件，或貼上題目文字後開始。'}</p>
      ${files.length ? `<details ${hasExcerpt ? '' : 'open'}><summary>附件（已選 ${value.selected_asset_ids.length} 份）</summary>
        ${sourceFiles()}${button('下載並讀取資料','work-parse','small')}
      </details>` : !hasExcerpt && assignment?.description ? button('讀取題目','work-parse','small') : ''}
      ${sourceError ? `<p class="notice warning" role="alert">${esc(sourceError)}</p>` : ''}
      <details ${hasExcerpt ? '' : 'open'}>
        <summary>題目與即將傳送的文字</summary>
        <label>題目文字與節錄<textarea id="work-excerpt" rows="6" maxlength="24000" placeholder="貼上題目或讀取附件…">${esc(value.excerpt)}</textarea></label>
        <label>題目要求<textarea id="work-requirements" rows="3" maxlength="6000">${esc(value.requirements)}</textarea></label>
      </details>
      <details ${value.research_query ? 'open' : ''}><summary>補充設定（選填）</summary>
        <label>處理方式<select id="work-mode">${Object.entries(guidedModes).map(([k,v]) => option(k,v,value.mode)).join('')}</select></label>
        <label>補充說明<textarea id="work-instruction" maxlength="4000" rows="3">${esc(value.instruction)}</textarea></label>
        <label class="check"><input id="work-visual" type="checkbox" ${visual ? 'checked' : ''}><span>允許傳送附件中的圖表與公式影像</span></label>
        <details ${value.research_query ? 'open' : ''}><summary>公開文獻查詢</summary>
          <label>公開主題或 DOI<input id="work-research-query" type="text" maxlength="300" value="${esc(value.research_query)}"></label>
          <label>查詢來源<select id="work-research-provider">${Object.entries(researchProviders).map(([key,label]) => option(key,label,value.research_provider)).join('')}</select></label>
          <div id="work-research-full-text-option" ${value.research_provider === 'crossref' ? 'hidden' : ''}>
            <label class="check"><input id="work-research-full-text" type="checkbox" ${value.research_full_text ? 'checked' : ''}><span>同時取得可用的 Europe PMC 開放全文節錄</span></label>
          </div>
          <p class="muted">填寫查詢後，開始執行時會先請你確認要送往所選來源的內容。</p>
        </details>
      </details>
      <label class="check"><input id="work-approve" type="checkbox" ${approved ? 'checked' : ''}><span>我已核對題目與選定資料，同意傳送至 NTPU AI</span></label>
      <div class="button-row between">${button('返回選作業','work-prev')}${button('開始執行','work-start','primary',!approved || !hasExcerpt ? 'disabled' : '')}</div>`;
  }
  function generationView() {
    if (!task) return empty('尚未開始','先選作業與完成方式。',button('選擇作業','work-configure','primary'));
    const steps = task.plan?.steps || [], cursor = task.checkpoint?.cursor || 0;
    if (active(task)) {
      return `<div class="work-primary-status">
        <h2>正在執行作業</h2>
        <p class="work-status-meta">${esc(steps[cursor]?.objective || '正在準備成果')}${steps.length ? ` · ${cursor}／${steps.length} 已完成` : ''}</p>
        ${steps.length ? `<progress class="progress-track" max="${steps.length}" value="${cursor}" aria-label="作業執行進度"></progress>` : ''}
        ${task.checkpoint?.consecutive_errors ? '<p class="work-mode-note">正在自動重試目前的步驟。</p>' : ''}
      </div>
      ${pollError ? `<div class="work-recovery"><p>暫時無法取得進度，已完成的步驟會保留。</p>${button('重新讀取狀態','work-refresh','small')}</div>` : ''}
      <div class="button-row end">${button('停止執行','work-cancel','small')}</div>`;
    }
    if (finished(task)) return button('前往審閱與輸出','work-review','primary');

    const waiting = task.status === 'waiting_approval';
    const resume = canResume(task);
    const reconnect = needsAI(task) || connectionRequired;
    const reason = restartReasons[task.resume_unavailable] || (resume
      ? (waiting ? '確認後即可開始原任務。' : '從中斷點繼續會沿用已完成的步驟。')
      : '這份任務無法續跑，可以重新執行。');
    return `<div class="work-primary-status">
      <h2>${esc(reconnect ? '需要重新連線' : waiting ? '等待確認' : task.status === 'cancelled' ? '已停止執行' : '作業執行中斷')}</h2>
      <p class="work-status-meta">${steps.length ? `已完成 ${cursor}／${steps.length} 步。 ` : ''}${esc(reason)}</p>
      ${waiting && task.request.allow_external_research ? `<p class="work-mode-note">${esc(researchScope(task.request))}。</p>` : ''}
      ${task.error && task.error !== task.resume_unavailable && task.error !== 'CANCELLED'
        ? `<p class="work-mode-note">${esc(interruptionMessages[task.error] || errorText({code:task.error}))}</p>` : ''}
    </div>
    <div class="work-recovery">
      ${recoveryError ? `<p class="notice warning" role="alert">${esc(recoveryError)}</p>` : ''}
      <div class="button-row">
        ${resume ? button(waiting ? '確認並開始' : '從中斷點繼續','work-resume','primary') : ''}
        ${waiting && task.request.allow_external_research && runStyle === 'guided'
          ? button('返回修改資料','work-next') : button('重新執行','work-regenerate',resume ? '' : 'primary')}
        ${reconnect ? button('登入 NTPU AI','connections','small','data-provider="ntpu-ai"') : ''}
      </div>
      ${task.draft ? `<div class="button-row">${button('查看已保留的草稿','work-review','small')}</div>` : ''}
    </div>`;
  }
  function downloads() {
    if (!task || dirty) return '';
    const kinds = [...(task.pdf ? ['pdf','manifest'] : []),...(task.draft ? ['markdown'] : []),...Object.keys(task.exports || {})];
    return kinds.map(k => `<a class="button small" href="/api/artifacts/${id(task.id)}/download?kind=${id(k)}" download>${icon('download')} 下載 ${formats[k] || esc(k)}</a>`).join('') + Object.keys(task.generated_files || {}).map(name => `<a class="button small" href="/api/tasks/${id(task.id)}/artifacts/${id(name)}" download>${icon('download')} ${esc(name)}</a>`).join('');
  }
  function researchResults() {
    const step = task?.plan?.steps?.find(step => step.tool === 'web_research');
    const result = step && task.tool_results?.[step.id];
    if (!result) return '';
    const sources = result.sources || [];
    const issues = (result.issues || []).map(({provider,code}) => `${provider}：${errorText({code})}`).join('、');
    return `<details><summary>查得的公開文獻（${sources.length} 筆）</summary>
      ${issues ? `<p class="notice warning">${esc(issues)}</p>` : ''}
      ${sources.length ? `<ul>${sources.map(source => {
        const scope = source.content_scope === 'open_access_full_text_excerpt' ? '開放全文節錄'
          : source.abstract ? '書目與摘要' : '書目';
        return `<li class="pre-wrap">${externalLink(source.url,source.title) || esc(source.title)} <small class="muted">${esc(source.provider)} · ${scope}${source.doi ? ` · DOI ${esc(source.doi)}` : ''}</small></li>`;
      }).join('')}</ul>` : '<p class="muted">這次查詢沒有取得文獻。</p>'}
    </details>`;
  }
  function reviewView() {
    const coverage = (task?.coverage || []).filter(item => item.deliverable_id);
    const pending = coverage.filter(item => item.status === 'needs_action');
    const unitNotices = pending.filter(item => item.reasons?.length);
    const explained = new Set(unitNotices.flatMap(item =>
      item.reasons.map(reason => `${item.title || item.requirement} — ${reason}`)));
    const actions = [...new Set([
      ...(task?.completion?.required_actions || []),
      ...(task?.completion?.blockers || []),
      ...(task?.blockers || []),
    ])].filter(item => item !== 'RUBRIC_REVIEW_REQUIRED' && !explained.has(item));
    const unitNoticeItems = unitNotices.map(item => {
      const reasons = item.reasons.map(reason => `<li>${esc(reason)}</li>`).join('');
      return `<li><strong>${esc(item.title || item.requirement)}</strong><ul>${reasons}</ul></li>`;
    }).join('');
    const notice = actions.length || unitNotices.length ? `<div class="notice warning"><strong>尚待處理</strong><ul>
      ${unitNoticeItems}
      ${actions.map(a => `<li>${esc(a === task?.error ? errorText({code:a}) : a)}</li>`).join('')}
      </ul></div>` : '';
    if (recoverable(task)) {
      return `<h2>已保留的部分草稿</h2>
        <p>作業尚未完成。可以先下載草稿，再返回執行狀態。</p>
        ${notice}
        <article class="markdown-preview" aria-label="草稿預覽">${markdown(value.editor)}</article>
        ${researchResults()}
        <div class="work-export-list">${downloads()}</div>
        <div class="button-row">${button('返回執行狀態','work-progress','primary')}</div>`;
    }
    const deliverableStatus = {verified:'已核對執行與輸出',ready_for_review:'草稿待審閱',needs_action:'需處理待辦'};
    const omitted = Object.entries(task?.exports?.zip?.omitted_exports || {});
    const exportButton = (kind,style = '') => button(`匯出 ${formats[kind]}`,'work-export',style,`data-kind="${kind}" ${!reviewed ? 'disabled' : ''}`);
    return `<h2>審閱與輸出</h2>
      <p>核對成果後即可匯出。需要修正時，可直接編輯草稿。</p>
      ${coverage.length ? `<ul aria-label="成果進度">${coverage.map(item => `<li>${esc(item.title || item.requirement)} · ${esc(deliverableStatus[item.status] || item.status)}</li>`).join('')}</ul>` : ''}
      ${notice}
      <article class="markdown-preview" id="draft-preview" aria-label="草稿預覽">${markdown(value.editor)}</article>
      ${researchResults()}
      <details class="work-editor-details" ${dirty ? 'open' : ''}><summary>編輯草稿</summary>
        <label>草稿編輯 · Markdown<textarea id="work-editor" rows="10" maxlength="80000" spellcheck="false">${esc(value.editor)}</textarea></label>
        <div class="button-row">${button('保存修改','work-save','small')}<span class="work-status-meta" id="draft-save-state">${dirty ? '修改將在匯出時一併保存' : '草稿已保存'}</span></div>
      </details>
      <label class="check"><input id="work-reviewed" type="checkbox" ${reviewed ? 'checked' : ''}><span>${actions.length || pending.length ? '我已審閱內容與待處理事項，同意輸出目前成果' : '我已核對題目、內容與來源，可以匯出'}</span></label>
      ${outputError ? `<p class="notice warning" role="alert">${esc(outputError)}</p>` : ''}
      <div class="button-row">${exportButton('pdf','primary')}</div>
      <details ${outputError ? 'open' : ''}><summary>其他格式</summary>
        <div class="button-row">${exportButton('docx')}${exportButton('zip')}</div>
        <label class="check"><input id="include-sources" type="checkbox"><span>ZIP 也包含原始題目與附件</span></label>
      </details>
      <div class="work-export-list" id="work-downloads">${downloads()}</div>
      ${omitted.length && !dirty ? `<p class="notice warning">ZIP 未包含的格式：${omitted.map(([kind,code]) => `${esc(kind.toUpperCase())}（${esc(code)}）`).join('、')}。其他可用成果仍可下載。</p>` : ''}
      <div class="button-row">${button('重新執行','work-regenerate','small')}</div>`;
  }
  function html() {
    const pages = Object.keys(steps).filter(page => runStyle === 'guided' || page !== 'sources');
    const index = pages.indexOf(value.page);
    const body = ({select:selectView,sources:sourcesView,generate:generationView,review:reviewView}[value.page] || selectView)();
    return heading('作業工作台','選作業、執行、審閱與輸出。',button('歷史任務','history','small')) +
      `<ol class="steps" aria-label="作業進度">${pages.map((page,i) => `<li class="${i === index ? 'current' : i < index ? 'done' : ''}" ${i === index ? 'aria-current="step"' : ''}><span class="step-number">${i < index ? '✓' : i + 1}</span>${steps[page]}</li>`).join('')}</ol>
      ${ctx.courses().length ? `<section class="panel form-panel">${value.page === 'select' ? '' : summary()}${body}</section>` : empty('先連結你的課程','登入 Moodle 並同步課程後，就可以開始處理作業。',button('管理連線','connections','primary'))}`;
  }
  async function go(page, sourceMessage = '') {
    value.page = page;
    clearApproval();
    clearMessages();
    sourceError = sourceMessage;
    await save();
    repaint(true);
  }
  async function parse(silent = false) {
    if (!assignment) throw new Error('請先選擇作業。');
    if (value.selected_asset_ids.some(ref => !assignment.assets.find(a => a.id === ref)?.path)) {
      await ctx.api(`/assignments/${id(assignment.id)}/download`,'POST',{selected_asset_ids:value.selected_asset_ids,approved:true});
    }
    const parsed = await ctx.api(`/assignments/${id(assignment.id)}/parse`,'POST',{selected_asset_ids:value.selected_asset_ids});
    assignment = await ctx.api(`/assignments/${id(assignment.id)}`);
    value.excerpt = parsed.excerpt;
    value.requirements = parsed.requirements.map(r => {
      const source = typeof r.source === 'object' ? `${r.source.file_id} / 頁或節 ${r.source.page ?? r.source.section ?? '?'}` : r.source;
      return `[ ] ${r.id} ${r.text}（來源：${source}）`;
    }).join('\n').slice(0,6000);
    clearApproval();
    sourceError = parsed.truncated ? '節錄已達長度上限，請核對題目內容。' : '';
    if (!silent) { await save(); repaint(); }
  }
  async function prepareSources() {
    let message = '';
    if (!value.excerpt.trim() && !value.selected_asset_ids.length && assignment?.description?.trim()) {
      try {
        await parse(true);
        message = sourceError;
      } catch (error) { message = errorText(error); }
    }
    await go('sources', message);
  }
  async function readyToRetry() {
    const nextTool = task?.plan?.steps?.[task.checkpoint?.cursor]?.tool;
    if (!needsAI(task) && !['generate_report','plan_completion'].includes(nextTool)) return true;
    const result = await ctx.api('/connections/ntpu-ai/restore','POST');
    if (result.ready) return true;
    connectionRequired = true;
    recoveryError = '請先登入 NTPU AI，再按原本的執行按鈕。';
    repaint();
    return false;
  }
  async function start(regenerate = false) {
    if (!assignment) throw new Error('請先選擇作業。');
    if (active(task)) throw new Error('目前任務仍在執行，請先等待或停止。');
    if (!regenerate && runStyle === 'complete') {
      const confirmed = await ctx.confirm('開始直接完成？','這次會將題目與支援的附件傳送至 NTPU AI，並視需要取得公開資料集或在隔離環境執行程式。','同意並開始');
      if (!confirmed) return;
    } else if (!regenerate && !approved) {
      throw new Error('請先核對內容並核准本次傳送。');
    }
    if (runStyle === 'guided' && !value.excerpt.trim()) throw new Error('請先讀取題目文字或附件內容。');
    const query = value.research_query.trim();
    if (runStyle === 'guided' && query && [...query].some(character => character.charCodeAt(0) < 32)) {
      throw new Error('請將公開主題或 DOI 填在同一行。');
    }
    await flush();
    if (regenerate && dirty && value.editor.trim()) {
      task = await ctx.api(endpoint('/draft'),'PUT',{markdown:value.editor,reviewed:false});
      dirty = false;
    }
    const body = runStyle === 'complete' ? {approved:true} : {
      assignment_id:value.assignment_id,selected_asset_ids:value.selected_asset_ids,mode:value.mode,
      instruction:value.instruction,excerpt:value.excerpt,requirements:value.requirements,
      data_transfer_approved:true,allow_visual_transfer:visual,
    };
    if (runStyle === 'guided' && query) {
      body.allow_external_research = true;
      body.agent = {
        research_query:query,
        research_provider:value.research_provider,
        research_full_text:value.research_provider !== 'crossref' && value.research_full_text,
        max_requests:5, // Two provider searches, up to two excerpts, and one AI response.
      };
    }
    const path = runStyle === 'complete' ? `/assignments/${id(value.assignment_id)}/complete` : '/tasks';
    const signature = JSON.stringify([path,body]);
    // Reuse the key after a lost HTTP response, so retrying cannot create a duplicate job.
    if (retryRequest?.signature !== signature) retryRequest = {signature,key:crypto.randomUUID()};
    const next = await ctx.api(path,'POST',body,{'Idempotency-Key':retryRequest.key});
    retryRequest = null; task = next; value.task_id = next.id; value.editor = next.draft || ''; dirty = false;
    await go(finished(next) ? 'review' : 'generate');
    watch();
  }
  async function refreshTask(automatic = false) {
    if (!value.task_id) return;
    const ref = value.task_id, version = epoch;
    const next = await ctx.api(`/tasks/${id(ref)}`);
    if (version !== epoch || value.task_id !== ref) return;
    const wasActive = active(task);
    const changed = pollError || task?.status !== next.status || task?.error !== next.error ||
      task?.checkpoint?.cursor !== next.checkpoint?.cursor ||
      task?.checkpoint?.consecutive_errors !== next.checkpoint?.consecutive_errors;
    task = next;
    pollError = false;
    if (!dirty) value.editor = next.draft || '';
    if (finished(next) && value.page === 'generate') {
      await go('review');
      return;
    }
    if (!automatic || changed) repaint(wasActive && !active(next));
  }
  function watch() {
    clearTimeout(timer);
    if (!active(task)) return;
    const version = epoch;
    timer = setTimeout(async () => {
      if (pollBusy) { watch(); return; }
      pollBusy = true;
      try { await refreshTask(true); }
      catch {
        if (version === epoch && !pollError) { pollError = true; repaint(); }
      }
      finally { pollBusy = false; if (version === epoch && active(task)) watch(); }
    },2500);
  }
  function exportMessage(code) {
    const reason = errorText({code,message:'匯出時發生問題。'});
    return `${reason} 草稿已保留，可以再試一次，或選擇其他格式。`;
  }
  async function resumeTask() {
    if (!canResume(task)) return;
    const request = task.request;
    const waiting = task.status === 'waiting_approval';
    const extras = [
      researchScope(request),
      request.allow_code_execution ? '在隔離環境執行程式' : '',
    ].filter(Boolean);
    const description = (waiting ? '依原任務設定執行並傳送資料至 NTPU AI。' : '沿用已完成的步驟，必要時再次傳送資料至 NTPU AI。') +
      (extras.length ? `這次也核准${extras.join('與')}。` : '');
    if (!await ctx.confirm(waiting ? '確認並開始？' : '從中斷點繼續？',description,'同意並執行')) return;
    try {
      if (!await readyToRetry()) return;
      task = await ctx.api(endpoint(waiting ? '/approve' : '/resume'),'POST',{
        request_sha256:task.request_sha256,
        code_execution_approved:Boolean(request.allow_code_execution),
        external_research_approved:Boolean(request.allow_external_research),
      });
      if (!dirty) value.editor = task.draft || '';
      await go(finished(task) ? 'review' : 'generate');
      watch();
    } catch (error) {
      await refreshTask().catch(() => {});
      if (restartReasons[error.code]) task = {...task,resume_unavailable:error.code};
      recoveryError = restartReasons[error.code] ? '' : errorText(error);
      repaint();
    }
  }
  async function restartTask() {
    if (runStyle === 'guided' && (!value.excerpt.trim() || task?.resume_unavailable === 'ASSET_CHANGED')) {
      if (task?.resume_unavailable === 'ASSET_CHANGED') {
        assignment = await ctx.api(`/assignments/${id(value.assignment_id)}`);
        value.selected_asset_ids = value.selected_asset_ids.filter(ref => assignment.assets.some(asset => asset.id === ref));
        value.excerpt = '';
        value.requirements = '';
      }
      await prepareSources();
      return;
    }
    const scope = runStyle === 'complete'
      ? '會重新讀取題目與附件並送至 NTPU AI，視需要取得公開資料集或在隔離環境執行程式。'
      : '會用目前確認的資料重新傳送至 NTPU AI。' + (value.research_query.trim() ? '公開文獻查詢會另行確認。' : '');
    const preservation = dirty ? '會先保存這次修改。' : '';
    if (!await ctx.confirm('重新執行這份作業？',`${scope} ${preservation}舊草稿可從歷史任務開啟。`,'同意並重新執行')) return;
    try {
      if (await readyToRetry()) await start(true);
    } catch (error) {
      if (value.page === 'generate') { recoveryError = errorText(error); repaint(); }
      else throw error;
    }
  }
  async function exportDraft(kind) {
    if (!reviewed) throw new Error('請先確認審閱。');
    const include = document.querySelector('#include-sources')?.checked || false;
    const accept = Boolean(task.blockers?.length || task.completion?.required_actions?.length ||
      task.completion?.blockers?.length || task.coverage?.some(item => item.status === 'needs_action') ||
      task.accepted_incomplete);
    outputError = '';
    try {
      if (dirty || task.draft !== value.editor || !task.reviewed || Boolean(task.accepted_incomplete) !== accept) {
        task = await ctx.api(endpoint('/draft'),'PUT',{markdown:value.editor,reviewed:true,accept_incomplete:accept});
        dirty = false;
      }
      task = await ctx.api(endpoint(kind === 'pdf' ? '/export' : '/export-extra'),'POST',
        kind === 'pdf' ? undefined : {kind,include_sources:include});
      ctx.notify(`${formats[kind]} 已匯出，可下載檔案。`);
    } catch (error) {
      outputError = exportMessage(error.code);
    } finally { await save(); repaint(); }
  }
  async function action(name,el) {
    if (!name.startsWith('work-')) return false;
    switch (name) {
      case 'work-sync':
        await ctx.api(`/sync/moodle?course_id=${id(value.course_id)}`,'POST');
        assignments = await ctx.api(`/courses/${id(value.course_id)}/assignments`);
        if (value.assignment_id) assignment = await ctx.api(`/assignments/${id(value.assignment_id)}`);
        repaint(); ctx.notify('本課作業已同步。'); break;
      case 'work-next':
        if (!assignment) throw new Error('請先選擇作業。');
        await prepareSources(); break;
      case 'work-prev': await go('select'); break;
      case 'work-parse':
        try { await parse(); }
        catch (error) { sourceError = errorText(error); repaint(); }
        break;
      case 'work-start': await start(); break;
      case 'work-configure': if (!active(task)) await go('select'); break;
      case 'work-review': if (!active(task) && task?.draft) await go('review'); break;
      case 'work-progress': await go('generate'); break;
      case 'work-refresh': await refreshTask(); watch(); break;
      case 'work-cancel':
        if (await ctx.confirm('停止這次任務？','已完成的步驟與草稿會保留，可以稍後繼續。','停止執行')) { await ctx.api(endpoint('/cancel'),'POST'); await refreshTask(); watch(); } break;
      case 'work-resume': await resumeTask(); break;
      case 'work-regenerate': await restartTask(); break;
      case 'work-save':
        task = await ctx.api(endpoint('/draft'),'PUT',{markdown:value.editor,reviewed:false}); dirty = false; clearApproval(); await save(); repaint(); ctx.notify('草稿已保存，匯出前請重新確認審閱。'); break;
      case 'work-export': await exportDraft(el.dataset.kind); break;
      default: return false;
    }
    return true;
  }
  function input(el) {
    const fields = {'work-excerpt':'excerpt','work-requirements':'requirements','work-instruction':'instruction',
      'work-editor':'editor','work-research-query':'research_query'};
    const field = fields[el.id]; if (!field) return;
    value[field] = el.value; clearApproval();
    if (field === 'editor') {
      dirty = true;
      document.querySelector('#draft-preview').innerHTML = markdown(value.editor);
      document.querySelector('#work-downloads').innerHTML = '';
      document.querySelector('#draft-save-state').textContent = '修改將在匯出時一併保存';
    }
    for (const checkbox of document.querySelectorAll('#work-approve,#work-reviewed')) checkbox.checked = false;
    for (const btn of document.querySelectorAll('[data-action="work-export"],[data-action="work-start"]')) btn.disabled = true;
    scheduleSave();
  }
  async function change(el) {
    if (el.id === 'work-course') {
      await flush(); const cid = el.value;
      const list = cid ? await ctx.api(`/courses/${id(cid)}/assignments`) : [];
      const a = list.length ? await ctx.api(`/assignments/${id(list[0].id)}`) : null;
      reset(); initialized = true; value.course_id = cid || null; assignments = list; assignment = a; value.assignment_id = a?.id || null; await save(); repaint();
    } else if (el.id === 'work-assignment') {
      if (el.value) await openAssignment(el.value);
      else { await flush(); const course = value.course_id, list = assignments; reset(); initialized = true; value.course_id = course; assignments = list; }
      repaint();
    } else if (el.name === 'run-style') { runStyle = el.value; clearApproval(); repaint(); }
    else if (el.dataset.asset) { value.selected_asset_ids = [...document.querySelectorAll('[data-asset]:checked')].map(e => e.dataset.asset); value.excerpt = ''; value.requirements = ''; clearApproval(); await save(); repaint(); }
    else if (['work-mode','work-visual','work-research-provider','work-research-full-text'].includes(el.id)) {
      if (el.id === 'work-mode') value.mode = el.value;
      if (el.id === 'work-visual') visual = el.checked;
      if (el.id === 'work-research-provider') {
        value.research_provider = el.value;
        const fullText = document.querySelector('#work-research-full-text');
        const fullTextOption = document.querySelector('#work-research-full-text-option');
        fullTextOption.hidden = el.value === 'crossref';
        if (fullTextOption.hidden) value.research_full_text = fullText.checked = false;
      }
      if (el.id === 'work-research-full-text') value.research_full_text = el.checked;
      approved = false;
      const checkbox = document.querySelector('#work-approve');
      const startButton = document.querySelector('[data-action="work-start"]');
      if (checkbox) checkbox.checked = false;
      if (startButton) startButton.disabled = true;
      if (el.id !== 'work-visual') await save();
    }
    else if (el.id === 'work-approve') { approved = el.checked; const btn = document.querySelector('[data-action="work-start"]'); if (btn) btn.disabled = !approved || !value.excerpt.trim(); }
    else if (el.id === 'work-reviewed') { reviewed = el.checked; document.querySelectorAll('[data-action="work-export"]').forEach(btn => btn.disabled = !reviewed); }
  }
  return {load,html,action,input,change,reset,flush,openAssignment,openTask,hasWork:() => Boolean(value.assignment_id)};
}
