'use strict';
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { promisify } = require('node:util');
const run = promisify(require('node:child_process').execFile);
const { createHash } = require('node:crypto');
const sharp = require('sharp');
const { classifyContent } = require('./guard');
const checked = new WeakMap();

// Proof is held only in this process, bound to the exact bytes. Never persist OCR text.
class AttachmentScanner {
  constructor(options = {}) { this.options = options; }
  async scan(buffer, mimeType) {
    if (!Buffer.isBuffer(buffer) || buffer.length > 20 * 1024 * 1024) throw new Error('附件必須小於 20 MB。');
    const digest = createHash('sha256').update(buffer).digest('hex');
    if (checked.get(buffer)?.mimeType === mimeType && checked.get(buffer)?.digest === digest) return checked.get(buffer);
    const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'ntpu-ocr-'));
    await fs.chmod(directory, 0o700);
    const execute = this.options.run || run;
    const resolve = async program => {
      if (path.isAbsolute(program)) return program;
      const candidates = process.platform === 'win32' ? [path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Tesseract-OCR', program + '.exe')] : ['/opt/homebrew/bin/' + program, '/usr/local/bin/' + program, '/usr/bin/' + program];
      for (const candidate of candidates) { try { await fs.access(candidate); return candidate; } catch {} }
      return program;
    };
    const invoke = async (program, args) => execute(this.options.run ? program : await resolve(program), args, { timeout: 45000, maxBuffer: 8 * 1024 * 1024, windowsHide: true, cwd: directory });
    let pages = 0, bundled = false;
    try {
      const input = path.join(directory, mimeType === 'application/pdf' ? 'input.pdf' : 'input.image');
      await fs.writeFile(input, buffer, { mode: 0o600 });
      let images;
      if (mimeType === 'application/pdf') {
        const info = (await invoke(this.options.pdfinfo || 'pdfinfo', [input])).stdout;
        const count = Number(/^Pages:\s+(\d+)\s*$/m.exec(info)?.[1]);
        if (!count || count > 20 || /^Encrypted:\s+yes/m.test(info)) throw new Error('PDF 必須可讀取且不超過 20 頁。');
        // Scan embedded text as well as rendered pixels. A successful raster OCR alone
        // does not cover hidden/selectable text in the original document.
        const text = (await invoke(this.options.pdftotext || 'pdftotext', [input, '-'])).stdout;
        if (classifyContent(text).detected) throw new Error('附件辨識到疑似憑證，禁止上傳。');
        await invoke(this.options.pdftoppm || 'pdftoppm', ['-png', '-scale-to', '2400', input, path.join(directory, 'page')]);
        images = (await fs.readdir(directory)).filter(name => /^page-\d+\.png$/.test(name)).map(name => path.join(directory, name));
        if (images.length !== count) throw new Error('PDF 頁面未完整轉換，禁止上傳。');
      } else {
        const metadata = await sharp(buffer, { animated: true, limitInputPixels: 40000000 }).metadata();
        const count = metadata.pages || 1;
        if (count > 20) throw new Error('動畫圖片超過 20 格，禁止上傳。');
        images = [];
        for (let page = 0; page < count; page++) {
          const file = path.join(directory, 'frame-' + page + '.png');
          await sharp(buffer, { page, pages: 1, limitInputPixels: 40000000 }).flatten({ background: '#ffffff' }).png().toFile(file); images.push(file);
        }
      }
      for (const image of images) {
        let text;
        try {
          text = (await invoke(this.options.tesseract || 'tesseract', [image, 'stdout', '-l', this.options.language || 'eng', '--psm', '11'])).stdout;
        } catch (error) {
          // Only a missing default executable falls back. Respect custom paths,
          // languages and genuine native parser failures rather than masking them.
          if (error.code !== 'ENOENT' || this.options.tesseract || ![undefined, 'eng'].includes(this.options.language)) throw error;
          text = await require('./offline-ocr').recognize(await fs.readFile(image)); bundled = true;
        }
        if (classifyContent(text).detected) throw new Error('附件辨識到疑似憑證，禁止上傳。');
        pages++;
      }
      const result = { mimeType, digest, pages, engine: bundled ? 'Bundled Tesseract WASM' + (mimeType === 'application/pdf' ? ' + Poppler' : '') : 'Tesseract + Poppler', checked: true };
      checked.set(buffer, result); return result;
    } catch (error) {
      // Native parser stderr can contain document text or filenames. Never expose it.
      if (error.message?.startsWith('附件') || error.message?.startsWith('PDF') || error.message?.startsWith('動畫')) throw new Error(error.message);
      throw new Error('本機附件檢查失敗，未上傳。請確認附件可讀取、內建 OCR 資源完整；PDF 另需 Poppler，自訂 Tesseract／語言需可用。');
    } finally { await fs.rm(directory, { recursive: true, force: true }); }
  }
}
const scanner = new AttachmentScanner();
async function ensureAttachment(attachment) {
  if (!attachment?.approved) throw new Error('附件需要先確認目的地與敏感資料風險。');
  return scanner.scan(attachment.buffer, attachment.mimeType);
}
module.exports = { AttachmentScanner, scanner, ensureAttachment };
