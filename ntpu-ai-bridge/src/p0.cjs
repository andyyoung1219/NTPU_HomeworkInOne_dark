'use strict';
// Thin adapter. The pinned upstream retains browser login, guards and cancellation.
const path = require('node:path');
const os = require('node:os');
const readline = require('node:readline');
const { BrowserBridge } = require('../vendor/src/browser');
const storageRoot = path.join(process.env.LOCALAPPDATA || os.homedir(), 'NTPU HomeworkInOne', 'p0');
const bridge = new BrowserBridge({ storageRoot, workspaces: [path.resolve(__dirname, '../..')] });
const jobs = new Map();
let stopped = false;
function output(message) { process.stdout.write(JSON.stringify(message) + '\n'); }
function code(error) {
  const message = String(error?.message || '');
  if (error?.status === 429 || error?.serviceLimited || /429|額度|限制|rate.limit/i.test(message)) return 'AI_SERVICE_LIMIT';
  if ([401, 403].includes(error?.status) || /登入|驗證|401|403/.test(message)) return 'AI_AUTH_REQUIRED';
  if (/停止|abort/i.test(message)) return 'CANCELLED';
  if (/逾時|timeout/i.test(message)) return 'AI_TIMEOUT';
  return 'AI_BRIDGE_ERROR';
}
async function dispatch(method, params) {
  switch (method) {
    case 'health': return { ok: true, transport: 'stdio', version: '0.0.1' };
    case 'open_login':
      await bridge.openLogin();
      return { login_window: true };
    case 'connect':
      await bridge.open();
      return { ready: (await bridge.status()).ready };
    case 'logout':
      await bridge.logout();
      return { ready: false };
    case 'login_status': {
      const status = bridge.mode === 'login' ? await bridge.finishLogin() : await bridge.status();
      return { ready: status.ready, authentication: status.authentication || 'unknown' };
    }
    case 'start_task': {
      if (jobs.size) throw new Error('BUSY');
      if (!/^[a-z0-9-]{1,80}$/.test(params.task_id || '')) throw new Error('INVALID_REQUEST');
      if (params.approved !== true) throw new Error('APPROVAL_REQUIRED');
      if (typeof params.prompt !== 'string' || params.prompt.length > 48000) throw new Error('INVALID_REQUEST');
      // Reference only; never accept or resolve caller-provided file paths.
      if (params.asset_ref && !/^[a-f0-9]{24}$/.test(params.asset_ref)) throw new Error('INVALID_REFERENCE');
      const controller = new AbortController();
      jobs.set(params.task_id, controller);
      void bridge.chat(params.prompt, {
        signal: controller.signal, timeoutMs: 90000,
        onToken: () => output({ event: 'progress', task_id: params.task_id }),
      }).then(result => output({ event: 'completed', task_id: params.task_id, text: result.text }))
        .catch(error => output({ event: 'failed', task_id: params.task_id, code: code(error) }))
        .finally(() => jobs.delete(params.task_id));
      return { task_id: params.task_id, status: 'running' };
    }
    case 'cancel_task': {
      const controller = jobs.get(params.task_id);
      if (controller) controller.abort();
      return { requested: !!controller };
    }
    case 'stream_events': return { transport: 'stdio-notifications' };
    case 'shutdown':
      for (const controller of jobs.values()) controller.abort();
      await bridge.close({ cleanup: false });
      stopped = true;
      return { stopped: true };
    default: throw new Error('METHOD_NOT_ALLOWED');
  }
}
const input = readline.createInterface({ input: process.stdin });
input.on('line', line => {
  if (line.length > 100000) return;
  let request;
  try { request = JSON.parse(line); } catch { return; }
  void dispatch(request.method, request.params || {})
    .then(result => { output({ id: request.id, result }); if (stopped) process.exit(0); })
    .catch(error => output({ id: request.id, error: { code:
      ['BUSY', 'INVALID_REQUEST', 'APPROVAL_REQUIRED', 'METHOD_NOT_ALLOWED', 'INVALID_REFERENCE'].includes(error.message)
        ? error.message : code(error) } }));
});
input.on('close', () => { void bridge.close({ cleanup: false }).finally(() => process.exit(0)); });
