# P1 / P2 / P3 本機 API

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
P3 的 code execution/external research 需要獨立核准，詳見下節。

任務狀態：waiting_approval、running、waiting_review、exporting、completed；
另有 failed、cancelled、blocked_auth、partial。重新啟動的中斷任務轉 partial，
不自動重新傳送資料。P3 可明確核准 checkpoint 續跑；舊版沒有 plan 的任務不能續跑。

## P3 工具任務

`GET /api/tools` 回傳白名單、JSON Schema、核准等級與沙箱可用狀態。
建立任務的 `mode` 新增 `literature_comparison`、`csv_analysis`、`coding_test`。
可附 `agent`，欄位包含 `selected_material_ids`、`research_query`、`python_code`、
`csv_asset_id`、`csv_column`、`max_steps`、`max_retries`、`max_requests`、
`tool_timeout`、`deadline_seconds`、`max_prompt_chars`；未知欄位與越界預算拒絕。

`allow_code_execution`／`allow_external_research` 只表示計畫要使用工具，**不是核准**。
含這兩種工具的計畫即使 `data_transfer_approved=true` 也停在 `waiting_approval`。
回傳包含 `plan`、`plan_sha256`、`request_sha256`、完整 `request` 快照。

`POST /api/tasks/{id}/approve` 與 `POST /api/tasks/{id}/resume` 使用：

```json
{
  "request_sha256": "以任務回傳的確切值為準",
  "code_execution_approved": true,
  "external_research_approved": false
}
```

只有已審閱的計畫才送此請求。resume 不重設期限／預算，不重跑完成步驟。
`GET /api/tasks/{id}` 包含 `checkpoint`、`tool_audit`、`coverage`、`blockers`、`runs`。
`GET /api/tasks/{id}/artifacts/{name}` 僅下載已登錄、hash 相符的本任務產物。
未經登錄的路徑、跨使用者／任務產物皆拒絕。

`PUT /api/tasks/{id}/draft` 新增 `accept_incomplete`。若有 blockers，須同時
`reviewed=true`、`accept_incomplete=true` 才可匯出。編輯草稿會取消舊 PDF 下載連結，避免誤用舊版本。

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
| GET | `/api/courses/{id}/assignments?include_removed=true` | 包含來源已消失的歷史記錄。 |

批次同步 HTTP 200 不表示全部成功；需檢查每個快照的 `error/status/stale`。
失敗時 `data` 與 `last_synced_at` 保留上次成功資料。沒有快取則 data=null，不偽裝為空課程成功。
SIS_SCHEMA_CHANGED、SIS_ACCOUNT_CHANGED、SIS_LOGIN_REQUIRED、SIS_REPORT_NOT_AVAILABLE、
MOODLE_PERMISSION_DENIED、RATE_LIMITED 都是可操作的固定錯誤碼。
資料模型與身份／來源限制詳見 [P2_DATA_MODEL.md](P2_DATA_MODEL.md)。
