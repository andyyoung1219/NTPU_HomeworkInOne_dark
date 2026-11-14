'use strict';

// Pipe this fixture into Node in the Compose app's scripts directory.
// It uses a temporary profile and a local sign-in page.
const assert = require('node:assert/strict');
const { mkdtemp, rm } = require('node:fs/promises');
const { createServer } = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { BrowserBridge } = require('../ntpu-ai-bridge/vendor/src/browser');
const { WorkspaceBrowserManager, browserOptions } = require('../ntpu-ai-bridge/src/p0.cjs');

async function main() {
  const profile = await mkdtemp(path.join(os.tmpdir(), 'ntpu-browser-check-'));
  const server = createServer((request, response) => {
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    const signingIn = request.url === '/login';
    const signedIn = signingIn || request.headers.cookie === 'fixture_session=ok';
    if (signingIn) response.setHeader('Set-Cookie', 'fixture_session=ok; HttpOnly; SameSite=Strict; Path=/');
    response.end(signedIn ? '<div id="auth-overlay" class="hidden"></div><textarea id="input"></textarea><button id="sendBtn">Send</button>' :
      '<div id="auth-overlay"><a id="fixture-login" href="/login">Sign into synthetic session</a></div>');
  });
  await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const options = { ...browserOptions, storageRoot: profile };
  const bridge = new BrowserBridge({ ...options, origin, manager: new WorkspaceBrowserManager(options) });
  try {
    await bridge.openLogin();
    assert.equal(bridge.mode, 'login');
    assert.equal(bridge.manager.info.headless, false);
    assert.equal((await bridge.status()).authentication, 'required');
    await bridge.page.locator('#fixture-login').click();
    assert.equal((await bridge.status()).authentication, 'authenticated');
    assert.equal((await bridge.finishLogin()).ready, true);
    assert.equal(bridge.mode, 'background');
    assert.equal(bridge.manager.info.headless, true);
    console.log('Synthetic graphical login and background browser handoff passed.');
  } finally {
    await bridge.close({ cleanup: false });
    await new Promise(resolve => server.close(resolve));
    await rm(profile, { force: true, recursive: true });
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
