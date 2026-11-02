'use strict';
const { redact } = require('../security/guard');
const STAGES = { discovery: '候選探索', 'profile-policy': 'Profile 路徑檢查', 'profile-prepare': 'Profile 準備', spawn: 'Chromium 啟動', control: '頁面控制', 'window-sizing': '登入視窗尺寸調整', bindings: '頁面初始化', navigation: '網站導覽', handoff: '登入後背景交接' };

// Browser stderr is useful; full launch arguments, URLs and page contents are not.
// Sanitize before limiting output, including credential lines prefixed by a PID.
function browserReason(error) {
  const text = String(error?.message ?? error ?? '未知錯誤')
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/[^\S\r\n]*\b(?:cookie|cookies|set-cookie|set_cookie)\s*[:=][^\r\n]*/gi, ' [REDACTED_COOKIE]')
    .replace(/https?:\/\/[^\s<>"']+/gi, '[REDACTED_URL]');
  const lines = redact(text).replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]/g, '').split(/\r?\n/)
    .map(line => line.trim()).filter(line => line && !/^(?:Call log:|[- ]*<launching>|[- ]*\[pid=\d+\] (?:<(?:gracefully|kill|will force|skipped)|(?:starting|finished) temporary))/.test(line));
  return lines.slice(0, 12).map(line => line.slice(0, 650)).join('\n').slice(0, 3500);
}
function failureKind(reason, stage) {
  // File exists/ProcessSingleton alone does not prove a live profile owner.
  if (/EACCES|EPERM|permission denied|access is denied|read-only file system/i.test(reason)) return 'permission';
  if (/error while loading shared libraries|cannot open shared object|host system is missing dependencies|missing libraries/i.test(reason)) return 'dependencies';
  if (/no usable sandbox|suid sandbox|user namespace|failed to move to new namespace|sandbox.*(?:failed|not configured)|running as root/i.test(reason)) return 'sandbox';
  if (/missing x server|cannot open display|failed to (?:connect|initialize).*wayland|\$DISPLAY|ozone.*(?:failed|initialize)/i.test(reason)) return 'display';
  if (/profile.*(?:in use by (?:process|another)|being used by)|SingletonLock.*File exists|ProcessSingleton/i.test(reason)) return 'profile-singleton';
  if (stage === 'navigation') return 'navigation';
  return 'unknown';
}
const NEXT = {
  permission: '請檢查該執行檔與專用 Profile 的擁有者、存取權及 Snap／Flatpak 路徑限制。',
  dependencies: '請依瀏覽器官方安裝方式補齊此系統的共享函式庫依賴。',
  sandbox: '請檢查發行版的 user namespace／AppArmor 與瀏覽器 sandbox 安裝；保留 sandbox，使用一般使用者啟動。',
  display: '請確認本機 Extension Host 可連線 DISPLAY／WAYLAND_DISPLAY 所指的桌面顯示服務。',
  'profile-singleton': '請檢查此專用 Profile 的 SingletonLock 與所屬程序；只有確認仍有占用程序時才關閉該專用視窗。不要刪除 Profile 或鎖檔。',
  navigation: '請檢查網站連線、DNS、TLS 與學校服務狀態；瀏覽器連線失敗不代表登入已過期。',
  unknown: '請依下列失敗階段與原因檢查；可在連線設定選擇另一個已安裝瀏覽器後重新連線。'
};
function browserFailure(error, { stage, candidate = {}, headless = true } = {}) {
  const reason = browserReason(error), kind = failureKind(reason, stage);
  return { browser: candidate.id, label: candidate.label || '瀏覽器', executablePath: browserReason(candidate.executablePath || ''),
    mode: headless ? '背景' : '登入視窗', stage, kind, reason, next: stage === 'window-sizing' ? '可手動調整登入視窗大小；頁面已重新確認可由 Playwright 控制。' : NEXT[kind] };
}
function formatFailure(failure) {
  return `${failure.label}${failure.executablePath ? ' (' + failure.executablePath + ')' : ''}／${failure.mode}／${STAGES[failure.stage] || failure.stage} [${failure.kind}]\n${failure.reason}\n${failure.next}`;
}
function browserError(failures) {
  const error = new Error('瀏覽器連線失敗。\n' + failures.map(formatFailure).join('\n\n'));
  error.code = 'BROWSER_CONNECTION_FAILED'; error.failures = failures;
  return error;
}
module.exports = { browserReason, browserFailure, browserError, formatFailure };
