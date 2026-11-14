import assert from 'node:assert/strict';
import test from 'node:test';

test('Hub keeps the requested semester and its course results across SIS tabs', async () => {
  const nodes = new Map();
  const listeners = new Map();
  const node = selector => {
    if (!nodes.has(selector)) {
      nodes.set(selector, {innerHTML: '', focus() {}, setAttribute() {}, addEventListener() {}, replaceChildren() {}});
    }
    return nodes.get(selector);
  };
  let checkedCourses = [];
  globalThis.document = {
    querySelector: node,
    querySelectorAll: selector => selector === '[name="sync-course"]:checked' ? checkedCourses : [],
    createElement: () => ({remove() {}}),
    addEventListener: (name, listener) => listeners.set(name, listener),
  };
  globalThis.window = {scrollTo() {}, addEventListener() {}};
  globalThis.location = {hash: '#hub', origin: 'http://127.0.0.1:8766'};
  const semesters = [];
  const current = {id:'1',name:'Current course',semester:'115-1',scope:'inprogress'};
  const historical = {id:'2',name:'Historical course',semester:'114-1',scope:'114-1'};
  globalThis.fetch = async (url, options) => {
    let data;
    if (url === '/api/courses') data = [current,historical];
    else if (url === '/api/hub') data = {sis:{timetable:{},grades:{},rank:{}},mappings:[]};
    else {
      assert.equal(options.method,'POST');
      const {pathname,searchParams} = new URL(url,globalThis.location.origin);
      assert.equal(pathname,'/api/hub/courses/discover');
      const semester = searchParams.get('semester');
      semesters.push(semester);
      data = semester === '114-1' ? [historical] : [];
    }
    return {ok:true,json:async () => ({data})};
  };
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const click = async (action, details = {}) => {
    const button = {dataset:{action,...details},childNodes:[],tagName:'BUTTON',
      classList:{contains:() => false},setAttribute() {}};
    listeners.get('click')({target:{closest:selector => selector === '[data-action]' ? button : null}});
    await settle();
  };
  const enterSemester = value => {
    const input = node('#semester');
    input.id = 'semester';
    input.value = value;
    listeners.get('input')({target:input});
  };
  const timeout = globalThis.setTimeout;
  globalThis.setTimeout = (...args) => timeout(...args).unref();
  try {
    await import('../static/app.js');
    await settle();
    const content = node('#content');
    assert.match(content.innerHTML,/Current course/);
    assert.doesNotMatch(content.innerHTML,/Historical course/);

    enterSemester('114-2');
    assert.doesNotMatch(node('#sync-course-list').innerHTML,/Current course/);
    assert.equal(node('[data-action="sync-selected"]').disabled,true);
    await click('discover-courses');
    await click('hub-tab',{tab:'grades'});
    assert.match(content.innerHTML,/id="semester" maxlength="20" value="114-2"/);
    assert.match(content.innerHTML,/此學期沒有找到可同步的課程/);
    assert.doesNotMatch(content.innerHTML,/Current course|Historical course/);
    assert.match(content.innerHTML,/data-action="sync-selected" disabled/);

    enterSemester('114-1');
    await click('discover-courses');
    checkedCourses = [{value:'2'}];
    await click('hub-tab',{tab:'rank'});
    assert.match(content.innerHTML,/id="semester" maxlength="20" value="114-1"/);
    assert.match(content.innerHTML,/name="sync-course" value="2" checked/);
    assert.match(content.innerHTML,/Historical course/);
    assert.doesNotMatch(content.innerHTML,/Current course/);

    checkedCourses = [];
    enterSemester('115-2');
    assert.doesNotMatch(node('#sync-course-list').innerHTML,/Historical course/);
    await click('hub-tab',{tab:'timetable'});
    assert.match(content.innerHTML,/id="semester" maxlength="20" value="115-2"/);
    assert.doesNotMatch(content.innerHTML,/Current course|Historical course/);
    assert.deepEqual(semesters,['114-2','114-1']);
  } finally { globalThis.setTimeout = timeout; }
});
