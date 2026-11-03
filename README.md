# NTPU HomeworkInOne Dark · v0.2.0

本機 Moodle 作業工作台：**同步作業 → 選取附件 → 解析與核對要求 → NTPU AI 草稿 → 人工審閱 → 繁體中文 PDF**。
Python 3.12 / FastAPI / Gradio / SQLite，加上獨立 Node BrowserBridge。
非臺北大學官方產品。完整範圍見 `NTPU_HomeworkInOne_Dark_spec.md`。

P2 新增「學習資料中心」：正式課表、歷年成績、官方排名／畢業進度，以及指定課程的
Moodle 教材、公告、本人評分與原檔下載。功能與自動測試已完成，**真人 P2 Gate 待本人核對**，
詳見 [P2 驗收與限制](docs/P2_VALIDATION.md) 與 [資料模型／Parser 契約](docs/P2_DATA_MODEL.md)。

## 程式碼導覽

想知道每個檔案在做什麼，請看 **[專案檔案導覽](docs/FILE_GUIDE.md)**。
包含逐檔用途、啟動到 PDF 的流程、資料目錄，以及修改各功能時的閱讀順序。

## 開啟使用

目前電腦已完成安裝。在本資料夾 PowerShell 執行：

```powershell
./scripts/run_mvp.ps1
```

開啟終端機顯示的 `http://127.0.0.1:8766/unlock/...` 網址。每次啟動換發本機存取金鑰。

1. **連線管理**：登入本人 Moodle，或在「我的作業」恢復既有連線。NTPU AI 由本人在專用瀏覽器正常登入。
2. **我的作業**：選課程、同步作業、查看截止日、提交狀態與 Moodle 作業敘述／活動指示。
   網頁文字會直接讀取；有需要再選附件並讀取。只有文字、沒有 PDF/Word 的作業也能建立任務。
3. **AI 草稿工作區**：核對／更正要求清單及文件節錄，選「摘要題目」「提出解題規劃」或「產生報告草稿」，核准本次傳送。
4. **審閱與匯出**：編輯 Markdown、核對來源與 TODO、確認審閱，再輸出 PDF。可下載 PDF、Markdown 與驗證 manifest。
5. 重新啟動後會恢復課程快取、已解析附件與最近草稿；其他草稿可由歷史任務載入。
6. **學習資料中心**：以相同本人帳號登入校務，按同步讀取課表、成績、排名與畢業進度。
   校務 Session 僅保留在記憶體，關閉後需重新登入，已保存資料仍可閱讀。
7. 列出目前修課或指定學期（例如 `115-1`），勾選課程再完整同步教材、公告與 Moodle 評分。
   教材逐份核准下載；來源與最後成功時間可見，課程對應有衝突時由本人確認。
8. 可私人隱藏作業，在資料中心恢復；重複同步不會取消隱藏。成績與教材不會自動送給 AI。

未核准不傳送、未審閱不匯出。同步失敗保留上次成功資料；模型失敗保留既有草稿。
任務可取消。掃描 PDF 會提示需 OCR，無法解析不會被當成已讀取。
API 未回傳作業文字時會明確提示，可點 Moodle 原頁核對，不把附件內容當作網頁敘述。

## 安裝與開發

需要 Windows x64、Python 3.12、uv、Node >=20.9、Git，以及 Chrome/Edge。
已真人驗證 Windows；其他平台尚未驗收。

```powershell
./scripts/setup_dev.ps1
./scripts/run_mvp.ps1
```

setup 會 checkout `docs/sources.lock.json` 的固定來源、依 `uv.lock` 安裝 Python 套件、
安裝最小 Bridge 依賴與固定 hash 的 Noto Sans TC 字型。不要以任意 pip install 取代
`uv sync --locked`；安全相容覆寫記錄在 `docs/SECURITY.md`。

```powershell
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/ruff.exe check backend frontend tests
.venv/Scripts/python.exe scripts/check_repository.py
```

`.github/workflows/ci.yml` 只跑合成／mock 資料、來源契約、秘密檔案檢查及依賴漏洞掃描。
本人學校登入不進 CI。推送後的遠端檢查結果可在 [GitHub Actions](https://github.com/andyyoung1219/NTPU_HomeworkInOne_dark/actions) 查看。

## 驗收結果

- **P0 通過**：本人 Moodle、真實 PDF 下載、NTPU AI 10 次文字請求、文件內容傳遞及取消。
- **P1 真實 E2E 通過**：人工智慧代理人原理及實務／指定作業1，產生草稿，經本人審閱後匯出 8 頁中文 PDF。
- 新專案測試、來源回歸、PDF 視覺檢查及依賴掃描詳見 `docs/P1_VALIDATION.md`。

## 本機資料與安全

| 資料 | 位置 |
| --- | --- |
| SQLite、附件、快取、歷史 | `.local/app/` |
| 任務輸入／草稿／PDF／manifest | `.local/app/workspaces/<user_hash>/<task_id>/` |
| P0 去識別化證據 | `.local/p0/evidence.jsonl` |
| Moodle Token | OS Vault，服務 `NTPU.HomeworkInOne.P0` |
| NTPU AI 獨立 Profile | `%LOCALAPPDATA%/NTPU HomeworkInOne/p0/browser-profiles/` |
| 校務與 Moodle 資料中心快取 | `.local/app/study.sqlite3`，依本人帳號及來源分開 |
| 教材／公告原檔 | `.local/app/materials/<user_hash>/` |

個人資料、附件、瀏覽器 Profile、字型與 `.env` 不進 Git。無自動繳交或任意程式執行。
AI 作業附件解析支援 PDF/DOCX/TXT/CSV；資料中心另支援常見 Office、ZIP、圖片與程式文字原檔下載。
複雜公式、圖片與 PDF 表格語意須人工核對。
資料保存於本機；登出清除連線憑證，附件與草稿保留供本人後續登入使用。

來源與授權：`THIRD_PARTY_NOTICES.md`、`docs/AUTHORIZATION.md`。
架構：`docs/ADR-001-ai-adapter.md`。API：`docs/API.md`。安全：`docs/SECURITY.md`。
