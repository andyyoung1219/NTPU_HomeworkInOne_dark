# 本機安全邊界與依賴

P1 只綁定 127.0.0.1:8766。每次啟動更換存取金鑰，以 HttpOnly/SameSite=Strict
cookie 驗證；拒絕未知 Host 與跨 Origin 請求。無公開分享、CORS 放行或任意工具執行。
不接受任意檔案路徑的下載 API。任務、附件與歷史依本人 Moodle 身分的雜湊分區。

Moodle Token 存 OS Vault；密碼只在官方 HTTPS 登入請求的記憶體內使用。
NTPU AI 使用獨立專用瀏覽器 Profile，沒有匯出 Cookie。登出清除對應本機憑證；
不宣稱已撤銷校方伺服器全部 Token。Gradio analytics 與 run history 停用。

附件 URL 限 Moodle 指定 HTTPS host/port/path；下載前驗證 DNS 為公網；拒絕任何
redirect。檔案大小、MIME、檔頭、PDF 頁數、DOCX 解壓大小／比例與巨集皆檢查。
不執行附件程式、Office 巨集或文件中的指令。TXT/CSV 不接受 NUL；CSV 僅傳預覽。

每次 AI 核准綁定請求內容 SHA-256 與來源 hash；變動後原核准失效。
逐步草稿中未執行的實驗保留 TODO；直接完成模式改以具體限制與使用者待辦交代，不宣稱已有執行結果。PDF 只由本機 ReportLab 渲染，不載入外部資源。

## 2026-11-02 依賴修補

初次掃描完整上游 Extension 有 7 個 runtime 套件受影響，其中多個屬於本產品不用的
MCP、Shell sandbox、Markdown renderer 等。P1 改為固定、保留授權的 BrowserBridge
原始模組子集，只安裝 playwright-core/sharp。最終 `npm audit --omit=dev` 為 0。
完整上游 checkout 僅供來源查證；不將其完整 node_modules 當成 P1 runtime。

Gradio 6.31.0 的 metadata 限 Pillow <13。pip-audit 在 Pillow 12.3.0 發現已知漏洞，
修補版本為 13.0.0（見 [Pillow 官方發布說明](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)）。
本專案採 uv 明確覆寫為 Pillow >=13,<14；P1 不提供影像上傳或影像編輯元件。
此為有意識的版本範圍例外，完整 Gradio 頁面、API、PDF 與解析器測試均通過。
以 `uv sync --locked` 安裝此版本，不能使用忽略 uv 覆寫的任意 pip 安裝流程。
最終 Python 全環境 pip-audit 無已知漏洞；CI 重跑此檢查，不忽略特定漏洞。

目前只真人驗證 Windows x64。Moodle/NTPU AI 改版或登入失效仍需要重新驗證。
OCR、複雜公式、PDF 表格語意與模型內容品質須人工核對。字型與各套件權利
依 THIRD_PARTY_NOTICES 各項處理；沒有自動發布或提交學校資料。

## P2 補充（2026-11-03）

SIS 密碼、CSRF、Cookie 與 bridge formData 只在記憶體，不寫 SQLite／檔案／日誌。
登入限正常官方表單；只允許 my.ntpu.edu.tw portal 與 cof/ohs01 精確 bridge 路徑，
未知主機／跳轉及互動驗證立即停止。校務與 Moodle 帳號一致才可連結，跨帳號快取分區。
校務成績與 Moodle 評分按來源保存，不由課程配對合併，不加入 AI context。

教材下載需逐項核准，沿用檔案 URL／DNS 白名單與 20 MiB 上限。Office／ZIP 檢查
解壓總量／比例／巨集；不解壓或執行。保存以 hash 命名的原檔，下載回應強制 attachment。
外部教材 URL 只顯示，不自動帶學校憑證抓取。個人資料與驗收檔仍只放 `.local/`。
429 尊重 Retry-After，不開新 Session 或登入來避開限制。

## P3 補充（2026-11-03）

一般工具計畫的程式執行必須經獨立核准，直接完成模式則由開始按鈕一次核准本次必要執行；兩者皆須有可用 Docker Linux Runner，無宿主機 fallback。
完整限制與待驗收範圍見 [P3_VALIDATION.md](P3_VALIDATION.md)。不能將 mock Runner 測試當作隔離實測。
執行只用固定 image ID，無網路、無秘密掛載、非 root、唯讀基底、CPU／記憶體／程序／檔案／時間上限。
取消與逾時會清除對應容器。預設不安裝套件；依賴改變需重新建置與驗收映像。

LangGraph 只跑程式定義的工具圖；模型不能增設工具。JSON Schema、固定路徑與核准快照共同限制能力。
LangSmith／LangChain tracing 明確停用，不傳送學校資料到觀測服務。所有重試、請求與 prompt 字元預算先保存再執行。
Crossref 查詢只有使用者核准的文字，不帶學校 Cookie／Token，不跟隨重導向，回應限制 1 MB。
教材只有明確選取、同課程、已下載且 hash 相符者可供 AI 使用；校務／評分不參與。

## 直接完成作業補充（2026-11-04）

「開始直接完成作業」核准範圍限本次選定作業的題目、全部支援附件、AI 傳送、允許的公開資料查詢與必要的隔離程式執行。
校務成績、排名及其他課程資料不加入 context。文件、模型與工具輸出不能新增權限或任意工具；伺服器固定流程與 schema 決定可執行的動作。

公開資料限固定 UCI Wine Quality 目錄與固定主題的 Crossref metadata 查詢；不攜帶學校憑證，拒絕重導向並限制下載大小。
固定 CSV 統計可在宿主機執行，模型產生的程式一律進 Docker。建立 Runner 時優先選用 Docker Desktop 的 `desktop-linux` context，通過實際執行檢查後才替換本機設定；執行仍使用不可變 image ID 與原有隔離限制。

可恢復錯誤從 checkpoint 重試，同一步驟最多嘗試三次；非致命工具失敗後以已取得資料繼續生成，AI 無法產生內容時停止。總期限 900 秒、AI／公開資料請求最多 18 次，重試不重設預算。
完全無題目文字、來源 hash／路徑驗證失敗、取消、登入失效、權限不足、流量限制與期限／預算耗盡仍會停止。
缺少部分來源、本人資訊、工具或執行證據時，完成可生成的部分，在報告末尾依題目語言列出使用者待辦與限制；不得以模型推測取代實驗結果，也不把跳過或失敗的工具記成執行成功。
有待辦的成果標示 `needs_action`，不宣稱全部完成。待辦仍須由本人在審閱時明確接受，才可匯出；此政策不擴大工具、資料來源或對外上傳權限。
完成後仍須本人審閱再匯出，不會自動繳交。2026-11-04 真實 Docker 隔離驗證已通過，AI 使用合成回覆，詳見 [P3 驗收](P3_VALIDATION.md)。

## P5 工程補強（2026-11-05，未發布）

資料清除採帳號範圍預覽、快照確認與持久清除紀錄；拒絕執行中任務、越界路徑、symlink 與 junction。
AI Profile 為應用程式共用資源，另行選擇清除。不能將清除本機憑證描述為撤銷校方伺服器 Token，或將檔案／SQLite 刪除描述為鑑識不可復原。
診斷以固定狀態與錯誤碼白名單輸出，不包含任意例外文字或學校內容。PDF 下載補上 hash 與任務工作區邊界檢查。
草稿修改以持久回復紀錄跨越檔案與資料庫寫入；不一致時封鎖該任務存取，重啟恢復最後成功內容。
範圍、資料殘留界線及驗證狀態見 [P5 工程補強](P5_VALIDATION.md)。


## OCR、圖片傳送與公開研究（2026-11-05）

後續已啟用圖片附件解析與傳送；前述 P1「沒有影像輸入」僅描述當時範圍。Pillow 13 的覆寫仍由 uv.lock 鎖定，新增真實圖片解碼、像素／動畫限制及 OCR 回歸。
OCR 模型隨鎖定 wheel 提供，不動態下載或執行遠端模型程式碼；使用短生命週期、可取消子程序與自動清除的私有暫存輸入。解析快取依本機帳號與來源 hash 保存。
逐步模式的圖片外送另行核准；傳送圖像來源限定所選快照，Python／Node 雙重驗 hash、6 MiB／20 MP 與 PNG 格式，仍由上游附件掃描器檢查，不接受呼叫端本機路徑。
新增公開研究僅使用固定 HTTPS Crossref／Europe PMC／UCI 端點，不帶學校憑證、不跟隨重導向；來源回應有大小限制，XML 禁止 DTD／外部實體，每個請求先保存預算。
Word 圖片僅採用已登錄且 hash 核對的本機產物，公式不執行任意 TeX 或外部命令，輸出不載入網路圖片。
