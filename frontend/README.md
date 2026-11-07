# HTML / CSS / JavaScript 前端

正式入口為 `static/index.html`，由 `backend/main.py` 的 FastAPI 提供。所有靜態資源與 API
沿用原本的 Host、Origin 與 HttpOnly session 檢查。啟動方式仍為 `scripts/run_mvp.ps1`，
再開啟終端機提供的 `/unlock/...` 網址。無 npm 安裝、編譯、CDN 或遠端字型需求。

| 檔案 | 用途 |
| --- | --- |
| `static/index.html` | 導覽、頁面外框、登入與確認對話框 |
| `static/tokens.css` | 共用配色 |
| `static/styles.css` | 元件樣式、桌面／平板／手機斷點 |
| `static/app.js` | 路由、API、儀表板、課程、校務、搜尋、連線與清除資料 |
| `static/workflow.js` | 逐步／直接完成、進度保存、任務恢復、審閱與匯出 |
| `static/ui.js` | HTML escaping、Markdown 基本預覽、狀態與錯誤訊息 |

瀏覽器只呼叫 `/api/*`；學校帳密不寫入 localStorage，也不由前端直接呼叫學校或 AI。
進度保存會序列化，切換作業前先保存；重新載入不恢復 AI 傳送或匯出審閱核准。
草稿修改立即隱藏舊下載連結並撤銷審閱，保存成功後才提供當前成果下載。
重試遺失回應的任務建立請求會沿用同一個 Idempotency-Key。
Markdown 預覽支援常用文字、標題、清單、表格與程式碼，原始 HTML 會轉義。

在專案根目錄執行：

```powershell
npm --prefix frontend run check
npm --prefix frontend test
.venv/Scripts/python.exe -m pytest -q
```

`frontend/*.py` 保留作為舊 Gradio callback 回歸測試基準；正式程式不會載入它們。
Gradio 已移到 `dev` 相依群組。只安裝正式執行相依可用 `uv sync --no-dev --locked`；
要跑舊回歸測試則使用 `uv sync --locked`。歷史 P1–P4 驗收文件中的 Gradio 說明屬於舊介面。
