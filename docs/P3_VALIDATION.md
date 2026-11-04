# P3 工具型 Agent（v0.3.0）

介面調整：依使用者要求，「工具計畫與續跑」入口暫時關閉。現在使用 README 所述的「作業一條龍」與四種處理方式。
下方工具計畫操作保留為先前功能的驗收紀錄。新增「程式解題」直接由 NTPU AI 生成說明與可下載原始檔，供人工審閱與匯出。

更新：2026-11-03。實作已包含 Sprint 6 的四個項目；**不將合成驗收當作真實沙箱或真人 AI Gate 通過**。

2026-11-03 階段回歸：**175 passed、18 skipped**（31.27 秒）。當時 18 項 skip 全部是明確 opt-in 的真實 Docker 測試；2026-11-04 最新完整回歸與真實隔離證據見下方「驗收分層」。
五類代表任務各三次合成測試為 **15/15（100%）**，包含在 P3 的 41 項功能測試內。
Ruff、Python 編譯、repository privacy check、uv lock 一致性皆通過；Python 全環境 pip-audit 未發現已知漏洞。
完整 JUnit 本機證據：`.local/p3-final.xml`；不包含真實學校資料。

## 使用方式

1. 在「我的作業」選擇作業並解析附件。在「Agent 工作區」選擇摘要、文獻比較、CSV 分析、程式測試或報告草稿，核對需求與節錄。
2. 打開「工具計畫與續跑」。需要教材時先在資料中心下載，再列出本課已下載教材並明確選取。成績、排名、其他課程不進 Agent context。
3. CSV 模式選擇已選取的 CSV 附件與數值欄位；系統產生的 Python 會完整列在計畫預覽。程式測試模式直接填寫 Python。兩種模式皆須啟用程式執行。
4. 若需公開文獻，啟用 Crossref 查詢並填寫確切查詢文字。只取得最多五筆 metadata，不抓全文、不使用學校憑證、不接受任意 URL。
5. 建立計畫。查看步驟、來源 hash、完整程式／查詢、資料節錄與預算，再勾選傳送核准。程式與外部研究各有獨立核准欄位。
6. 執行後查看紀錄與下載工具產物。草稿在「審閱與匯出」編輯；存在 TODO／需求覆蓋待核對時，必須明確接受未完成項目才可匯出。
7. 重新啟動後不會自動傳送或執行。從歷史任務載入、查看計畫，再核准續跑。完成的步驟不重跑；未完成步驟會消耗剩餘重試額度。期限或額度耗盡須建立新任務。

## 啟用 Runner

需要可用的 Docker **Linux containers**，Windows 使用已啟動的 Docker Desktop／WSL 2 後端。2026-11-04 已完成本機真實隔離測試；Docker 同時是程式執行的 runtime，不只是測試工具。

```powershell
# 在已安裝且啟動 Docker 後，明確下載基底並建立 Runner：
.venv/Scripts/python.exe scripts/setup_runner.py

# 真實容器隔離與五類任務各三次（AI 仍使用合成回覆）：
$env:NTPU_TEST_RUNNER = '1'
.venv/Scripts/python.exe scripts/validate_p3.py
Remove-Item Env:NTPU_TEST_RUNNER
```

建置使用官方 `docker.io/library/python:3.12-slim`；優先選用 `desktop-linux` context，實際執行非 root smoke test 成功後，才將不可變 image ID 與 context 寫入 `.local/app/runner.json`。
Windows 會尋找常見 Docker Desktop 安裝路徑；目前不支援缺少必要 tmpfs inode 限制的 Podman 相容引擎。啟動與疑難排解見 [README](../README.md#準備程式執行環境docker)。
執行只接受 `sha256:...` ID，使用 `--pull=never`，不在任務執行期間安裝套件或下載映像。
Runner 只有 Python 標準函式庫；程式可讀 `/input/<來源 SHA256>.<副檔名>`，寫入 `/output/`。
安全限制由容器設定實施，不能用 Python import 黑名單替代隔離。不存在宿主機執行 fallback。

限制：非 root、唯讀根目錄、無網路、移除 capabilities、禁止提升權限、1 CPU、256 MiB 記憶體、
32 PIDs、64 開啟檔案、每檔 1 MiB、output tmpfs 8 MiB／64 inodes、tmp tmpfs 16 MiB／128 inodes。
只掛載本次選定附件的副本與程式碼，且唯讀；無 Token、Cookie、Profile、Docker socket 或宿主輸出掛載。
輸出最多取回 16 個固定副檔名的一般檔案；不取 symlink。取消／逾時移除該次容器。

## 實作與檢查點契約

- `backend/agent/contracts.py`：伺服器產生的 TaskPlan、JSON Schema 白名單、不可由模型增設的工具與分級權限。
- `backend/agent/engine.py`：LangGraph 執行／驗證節點；SQLite task record 保存每步前後的持久檢查點。此版使用既有使用者分區 Store，沒有另外的 LangGraph saver DB。
- 預設最多 24 次工具嘗試、每步 1 次重試、3 次外部請求、累計 60000 prompt 字元、單步 60 秒（工具介面預設 120 秒）、總期限 600 秒。所有限制跨 resume 累計。
- 一般工具計畫中，`NETWORK_UNAVAILABLE` 與模型 JSON 格式錯誤可在額度內重試；不確定的 AI 逾時不自動重送。直接完成模式另依一次核准從 checkpoint 重試可恢復錯誤，每步最多三次；非致命資料／執行失敗後以已有證據生成報告並列出使用者待辦，AI 無法生成時才停止。總期限 900 秒、AI／公開資料請求最多 18 次。兩者遇到 429／SSO／權限拒絕皆立即停止，不重開 Session 規避限制。
- 日誌只保存輸入欄位名、hash、時間、結果 hash、產物 hash 與固定錯誤碼。原文及工具輸出留在本機任務資料／產物，不進事件日誌。
- Skills 只是指引，不能授權工具。一般工具計畫不接受文件或模型改寫步驟；直接完成模式僅接受固定 schema 的成果規劃、程式碼與報告，由伺服器固定工具圖執行，不接受任意工具呼叫。
- CSV 以完整已選資料計算 count、missing、mean、minimum、maximum、sample stdev；空值略過且計數，非數字／非有限值拒絕。主程式用固定實作獨立重算，完全一致才產生圖表。
- 程式退出碼 0 只證明該程式成功結束，不自動證明作業正確。rubric 的語意覆蓋仍需本人核對。
- PDF 只載入 hash 驗證過、由程式固定生成的圖表，不依 Markdown 讀外部 URL 或任意本機檔案。manifest v2 包含計畫、核准來源、工具紀錄、run_id、程式／輸入 hash、執行參數、映像與套件版本、缺漏接受紀錄。

## 驗收分層

| 層級 | 證據／範圍 | 狀態 |
| --- | --- | --- |
| 合成任務 | `tests/test_p3.py`：摘要、文獻比較、CSV、程式測試、草稿各 3 次 | 本機自動驗證；Runner 與 AI 明確使用合成替身 |
| 功能／失敗路徑 | 核准、來源範圍、JSON Schema、停止、有限重試、重啟續跑、預算、偽造數據、審閱與 PDF | 本機自動測試 |
| PDF 視覺 | 合成 CSV 草稿的兩頁 PDF 渲染圖片 | 已核對圖表、表格、中文與頁碼，無截斷／溢出 |
| 真實隔離 | `tests/test_p3_sandbox.py`：私人檔案、環境秘密、網路、唯讀根目錄、檔案限制、逾時、取消與直接完成作業的真實程式執行 | 2026-11-04 本機 Docker Desktop `desktop-linux` 實測 19 項通過；AI 使用合成回覆 |
| 真人 AI | 五類任務各三次的真實學校連線、內容品質、成功率與原因 | 待本人核准資料後驗收 |

```powershell
# 合成 gate：輸出每次案例結果、成功率與失敗原因
.venv/Scripts/python.exe scripts/validate_p3.py
# 完整回歸
.venv/Scripts/python.exe -m pytest -q
# 啟用真實 Docker 後的完整回歸
$env:NTPU_TEST_RUNNER = '1'
.venv/Scripts/python.exe -m pytest -q
Remove-Item Env:NTPU_TEST_RUNNER
.venv/Scripts/ruff.exe check backend frontend tests runner scripts/setup_runner.py scripts/validate_p3.py
.venv/Scripts/python.exe scripts/check_repository.py
```

結果保存在 `.local/p3/validation.json`；真實 Runner 啟用時若無法啟動，不會跳過而偽裝通過。
2026-11-04 初次 Docker 完整回歸為 301 項通過、0 失敗、0 略過，JUnit 證據為 `.local/docker-validation-all.xml`。
使用者待辦政策的完整回歸為 357 項通過、0 失敗、0 略過，另有 34 項程式碼保留補強驗證通過；證據與範圍見 [P4 驗收](P4_VALIDATION.md)。
一般回歸會明確 skip 真實容器測試。真人 P3 Gate 完成前不能將本文件標成「P3 Gate 全通過」。

參考官方契約：[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、
[Docker runtime constraints](https://docs.docker.com/engine/containers/run/)、
[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)。
