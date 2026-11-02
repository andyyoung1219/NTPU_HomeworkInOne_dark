# P1 本機 API

主機 `http://127.0.0.1:8766`。先開啟啟動時產生的 `/unlock/<key>` 取得 HttpOnly session
cookie。所有 `/api/*` 需要相同 cookie；非同源 Origin 與未知 Host 拒絕。
JSON 回覆使用 `data / meta / error`；例外只回固定代碼，不回原始回應或 stack trace。

| Method | 路徑 | 用途 |
| --- | --- | --- |
| GET | `/health` | 版本與可用性 |
| GET | `/api/connections` | 連線狀態 |
| POST | `/api/connections/moodle/start` | 本人帳密登入（JSON username/password） |
| POST | `/api/connections/moodle/restore` | 驗證 Vault 身分並同步課程 |
| POST | `/api/connections/ntpu-ai/start` | 開啟本人登入視窗 |
| DELETE | `/api/connections/{provider}` | 清除對應連線憑證 |
| POST | `/api/sync/moodle?course_id=...` | 同步指定課程作業；無 course_id 則同步課程 |
| GET | `/api/courses` | 課程快取 |
| GET | `/api/courses/{id}/assignments` | 作業清單 |
| GET | `/api/assignments/{id}` | 詳細資訊與附件 metadata |
| POST | `/api/assignments/{id}/download` | 明確 approved=true 與 selected_asset_ids |
| POST | `/api/assignments/{id}/parse` | 解析已下載附件 |
| GET | `/api/assets/{id}/preview` | 本人原始附件下載／檢視 |
| POST | `/api/tasks` | 建立任務，必帶 Idempotency-Key header，回傳 202 |
| GET | `/api/tasks` | 本人任務歷史 |
| GET | `/api/tasks/{id}` | 狀態與草稿 |
| POST | `/api/tasks/{id}/approve` | 以 request_sha256 核准當次快照 |
| POST | `/api/tasks/{id}/cancel` | 停止任務 |
| GET | `/api/tasks/{id}/events` | SSE，事件只含狀態與固定錯誤碼 |
| PUT | `/api/tasks/{id}/draft` | 保存 Markdown；reviewed 預設 false |
| POST | `/api/tasks/{id}/export` | 僅匯出已審閱版本 |
| GET | `/api/artifacts/{task_id}/download?kind=pdf` | PDF；kind 可選 markdown/manifest |

建立任務需含 assignment_id、excerpt；mode 預設 report_draft，可附 selected_asset_ids、
instruction 與 requirements。data_transfer_approved 預設 false；只有明確 true 才開始送模型。
selected_asset_ids 可省略或為空，支援只有 Moodle 作業敘述／活動指示的作業；
`/parse` 也接受空的 selected_asset_ids。`/download` 仍需至少選一個附件並核准。
作業回覆含 description、description_parts（欄位來源）、description_status。
文字取自 intro／activity／官方 submission status 的 assignmentdata.activity；
intro 沒文字時補查同一作業的 course module description，不讀取學生提交內容或評語。
作業文字也會保存快照與 SHA-256，文字變動後原核准失效。
重複 Idempotency-Key 與相同內容回原任務；內容不同則 IDEMPOTENCY_CONFLICT。
code execution/external research 在 P1 一律拒絕。

任務狀態：waiting_approval、running、waiting_review、exporting、completed；
另有 failed、cancelled、blocked_auth、partial。重新啟動的中斷任務轉 partial，
不自動重新傳送資料。P1 不提供 checkpoint 自動續跑（屬 P3）。
