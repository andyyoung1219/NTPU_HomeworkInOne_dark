import assert from 'node:assert/strict';
import test from 'node:test';

test('home and course pages refresh submission status when opened or brought back into view', async () => {
  const course = {id: '1', name: 'Synthetic course', semester: '115-1'};
  const assignment = {id: '2', course_id: '1', title: 'Synthetic assignment'};
  const nodes = new Map();
  const documentEvents = new Map();
  const windowEvents = new Map();
  globalThis.document = {
    visibilityState: 'visible',
    querySelector(selector) {
      if (selector === 'dialog[open]') return null;
      if (!nodes.has(selector)) {
        const attributes = new Map();
        nodes.set(selector, {
          innerHTML: '',
          setAttribute: (key, value) => attributes.set(key, value),
          getAttribute: key => attributes.get(key),
          querySelectorAll: () => [],
          addEventListener() {},
          focus() {},
        });
      }
      return nodes.get(selector);
    },
    querySelectorAll: () => [],
    addEventListener: (event, callback) => documentEvents.set(event, callback),
  };
  globalThis.window = {
    scrollY: 0,
    scrollTo() {},
    addEventListener: (event, callback) => windowEvents.set(event, callback),
  };
  globalThis.location = {hash: '#home'};
  const calls = [];
  let label = '未完成';
  let refreshError = null;
  globalThis.fetch = async url => {
    calls.push(url);
    const item = {...assignment, submission_label: label};
    let data;
    if (url === '/api/dashboard?live=true') {
      data = {courses: [course], assignments: [item], today: [], upcoming: [], review: [], overdue: [],
        unknown: label === '待確認' ? [item] : [], submission_refresh_error: refreshError};
    } else {
      assert.equal(url, '/api/learning/courses/1?live=true');
      data = {course, assignments: [item], snapshots: {contents: {}, announcements: {}, grades: {}},
        materials: [], tasks: [], submission_refresh_error: refreshError};
    }
    return {ok: true, json: async () => ({data})};
  };
  const settle = () => new Promise(resolve => setImmediate(resolve));
  await import('../static/app.js');
  await settle();
  const content = nodes.get('#content');
  assert.deepEqual(calls, ['/api/dashboard?live=true']);
  assert.match(content.innerHTML, /Synthetic assignment/);

  label = '已提交';
  windowEvents.get('focus')();
  documentEvents.get('visibilitychange')();
  assert.match(content.innerHTML, /正在向 Moodle 查詢繳交狀態/);
  await settle();
  assert.deepEqual(calls, ['/api/dashboard?live=true', '/api/dashboard?live=true']);
  assert.doesNotMatch(content.innerHTML, /Synthetic assignment/);

  location.hash = '#course/1';
  windowEvents.get('hashchange')();
  await settle();
  assert.equal(calls.at(-1), '/api/learning/courses/1?live=true');
  assert.match(content.innerHTML, /已提交/);

  label = '待確認';
  refreshError = 'NETWORK_UNAVAILABLE';
  windowEvents.get('focus')();
  await settle();
  assert.equal(calls.length, 4);
  assert.equal(calls.at(-1), '/api/learning/courses/1?live=true');
  assert.match(content.innerHTML, /未取得最新繳交狀態/);
  assert.match(content.innerHTML, /class="badge amber">待確認</);
  assert.doesNotMatch(content.innerHTML, /已提交/);
});
