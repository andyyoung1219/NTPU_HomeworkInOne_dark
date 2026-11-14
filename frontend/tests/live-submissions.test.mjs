import assert from 'node:assert/strict';
import test from 'node:test';

test('home and course stay usable during live refresh and discard results after navigation', async () => {
  const course = {id: '1', name: 'Synthetic course', semester: '115-1'};
  const assignment = {id: '2', course_id: '1', title: 'Synthetic assignment'};
  const nodes = new Map();
  const documentEvents = new Map();
  const windowEvents = new Map();
  let confirmClosed;
  let accountMessage;
  const courseBadge = {dataset: {submissionId: assignment.id}, innerHTML: ''};
  globalThis.document = {
    visibilityState: 'visible',
    querySelector(selector) {
      if (selector === 'dialog[open]') return null;
      if (!nodes.has(selector)) {
        const attributes = new Map();
        nodes.set(selector, {
          innerHTML: '',
          showModal() {},
          close(value) { this.returnValue = value; confirmClosed(); },
          addEventListener(event, callback) { if (event === 'close') confirmClosed = callback; },
          replaceChildren() {},
          setAttribute: (key, value) => attributes.set(key, value),
          getAttribute: key => attributes.get(key),
          querySelectorAll: child => selector === '#content' && child === '[data-submission-id]' ? [courseBadge] : [],
          focus() {},
        });
      }
      return nodes.get(selector);
    },
    querySelectorAll: () => [],
    createElement: () => ({remove() {}}),
    addEventListener: (event, callback) => documentEvents.set(event, callback),
  };
  globalThis.window = {
    BroadcastChannel: class {
      addEventListener(event, callback) { if (event === 'message') accountMessage = callback; }
    },
    scrollY: 0,
    scrollTo() {},
    addEventListener: (event, callback) => windowEvents.set(event, callback),
  };
  globalThis.location = {hash: '#home', origin: 'http://127.0.0.1:8766'};
  const calls = [];
  const scopes = [];
  const liveRequests = [];
  const deleted = [];
  let drafts = [
    {id:'draft',title:'Unfinished work',status:'failed',can_delete_draft:true},
    {id:'exported',title:'Exported work',status:'completed',can_delete_draft:false},
  ];
  let account = 'original-account';
  globalThis.fetch = async (url, options) => {
    calls.push(url);
    scopes.push(options.headers['X-Workbench-Account']);
    const requestAccount = account;
    const headers = {get: name => name === 'X-Workbench-Account' ? requestAccount : null};
    if (url === '/api/courses') return {ok: true, headers, json: async () => ({data: [course]})};
    if (url === '/api/tasks') return {ok:true,headers,json:async () => ({data:drafts})};
    if (url === '/api/tasks/draft' && options.method === 'DELETE') {
      deleted.push(url);
      drafts = drafts.filter(task => task.id !== 'draft');
      return {ok:true,headers,json:async () => ({data:{deleted:true}})};
    }
    if (url === '/api/hub') return new Promise(resolve => liveRequests.push(() => resolve({
      ok: true, headers, json: async () => ({data: {sis:{timetable:{data:{courses:[{
        name: requestAccount === 'original-account' ? 'Original timetable' : 'Current timetable',
      }]}}},mappings:[],preferences:{course_ids:[]}}}),
    })));
    const pending = url.endsWith('?pending=true');
    const {label, error, scope = 'original-account'} = pending ? {label: '更新中'} : await new Promise(resolve => liveRequests.push(resolve));
    const item = {...assignment, submission_label: label};
    const status = pending ? {submission_refresh_pending: true} : {submission_refresh_error: error};
    let data;
    if (url.startsWith('/api/dashboard?')) {
      data = {courses: [course], assignments: [item], today: [], upcoming: [], review: [], overdue: [],
        unknown: label === '待確認' ? [item] : [], ...status};
    } else {
      assert.match(url, /^\/api\/learning\/courses\/1\?(pending|live)=true$/);
      data = {course, assignments: [item], snapshots: {contents: {}, announcements: {}, grades: {}},
        materials: [], tasks: [], ...status};
    }
    data.submission_scope = scope;
    return {ok: true, headers, json: async () => ({data})};
  };
  const settle = () => new Promise(resolve => setImmediate(resolve));
  await import('../static/app.js');
  await settle();
  const content = nodes.get('#content');
  assert.deepEqual(calls, ['/api/dashboard?pending=true', '/api/dashboard?live=true']);
  assert.deepEqual(scopes, [undefined, 'original-account']);
  assert.match(content.innerHTML, /data-action="open-assignment".*Synthetic assignment/);
  assert.match(content.innerHTML, /class="badge gray">更新中</);
  assert.match(content.innerHTML, /正在向 Moodle 查詢繳交狀態/);
  assert.equal(content.getAttribute('aria-busy'), 'false');

  windowEvents.get('focus')();
  documentEvents.get('visibilitychange')();
  await settle();
  assert.equal(calls.length, 2);
  liveRequests.shift()({label: '已提交'});
  await settle();
  assert.doesNotMatch(nodes.get('#home-assignment-rows').innerHTML, /Synthetic assignment/);
  assert.equal(nodes.get('#submission-refresh-status').innerHTML, '');

  windowEvents.get('focus')();
  documentEvents.get('visibilitychange')();
  await settle();
  assert.deepEqual(calls.slice(2), ['/api/dashboard?pending=true', '/api/dashboard?live=true']);
  location.hash = '#course/1';
  windowEvents.get('hashchange')();
  await settle();
  assert.deepEqual(calls.slice(4), ['/api/learning/courses/1?pending=true', '/api/learning/courses/1?live=true']);
  assert.match(content.innerHTML, /data-action="sync-one"/);
  assert.match(content.innerHTML, /class="badge gray">更新中</);
  assert.equal(content.getAttribute('aria-busy'), 'false');
  const courseHtml = content.innerHTML;
  liveRequests.shift()({label: '待確認', error: 'NETWORK_UNAVAILABLE'});
  await settle();
  assert.equal(content.innerHTML, courseHtml);
  assert.equal(nodes.get('#submission-refresh-status').innerHTML, '');
  liveRequests.shift()({label: '已提交'});
  await settle();
  assert.match(courseBadge.innerHTML, /已提交/);
  assert.equal(content.innerHTML, courseHtml);

  windowEvents.get('focus')();
  await settle();
  assert.deepEqual(calls.slice(6), ['/api/learning/courses/1?pending=true', '/api/learning/courses/1?live=true']);
  liveRequests.shift()({label: '待確認', error: 'NETWORK_UNAVAILABLE'});
  await settle();
  assert.match(nodes.get('#submission-refresh-status').innerHTML, /未取得最新繳交狀態/);
  assert.match(courseBadge.innerHTML, /class="badge amber">待確認</);
  assert.doesNotMatch(courseBadge.innerHTML, /已提交/);

  windowEvents.get('focus')();
  await settle();
  liveRequests.shift()({label: '待確認', error: 'SUBMISSION_STATUS_UNAVAILABLE'});
  await settle();
  assert.match(nodes.get('#submission-refresh-status').innerHTML, /繳交狀態顯示「待確認」的作業，可至 Moodle 查看/);
  assert.doesNotMatch(nodes.get('#submission-refresh-status').innerHTML, /SUBMISSION_STATUS_UNAVAILABLE/);
  assert.match(courseBadge.innerHTML, /待確認/);

  windowEvents.get('focus')();
  await settle();
  liveRequests.shift()({label: '已提交', scope: 'different-account'});
  await settle();
  assert.match(content.innerHTML, /請重新驗證目前的 Moodle 連線/);
  assert.doesNotMatch(content.innerHTML, /Synthetic assignment/);

  location.hash = '#hub';
  windowEvents.get('hashchange')();
  await settle();
  assert.equal(calls.at(-1), '/api/hub');
  assert.equal(scopes.at(-1), 'original-account');
  account = 'new-account';
  accountMessage({data: account});
  assert.doesNotMatch(content.innerHTML, /Synthetic assignment/);
  await settle();
  assert.deepEqual(calls.slice(-2), ['/api/courses', '/api/hub']);
  assert.equal(scopes.at(-1), account);
  liveRequests.shift()();
  await settle();
  assert.doesNotMatch(content.innerHTML, /Original timetable|請重新驗證目前的 Moodle 連線/);
  liveRequests.shift()();
  await settle();
  assert.match(content.innerHTML, /Current timetable/);
  assert.doesNotMatch(content.innerHTML, /校務資料保存在本機/);
  assert.doesNotMatch(content.innerHTML, /Original timetable/);

  location.hash = '#history';
  windowEvents.get('hashchange')();
  await settle();
  assert.match(content.innerHTML, /刪除草稿：Unfinished work/);
  assert.doesNotMatch(content.innerHTML, /刪除草稿：Exported work/);
  const deleteButton = {
    dataset:{action:'delete-draft',id:'draft',title:'Unfinished work'},
    tagName:'BUTTON',classList:{contains:() => false},childNodes:[],setAttribute() {},
  };
  const clickDelete = () => documentEvents.get('click')({
    target:{closest: selector => selector === '[data-action]' ? deleteButton : null},
  });
  clickDelete();
  await settle();
  assert.match(nodes.get('#confirm-description').textContent, /Unfinished work/);
  nodes.get('#confirm-dialog').close();
  await settle();
  assert.deepEqual(deleted, []);
  deleteButton.disabled = false;
  clickDelete();
  await settle();
  const originalTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (...args) => originalTimeout(...args).unref();
  try {
    nodes.get('#confirm-accept').onclick();
    await settle();
  } finally {
    globalThis.setTimeout = originalTimeout;
  }
  assert.deepEqual(deleted, ['/api/tasks/draft']);
  assert.doesNotMatch(content.innerHTML, /Unfinished work/);
  assert.match(content.innerHTML, /Exported work/);
});
