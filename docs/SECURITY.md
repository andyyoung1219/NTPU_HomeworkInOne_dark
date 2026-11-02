# 本機安全邊界與依賴

P1 只綁定 127.0.0.1:8766。每次啟動更換存取金鑰，以 HttpOnly/SameSite=Strict
cookie 驗證；拒絕未知 Host 與跨 Origin 請求。無公開分享、CORS 放行或任意工具執行。
不接受任意檔案路徑的下載 API。任務、附件與歷史依本人 Moodle 身分的雜湊分區。

Moodle Token 存 OS Vault；密碼只在官方 HTTPS 登入請求的記憶體內使用。
NTPU AI 使用獨立專用瀏覽器 Profile，沒有匯出 Cookie。登出清除對應本機憑證；
不宣稱已撤銷校方伺服器全部 Token。Gradio analytics 與 run history 停用。

附件 URL 限 Moodle 指定 HTTPS host/port/path；下載前驗證 DNS 為公網；拒絕任何
redirect。檔案大小、MIME、檔頭、PDF 頁數、DOCX 解壓大小／比例與巨集皆檢查。
不執行附件程式、Office 巨集或文件中的指令。TXT/CSV 不接受 NUL；CSV 僅傳預覽。

每次 AI 核准綁定請求內容 SHA-256 與來源 hash；變動後原核准失效。
草稿中未執行的實驗保留 TODO。PDF 只由本機 ReportLab 渲染，不載入外部資源。

## 2026-11-02 依賴修補

初次掃描完整上游 Extension 有 7 個 runtime 套件受影響，其中多個屬於本產品不用的
MCP、Shell sandbox、Markdown renderer 等。P1 改為固定、保留授權的 BrowserBridge
原始模組子集，只安裝 playwright-core/sharp。最終 `npm audit --omit=dev` 為 0。
完整上游 checkout 僅供來源查證；不將其完整 node_modules 當成 P1 runtime。

Gradio 6.31.0 的 metadata 限 Pillow <13。pip-audit 在 Pillow 12.3.0 發現已知漏洞，
修補版本為 13.0.0（見 [Pillow 官方發布說明](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)）。
本專案採 uv 明確覆寫為 Pillow >=13,<14；P1 不提供影像上傳或影像編輯元件。
此為有意識的版本範圍例外，完整 Gradio 頁面、API、PDF 與解析器測試均通過。
以 `uv sync --locked` 安裝此版本，不能使用忽略 uv 覆寫的任意 pip 安裝流程。
最終 Python 全環境 pip-audit 無已知漏洞；CI 重跑此檢查，不忽略特定漏洞。

目前只真人驗證 Windows x64。Moodle/NTPU AI 改版或登入失效仍需要重新驗證。
OCR、複雜公式、PDF 表格語意與模型內容品質須人工核對。字型與各套件權利
依 THIRD_PARTY_NOTICES 各項處理；沒有自動發布或提交學校資料。
