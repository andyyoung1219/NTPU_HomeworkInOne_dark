'use strict';
// Thin adapter. The pinned upstream retains browser login, guards and cancellation.
const path = require('node:path');
const os = require('node:os');
const readline = require('node:readline');
const { createHash } = require('node:crypto');
const sharp = require('sharp');
const { BrowserBridge } = require('../vendor/src/browser');
const storageRoot = path.join(process.env.LOCALAPPDATA || os.homedir(), 'NTPU HomeworkInOne', 'p0');
const bridge = new BrowserBridge({ storageRoot, workspaces: [path.resolve(__dirname, '../..')] });
const jobs = new Map();
let stopped = false;
const MAX_ATTACHMENT_BYTES = 6 * 1024 * 1024;
const MAX_ATTACHMENT_ENCODED = Math.ceil(MAX_ATTACHMENT_BYTES / 3) * 4;
const MAX_ATTACHMENT_PIXELS = 20000000;
const MAX_VISUAL_REQUEST_CHARS = MAX_ATTACHMENT_ENCODED + 400000;

async function decodeAttachment(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || Object.keys(value).sort().join(',') !== 'approved,data_base64,mime_type,sha256')
    throw new Error('INVALID_ATTACHMENT');
  if (value.approved !== true) throw new Error('ATTACHMENT_APPROVAL_REQUIRED');
  if (value.mime_type !== 'image/png' || typeof value.data_base64 !== 'string'
      || !value.data_base64.length || value.data_base64.length > MAX_ATTACHMENT_ENCODED
      || value.data_base64.length % 4 || /[^A-Za-z0-9+/=]/.test(value.data_base64)
      || typeof value.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(value.sha256))
    throw new Error('INVALID_ATTACHMENT');
  const buffer = Buffer.from(value.data_base64, 'base64');
  if (!buffer.length || buffer.length > MAX_ATTACHMENT_BYTES
      || buffer.toString('base64') !== value.data_base64
      || !buffer.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')))
    throw new Error('INVALID_ATTACHMENT');
  if (createHash('sha256').update(buffer).digest('hex') !== value.sha256)
    throw new Error('ATTACHMENT_CHANGED');
  try {
    const image = sharp(buffer, { limitInputPixels: MAX_ATTACHMENT_PIXELS, animated: true });
    const metadata = await image.metadata();
    if (metadata.format !== 'png' || !metadata.width || !metadata.height
        || metadata.width * metadata.height > MAX_ATTACHMENT_PIXELS || (metadata.pages || 1) !== 1)
      throw new Error('INVALID_ATTACHMENT');
    // Decode all pixels now: a PNG signature/IHDR alone is not a valid image.
    await image.stats();
  } catch { throw new Error('INVALID_ATTACHMENT'); }
  return { name: 'sources.png', mimeType: 'image/png', buffer, approved: true };
}
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
    case 'connect': {
      await bridge.open();
      const status = await bridge.status();
      return { ready: status.ready, authentication: status.authentication || 'unknown' };
    }
    case 'logout':
      await bridge.logout();
      return { ready: false };
    case 'login_status': {
      const status = bridge.mode === 'login' ? await bridge.finishLogin() : await bridge.status();
      return { ready: status.ready, authentication: status.authentication || 'unknown',
        loginWindow: !!status.loginWindow };
    }
    case 'start_task': {
      if (jobs.size) throw new Error('BUSY');
      if (!/^[a-z0-9-]{1,80}$/.test(params.task_id || '')) throw new Error('INVALID_REQUEST');
      if (params.approved !== true) throw new Error('APPROVAL_REQUIRED');
      if (typeof params.prompt !== 'string' || params.prompt.length > 48000) throw new Error('INVALID_REQUEST');
      if (params.timeout_ms !== undefined && (!Number.isInteger(params.timeout_ms)
          || params.timeout_ms < 1 || params.timeout_ms > 300000)) throw new Error('INVALID_REQUEST');
      // Reference only; never accept or resolve caller-provided file paths.
      if (params.asset_ref && !/^[a-f0-9]{24}$/.test(params.asset_ref)) throw new Error('INVALID_REFERENCE');
      const attachment = params.attachment === undefined ? undefined : await decodeAttachment(params.attachment);
      // Image decoding yields; recheck before claiming the single chat slot.
      if (jobs.size) throw new Error('BUSY');
      const controller = new AbortController();
      jobs.set(params.task_id, controller);
      void bridge.chat(params.prompt, {
        signal: controller.signal, timeoutMs: params.timeout_ms ?? 180000,
        attachment,
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
if (require.main === module) {
  const input = readline.createInterface({ input: process.stdin });
  input.on('line', line => {
  if (line.length > MAX_VISUAL_REQUEST_CHARS) return;
  let request;
  try { request = JSON.parse(line); } catch { return; }
  if (line.length > 100000 && (request.method !== 'start_task' || !request.params?.attachment)) {
    output({ id: request.id, error: { code: 'INVALID_REQUEST' } }); return;
  }
  void dispatch(request.method, request.params || {})
    .then(result => { output({ id: request.id, result }); if (stopped) process.exit(0); })
    .catch(error => output({ id: request.id, error: { code:
      ['BUSY', 'INVALID_REQUEST', 'APPROVAL_REQUIRED', 'METHOD_NOT_ALLOWED', 'INVALID_REFERENCE',
        'INVALID_ATTACHMENT', 'ATTACHMENT_APPROVAL_REQUIRED', 'ATTACHMENT_CHANGED'].includes(error.message)
        ? error.message : code(error) } }));
});
input.on('close', () => { void bridge.close({ cleanup: false }).finally(() => process.exit(0)); });
}

module.exports = { decodeAttachment, MAX_ATTACHMENT_BYTES };
