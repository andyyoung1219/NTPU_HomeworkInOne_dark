# 文件、視覺與公開研究擴充

更新：2026-11-05。這次完成既有工作台的 Word 複雜排版、本機 OCR／圖片與公式判讀，以及公開文獻／資料集取得。版本保持 0.4.0，沒有安裝包裝或發布。

## Word 使用方式與版面

審閱後照常按「匯出 Word」。支援行內 `$...$`／`\(...\)` 與獨立 `$$...$$`／`\[...\]` 公式，轉成可在 Word 編輯的 OMML；分式、根號、上下標、矩陣、常見運算子均保留結構。無法轉換的 LaTeX 保留原文並回報 EQUATION_REQUIRES_REVIEW。

報告圖片使用 `![圖說](image_name)`；只能嵌入本次任務已登錄且 hash 相符的來源圖片或工具圖表。模型在 visual_input.items 可取得 image_name。匯出不下載網頁圖片，不讀取 Markdown 指定的任意本機路徑。沒有登錄的圖片保留待補標記。圖片嵌入 Word 後不依賴外部檔案。

7–12 欄表格改用橫向分節，超過 12 欄拆成多表並重複第一欄；後續正文恢復直向。表格保留重複標頭、明確欄寬、邊框與儲存格內公式／格式。支援粗體、斜體、行內程式碼、等寬程式區塊、`<!-- pagebreak -->`、頁首及 PAGE／NUMPAGES 欄位。

以產品匯出器產生代表性五頁合成文件，已用本機 Microsoft Word 開啟、匯出 PDF 並逐頁檢查：中文、公式／矩陣、兩頁橫向重複表頭、圖片、程式縮排與頁碼均可讀，未見溢出或遮擋。證據：`.local/rich-docx/sample.docx`、`sample.pdf`、`page-1.png` 至 `page-5.png`。LibreOffice 未安裝，因此使用原生 Word 作為本次渲染驗證；其他 Word 客戶端與每份實際作業仍應核對。

## OCR 與圖片／公式判讀

- 選定附件支援 PDF、DOCX、TXT、CSV，以及 PNG／JPEG／WebP 靜態圖片；圖片仍通過 MIME、內容與大小驗證。
- RapidOCR 的鎖定 wheel 內附中英 ONNX 模型，使用 CPU 本機辨識；執行時不下載模型、不傳送 OCR 圖片到外部服務。回傳來源頁／節、文字、信心分數與文字框。OCR 不是圖表語意或數學推理。
- 掃描／少文字 PDF 每份最多 OCR 12 頁；Word 最多辨識 8 個實際被正文引用的內嵌圖片，忽略 ZIP 中未引用媒體，保留圖片裁切範圍。原生 Word 公式另外擷取為可核對 LaTeX；不支援的結構明示需核對。
- 每份文件 OCR 共用 60 秒上限，單個程序最多 40 秒，直接完成流程另受任務工具時限／取消控制。程序可取消，暫存輸入自動清除。已成功的解析結果按帳號與來源 hash 保存，不建立跨帳號的全域圖片／文字快取。
- 真正圖片、圖表與公式語意透過現有 NTPU AI 圖片上傳功能判讀。只從本次選定的 hash 核對輸入快照產生最多四張來源頁面／圖片的合成圖，附 file_id 與頁／節標籤。省略的頁面列為限制與直接完成任務的使用者待辦。
- **逐步模式另行勾選「允許將選定附件中的圖表／公式影像傳給 NTPU AI」才傳圖**，文字節錄核准與圖片核准分開。直接完成按鈕的本次來源核准涵蓋必要影像。傳送前再次驗證 hash、核准、PNG 解碼、單圖、20 MP 與 6 MiB 上限。
- Node bridge 仍執行上游 AttachmentScanner；使用鎖定的本機 Tesseract WASM／英文辨識資料，不使用 CDN。新增實測確認安全圖可通過、可見合成密碼欄位會被拒絕，沒有繞過掃描。

### 實際視覺服務驗證

以完全合成圖片（兩根長條、分式、直角三角形）透過本人既有專用瀏覽器登入送到 NTPU AI。模型正確回答較高長條 B、公式 `\frac{x+1}{2}`、代入 x=3 的值 2，以及直角頂點 P。證據：`.local/visual-live/synthetic.png` 與 `response.txt`。沒有傳送本人作業、身分、成績或課程資料。此樣本證明圖片傳輸與基本視覺判讀可用，不代表所有手寫字、模糊掃描、複雜公式或圖表皆正確。

## 公開文獻與資料集

直接完成模式會按題目產生公開主題／DOI 查詢，取代原本固定主題列舉。文獻來源為 Crossref metadata／摘要及 Europe PMC 摘要／可用開放全文節錄。去重後保存提供者、作者、DOI／PMCID、來源 URL、取得時間、內容範圍、全文來源 hash、節錄截斷與授權資訊；報告須引用來源，不把 metadata 宣稱成全文。

每次最多 5 筆／提供者，最多取得 2 份開放全文節錄，每份正文最多 16000 字元；截斷明示。單一提供者暫時失敗時 combined 模式保留另一來源；登入／權限／429、取消與預算不足仍停止。每個實際 API／全文請求都先扣除原有任務請求預算。

UCI 目錄擴充為 Wine Quality 紅／白酒、Iris、Heart Disease、Breast Cancer Wisconsin Diagnostic 與 Abalone。題目明示其他 UCI ID 時可透過官方 metadata API 取得該 ID 的 CSV，核對 ID／官方固定下載端點後下載並以固定程式實算資料描述。不得拿別的目錄資料取代題目指定資料。

只連固定 HTTPS 公開主機與端點，不帶學校憑證、Cookie 或代理環境設定、不跟隨重導向，限制回應大小；XML 不允許外部實體或 DTD。公開來源與附件／工具內容均不能新增權限。研究來源 JSON 與全文節錄可隨「加入題目與原始附件」的 ZIP 保存，下載前核對來源 hash。

實際公開來源 smoke：Crossref 回傳 5 筆、UCI Iris 150 筆；Europe PMC 搜尋當時 HTTP 503，已知開放論文 PMC7092803 的全文端點成功，取得有明確截斷的 16000 字元節錄。證據：`.local/public-research-live.json`。全文讀取不涵蓋付費牆、不下載任意網站，也不是完整通用網路瀏覽器。

## 自動驗證

新增四組回歸：`test_rich_docx.py`、`test_ocr_visual.py`、`test_visual_bridge.py`、`test_research_expanded.py`，涵蓋實際本機 OCR、原生公式、圖片範圍／核准／hash、執行中取消、版面結構、研究預算、XXE／重導向拒絕、全文節錄與 ZIP、公開資料實算及既有流程相容性。

最終完整回歸：**437 passed、0 failed、0 skipped（199.53 秒）**，包含 19 項真實 Docker 測試。JUnit：`.local/enhanced-all-final.xml`。最後的短公式文字保留與來源解析補強另經 **42 passed（28.87 秒）**，JUnit：`.local/enhanced-final-target.xml`。

Ruff、repository privacy check、git diff --check、離線 uv lock 一致性檢查通過；Python pip-audit 無已知漏洞，新增 Bridge 套件的 npm audit 0 項漏洞。初輪完整回歸發現一項 Windows OCR 管線取消阻塞，已修正；失敗結果保留於 `.local/enhanced-all-1.xml`，不當作最終通過證據。

所有學校本人品質 Gate 仍依既有文件驗收。這次視覺真實請求僅合成圖，公開來源請求只含公開測試主題。
