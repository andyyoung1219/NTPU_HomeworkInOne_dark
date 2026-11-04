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
草稿中未執行的實驗保留 TODO。PDF 只由本機 ReportLab 渲染，不載入外部資源。

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

可恢復錯誤從 checkpoint 重試，連續失敗三次停止；總期限 900 秒、AI／公開資料請求最多 18 次，重試不重設預算。
取消、登入失效、權限不足、流量限制與期限／預算耗盡立即停止。缺少必要來源、工具或執行證據會標示阻塞事項，不得以模型推測取代實驗結果。
完成後仍須本人審閱再匯出，不會自動繳交。2026-11-04 真實 Docker 隔離驗證已通過，AI 使用合成回覆，詳見 [P3 驗收](P3_VALIDATION.md)。
