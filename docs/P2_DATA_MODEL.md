# P2 資料模型與來源契約

## SQLite 相容性

沿用 P1 `records(user_id, kind, id, payload, updated_at)`、`events` 與 schema version 1。
新增 record kinds／JSON 欄位，不重建資料表；舊任務、附件及 PDF 可繼續開啟。
缺少新欄位時用保守預設，未知學期／課號為 null。舊資料的 `hidden` 欄位不再影響作業清單。

| kind | id | payload／用途 |
| --- | --- | --- |
| course | Moodle ID | 原 name/source，加 semester/code/shortname/idnumber/start_at/end_at/scope；scope 為 inprogress、inactive 或明確學期。 |
| assignment | Moodle assignment ID | 以 removed 標記來源已消失的作業，仍可用於歷史任務。 |
| hub | `sis:timetable/grades/rank/graduation` | 身份核對後的校務結構化快照。 |
| hub | `moodle:{course_id}:assignments/contents/announcements/grades` | 每課程每來源分別同步，失敗只標該快照 stale/error。 |
| material | SHA-256 的前 24 字元 | ID 由 Moodle course + module/discussion ID + 已驗證的檔案 URL 產生；教材與 P1 asset 分開，不能混入任務附件清單。 |
| connection | sis | SIS 身份 hash，綁定目前 Moodle 使用者分區；沒有原始學號／密碼／Session。 |
| course_mapping | `{semester}:{SIS code}` | 人工確認的 Moodle ID 與 confirmed_at，不複製成績資料。 |
| preferences | hub | 本人最近選取的 course_ids。 |

快照包含 `source`、`source_url`、`data`、`status`、`stale`、`error`、`last_synced_at`、
`content_sha256`、`changed_at`。失敗保留 data/last_synced_at，另記 last_attempted_at。
content hash 排除下載位置與同步時間等本機狀態；相同內容不改 changed_at。
時間保存 UTC ISO-8601，UI 轉為臺北時間。

教材原檔位於 `.local/app/materials/<user_hash>/<sha256>.<ext>`。使用者提供的原始檔名不作為磁碟路徑。
下載需確認目前課程快照仍列有該 ref；大小／來源修改時間變更會使舊下載快取失效。
既有的檔案需通過 hash 驗證才可免重新下載。

## 校務 Parser 契約

來源固定為 `docs/sources.lock.json` 的 NTPUOne commit
`b7f07f1f5b856b7016fce80eaa0983e2d401760f`：

- `scripts/sis_probe.py`：正常表單登入、官方 menu、橋接描述、選課清單 headers／本人確認單連結。
- `NTPUOne/Academic/AcademicAPI.swift`：`SchoolAPI`、`AcademicReportParser` v4 的成績、排名、畢業進度 selectors。
- `NTPUOne/Academic/AcademicPolicy.swift`：保守時段解析與私人修改保留原則。

只接受官方 menu 的 project 10 與固定 `3_m/5_m/8_m/9_m`。
bridge 的 `form_url`、`base_url`、`formData` keys／identity 都先驗證，再傳送登入資料。
未知主機、不同 report 路徑、額外 form 欄位均不送出。
每頁再次檢查本人身份；表格欄位或必要 section 不符就回 SIS_SCHEMA_CHANGED，不以空成績成功覆蓋。

## Moodle 契約與隱私

沿用上游 READ_FUNCTIONS allowlist。只有學生本人查詢；grade request 明確帶已驗證 userid，
回應也須只有同一 course/user，隱藏成績不保存。公告只取 type=news 的 forum，不抓一般同學討論。
章節／module ID 與檔案 URL 提供穩定來源；外部閱讀 URL 只顯示，不自動下載。
Moodle 成績不能覆寫 SIS 成績，課程對應也不解除這個限制。

AI 仍只接受 P1 assignment + 當次選定的 asset 快照；hub/material 不在模型 context 建構路徑中。
資料中心同步／下載不會呼叫 AIAdapter。所有 API 沿用本機 cookie、Host、Origin 檢查。
