# P0 驗證紀錄

日期：2026-11-02（Asia/Taipei）。版本：0.0.1。
目標：人工智慧代理人原理及實務／指定作業1。

**P0 Gate 已於 2026-11-02 21:09（Asia/Taipei）全部通過，開始 P1。**

## 已完成

- P0-01：兩個來源固定 commit，來源通知、原始 License、提出者授權範圍紀錄。
- Python 3.12.14、Node 24.14.0、npm 11.9.0；Python `uv.lock`，Node 原始 lockfile。
- Moodle 官方公開設定 HTTP 200：Web Services/mobile 開啟、`typeoflogin=1`。
- 未帶帳密的登入回覆 `missingparam`；未帶 Token 的站台查詢回覆 `invalidtoken`。
  此兩項是拒絕未登入的正常結果，不算本人登入成功證據。
- 本人已在本機頁面登入：身分驗證通過，讀取目前進行中課程，選定課程回傳 4 份作業。
- 真實附件 HTTP 200、444575 bytes、PDF 15 頁、抽取 8385 字元。
  SHA-256：`87b8c94624d48a2d60a539b1d0dbbc8f7e81dd98a2b9a0038eca50046b5a360f`。
- Windows Credential Manager 合成值寫入／讀取／清除通過。
- Python 來源測試：37 通過。
- Node 來源測試：432 通過、2 跳過；瀏覽器子集 30 通過；build 通過。
- 新專案 P0：18 項測試通過，包括下載網域與 DNS、MIME/PDF、DOCX zip bomb、
  未選附件拒絕、證據欄位限制、Gate、stdio 核准、本機 Host/Origin/cookie。
- 本機 FastAPI `/health` HTTP 200；Gradio 真實瀏覽器畫面已顯示可操作表單。

## 真人 AI 驗證

- P0-04：本人 NTPU AI 登入成功；10 次合成文字回覆皆可解析且內容正確。
- 已送出使用者核准的真實文件節錄，回覆含非空摘要與可逐字回查的引用。
- P0-05：10 次請求皆有間隔；合成請求中止得到 CANCELLED，取消驗證通過。
- P0-06：證據只記錄白名單欄位，機密不進日誌；六項 Gate 全為 true。

`./.venv/Scripts/python.exe scripts/p0_status.py` 讀取目前本機去識別化結果。
真實附件只存 `.local/p0/input/`；原始 Token 不在 SQLite、JSON logs 或 Git。
不把來源既有測試、公開設定成功或合成 fixture 當作本人真人 E2E。
