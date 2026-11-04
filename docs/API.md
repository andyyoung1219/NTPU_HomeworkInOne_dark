# P1 / P2 / P3 / P4 本機 API

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
| POST | `/api/assignments/{id}/complete` | `{"approved":true}` 與 `Idempotency-Key`；直接完成所選作業，回傳 202 |
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

## 直接完成作業

`POST /api/assignments/{id}/complete` 必帶 `Idempotency-Key` header，JSON body 為 `{"approved":true}`。
`approved` 預設 false，未核准回報 `COMPLETION_APPROVAL_REQUIRED`。
此核准涵蓋本次作業的全部支援附件讀取、AI 傳送、允許的公開資料查詢與必要的隔離程式執行。
服務建立 `mode=complete_assignment` 的背景任務並回傳 202，可透過既有任務查詢、SSE 與取消 API 追蹤。此模式須使用本端點，不能經一般 `POST /api/tasks` 建立。
相同使用者、作業及 `Idempotency-Key` 會取得原任務；同一 key 用於不同作業或模式回報 `IDEMPOTENCY_CONFLICT`。

流程為讀題與來源快照、成果規劃、資料取得與統計、必要的隔離執行、完成報告。
`TOOL_TIMEOUT` 等可重試錯誤從失敗的 checkpoint 續跑，保留已完成的步驟與產物。
連續失敗三次停止，成功一步後計數歸零；取消、身分驗證、權限、流量限制與預算／期限錯誤立即停止。
伺服器設定總期限 900 秒、AI 與公開資料請求合計預算 18 次，重試不重設這些限制；Moodle 題目與附件讀取不計入此請求額度。

任務回覆包含 `completion.status`（running、complete 或 blocked）、`completion.blockers` 與
`checkpoint.consecutive_errors`。成功後狀態為 `waiting_review`，由前端自動開啟審閱頁。
必要條件缺失或成果仍含待填內容時保留已有結果並回報具體 blockers，經核准審閱才可使用既有匯出 API。
成果提供具體解答、設計或分析，並依題目指定語言撰寫；提案與已執行實驗分別以相應證據敘述。

目前資料能力為本次作業附件、UCI Wine Quality 紅／白酒資料集目錄，以及固定公開主題的 Crossref metadata。
CSV 統計由本機固定程式計算；必要的模型產生程式只在 Docker 內以 Python 標準函式庫執行。
需要其他資料、全文內容、本人資訊或目前不可用工具時，回報阻塞事項供審閱處理。

## P3 工具任務

`GET /api/tools` 回傳白名單、JSON Schema、核准等級與沙箱可用狀態。
建立任務的 `mode` 新增 `literature_comparison`、`csv_analysis`、`coding_test`。
可附 `agent`，欄位包含 `selected_material_ids`、`research_query`、`python_code`、
`csv_asset_id`、`csv_column`、`max_steps`、`max_retries`、`max_requests`、
`tool_timeout`、`ai_timeout`、`deadline_seconds`、`max_prompt_chars`；未知欄位與越界預算拒絕。
`ai_timeout` 專供產生草稿，預設 180 秒、範圍 1–300 秒；其他工具維持 `tool_timeout` 預設 60 秒。
既有計畫沒有 `ai_timeout` 時沿用原 `tool_timeout`，續跑不重設 deadline、步驟或傳送預算。
AI 登入檢查先恢復既有瀏覽器連線，確認可用後才扣除傳送預算；背景交接未完成回報 `AI_CONNECTION_PENDING`。

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

## P4 學習中心

| Method | 路徑 | 用途 |
| --- | --- | --- |
| GET | `/api/dashboard` | 今日課程、七日內截止、逾期／未知提交狀態、待審閱與近期任務 |
| GET | `/api/learning/courses/{course_id}` | 本課作業、附件、教材、來源快照、歷史任務與產物 |
| GET | `/api/search?q=分類&course_id=1&due_from=2026-11-01&due_to=2026-11-30` | 本機搜尋，所有篩選選填；截止日期採臺北時間、包含迄日 |
| POST | `/api/search/rebuild` | 更新目前帳號索引；僅讀已下載文件，無網路／AI 呼叫 |
| GET / PUT | `/api/workflow/progress` | 讀取／保存目前帳號五步驟表單，核准狀態不持久化 |
| POST | `/api/tasks/{id}/export-extra` | `{"kind":"docx"}` 或 `{"kind":"zip","include_sources":false}`；須先保存已審閱草稿 |
| GET | `/api/artifacts/{id}/download?kind=docx` | 已登錄且 hash 相符的 Word；ZIP 使用 `kind=zip` |

進度欄位：`course_id`、`assignment_id`、`selected_asset_ids`、`task_id`、`page`、`mode`、
`instruction`、`excerpt`、`requirements`、`editor`。跨課程作業／任務或越界附件會拒絕。
搜尋最多 100 筆，附 `truncated`、`index.issues` 與來源 `page/section`。
指定期限時只回傳具該期限的作業／相關附件與任務；無截止日不虛構日期。
`save_draft` 同時使舊 PDF、DOCX 與 ZIP 下載失效。ZIP 不包含登入資料或任意工作區檔案。
PDF 匯出失敗時，Markdown 與已生成程式檔仍可直接下載。ZIP 會打包 Markdown、程式檔與可用文件；
已知的 PDF／DOCX 格式或品質檢查失敗不阻擋 ZIP，省略格式及原因列於 `exports.zip.omitted_exports`
與 ZIP 內 `manifest.json` 的 `omitted_exports`。檔案 hash 異動、權限或磁碟錯誤仍會拒絕匯出。
`include_sources=true` 會加入本次任務建立時保存的 Moodle 題目文字與選定原始附件，放在 `sources/`；
不重新下載，也不送 AI。新文字快照以 UTF-8 原樣保存換行；舊 Windows 快照只有在還原 CRLF 後
完全符合原核准 hash 時才相容處理，內容或附件真的異動仍回報 `ARTIFACT_CHANGED`。
打包 ZIP 會重用並驗證已登錄的 Word，避免後續來源檢查失敗時改寫既有下載檔。
