import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkflow} from '../static/workflow.js';

const assignment = {id:'a1',course_id:'c1',title:'Test assignment',assets:[],description:'Compare methods.'};
const draftTask = {id:'t1',assignment_id:'a1',title:'Test draft',status:'waiting_review',draft:'# Saved draft',request:{mode:'report_draft',excerpt:'source',selected_asset_ids:[]},exports:{docx:{path:'report.docx'}}};
function harness(progress = null, overrides = {}) {
  const calls = [], notices = [], nodes = new Map();
  globalThis.document = {
    querySelector: selector => { if (!nodes.has(selector)) nodes.set(selector,{checked:false,innerHTML:'',textContent:'',disabled:false}); return nodes.get(selector); },
    querySelectorAll: () => [],
  };
  const api = async (path,method = 'GET',body,headers) => {
    calls.push({path,method,body:structuredClone(body),headers});
    if (overrides[path]) return overrides[path](method,body,headers);
    if (path === '/workflow/progress') return method === 'GET' ? progress : body;
    if (path === '/assignments/a1') return structuredClone(assignment);
    if (path === '/courses/c1/assignments') return [assignment];
    if (path === '/tasks/t1') return structuredClone(draftTask);
    if (path === '/tasks/t1/draft') return {...draftTask,draft:body.markdown,reviewed:body.reviewed};
    throw new Error('Unexpected call: ' + path);
  };
  const flow = createWorkflow({api,notify:(...args) => notices.push(args),confirm:async () => true,courses:() => [{id:'c1',name:'Course'}],repaintWork:() => {}});
  return {flow,calls,notices,nodes};
}
test('restoring progress keeps unsaved editor content and resets all approvals',async () => {
  const {flow,calls} = harness({course_id:'c1',assignment_id:'a1',task_id:'t1',page:'review',editor:'# My unsaved edit',reviewed:true,data_transfer_approved:true});
  await flow.load(); const html = flow.html();
  assert.match(html,/# My unsaved edit/);
  assert.doesNotMatch(html,/id="work-reviewed"[^>]*checked/);
  assert.doesNotMatch(html,/id="work-incomplete"[^>]*checked/);
  assert.match(html,/data-action="work-export"[^>]*disabled/);
  assert.doesNotMatch(html,/href="\/api\/artifacts/); // old downloads hidden while editor differs
  assert.ok(calls.every(c => c.method === 'GET'));
  flow.reset();
});
test('editing a reviewed draft revokes export approval and hides stale downloads',async () => {
  const {flow,nodes} = harness(); await flow.openTask('t1');
  await flow.change({id:'work-reviewed',checked:true,dataset:{}});
  flow.input({id:'work-editor',value:'# Updated'});
  assert.equal(nodes.get('#work-downloads').innerHTML,'');
  await assert.rejects(flow.action('work-export',{dataset:{kind:'pdf'}}),/先確認審閱/);
  await flow.flush(); flow.reset();
});
test('a lost create response retries the same idempotency key and preserves prior work',async () => {
  let attempts = 0;
  const {flow,calls} = harness({course_id:'c1',assignment_id:'a1',task_id:'t1',page:'configure',excerpt:'source',editor:'# Kept draft'}, {
    '/tasks': () => { if (++attempts === 1) throw new Error('network lost'); return {...draftTask,id:'t2',draft:'# New draft'}; },
  });
  await flow.load(); await flow.change({id:'work-approve',checked:true,dataset:{}});
  await assert.rejects(flow.action('work-start',{}),/network lost/);
  await flow.action('work-start',{});
  const creates = calls.filter(c => c.path === '/tasks');
  assert.equal(creates.length,2);
  assert.equal(creates[0].headers['Idempotency-Key'],creates[1].headers['Idempotency-Key']);
  assert.match(flow.html(),/# New draft/);
  flow.reset();
});
test('parsed requirements preserve human-readable source identifiers',async () => {
  const {flow,calls} = harness(null,{'/assignments/a1/parse': () => ({excerpt:'source',requirements:[{id:'REQ-01',text:'Submit a report',source:{file_id:'assignment-description',section:1}}]})});
  await flow.load(); await flow.action('work-parse',{});
  const saved = calls.filter(c => c.path === '/workflow/progress' && c.method === 'PUT').at(-1).body;
  assert.match(saved.requirements,/assignment-description \/ 頁或節 1/);
  assert.doesNotMatch(saved.requirements,/object Object/);
  flow.reset();
});
test('failed progress save prevents switching assignments and losing the editor',async () => {
  const {flow} = harness(null,{'/workflow/progress': method => { if (method === 'PUT') throw new Error('disk full'); return null; }});
  await flow.load();
  await assert.rejects(flow.openAssignment('a1'),/disk full/);
  assert.match(flow.html(),/Test assignment/);
  flow.reset();
});
test('opening a legacy CSV analysis task preserves the draft and saves a supported editor mode', async () => {
  const legacyTask = {...draftTask, request: {...draftTask.request, mode: 'csv_analysis'}};
  const {flow, calls} = harness(null, {
    '/tasks/t1': () => legacyTask,
    '/workflow/progress': (method, body) => {
      if (method === 'PUT' && body.mode === 'csv_analysis') throw new Error('INVALID_MODE');
      return body;
    },
  });
  await flow.openTask('t1');
  await flow.flush();
  const saved = calls.filter(call => call.path === '/workflow/progress' && call.method === 'PUT').at(-1).body;
  assert.equal(saved.mode, 'report_draft');
  assert.equal(saved.task_id, legacyTask.id);
  assert.equal(saved.editor, legacyTask.draft);
  assert.equal(legacyTask.request.mode, 'csv_analysis');
  assert.match(flow.html(), /# Saved draft/);
  flow.reset();
});
test('exporting another format preserves existing exports of the same reviewed draft',async () => {
  const exported = {...draftTask,reviewed:true,accepted_incomplete:false,pdf:'report.pdf'};
  const {flow,calls} = harness(null,{'/tasks/t1': () => exported,'/tasks/t1/export-extra': () => exported});
  await flow.openTask('t1'); await flow.change({id:'work-reviewed',checked:true,dataset:{}});
  await flow.action('work-export',{dataset:{kind:'docx'}});
  assert.ok(!calls.some(c => c.path === '/tasks/t1/draft'));
  assert.match(flow.html(),/download\?kind=pdf/);
  assert.match(flow.html(),/download\?kind=docx/);
  flow.reset();
});

const pausedTask = {
  ...draftTask,
  status:'partial',
  error:'AI_TIMEOUT',
  request_sha256:'original-snapshot',
  plan:{steps:[{objective:'Read sources'},{objective:'Generate draft'}]},
  checkpoint:{cursor:1},
  resume_unavailable:null,
};

test('a stopped task opens recovery and resumes its original checkpoint and permissions', async () => {
  const paused = {...pausedTask,request:{...pausedTask.request,allow_code_execution:true}};
  let resumed = false;
  const {flow,calls} = harness(null,{
    '/tasks/t1': () => resumed ? {...draftTask,draft:'# Finished after resume'} : paused,
    '/tasks/t1/resume': () => {
      resumed = true;
      return {...paused,status:'running',error:null};
    },
  });
  await flow.openTask('t1');
  assert.match(flow.html(), /從中斷點繼續/);
  assert.doesNotMatch(flow.html(), /data-action="work-export"/);
  await flow.action('work-review',{});
  assert.match(flow.html(), /已保留的部分草稿/);
  assert.doesNotMatch(flow.html(), /data-action="work-export"/);
  await flow.action('work-progress',{});
  await flow.action('work-resume',{});
  const resume = calls.find(call => call.path.endsWith('/resume'));
  assert.equal(resume.path,'/tasks/t1/resume');
  assert.deepEqual(resume.body,{
    request_sha256:'original-snapshot',code_execution_approved:true,external_research_approved:false,
  });
  assert.ok(!calls.some(call => call.path === '/tasks'));
  await flow.action('work-refresh',{});
  assert.match(flow.html(), /Finished after resume/);
  assert.match(flow.html(), /data-action="work-export"/);
  flow.reset();
});

test('a disconnected AI cannot consume the last retry at a model checkpoint', async () => {
  const paused = {...pausedTask,plan:{steps:[{objective:'Read sources'},{tool:'generate_report',objective:'Generate draft'}]}};
  const {flow,calls} = harness(null,{
    '/tasks/t1': () => paused,
    '/connections/ntpu-ai/restore': () => ({ready:false,authentication:'required'}),
  });
  await flow.openTask('t1');
  await flow.action('work-resume',{});
  assert.ok(calls.some(call => call.path === '/connections/ntpu-ai/restore'));
  assert.ok(!calls.some(call => call.path.endsWith('/resume')));
  assert.match(flow.html(), /登入 NTPU AI/);
  flow.reset();
});

for (const mode of ['report_draft','complete_assignment']) {
  test(`an expired ${mode} task can restart without replacing the old result on failure`, async () => {
    const old = {...pausedTask,resume_unavailable:'TASK_DEADLINE_EXCEEDED',request:{...pausedTask.request,mode}};
    const path = mode === 'complete_assignment' ? '/assignments/a1/complete' : '/tasks';
    let attempts = 0;
    const {flow,calls} = harness(null,{
      '/tasks/t1': () => old,
      [path]: () => {
        if (++attempts === 1) throw new Error('network lost');
        return {...draftTask,id:'t2',draft:'# Fresh result'};
      },
    });
    await flow.openTask('t1');
    assert.doesNotMatch(flow.html(), /data-action="work-resume"/);
    await flow.action('work-regenerate',{});
    assert.match(flow.html(), /network lost/);
    await flow.action('work-review',{});
    assert.match(flow.html(), /Saved draft/);
    await flow.action('work-progress',{});
    await flow.action('work-regenerate',{});
    const creates = calls.filter(call => call.path === path);
    assert.equal(creates.length,2);
    assert.equal(creates[0].headers['Idempotency-Key'],creates[1].headers['Idempotency-Key']);
    if (mode === 'complete_assignment') assert.deepEqual(creates[1].body,{approved:true});
    assert.match(flow.html(), /Fresh result/);
    assert.ok(!calls.some(call => call.path.endsWith('/resume') || call.path.endsWith('/draft')));
    flow.reset();
  });
}

test('one review confirmation permits output and a PDF failure leaves the Word option', async () => {
  const withBlocker = {...draftTask,blockers:['RUBRIC_REVIEW_REQUIRED','Verify the rubric']};
  const {flow,calls} = harness(null,{
    '/tasks/t1': () => withBlocker,
    '/tasks/t1/draft': (_,body) => ({...withBlocker,reviewed:body.reviewed,accepted_incomplete:body.accept_incomplete}),
    '/tasks/t1/export': () => { throw Object.assign(new Error('render failed'),{code:'PDF_QA_FAILED'}); },
    '/tasks/t1/export-extra': () => ({...withBlocker,reviewed:true,accepted_incomplete:true}),
  });
  await flow.openTask('t1');
  await assert.rejects(flow.action('work-export',{dataset:{kind:'pdf'}}),/先確認審閱/);
  await flow.change({id:'work-reviewed',checked:true,dataset:{}});
  await flow.action('work-export',{dataset:{kind:'pdf'}});
  assert.deepEqual(calls.find(call => call.path.endsWith('/draft')).body,{
    markdown:'# Saved draft',reviewed:true,accept_incomplete:true,
  });
  assert.match(flow.html(), /PDF_QA_FAILED/);
  assert.match(flow.html(), /<details open><summary>其他格式/);
  await flow.action('work-export',{dataset:{kind:'docx'}});
  assert.deepEqual(calls.find(call => call.path.endsWith('/export-extra')).body,{kind:'docx',include_sources:false});
  assert.match(flow.html(), /download\?kind=docx/);
  assert.match(flow.html(), /Saved draft/);
  flow.reset();
});
