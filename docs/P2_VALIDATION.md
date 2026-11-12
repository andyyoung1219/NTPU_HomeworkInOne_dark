# P2 實作與驗收（2026-11-03）

**v0.2.0 功能已實作；自動化驗證通過。SPEC §5.3 的真人 Gate 尚未完成。**
本次沒有使用學校帳密或真實成績測試。合成的 5 門課與 2 學期成績不是本人驗收證據。
P1 的真實驗收仍見 [P1_VALIDATION.md](P1_VALIDATION.md)。

## 已交付

| SPEC | 實作與驗證 |
| --- | --- |
| SIS-01、05 | 正常 `/login` 表單／CSRF；portal menu → 指定 bridge → 唯讀頁面。只允許 my、cof、ohs01 的精確端點；未知 redirect、CAPTCHA、SSO challenge 停止。Session 只在記憶體，關閉／登出清除。 |
| SIS-02 | 學期、課號、課名、老師、星期、節次、教室及官方時段原文。完整時段匹配才轉換；不支援的格式顯示未知。 |
| SIS-03、04 | 依固定來源的 AcademicReportParser v4 移植歷年成績、排名、累計與畢業進度。官方值原樣保存，類別學分與總學分分開，不計算 GPA 或資格。 |
| SIS-06 | 課號、明確學期、正規化課名全相符且唯一才比對；模糊候選人工確認，已知不同學期拒絕。確認結果不合併成績。 |
| MD-01 | 沿用 P1 作業、本人提交狀態；查不到仍為 unknown。已消失作業保留歷史但退出目前清單；舊版私人隱藏設定已停用，作業在重同步／重啟後正常顯示。 |
| MD-02、03 | 完整章節、教材 metadata／文字／URL、news forum 公告（分頁）、公告附件、本人 Moodle 評分。明確帶本人 userid，拒絕其他帳號／課程結果，排除 hidden grades。 |
| MD-04 | 逐一核准下載教材與公告原檔；20 MiB 上限、HTTPS 主機／URL、MIME、檔頭、SHA-256、Office／ZIP 大小與巨集檢查。程式原檔不執行。受保護或不支援格式保留官方查看入口。 |
| MD-05 | 預設只列 inprogress。使用者明確選定學期後才查歷年 metadata，僅保存可明確辨認該學期的課程；批次同步限勾選的 1–30 門。 |
| MD-06 | 穩定來源 ID、內容 hash、每來源最後成功快取與更新時間。請求間距至少 350ms；網路錯誤／502/503/504 最多重試兩次，1s/2s backoff。429 不立即重試，遵守秒數或 HTTP 日期 Retry-After，冷卻期間阻止後續請求。 |

教材原檔格式：PDF、DOCX、PPTX、XLSX、ZIP、PNG、JPG/JPEG、TXT、CSV、TSV、MD、JSON、IPYNB、PY、R。
只有 P1 作業工作區的 PDF/DOCX/TXT/CSV 可以進既有文件解析流程；資料中心的教材不會因下載而自動進入 AI 任務。
PDF 原檔下載不宣稱完成 OCR／語意解析。舊式 Office、可執行檔等請至官方頁面取得。

## 自動化結果

- 134 個專案測試通過（包含 P0/P1 回歸與 P2，以及畢業摘要與橋接診斷修正後的推送前驗證）。
- `ruff check backend frontend tests` 通過。
- `scripts/check_repository.py` 通過；無真實帳號、Token、Cookie、成績、原始校務 HTML 納入 Git。
- `uv lock --offline --cache-dir .cache/uv --python .venv/Scripts/python.exe` 成功；只將既有 lxml 列為直接依賴，未更新其他套件版本。
- Gradio/FastAPI 測試在允許 Windows 本機 socket 的執行環境完成。受限沙箱的 asyncio socketpair 會卡住，並非應用測試結果。
- 本機合成資料 UI 已檢查：5 門課的時段、分學期成績、中文欄位、來源與快取狀態；獨立預覽資料位於 `.local/p2-preview/`，未修改本人 `.local/app/`。

關鍵測試：

- `tests/test_sis.py`：課表及成績／排名／畢業 Parser、身份與學期、CSRF、未知主機／跳轉、429、bounded retry。
- `tests/test_data_hub.py`：多課程重複同步、來源分離、舊版隱藏設定相容／消失作業、舊快取、跨帳號、課程對應衝突、AI 隱私邊界、API 核准。
- `tests/test_materials.py`：教材原檔、壓縮 HTTP、MIME／偽登入頁、Office/ZIP、巨集／zip bomb、檔名及來源 URL。

重現：

```powershell
$env:PYTHONUTF8 = '1'
.venv/Scripts/python.exe -m pytest -q --basetemp .local/test-p2
.venv/Scripts/ruff.exe check backend frontend tests
.venv/Scripts/python.exe scripts/check_repository.py
```

## 真人 Gate 待核對

1. 啟動 `./scripts/run.ps1`，恢復本人 Moodle。在「學習資料中心」以相同帳號連結校務；帳密只填本機頁面，不貼入聊天或文件。
2. 按「同步課表、歷年成績、排名與畢業進度」。在官方頁面對照至少 5 門課的學期、課號、時段；未知時段不能算核對通過。
3. 核對至少 2 筆本人成績及來源；若校方未開放，記錄固定錯誤碼和資料未開放情況。
4. 勾選多門 Moodle 課程完整同步，逐一查看教材、公告、評分及一份教材下載。再次同步，確認 ID 不重複；先前私人隱藏的作業正常顯示。
5. 斷線／Session 失效時確認最後成功時間及 stale；切換帳號不得看到上一帳號資料。

可在 `.local/p2/` 保存去識別化證據，只記錄：核對數量、是否一致、錯誤分類、檔案 hash、日期。
目前沒有這份本人核對證據，因此不得將 §5.3 或正式 `v0.2.0` 真人驗收標為通過。

## 已知限制

2026-11-03 橋接診斷更新：本機僅讀取狀態與時間，確認課表、成績、排名先前同步成功，
後續四項均為 `SIS_BRIDGE_NOT_ALLOWED`。該代碼原本合併了主機、路徑、欄位與憑證值檢查，
因此不能據此斷定此次校方回應的差異。改以固定錯誤碼區分登入／報表的 URL 格式、主機、
路徑／查詢參數、兩端主機不同、欄位缺漏／新增／型態與空憑證；不輸出或保存 descriptor。
同時以 URL components 接受既有 HTTPS 主機的大小寫與明示預設埠 `:443`，驗證後只請求
canonical URL。路徑與參數仍精確比對，登入與報表必須同主機，未知主機不發送憑證。
已用合成資料重現原字串比較對等價 URL 的誤拒絕，但尚未證明這是此次真人失敗原因。
使用者需重啟並重新連結校務才能取得本次失敗的細項，真人 Gate 仍未通過。

2026-11-03 登入診斷修正：公開登入頁 GET 200，仍有 `_token/username/password` 正常表單，
沒有觀測到驗證挑戰。入口登入成功後，課表格式／資料主機錯誤不再清除已登入 Session；
UI 分開顯示「入口登入成功，但課表未讀取」與真正驗證失敗。第一次讀取失敗可直接重試同步，
在首次成功取得本人資料後才建立身份 binding。bridge 送出前先核對預期本人 identity。
使用者後續提供 `SIS_REDIRECT_REQUIRES_REVIEW`。以不含任何帳密的空表單實測，
校方回傳 HTTP 302、`Location: https://my.ntpu.edu.tw`（沒有尾斜線）。原程式精確字串比對
只接受有尾斜線首頁，因此誤拒絕。現以 URL components 驗證 HTTPS／固定主機／預設埠／允許路徑，
將空首頁 path 正規化為 `/`；轉址後只發 GET，不重送密碼。外部主機、未知路徑、query、fragment、
URL credentials 與非預設埠仍拒絕。修正後實際空表單流程回報預期的 SIS_LOGIN_REQUIRED，
不再回報 redirect 錯誤。正常本人帳號登入仍待使用者重啟程式後驗證。

後續同步紀錄的四項校務快照均回報 `NETWORK_UNAVAILABLE`。本機實測 Moodle 與校務入口可連線，
但 `cof.ntpu.edu.tw` 與 `ohs01.ntpu.edu.tw` 的 TLS 握手不接受 Python 預設的加密套件。
兩台主機均能以 `TLS 1.2` 與 `ECDHE-RSA-AES256-SHA` 通過伺服器憑證驗證。
現僅對這兩台主機補上該套件，保留憑證與主機名稱驗證。以新版 `SISClient` 讀取校務登入頁及兩台資料主機的
公開入口均回傳 HTTP 200。操作結果會逐項列出課表、成績、排名與畢業進度的狀態及錯誤碼。
本人資料同步仍待重新登入驗證。

- 官方頁面改版或新增主機須另行確認契約與白名單；目前不處理 CAPTCHA／互動式 SSO，也不讀取日常瀏覽器 Cookie。
- 校務目前只取官方入口的當期課表；歷年成績各學期保留。學期不明的 Moodle 課程不猜測學期。
- 增量是本地內容比對／來源 ID upsert 及檔案快取。Moodle 未提供 delta 的端點仍會重新讀取選定課程的 metadata，沒有背景輪詢。
- 本機快取一小時後標示 stale，仍可閱讀；學校 Session 失效不會自動登出另一個來源或清空資料。
- P3 的工具執行、P4 的搜尋／提醒／統一儀表板不在本次範圍。
