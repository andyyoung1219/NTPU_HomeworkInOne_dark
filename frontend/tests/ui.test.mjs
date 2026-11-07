import test from 'node:test';
import assert from 'node:assert/strict';
import {esc,markdown,safeURL,taskStatus} from '../static/ui.js';

test('untrusted source and AI HTML never executes in preview', () => {
  const output = markdown('# Title\n<script>alert(1)</script>\n![image](https://tracker.example/image)\n<a href="javascript:alert(1)">go</a>\n```html\n<img src=x onerror=alert(1)>\n```');
  assert.ok(output.includes('<h1>Title</h1>'));
  assert.ok(!/<script|<img|<a\s/.test(output));
  assert.ok(output.includes('&lt;script&gt;'));
  assert.equal(esc('"<&'), '&quot;&lt;&amp;');
});
test('Markdown keeps tables, lists and incomplete code blocks readable', () => {
  const html = markdown('| Name | Value |\n| --- | --- |\n| <b> | 4 |\n\n- **First**\n- Second\n\n```python\nprint(1)');
  assert.match(html, /<table>/); assert.match(html, /&lt;b&gt;/);
  assert.match(html, /<ul><li><strong>First<\/strong>/);
  assert.match(html, /<pre><code>print\(1\)<\/code><\/pre>/);
});
test('external links reject script and data protocols', () => {
  for (const url of ['javascript:alert(1)','data:text/html,test','file:///secret']) assert.equal(safeURL(url),'');
  assert.equal(safeURL('https://lms3.ntpu.edu.tw/course/view.php?id=1'),'https://lms3.ntpu.edu.tw/course/view.php?id=1');
});
test('Markdown source links are clickable without allowing executable protocols', () => {
  const html = markdown('[Source](https://example.org/paper)\n[Unsafe](javascript:alert)\n`[Code](https://example.org)`');
  assert.match(html, /href="https:\/\/example.org\/paper"/);
  assert.doesNotMatch(html, /href="javascript:/);
  assert.match(html, /<code>\[Code\]\(https:\/\/example.org\)<\/code>/);
});
test('completed generation with required user actions is not labelled fully complete', () => {
  assert.equal(taskStatus({status:'waiting_review',completion:{required_actions:['補上資料']}}),'需處理待辦');
  assert.equal(taskStatus({status:'completed',completion:{status:'needs_action'}}),'需處理待辦');
  assert.equal(taskStatus({status:'completed',completion:{status:'complete'}}),'已匯出');
});
