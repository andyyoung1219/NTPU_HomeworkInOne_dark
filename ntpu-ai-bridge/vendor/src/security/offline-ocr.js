'use strict';
const path = require('node:path');
const { Worker } = require('node:worker_threads');

// A short-lived supervisor owns the OCR engine's worker, including initialization.
// Terminating it also terminates nested workers on failure or timeout. No OCR text
// is written to disk, and engine stdout/stderr never reaches the Extension Host.
let previous = Promise.resolve();
function recognize(buffer) {
  const next = previous.then(async () => {
    let worker, timer;
    try {
      return await new Promise((resolve, reject) => {
        worker = new Worker(path.join(__dirname, 'ocr-worker.js'), {
          workerData: { image: buffer }, stdout: true, stderr: true,
        });
        worker.stdout.resume(); worker.stderr.resume();
        worker.once('message', result => typeof result?.text === 'string' ? resolve(result.text) : reject(new Error('離線 OCR 辨識失敗。')));
        worker.once('error', () => reject(new Error('離線 OCR 初始化失敗。')));
        worker.once('exit', () => reject(new Error('離線 OCR 提前結束。')));
        timer = setTimeout(() => reject(new Error('離線 OCR 等待逾時。')), 45000);
      });
    } finally { clearTimeout(timer); if (worker) await worker.terminate(); }
  });
  // The serialization tail must never retain a fulfilled OCR text result.
  previous = next.then(() => undefined, () => undefined);
  return next;
}
module.exports = { recognize };
