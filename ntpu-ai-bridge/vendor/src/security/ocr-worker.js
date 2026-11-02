'use strict';
const { workerData, parentPort } = require('node:worker_threads');

// Both the supervisor and engine use this entry point. Models and WASM are
// bundled dependencies; even a misconfigured language path cannot fetch a CDN.
global.fetch = async () => { throw new Error('OCR network access is disabled.'); };
if (!workerData) {
  require('tesseract.js/src/worker-script/node');
} else {
  (async () => {
    let engine;
    try {
      let fail;
      const failed = new Promise((_, reject) => { fail = reject; });
      const { langPath, gzip } = require('@tesseract.js-data/eng');
      engine = await Promise.race([
        require('tesseract.js').createWorker('eng', 1, {
          workerPath: __filename, langPath, gzip, cacheMethod: 'none',
          logger: () => {}, errorHandler: () => fail(new Error('OCR failed.')),
        }), failed,
      ]);
      await engine.setParameters({ tessedit_pageseg_mode: '11' });
      const result = await engine.recognize(Buffer.from(workerData.image));
      parentPort.postMessage({ text: result.data.text });
    } catch { parentPort.postMessage({ failed: true }); }
    finally { if (engine) await engine.terminate(); }
  })();
}
