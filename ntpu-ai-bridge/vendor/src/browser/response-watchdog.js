'use strict';

// Only actual answer content extends the idle deadline. Keep a hard ceiling so
// a peer sending occasional bytes cannot retain a model slot indefinitely.
function responseWatchdog({ idleMs, onTimeout, now = Date.now, schedule = setTimeout, cancel = clearTimeout }) {
  if (!Number.isFinite(idleMs) || idleMs <= 0) throw new Error('回答閒置逾時必須是正數毫秒。');
  const startedAt = now(), totalMs = Math.max(idleMs, Math.min(idleMs * 4, 15 * 60000));
  let lastProgressAt = startedAt, timer, stopped = false;
  const check = () => {
    if (stopped) return;
    const at = now(), idleRemaining = idleMs - (at - lastProgressAt), totalRemaining = totalMs - (at - startedAt);
    if (Math.min(idleRemaining, totalRemaining) <= 0) {
      stopped = true; onTimeout(totalRemaining <= 0 ? 'TOTAL_RESPONSE_TIMEOUT' : 'IDLE_RESPONSE_TIMEOUT'); return;
    }
    timer = schedule(check, Math.min(idleRemaining, totalRemaining)); timer?.unref?.();
  };
  check();
  return { progress: () => { if (!stopped) lastProgressAt = now(); }, stop: () => { stopped = true; cancel(timer); }, totalMs };
}

module.exports = { responseWatchdog };
