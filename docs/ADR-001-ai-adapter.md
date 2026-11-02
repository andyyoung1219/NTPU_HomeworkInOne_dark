# ADR-001：以 stdio 封裝原始 BrowserBridge

- 日期：2026-11-02
- 狀態：採納；離線與真人 AI 驗證均通過
- 來源：ntpu-ai-vscode `f8101fb112b9b1364386becbcd8d922b68eec335`

## 決策與證據

採用 Python 主服務啟動 Node 子程序，透過 JSON 行訊息和非同步事件溝通。
原始 `src/browser.js` 可在 Node 24.14.0 直接載入，未載入 VS Code API。
保留原始 BrowserManager、內容安全檢查、網站 UI 操作、串流觀察、取消與逾時行為。
不移植網站私有 API，不匯出 Cookie，不建立未驗證的本機 AI HTTP port。

獨立介面位於 `ntpu-ai-bridge/src/p0.cjs`：

P1 發布實作將完全相同的必要上游模組保存在 `ntpu-ai-bridge/vendor/`，
並以逐檔 SHA-256 驗證來源。僅安裝 playwright-core/sharp，避免載入完整 Extension
未使用的 MCP、Shell sandbox 及 UI 依賴；原始來源 checkout 仍用於回歸查證。

| 方法 | 行為 |
| --- | --- |
| `health` | 回報版本與 stdio transport |
| `open_login` | 開啟獨立瀏覽器，交由本人登入 |
| `login_status` | 僅回傳 ready/authentication，不回傳帳號或 email |
| `start_task` | 拒絕未核准請求；只接 prompt 與 asset reference，不接任意檔案路徑 |
| `cancel_task` | 同一任務的 AbortController 傳給上游 BrowserBridge |
| `stream_events` | stdout 非同步事件：progress/completed/failed |
| `shutdown` | 中止任務並關閉專用瀏覽器；不刪除站上對話 |

本機 Profile 位於 `%LOCALAPPDATA%/NTPU HomeworkInOne/p0/browser-profiles/`，
與日常瀏覽器、Moodle Token、Git 工作區分離。Moodle 使用 Windows Credential Manager。

## 實測結果

- 獨立匯入與介面檢查通過。
- 上游 browser-auth、browser-manager、browser-observer：30/30 通過。
- 上游全部 Node 測試：432 通過、2 跳過、0 失敗。
- 上游 UI 與 portable plugin build 通過。
- 新專案 stdio health／未核准拒絕／未登入狀態／取消未知 ID 契約測試通過。
- 上游 Python 探針 37/37 通過。
- 真人 AI 測試：10/10 合成文字請求通過；真實文件節錄回覆可核對；取消成功。

第一次在受限 Windows 沙箱跑上游測試，得到 372 通過、53 失敗、2 跳過；
錯誤包含暫存檔 rename、符號連結與本機連線權限。相同固定版本在正常 Windows
權限下重跑全部通過，因此記錄為驗證環境限制，未修改上游測試以忽略失敗。

## 範圍與下一階段

P0 僅做單次請求管線，不引入上游完整 Agent 的檔案工具、MCP、自動執行或並行代理。
P1 在此基礎增加持久化任務與核准快照、結構化草稿、Markdown 編輯與 PDF 匯出。
原始附件不會自動上傳；P0 使用已解析且由使用者核准的文件內容傳遞，符合 P0-04。
對限流、登入失效、未知錯誤立即停止，不採新 Session／自動重新登入等規避流程。
