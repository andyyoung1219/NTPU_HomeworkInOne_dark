'use strict';

// Host policy, never delegated to the model. Detection is deliberately based on
// credential formats and field context, not entropy alone (UUIDs/hashes are safe).
function classifyPath(input, reviewPaths = []) {
  const value = String(input).replaceAll('\\', '/').toLowerCase();
  const parts = value.split('/').map(part => part.replace(/[. ]+$/, ''));
  const protectedPath = parts.some(name => ['.git', 'node_modules', '.venv', 'venv', '.local', '.ssh', '.aws', '.azure', '.gcloud', '.kube', '.codex', 'secrets', 'credentials', 'browser-profile', 'browser-profiles', 'agent-memory'].includes(name) ||
    (/^\.env(?:\.|$)/.test(name) && !/^\.env\.(example|sample|template)$/.test(name)) ||
    /\.(pem|key|p12|pfx|keystore)$/.test(name) || /^(id_rsa|id_ed25519|id_ecdsa|id_dsa)(?:\.|$)/.test(name) ||
    /^(\.npmrc|\.pypirc|\.git-credentials|cookies|login data|credentials|secrets)(?:\.|$)/.test(name) ||
    /^(service-account.*\.json|.*-service-account\.json)$/.test(name));
  if (protectedPath) return { level: 'protected', reason: '憑證、私鑰或受保護資料目錄' };
  const patterns = ['**/*.csv', '**/appsettings*.json', '**/config.local.*', ...reviewPaths];
  for (const pattern of patterns) {
    const escaped = String(pattern).toLowerCase().replaceAll('\\', '/').replace(/\*\*\/|\*|\?|[.+^${}()|[\]\\]/g,
      token => token === '**/' ? '(?:.*/)?' : token === '*' ? '[^/]*' : token === '?' ? '.' : '\\' + token);
    try { if (new RegExp('^' + escaped + '$').test(value)) return { level: 'review', reason: '可能含私人資訊，傳送前需要單次確認' }; } catch {}
  }
  return { level: 'normal', reason: '一般專案資料' };
}

function redact(text) {
  if (typeof text !== 'string') return text;
  return text
    .replace(/^([ \t]*(?:cookie|cookies|set-cookie|set_cookie)[ \t]*[:=][ \t]*)([^\r\n]+)/gim,
      (match, prefix, value) => value.trim().startsWith('[REDACTED') ? match : prefix + '[REDACTED_COOKIE]')
    .replace(/(["'](?:cookie|cookies|set-cookie|set_cookie)["']\s*:\s*)(["'])([^\r\n]*?)\2/gi,
      (match, prefix, quote, value) => value.startsWith('[REDACTED') ? match : prefix + quote + '[REDACTED_COOKIE]' + quote)
    .replace(/-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, '[REDACTED_PRIVATE_KEY]')
    .replace(/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, '[REDACTED_CLOUD_KEY]')
    .replace(/\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{25,})\b/g, '[REDACTED_API_KEY]')
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, '[REDACTED_JWT]')
    .replace(/\bBearer\s+(?!\[REDACTED)[A-Za-z0-9._~+\/-]+={0,2}/gi, 'Bearer [REDACTED_TOKEN]')
    .replace(/(--(?:api[-_]?key|token|password|secret|client[-_]?secret)(?:=|\s+))(["']?)([^\s"']+)\2/gi,
      (match, prefix, quote, value) => value.startsWith('[REDACTED') ? match : prefix + quote + '[REDACTED_SECRET]' + quote)
    .replace(/\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp):\/\/[^\s"'<>]+/gi, '[REDACTED_CONNECTION_STRING]')
    .replace(/(https?:\/\/)[^\s/@:]+:[^\s/@]+@/gi, '$1[REDACTED_CREDENTIALS]@')
    .replace(/([?&](?:access_token|token|api[_-]?key|secret|password|signature|sig|code|x-amz-(?:signature|credential|security-token)|x-goog-(?:signature|credential))=)[^&#\s"']+/gi, '$1[REDACTED_SECRET]')
    .replace(/((?:["']?)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|secret|token|aws_secret_access_key|authorization|private[_-]?key|database_url|connection[_-]?string)(?:["']?)\s*[:=]\s*)(["']?)([^\r\n,;"'}]+)(["']?)/gi,
      (match, prefix, quote, value, ending) => {
        const trimmed = value.trim();
        if (!trimmed || trimmed.startsWith('[REDACTED') || /^(?:null|undefined|true|false|process\.env\.|os\.environ|\$\{|<|your[_-]|example|placeholder|test[_-]|fake[_-])/i.test(trimmed)) return match;
        return prefix + quote + '[REDACTED_SECRET]' + ending;
      });
}

function sanitize(value) {
  if (typeof value === 'string') return redact(value);
  if (value === null || value === undefined || typeof value !== 'object') return value;
  if (Buffer.isBuffer(value)) return '[BINARY_DATA_OMITTED]';
  if (Array.isArray(value)) return value.map(sanitize);
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    const cookie = /(?:^|[_-])(?:cookies?|set[_-]?cookie)$/i.test(key) && item != null && item !== '';
    const cleaned = cookie ? (typeof item === 'string' && item.startsWith('[REDACTED') ? item : '[REDACTED_COOKIE]') :
      typeof item === 'string' && /(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization|aws_secret_access_key|database[_-]?url|connection[_-]?string|private[_-]?key|token)$/i.test(key) && !item.startsWith('[REDACTED') && redact(key + '=' + item) !== key + '=' + item ? '[REDACTED_SECRET]' : sanitize(item);
    Object.defineProperty(out, key, { value: cleaned, enumerable: true, configurable: true, writable: true });
  }
  return out;
}
function classifyContent(value) {
  const input = typeof value === 'string' ? value : JSON.stringify(value);
  const clean = typeof value === 'string' ? redact(value) : JSON.stringify(sanitize(value));
  return { level: input === clean ? 'normal' : 'protected', detected: input !== clean, explanation: input === clean ? '未偵測到已知秘密格式' : '偵測到憑證格式或秘密欄位；原文已遮蔽' };
}
function inspectOutboundPayload(value, { reject = false } = {}) {
  const detection = classifyContent(value);
  if (reject && detection.detected) throw new Error('資料包含可能的秘密，已阻止外部傳送。請使用非敏感參數。');
  return sanitize(value);
}
module.exports = { classifyPath, classifyContent, redact, sanitize, inspectOutboundPayload };
