# Third-party notices

來源版本與引入日期見 `docs/sources.lock.json`（2026-11-02）。

| 來源 | 使用範圍 | 原授權 |
| --- | --- | --- |
| https://github.com/xujk0217/NTPUOne | 執行原始 Moodle 唯讀探針與測試；以 Windows Vault 包裝原探針 | 保留所有權利；提出者確認作者允許使用，見 `docs/AUTHORIZATION.md` |
| https://github.com/xujk0217/ntpu-ai-vscode | 原始 BrowserBridge、安全檢查及其測試；獨立 Node 子程序包裝 | MIT，原文保存在 `LICENSES/ntpu-ai-vscode.txt` 及 vendor/LICENSE |

兩個來源 checkout 放在忽略追蹤的 `.upstream/`。
P1 只將 BrowserBridge 必要的 8 個原始 JS 模組逐位元複製到 `ntpu-ai-bridge/vendor/`，
沒有改寫其中行為，來源 SHA 與逐檔 hash 保存在 vendor/manifest.json。
Python 套件以 `uv.lock` 鎖定，P1 Node 用自己的最小 `ntpu-ai-bridge/package-lock.json`。
完整版來源 lockfile 僅供原專案回歸，不是 P1 runtime 依賴。
本紀錄不授予第三方資產、課程教材或 NTPU 品牌的權利。

## 本專案直接依賴（本機安裝 metadata）

| 套件 | 鎖定版本 | 授權 |
| --- | --- | --- |
| FastAPI | 0.162.0 | MIT |
| Uvicorn | 0.54.0 | BSD-3-Clause |
| Gradio | 6.31.0 | Apache-2.0 |
| httpx | 0.28.1 | BSD-3-Clause |
| keyring | 25.7.0 | MIT |
| PyMuPDF | 1.28.2 | AGPL-3.0 或 Artifex 商業授權 |
| python-docx | 1.2.0 | MIT |
| pytest | 9.2.0 | MIT |
| Ruff | 0.17.3 | MIT |
| ReportLab | 4.5.1 | BSD |
| fonttools | 4.68.0 | MIT |
| Pillow（安全覆寫） | 13.0.0 | MIT-CMU |
| playwright-core | 1.64.0 | Apache-2.0 |
| sharp | 0.35.5 | Apache-2.0 |
| Noto Sans TC | docs/font.lock.json 固定 commit | SIL Open Font License 1.1；字型不提交 Git |

PyMuPDF 並非寬鬆 MIT 授權；未來發布時依所選授權處理。本次只做本機開發。
間接依賴完整版本由 `uv.lock` 與來源 `package-lock.json` 保存；套件原始聲明保留在安裝目錄。
