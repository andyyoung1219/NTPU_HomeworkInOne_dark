'use strict';

const { BrowserManager, dataDirectory } = require('./browser/manager');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { ensureAttachment } = require('./security/attachments');
const { inspectOutboundPayload } = require('./security/guard');
const { responseWatchdog } = require('./browser/response-watchdog');
const { browserFailure, browserError } = require('./browser/diagnostics');

const ORIGIN = 'https://ai.ntpu.edu.tw';
const PROFILE = path.join(dataDirectory(), 'browser-profiles');

// Observe only the site's own UI-initiated request. Cookies and verification stay
// inside the dedicated browser. No private API credentials are extracted.
function installStreamObserver(bindingName) {
  const key = '__ntpuLocalBridge';
  if (window[key]) { window[key].binding = bindingName; return; }
  window[key] = { active: null, controller: null, cancel: null, binding: bindingName };
  const originalFetch = window.fetch;
  window.fetch = async function (...args) {
    const request = args[0];
    const url = typeof request === 'string' ? request : request?.url;
    const state = window[key];
    const id = state.active;
    const relevant = id && url && new URL(url, location.href).origin === location.origin &&
      new URL(url, location.href).pathname === '/chat/stream';
    if (!relevant) return originalFetch.apply(this, args);
    // Keep the website's Stop action and the local Stop action connected.
    const options = { ...(args[1] || {}) };
    let conversation;
    try { conversation = JSON.parse(options.body).session_id; } catch {}
    if (typeof conversation === 'string' && /^[a-zA-Z0-9_-]{1,200}$/.test(conversation))
      await window[state.binding]({ id, kind: 'conversation', sessionId: conversation }).catch(() => {});
    const originalSignal = options.signal;
    const controller = new AbortController();
    state.controller = controller;
    const relayAbort = () => controller.abort();
    if (options.signal?.aborted) controller.abort();
    else options.signal?.addEventListener('abort', relayAbort, { once: true });
    options.signal = controller.signal;
    let reader;
    let completed = false;
    const emit = payload => window[state.binding]({ id, ...payload }).catch(() => {});
    const task = (async () => {
      try {
        const response = await originalFetch.call(this, request, options);
        if (!response.ok) {
          const body = await response.clone().json().catch(() => ({}));
          await emit({ kind: 'failure', status: response.status,
            message: body.detail || `網站回應 HTTP ${response.status}` });
          return response;
        }
        await emit({ kind: 'opened' });
        reader = response.clone().body.getReader();
        state.cancel = () => { controller.abort(); reader?.cancel().catch(() => {}); };
        (async () => {
          const decoder = new TextDecoder();
          let buffer = '';
          const line = async raw => {
            if (!raw.startsWith('data:')) return;
            const data = raw.slice(5).trim();
            if (!data || data === '[DONE]') return;
            let event;
            try { event = JSON.parse(data); } catch { return; }
            if (event.type === 'done') completed = true;
            await emit({ kind: 'event', event });
          };
          try {
            while (true) {
              const result = await reader.read();
              if (result.done) break;
              buffer += decoder.decode(result.value, { stream: true });
              const lines = buffer.split(/\r?\n/);
              buffer = lines.pop();
              for (const raw of lines) await line(raw);
            }
            buffer += decoder.decode();
            if (buffer) await line(buffer);
            if (!completed) await emit({ kind: 'failure', code: 'INCOMPLETE_MODEL_RESPONSE', message: '網站串流中斷，未收到完成事件。' });
          } catch (error) {
            await emit({ kind: 'failure', message: controller.signal.aborted ? '已停止' : error.message });
          } finally {
            originalSignal?.removeEventListener('abort', relayAbort);
            if (state.controller === controller) { state.controller = null; state.cancel = null; }
          }
        })();
        return response;
      } catch (error) {
        await emit({ kind: 'failure', message: controller.signal.aborted ? '已停止' : error.message });
        throw error;
      }
    })();
    return task;
  };
}

function showLoginComplete({ binding, duration }) {
  const host = document.createElement('div'); host.id = '__ntpu_login_complete';
  host.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;padding:20px;overflow:auto;background:#111827cc;backdrop-filter:blur(6px)';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `<style>
    *{box-sizing:border-box}section{width:min(440px,calc(100vw - 40px));max-height:calc(100vh - 40px);overflow:auto;padding:36px;border-radius:20px;background:#fff;color:#17212f;box-shadow:0 24px 80px #0004;text-align:center;font-family:system-ui,sans-serif}
    .brand{color:#6b7280;font-size:12px;letter-spacing:.08em}.check{display:grid;place-items:center;width:60px;height:60px;margin:24px auto 20px;border-radius:50%;background:#e9f8ef;color:#23814b;font-size:30px}
    h1{margin:0 0 12px;font-size:25px}p{margin:0;line-height:1.7;font-size:15px;color:#526070}button{width:100%;margin-top:26px;padding:13px;border:0;border-radius:10px;background:#2456a6;color:white;font:600 15px system-ui;cursor:pointer}button:hover{background:#1d478d}button:disabled{opacity:.65}small{display:block;margin-top:15px;color:#7a8595;font-size:12px}
    @media(prefers-color-scheme:dark){section{background:#202631;color:#edf2f8}p{color:#b9c3d0}.brand,small{color:#94a1b3}.check{background:#173c2a;color:#8bddab}}
  </style><section aria-label="NTPU AI Agent 登入完成"><div class="brand">NTPU AI AGENT</div><div class="check" aria-hidden="true">✓</div><h1>登入成功</h1><p>可以關閉此視窗，回到 VS Code。<br>Agent 會在背景保持連線。</p><button type="button">返回 VS Code</button><small></small></section>`;
  const button = root.querySelector('button'), countdown = root.querySelector('small');
  button.onclick = () => {
    button.disabled = true; button.textContent = '正在返回 VS Code…';
    void window[binding]({ kind: 'loginComplete' }).catch(() => {});
  };
  const deadline = Date.now() + duration;
  const update = () => { countdown.textContent = `${Math.max(0, Math.ceil((deadline - Date.now()) / 1000))} 秒後自動關閉登入視窗`; };
  document.documentElement.append(host); update(); button.focus();
  const timer = setInterval(() => { if (!host.isConnected) clearInterval(timer); else update(); }, 1000);
}

class BrowserBridge {
  constructor(options = {}) {
    this.options = options;
    this.origin = options.origin || ORIGIN;
    this.manager = options.manager || new BrowserManager({ ...options, profileRoot: options.profile, storageRoot: options.storageRoot });
    this.context = null;
    this.page = null;
    this.pending = null;
    this.opening = null;
    this.mode = null;
    this.selectedBrowser = null;
    this.loginWindowClosed = false;
    this.loginNoticeAt = 0;
    this.loginAcknowledged = false;
    this.channels = new Set(); this.ownedConversations = new Set(); this.ownsContext = true;
    this.binding = 'ntpuLocal_' + randomUUID().replaceAll('-', '');
    this.profile = options.profile || PROFILE;
  }

  async open() {
    return this._switch(true);
  }

  async openLogin() {
    if (this.pending) throw new Error('請先停止目前任務，再開啟登入視窗。');
    await this._switch(false);
    await this.page.bringToFront();
  }

  async _switch(headless) {
    // A single profile can only be owned by one Chromium process at a time.
    while (this.opening) await this.opening;
    const mode = headless ? 'background' : 'login';
    if (this.context && this.page && !this.page.isClosed() && this.mode === mode && !this.loginWindowClosed) return;
    if (this.pending || this.channels.size) throw new Error('回答或子任務執行期間不能切換瀏覽器。');
    this.opening = this._open(headless);
    try { await this.opening; } finally { this.opening = null; }
  }

  async _open(headless = true) {
    const previous = this.context;
    this.context = null; this.page = null; this.mode = null;
    this.manager.info = null;
    try { if (previous) await previous.close(); }
    catch (error) { throw browserError([browserFailure(error, { stage: 'handoff', headless })]); }
    // Pin the selected browser on handoff: an Auto fallback must not silently
    // switch profiles after the user has just authenticated.
    const launched = await this.manager.launch({ selection: this.selectedBrowser || this.options.browser || (this.options.chromePath ? 'custom' : 'auto'),
      customPath: this.options.customPath || this.options.chromePath || '', headless });
    this.context = launched.context; this.page = launched.page; this.profile = launched.info.profile;
    this.selectedBrowser = launched.info.browser;
    this.mode = headless ? 'background' : 'login'; this.loginWindowClosed = false;
    this.loginNoticeAt = 0; this.loginAcknowledged = false;
    const context = this.context;
    let stage = 'bindings';
    try {
      context.on('close', () => { if (this.context === context && !headless) this.loginWindowClosed = true; });
      await this.context.exposeBinding(this.binding, (source, payload) => {
        if (source.page !== this.page || !source.frame.url().startsWith(this.origin + '/')) return;
        if (payload?.kind === 'loginComplete' && this.mode === 'login' && this.loginNoticeAt) { this.loginAcknowledged = true; return; }
        this._receive(payload);
      });
      await this.context.addInitScript(installStreamObserver, this.binding);
      this.page = this.context.pages()[0] || await this.context.newPage();
      this.page.on('close', () => {
        if (this.context !== context) return;
        if (!headless && !context.pages().some(page => !page.isClosed())) this.loginWindowClosed = true;
        this._fail(new Error('瀏覽器已中斷。請重新連線 NTPU AI。'));
      });
      this.page.on('framenavigated', frame => {
        if (frame === this.page?.mainFrame() && this.pending) this._fail(new Error('回答期間網頁已離開，請重新確認登入狀態。'));
      });
      stage = 'navigation';
      await this.page.goto(this.origin, { waitUntil: 'domcontentloaded', timeout: 45000 });
      // The page restores its existing session asynchronously after DOMContentLoaded.
      // Wait for that bootstrap before reporting a false login failure. This only
      // observes normal UI state and never completes a login or CAPTCHA.
      if (headless && this.page.url().startsWith(this.origin + '/')) await this.page.waitForFunction(() => {
        const overlay = document.getElementById('auth-overlay');
        return overlay && (overlay.classList.contains('hidden') || getComputedStyle(overlay).display === 'none') && document.getElementById('input');
      }, null, { timeout: this.options.bootstrapTimeoutMs || 12000 }).catch(() => {});
    } catch (error) {
      this.context = null; this.page = null; this.mode = null; this.manager.info = null;
      await context.close().catch(() => {});
      throw browserError([browserFailure(error, { stage, headless,
        candidate: { id: launched.info.browser, label: launched.info.label, executablePath: launched.info.executablePath } })]);
    }
  }

  async finishLogin() {
    if (this.loginFinishing) return this.loginFinishing;
    this.loginFinishing = this._finishLogin();
    try { return await this.loginFinishing; } finally { this.loginFinishing = null; }
  }

  async createChannel() {
    if (!this.ownsContext) throw new Error('子聊天室不能再建立其他聊天室。');
    if (this.mode === 'login') throw new Error('請完成登入後再建立子聊天室。');
    await this.open();
    if (!(await this.status()).ready) throw new Error('NTPU AI 尚未登入。');
    const channel = new BrowserBridge({ ...this.options, manager: this.manager });
    channel.ownsContext = false; channel.parent = this; channel.context = this.context;
    channel.mode = 'background'; channel.profile = this.profile; this.channels.add(channel);
    try {
      channel.page = await this.context.newPage();
      await channel.page.exposeBinding(channel.binding, (source, payload) => {
        if (source.page === channel.page && source.frame.url().startsWith(this.origin + '/')) channel._receive(payload);
      });
      await channel.page.addInitScript(installStreamObserver, channel.binding);
      channel.page.on('close', () => channel._fail(new Error('子聊天室已關閉。')));
      channel.page.on('framenavigated', frame => {
        if (frame === channel.page?.mainFrame() && channel.pending) channel._fail(new Error('子聊天室回答期間已離開頁面。'));
      });
      await channel.page.goto(this.origin, { waitUntil: 'domcontentloaded', timeout: 45000 });
      await channel.page.waitForFunction(() => {
        const overlay = document.getElementById('auth-overlay');
        return overlay && (overlay.classList.contains('hidden') || getComputedStyle(overlay).display === 'none') && document.getElementById('input');
      }, null, { timeout: this.options.bootstrapTimeoutMs || 12000 }).catch(() => {});
      return channel;
    } catch (error) { await channel.close(); throw error; }
  }

  async _finishLogin() {
    if (this.opening) await this.opening;
    if (this.mode !== 'login') return this.status();
    const status = await this.status();
    if (!status.ready && !this.loginWindowClosed && this.page && !this.page.isClosed())
      return { ...status, loginWindow: true, message: '請在開啟的瀏覽器視窗完成學校登入或安全驗證。登入成功後會自動切回背景連線。' };
    if (status.ready && !this.loginWindowClosed && !this.page.isClosed()) {
      const duration = this.options.loginNoticeDurationMs ?? 8000;
      if (!this.loginNoticeAt) {
        const shownAt = Date.now();
        await this.page.evaluate(showLoginComplete, { binding: this.binding, duration });
        this.loginNoticeAt = shownAt;
      }
      if (!this.loginAcknowledged && Date.now() - this.loginNoticeAt < duration)
        return { ready: false, authentication: 'authenticated', loginWindow: true, loginComplete: true, message: '登入成功，可以關閉登入視窗或按「返回 VS Code」。正在準備背景連線。' };
    }
    try { await this.open(); }
    catch (error) { error.message = '登入後背景交接失敗。\n' + error.message; throw error; }
    // Confirm the background browser really restored authentication before
    // showing success. A school session may expire or refuse headless access.
    const background = await this.status();
    return { ...background, loginWindow: false, message: background.ready ? background.message :
      '背景連線尚未取得有效登入。請按「登入」重試；學校端可能已使工作階段失效或要求再次驗證。' };
  }

  async status() {
    if (!this.page || this.page.isClosed()) return { ready: false, authentication: 'unknown', message: '瀏覽器未連線。重新連線後會確認既有登入狀態；關閉瀏覽器不會清除登入資料。' };
    const url = this.page.url();
    if (!url.startsWith(this.origin + '/')) return { ready: false, authentication: 'required', message: '請按「登入」，在瀏覽器視窗完成學校登入或安全驗證。' };
    return this.page.evaluate(() => {
      const overlay = document.getElementById('auth-overlay');
      const input = document.getElementById('input');
      const hidden = overlay && (overlay.classList.contains('hidden') || getComputedStyle(overlay).display === 'none');
      const ready = !!(hidden && input && document.getElementById('sendBtn'));
      return { ready, authentication: ready ? 'authenticated' : overlay && !hidden ? 'required' : 'unknown', message: ready ? `已連線：${document.getElementById('user-email')?.textContent || 'NTPU AI'}` :
        '請按「登入」，在瀏覽器視窗使用學校帳號登入或完成安全驗證。' };
    }).catch(() => ({ ready: false, authentication: 'unknown', message: '登入頁面正在切換，請稍候再檢查。' }));
  }

  async logout() {
    if (this.pending || [...this.channels].some(channel => channel.pending)) throw new Error('請先停止目前執行中的任務，再登出 NTPU AI。');
    if (!this.context || !this.page || this.page.isClosed()) await this.open();
    await this.context.clearCookies({ domain: /(^|\.)ntpu\.edu\.tw$/ });
    if (!this.page.url().startsWith(this.origin + '/'))
      await this.page.goto(this.origin, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await this.page.evaluate(() => { localStorage.clear(); sessionStorage.clear(); }).catch(() => {});
    await this.page.goto(this.origin, { waitUntil: 'domcontentloaded', timeout: 45000 });
    if (this.mode === 'login') await this.open();
  }

  async chat(prompt, options = {}) {
    prompt = inspectOutboundPayload(prompt);
    if (options.attachment) await (this.options.attachmentGuard || ensureAttachment)(options.attachment);
    if (this.pending) throw new Error('已有一個 NTPU AI 請求執行中。');
    if (this.mode === 'login') await this.finishLogin();
    if (!this.page || this.page.isClosed()) await this.open();
    const status = await this.status();
    if (!status.ready) throw new Error(status.message);
    if (options.signal?.aborted) throw new Error('已停止');
    await this.page.waitForFunction(() => {
      const button = document.getElementById('sendBtn');
      return button && !button.classList.contains('stop');
    }, null, { timeout: 10000 });
    if (options.newConversation !== false) {
      const newChat = this.page.locator('#newChatBtn');
      await newChat.click({ timeout: 10000 });
    }
    for (const action of ['clear-attach', 'clear-quote']) {
      const clear = this.page.locator(`[data-action="${action}"]`);
      if (await clear.count()) await clear.first().click({ timeout: 5000 });
    }
    // Search is performed by the local MCP harness, with its own permissions.
    await this.page.evaluate(() => {
      for (const id of ['menuSearchItem', 'menuNtpuItem']) {
        const element = document.getElementById(id);
        if (element?.classList.contains('active') || element?.classList.contains('ntpu-active')) element.click();
      }
    });
    if (options.attachment) {
      options.onUpload?.(options.attachment.name || '圖片');
      await this._attach(options.attachment);
      if (options.signal?.aborted) throw new Error('已停止');
    }
    await this.page.locator('#input').fill(prompt, { timeout: 10000 });
    await this.page.evaluate(installStreamObserver, this.binding);
    const id = randomUUID();
    let resolve, reject;
    const result = new Promise((a, b) => { resolve = a; reject = b; });
    // Attach immediately: a click failure can reject before the final await.
    result.catch(() => {});
    const watchdog = responseWatchdog({ idleMs: options.timeoutMs || 240000, onTimeout: code => {
      this._fail(Object.assign(new Error(code === 'TOTAL_RESPONSE_TIMEOUT' ? '模型請求已達總等待上限；回覆未完成，Agent 將依恢復規則處理。' :
        '等待回答逾時；網站長時間未送回新內容，Agent 將依恢復規則處理。'), { code }));
      void this._abortPage();
    } });
    const abort = () => { this._fail(new Error('已停止')); void this._abortPage(); };
    options.signal?.addEventListener('abort', abort, { once: true });
    this.pending = { id, resolve, reject, watchdog, text: '', model: null, startedAt: Date.now(), promptCharacters: prompt.length,
      idleTimeoutMs: options.timeoutMs || 240000, totalTimeoutMs: watchdog.totalMs, maxContentGapMs: 0,
      opened: false, tokenEvents: 0, eventCounts: {}, onResponseMetadata: options.onResponseMetadata,
      ownsConversation: options.newConversation !== false, onToken: options.onToken, onJudge: options.onJudge, onOpen: options.onOpen, signal: options.signal, abort };
    try {
      await this.page.evaluate(requestId => { window.__ntpuLocalBridge.active = requestId; }, id);
      if (options.signal?.aborted) abort();
      else { options.onSend?.(); await this.page.locator('#sendBtn').click({ timeout: 10000 }); }
      return await result;
    } catch (error) {
      this._fail(error);
      await this._abortPage();
      throw error;
    }
  }

  async _attach(attachment) {
    const response = this.page.waitForResponse(result => result.url() === this.origin + '/upload' && result.request().method() === 'POST', { timeout: 45000 });
    response.catch(() => {});
    await this.page.locator('#fileInput').setInputFiles({ name: attachment.name,
      mimeType: attachment.mimeType, buffer: attachment.buffer }, { timeout: 10000 });
    const upload = await response;
    if (!upload.ok()) throw new Error(`附件上傳失敗（HTTP ${upload.status()}），請檢查網站驗證或用量限制。`);
    await this.page.waitForFunction(() => !!document.querySelector('#attachPreview .attach-chip-badge'), null, { timeout: 10000 });
  }

  _receive(payload) {
    const pending = this.pending;
    if (!pending || payload?.id !== pending.id) return;
    if (payload.kind === 'conversation') { if (pending.ownsConversation && typeof payload.sessionId === 'string' && /^[a-zA-Z0-9_-]{1,200}$/.test(payload.sessionId)) this.ownedConversations.add(payload.sessionId); return; }
    if (payload.kind === 'opened') { pending.opened = true; pending.openedAt ??= Date.now(); pending.onOpen?.(); return; }
    if (payload.kind === 'failure') {
      const suffix = [401, 403].includes(payload.status) ? ' 請回瀏覽器重新登入或完成驗證。' :
        payload.status === 429 ? ' 已達網站用量限制，請等待或使用學校帳號；不會自動重試。' : '';
      const error = new Error((payload.message || '網站請求失敗') + suffix); error.status = payload.status;
      if (payload.code === 'INCOMPLETE_MODEL_RESPONSE') error.code = payload.code;
      return this._fail(error);
    }
    const event = payload.event;
    if (!event) return;
    const eventType = ['judge', 'token', 'done', 'error', 'search', 'search_quota'].includes(event.type) ? event.type : 'unknown';
    pending.eventCounts[eventType] = (pending.eventCounts[eventType] || 0) + 1;
    if (event.type === 'judge') { pending.judgedAt ??= Date.now(); pending.model = event.model || event.selected_model || event; pending.onJudge?.(event); }
    else if (event.type === 'token') {
      pending.tokenEvents++; const token = typeof event.content === 'string' ? event.content : '';
      if (token) {
        const at = Date.now();
        pending.firstTokenMs ??= at - pending.startedAt;
        if (pending.lastContentAt !== undefined) pending.maxContentGapMs = Math.max(pending.maxContentGapMs || 0, at - pending.lastContentAt);
        pending.lastContentAt = at;
        pending.watchdog?.progress();
      }
      pending.text += token; pending.onToken?.(token);
    }
    else if (event.type === 'error') { const error = new Error(event.message || 'NTPU AI 回答失敗'); error.status = Number(event.status || event.status_code || event.code) || undefined; error.serviceLimited = /quota|rate.?limit|capacity|resource.?exhausted/i.test(String(event.code || '')); this._fail(error); }
    else if (event.type === 'done') {
      const result = { text: pending.text, model: pending.model, meta: event };
      pending.siteElapsedMs = Number.isFinite(event.answer_elapsed_ms) ? event.answer_elapsed_ms : undefined;
      const diagnostics = this._responseMetadata(pending, result.text.trim() ? 'completed' : 'empty');
      this._clear();
      if (!result.text.trim()) pending.reject(Object.assign(new Error('網站已結束回答，但沒有回傳內容。'), { code: 'EMPTY_MODEL_RESPONSE', diagnostics }));
      else pending.resolve(result);
    }
  }

  _clear() {
    if (!this.pending) return;
    const id = this.pending.id;
    clearTimeout(this.pending.timer);
    this.pending.watchdog?.stop();
    this.pending.signal?.removeEventListener('abort', this.pending.abort);
    this.pending = null;
    this.page?.evaluate(requestId => {
      if (window.__ntpuLocalBridge?.active === requestId) window.__ntpuLocalBridge.active = null;
    }, id).catch(() => {});
  }

  _responseMetadata(pending, outcome, error) {
    const at = Date.now();
    const metadata = { outcome, promptCharacters: pending.promptCharacters, durationMs: at - pending.startedAt,
      opened: pending.opened, tokenEvents: pending.tokenEvents, responseCharacters: pending.text.length, firstTokenMs: pending.firstTokenMs ?? null,
      phase: pending.firstTokenMs !== undefined ? 'streaming' : pending.opened ? 'awaiting_content' : 'awaiting_response',
      responseOpenMs: pending.openedAt === undefined ? null : pending.openedAt - pending.startedAt,
      judgeMs: pending.judgedAt === undefined ? null : pending.judgedAt - pending.startedAt,
      lastContentAgoMs: pending.lastContentAt === undefined ? null : at - pending.lastContentAt,
      maxContentGapMs: pending.maxContentGapMs || 0, idleTimeoutMs: pending.idleTimeoutMs, totalTimeoutMs: pending.totalTimeoutMs,
      events: { ...pending.eventCounts }, model: typeof pending.model === 'string' ? pending.model : undefined,
      siteElapsedMs: pending.siteElapsedMs, code: error?.code, status: error?.status };
    try { pending.onResponseMetadata?.(metadata); } catch {}
    return metadata;
  }

  _fail(error) { const pending = this.pending; if (pending) error.diagnostics = this._responseMetadata(pending, 'failed', error); this._clear(); pending?.reject(error); }

  async _abortPage() {
    if (!this.page || this.page.isClosed()) return;
    await this.page.evaluate(() => { window.__ntpuLocalBridge?.cancel?.(); window.__ntpuLocalBridge?.controller?.abort(); }).catch(() => {});
  }

  async cleanupConversations() {
    if (this.options.cleanupConversations === false || !this.ownedConversations.size) return { deleted: 0, failed: 0 };
    let deleted = 0, failed = 0;
    if (this.pending || !this.page || this.page.isClosed()) return { deleted, failed: this.ownedConversations.size };
    for (const sid of [...this.ownedConversations]) {
      if (!/^[a-zA-Z0-9_-]{1,200}$/.test(sid)) { failed++; continue; }
      const selector = `.conv-del-btn[data-del-sid="${sid}"]`;
      const dialog = async event => { if (event.type() === 'confirm' && /刪除此對話|Delete this conversation/i.test(event.message())) await event.accept(); else await event.dismiss(); };
      this.page.on('dialog', dialog);
      try {
        await this.page.waitForFunction(() => !document.getElementById('sendBtn')?.classList.contains('stop'), null, { timeout: 10000 });
        // Refresh the site's normal history list without extracting credentials.
        await this.page.evaluate(() => window.dispatchEvent(new Event('focus')));
        const button = this.page.locator(selector);
        await button.waitFor({ state: 'attached', timeout: 8000 });
        const response = this.page.waitForResponse(response => {
          const url = new URL(response.url());
          return url.origin === this.origin && url.pathname === '/reset' && url.searchParams.get('session_id') === sid && response.request().method() === 'POST';
        }, { timeout: 15000 });
        response.catch(() => {});
        // The button is normally shown on hover; use the site's own click handler.
        await button.evaluate(element => element.click());
        if (!(await response).ok()) throw new Error('網站拒絕對話清理');
        this.ownedConversations.delete(sid); deleted++;
      } catch { failed++; }
      finally { this.page.off('dialog', dialog); }
    }
    return { deleted, failed };
  }

  async close({ cleanup = true } = {}) {
    if (this.opening) await this.opening.catch(() => {});
    this._fail(new Error('已停止'));
    await this._abortPage();
    if (cleanup) { const result = await this.cleanupConversations(); if (result.failed) this.options.onCleanupFailure?.(result.failed); }
    await Promise.allSettled([...this.channels].map(channel => channel.close()));
    const context = this.context;
    const page = this.page;
    this.context = null;
    this.page = null;
    this.mode = null; this.loginWindowClosed = false;
    if (this.ownsContext) await context?.close().catch(() => {});
    else { await page?.close().catch(() => {}); this.parent?.channels.delete(this); }
    if (this.ownsContext && this.manager.info) this.manager.info.controlled = false;
  }
}

module.exports = { BrowserBridge, PROFILE, ORIGIN, installStreamObserver };
