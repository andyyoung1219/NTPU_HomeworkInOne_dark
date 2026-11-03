# P1 / P2 本機 API

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

## P2 資料中心

| Method | 路徑 | 用途／JSON |
| --- | --- | --- |
| POST | `/api/connections/sis/start` | username/password；需與已連結 Moodle 相同本人帳號。成功讀取正式課表。 |
| DELETE | `/api/connections/sis` | 清除記憶體校務 Session，保留本人快取。 |
| GET | `/api/hub` | 校務連線、四種校務快照、最近課程選取、課程對應。 |
| POST | `/api/sync/sis` | 更新課表／成績／排名／畢業進度；各來源可部分成功。 |
| POST | `/api/hub/courses/discover?semester=115-1` | 指定學期；省略 semester 只列 inprogress。 |
| POST | `/api/sync/moodle/full` | `{"course_ids":["1","2"]}`，限本人已列出的 1–30 門課。 |
| GET | `/api/hub/courses/{course_id}` | 作業／教材／公告／Moodle 評分的獨立快照。 |
| PUT | `/api/hub/mappings` | `{"sis_id":"115-1:U1234","moodle_id":"1"}`；人工確認同學期候選。 |
| POST | `/api/materials/{id}/download` | `{"approved":true}`；下載並驗證單一教材。 |
| GET | `/api/materials/{id}/file` | 本人已下載原檔，強制 attachment disposition。 |
| PUT | `/api/assignments/{id}/visibility` | `{"hidden":true}` 私人隱藏；false 恢復，不變更 Moodle。 |
| GET | `/api/courses/{id}/assignments?include_hidden=true` | 包含私人隱藏及來源已消失的歷史記錄。 |

批次同步 HTTP 200 不表示全部成功；需檢查每個快照的 `error/status/stale`。
失敗時 `data` 與 `last_synced_at` 保留上次成功資料。沒有快取則 data=null，不偽裝為空課程成功。
SIS_SCHEMA_CHANGED、SIS_ACCOUNT_CHANGED、SIS_LOGIN_REQUIRED、SIS_REPORT_NOT_AVAILABLE、
MOODLE_PERMISSION_DENIED、RATE_LIMITED 都是可操作的固定錯誤碼。
資料模型與身份／來源限制詳見 [P2_DATA_MODEL.md](P2_DATA_MODEL.md)。
