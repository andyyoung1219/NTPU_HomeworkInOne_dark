# NTPU HomeworkInOne Dark · v0.4.0

本機 Moodle 作業工作台：**同步作業 → 選取附件 → 解析與核對要求 → NTPU AI 草稿 → 人工審閱 → PDF／Word／ZIP**。
報告依題目指定語言撰寫，未指定時使用繁體中文。
Python 3.12 / FastAPI / Gradio / SQLite，加上獨立 Node BrowserBridge。
非臺北大學官方產品。完整範圍見 `NTPU_HomeworkInOne_Dark_spec.md`。

P2 新增「學習資料中心」：正式課表、歷年成績、官方排名／畢業進度，以及指定課程的
Moodle 教材、公告、本人評分與原檔下載。功能與自動測試已完成，**真人 P2 Gate 待本人核對**，
詳見 [P2 驗收與限制](docs/P2_VALIDATION.md) 與 [資料模型／Parser 契約](docs/P2_DATA_MODEL.md)。

開啟時先選擇「學習資料中心」或「懶人作業中心」。
首頁先顯示兩個中心的入口卡片，下方再放學習總覽、課程專頁與搜尋儀表板。
懶人作業中心的逐步處理採五步驟獨立畫面：選擇作業 → 讀取資料 → 設定 AI → 產生草稿 → 審閱匯出，
可透過上一頁／下一頁切換，保留已輸入內容。
尚無保存進度時，預設選取目前帳號課程清單的第一門課，以及該課程的第一份作業；沒有資料時對應欄位留空。
「工具計畫與續跑」入口已暫時關閉。逐步處理的「程式解題」會生成原始檔供本人審閱，僅產生程式碼時不需要 Docker；實際執行程式才需要隔離 Runner。

P4 採用首頁儀表板、課程專頁、本機搜尋、Word／ZIP 匯出與五步驟進度恢復。
首頁的「學習總覽／課程專頁／搜尋」可直接開啟作業、歷史任務與已生成成果。
搜尋已下載的 PDF／DOCX／TXT／CSV，顯示頁／節來源；CSV 僅索引前五筆，掃描 PDF 尚未提供 OCR。
原始附件可選擇加入 ZIP；未審閱不匯出，修改草稿會撤銷舊匯出下載連結。
「ZIP 加入本次任務的題目與原始附件」會在 ZIP 的 `sources/` 加入本次保存的 Moodle 題目文字與選定參考檔；不會額外下載資料或傳送 AI。
PDF 品質檢查失敗時，仍可下載 Markdown 與程式原始檔，或匯出包含可用文件的 ZIP；未包含的格式及原因會顯示在匯出狀態與 ZIP 清單中。
操作輸入自動保存到本機，重新開啟可恢復步驟與編輯內容；AI 傳送與成果審閱須重新核准。
範圍、資料遷移與驗收限制見 [P4 驗收文件](docs/P4_VALIDATION.md)。

## 程式碼導覽

想知道每個檔案在做什麼，請看 **[專案檔案導覽](docs/FILE_GUIDE.md)**。
包含逐檔用途、啟動到 PDF 的流程、資料目錄，以及修改各功能時的閱讀順序。

## 開啟使用

完成安裝後，在本資料夾 PowerShell 執行：

```powershell
./scripts/run_mvp.ps1
```

開啟終端機顯示的 `http://127.0.0.1:8766/unlock/...` 網址。每次啟動換發本機存取金鑰。
主程式直接在本機執行；啟動腳本不會自動啟動 Docker Desktop。一般查閱、同步、AI 文字生成與文件匯出不需要 Docker。

1. **首頁**：選擇學習資料中心或懶人作業中心。右上角 **連線管理** 會檢查連線，未登入或連線失效時顯示登入表單。帳號可直接查看，密碼右側眼睛按鈕可切換顯示。Moodle 與校務在彈窗登入；NTPU AI 由本人在專用瀏覽器登入。
2. **懶人作業中心**：先選課程與作業，查看截止日與提交狀態；按下一步前往資料頁，核對 Moodle 作業敘述／活動指示。
   網頁文字會直接讀取；有需要再選附件並讀取。只有文字、沒有 PDF/Word 的作業也能建立任務。
3. 前往 **設定 AI**，選「摘要題目」「提出解題規劃」「產生報告草稿」或「程式解題」，核對要求與節錄並核准傳送，按 **確認並開始產生草稿**。任務會切換到獨立進度頁，完成後再前往審閱頁。
4. **審閱與匯出**：編輯草稿與程式區塊、核對來源與待補事項、確認審閱，再匯出 PDF、Markdown 與原始檔。程式檔由 AI 生成，實際執行結果需另行驗證。修改程式後按保存會更新可下載檔案。
5. 重新啟動後會恢復課程快取、已解析附件與最近草稿；其他草稿可由歷史任務載入。
6. **學習資料中心**：以相同本人帳號登入校務，按同步讀取課表、成績、排名與畢業進度。
   校務 Session 僅保留在記憶體，關閉後需重新登入，已保存資料仍可閱讀。
7. 列出目前修課或指定學期（例如 `115-1`），勾選課程再完整同步教材、公告與 Moodle 評分。
   教材逐份核准下載；來源與最後成功時間可見，課程對應有衝突時由本人確認。
8. 成績與教材不會自動送給 AI。

未核准不傳送、未審閱不匯出。同步失敗保留上次成功資料；模型失敗保留既有草稿。
第 4 步可按「重新產生草稿」，明確以目前資料與設定再次傳送 AI 並建立新任務，先前草稿與成果保留在歷史任務。執行中會停用此按鈕；需要修改資料時先返回 AI 設定。

遇到登入或逾時問題，直接在第 4 步按「檢查／恢復 AI 連線」，會先沿用專用瀏覽器的既有登入；只有確認登入失效才需開啟登入視窗。登入後關閉視窗並再次檢查，即可按「從中斷處續跑」；已完成步驟保留，未完成的 AI 請求會再次傳送。若原任務已超過總時限或預算用完，改按「重新產生草稿」。新任務的 AI 等待上限預設 180 秒；舊任務維持原上限。`TOOL_TIMEOUT`／`AI_TIMEOUT` 表示等待逾時，不表示必須重新登入。
任務可取消。掃描 PDF 會提示需 OCR，無法解析不會被當成已讀取。
API 未回傳作業文字時會明確提示，可點 Moodle 原頁核對，不把附件內容當作網頁敘述。

## 直接完成作業

在首頁按 **直接完成作業**，或在選擇作業頁將 **作業流程** 切換為 **直接完成作業**。
選好課程與作業後，按 **開始直接完成作業**，即一次核准系統讀取該作業的全部支援附件、
將題目與附件內容傳給 AI，並視需要查詢公開資料及在隔離環境執行程式。
系統會依序讀題、選擇方法與資料、計算或執行，再撰寫具體成果報告，成功後自動進入 **審閱與匯出**。
報告依題目指定語言撰寫，未指定時使用繁體中文；題目允許自行決定的主題、資料與方法由系統完成選擇。
最後由本人核對內容及證據，核准後匯出文件。評分由教師依題目要求判定。
只要仍能產生有用內容，系統會先完成可生成的作業部分，自動前往審閱；報告末尾以題目要求的語言列出具體「使用者待辦」。
缺少學號／姓名、部分附件不可讀、公開資料取得失敗、缺少 Docker 或程式無法執行，都會如實寫明限制與需要補做的事情，不會僅因這些問題停止整份報告。
PDF 由系統在審閱後匯出；題目要求的命名、補上本人資訊及上傳學校平台會列為使用者待辦。未提供的身份欄位會省略，不亂填學號或保留佔位內容。
未執行的實驗不會寫成已完成結果，無法取得的數據也不會編造。審閱時須確認並接受尚待處理項目，才能匯出。

`TOOL_TIMEOUT` 等可重試錯誤會保留已完成步驟，從中斷點自動續跑。
同一步驟最多嘗試三次；非致命工具失敗後，改用已取得的資料生成報告並列出待辦。AI 仍無法生成內容時才停止，成功完成步驟後重新計算連續失敗次數。
完全讀不到題目文字、來源 hash／路徑驗證失敗、取消、登入或授權失效、流量限制及耗盡期限／預算會停止並顯示原因。
每個任務的總時限為 15 分鐘，AI 與公開資料請求合計預算為 18 次；重試也計入預算。Moodle 題目與附件讀取不計入此請求額度。

目前公開資料集目錄提供 UCI Wine Quality 紅酒／白酒 CSV，也可分析本次作業的 CSV 附件。
固定程式會實際計算筆數、欄位、缺值、統計量與範例紀錄，提案類作業可用這些證據完成具體提案。
需要執行題目程式或實驗時使用 Docker 隔離 Runner，環境提供 Python 標準函式庫。
公開文獻查詢使用固定主題的 Crossref metadata，全文閱讀或目錄以外資料需求會依實際可用來源列出限制。
報告有待補資料或操作時會標示「需處理待辦」，保留已有成果與證據；只有無待辦且交付內容具備時才標示完成。

## 準備程式執行環境（Docker）

Docker 是執行 AI 產生程式時使用的隔離環境，也用於隔離測試。需要實際執行程式的直接完成任務，使用期間必須保持 Docker 引擎運作。
只需閱讀、撰寫或以內建固定程式統計 CSV 的任務，可以在沒有 Docker 的情況下執行；缺少 Runner 時仍先產生可完成的報告，並列出尚未執行的程式／實驗與需要補做的事情。

1. 在 Windows 安裝 Docker Desktop，啟用 WSL 2 後端並使用 **Linux containers**，啟動後等待引擎就緒。
2. 在專案根目錄建立並檢查 Runner：

   ```powershell
   .venv/Scripts/python.exe scripts/setup_runner.py
   ```

3. 看到 `Runner ready` 後，依平常方式執行 `./scripts/run_mvp.ps1`。以後沿用已建立的映像，變更 Runner 或重裝 Docker 後再重新建置。

設定腳本會優先使用 `desktop-linux` context，建置官方 Python 基底映像，實際執行非 root 測試成功後才保存不可變映像 ID 與 context 至 `.local/app/runner.json`。
目前不支援以 Podman 代替此 Runner；其 tmpfs inode 限制相容性未通過。若顯示 `netavark`／`nftables`，請檢查 Docker CLI 是否連到 Podman，並啟動 Docker Desktop。
若兩者同時運作導致 Windows 的 Docker pipe 衝突，先確認 Podman 沒有需保留的執行中工作，再停止其 machine，重新啟動 Docker Desktop。
完整隔離限制與驗證方式見 [P3 驗收文件](docs/P3_VALIDATION.md)。

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

真實 Docker 完整回歸需先完成上述 Runner 設定：

```powershell
$env:NTPU_TEST_RUNNER = '1'
.venv/Scripts/python.exe -m pytest -q
Remove-Item Env:NTPU_TEST_RUNNER
```

`.github/workflows/ci.yml` 使用合成／mock 資料，並在 Linux job 執行真實 Docker 隔離測試（AI 使用合成回覆），另有來源契約、秘密檔案檢查及依賴漏洞掃描。
本人學校登入不進 CI。推送後的遠端檢查結果可在 [GitHub Actions](https://github.com/andyyoung1219/NTPU_HomeworkInOne_dark/actions) 查看。

## 驗收結果

- **P0 通過**：本人 Moodle、真實 PDF 下載、NTPU AI 10 次文字請求、文件內容傳遞及取消。
- **P1 真實 E2E 通過**：人工智慧代理人原理及實務／指定作業1，產生草稿，經本人審閱後匯出 8 頁中文 PDF。
- 新專案測試、來源回歸、PDF 視覺檢查及依賴掃描詳見 `docs/P1_VALIDATION.md`。
- **2026-11-04 完整回歸：357 項通過、0 失敗、0 略過**，包含 19 項真實 Docker 測試與直接完成作業的隔離執行；AI 使用合成回覆。缺少 Docker 時保留程式碼的補強另經 34 項受影響案例驗證通過。新政策與證據見 [P4 驗收](docs/P4_VALIDATION.md)；隔離範圍見 [P3 驗收](docs/P3_VALIDATION.md)。真人 P2／P3 品質 Gate 與 Word 頁面視覺驗收仍待完成。

## 本機資料與安全

| 資料 | 位置 |
| --- | --- |
| SQLite、附件、快取、歷史 | `.local/app/` |
| 本機 Runner 映像 ID 與 Docker context | `.local/app/runner.json`，建置腳本產生，不進 Git |
| 任務輸入／草稿／PDF／manifest | `.local/app/workspaces/<user_hash>/<task_id>/` |
| P0 去識別化證據 | `.local/p0/evidence.jsonl` |
| Moodle Token | OS Vault，服務 `NTPU.HomeworkInOne.P0` |
| NTPU AI 獨立 Profile | `%LOCALAPPDATA%/NTPU HomeworkInOne/p0/browser-profiles/` |
| 校務與 Moodle 資料中心快取 | `.local/app/study.sqlite3`，依本人帳號及來源分開 |
| 教材／公告原檔 | `.local/app/materials/<user_hash>/` |

個人資料、附件、瀏覽器 Profile、字型與 `.env` 不進 Git。無自動繳交；模型產生的 Python 只在核准的隔離 Runner 執行，直接完成按鈕的核准已包含本次必要執行。
AI 作業附件解析支援 PDF/DOCX/TXT/CSV；資料中心另支援常見 Office、ZIP、圖片與程式文字原檔下載。
複雜公式、圖片與 PDF 表格語意須人工核對。
資料保存於本機；登出清除連線憑證，附件與草稿保留供本人後續登入使用。

來源與授權：`THIRD_PARTY_NOTICES.md`、`docs/AUTHORIZATION.md`。
架構：`docs/ADR-001-ai-adapter.md`。API：`docs/API.md`。安全：`docs/SECURITY.md`。
