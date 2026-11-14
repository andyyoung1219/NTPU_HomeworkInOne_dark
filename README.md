# NTPU HomeworkInOne Dark · v0.5.0-rc.1

這是獨立開發的本機 Moodle 作業工作台。登入後可同步課程與作業，選擇題目和附件，用 NTPU AI 產生草稿，再審閱並匯出 PDF、Word 或 ZIP。

## 用 Docker 安裝與啟動

在 Windows x64 啟動 Docker Desktop，使用 WSL 2 與 Linux containers。需要 Docker Engine 26 以上。Windows 主機只需要 Docker 和能開啟工作台的瀏覽器。Python、Node.js、中文字型、專用 Chromium 和作業 Runner 都由映像準備。[Docker Desktop 安裝說明](https://docs.docker.com/desktop/setup/install/windows-install/)

將專案解壓縮或下載後，在包含 `compose.yaml` 的資料夾開啟 PowerShell。首次建置需要網路下載映像及套件。

```powershell
$env:NTPU_APP_VERSION = '0.5.0-rc.1'
docker compose up --build -d --wait
docker compose logs --tail 30 app
```

用瀏覽器開啟日誌顯示的完整 `http://127.0.0.1:8766/unlock/...` 網址。解鎖網址和 AI 登入桌面的連線碼只供本人使用。

1. 開啟右上角的 **連線管理**，選擇 **Moodle** 並以本人帳號登入。
2. 開啟或切回首頁與課程頁時，工作台會向 Moodle 查詢已同步作業的最新繳交狀態。查不到的作業會顯示 **待確認**。要取得新增的作業，請按首頁的 **同步課程與作業**。要同步教材、公告和評分，可在課程頁按 **完整同步本課**。
3. 要使用 AI 時，在 **連線管理** 選擇 **NTPU AI**，按 **開啟 NTPU AI 登入視窗**，再按 **前往 Docker 登入桌面**。執行 `docker compose exec app cat /data/login-desktop-password` 取得桌面連線碼。在桌面輸入連線碼，接著於專用 Chromium 完成校方登入。登入後直接回工作台按 **恢復所選連線**，系統會關閉專用登入視窗並接手連線。
4. 從首頁的作業清單或 **作業工作台** 選擇課程和作業。

## 日常啟動、更新與停止

| 用途 | 指令 |
| --- | --- |
| 啟動已建置的版本 | `docker compose up -d --no-build --pull never --wait` |
| 建置選定的版本 | `docker compose build` |
| 停止並保留資料 | `docker compose down` |
| 確認服務狀態 | `docker compose ps -a` |

開新 PowerShell 視窗時，先設定 `NTPU_APP_VERSION` 為要使用的映像版本。也可以將 `NTPU_APP_VERSION=0.5.0-rc.1` 寫入本機 `.env`。目前視窗設定的環境變數優先於 `.env`。未指定版本時會使用原有的工作台 `:local` 和 Runner `:compose` 標籤。

附件、草稿、課程快取和專用 AI 瀏覽器設定都存在本機的 `ntpu-homework-data` Docker volume。容器重啟後，請使用日誌上的新解鎖網址並重新登入 Moodle 和校務系統，這兩個服務的工作階段存在容器記憶體。原生安裝的 `.local/app` 與 Docker volume 各自保存。

如果 Windows 上原生工作台仍在使用 8766，請先關閉它。若 Docker Desktop 顯示 `docker_engine` 存取被拒，而 Podman 同時運作，請先確認 Podman 沒有執行中的工作，再停止 Podman machine 並重新啟動 Docker Desktop。

### 從舊版更新與回退

1. 從目前的 Compose 專案執行 `docker compose ps -a`，確認舊容器所用的兩個映像。首次從 `:local` 和 `:compose` 升級時，在確認這兩個標籤指向要保留的映像後，執行以下指令。

   ```powershell
   docker image tag ntpu-homework-in-one:local ntpu-homework-in-one:baseline
   docker image tag ntpu-p3-runner:compose ntpu-p3-runner:baseline
   ```

2. 等任務完成並保存草稿，再用 `docker compose stop app` 停止工作台。使用 Docker Desktop 的 **Volumes** 將原資料卷匯出到私人本機資料夾。備份可能包含校務資訊、作業原檔和瀏覽器登入資料，應保存在 Git 專案與雲端同步資料夾之外。[Docker Desktop 資料卷備份](https://docs.docker.com/desktop/use-desktop/volumes/#export-a-volume)
3. 保持原有 Compose 專案名稱與資料卷名稱。在目前版本的專案資料夾設定 `$env:NTPU_APP_VERSION = '0.5.0-rc.1'`，執行 `docker compose build`。建置成功後，執行 `docker compose up -d --no-build --pull never --wait`。確認 `app` 為 `healthy`，並開啟工作台核對已有草稿和新版匯出。
4. 若需回退，將 `NTPU_APP_VERSION` 設回本機已保留的版本，例如 `baseline`，再執行 `docker compose up -d --no-build --pull never --wait`。若新版變更了資料格式，請先確認舊版相容。需要還原時，將升級前的備份匯入另一個資料卷，並以 `NTPU_DATA_VOLUME` 指向還原的資料卷。

`docker compose down --volumes` 會刪除資料卷內的草稿、附件、匯出檔、課程快取與 AI 瀏覽器資料。日常更新只需建置和切換映像。

## 處理作業

### 一步一步完成

1. 選擇 **一步一步完成**，按 **下一步：確認資料**。閱讀題目，勾選需要的附件，再按 **下載並讀取資料**。若 Moodle 沒回傳題目文字，可到 Moodle 原頁確認後貼到工作台。
2. 核對讀取的題目與節錄。想指定摘要、解題規劃、報告草稿或程式解題，可展開 **補充設定**。支援 PDF、DOCX、TXT、CSV 和 PNG／JPEG／WebP 圖片。掃描 PDF 與圖片可做本機 OCR，請核對辨識內容。若要把選定附件的圖表或公式影像送給 AI，請在 **補充設定** 勾選影像傳送。
3. 核准本次傳送後按 **開始執行**。需要查文獻時，可先在 **補充設定 → 公開文獻查詢** 填寫主題或 DOI，選擇 Crossref、Europe PMC 或兩者，再確認要送出的查詢。取得的文獻可在審閱頁查看。

### 直接完成作業

選好作業後切換為 **直接完成**，按 **開始直接完成**，確認題目、附件傳送和執行範圍。系統會讀題、分析資料並產生草稿，視作業需要取得公開資料集或在 Docker Runner 執行 Python。報告依題目指定語言撰寫，未指定時使用繁體中文。

系統最多拆成六項成果。各步驟會讀取對應的附件頁節與前一步的輸出。缺少資料或個別步驟失敗時，工作台保留已產生的內容，可在審閱頁沿用現有資料補跑，或附上新資料再跑。需要留存時可匯出部分成果。繳交用文件只包含正文，操作事項在審閱頁查看或另存清單。

### 審閱與輸出

1. 在 **審閱與輸出** 核對草稿、來源和待處理事項。可展開 **編輯草稿** 修改，修改後請重新確認審閱。
2. 選擇 **匯出 PDF**，或在 **其他格式** 匯出 Word／ZIP。PDF 的 7 至 12 欄表格會改用橫頁，超過 12 欄會分組並保留首欄。跨頁會重複欄名。
3. 要一併保留題目和選定參考檔，匯出 ZIP 前勾選 **ZIP 也包含原始題目與附件**。若 PDF 匯出失敗，可以下載 Markdown、已產生的程式碼，或改用其他可用格式。

## 作業 Runner

工作台透過 Docker socket 啟動隔離的 Python Runner。請只在你信任的本機環境使用這套 Compose 設定。工作台和登入桌面都只開放給本機。Runner 提供 Python 標準函式庫，在無網路的容器內執行，每次只能讀取該次作業的輸入檔。

## 學習資料與搜尋

- 在 **連線管理** 以與 Moodle 相同的本人帳號登入校務系統，再到 **學習資料** 同步正式課表、歷年成績、排名與畢業進度。
- 在 **學習資料** 可選目前修課或指定學期（例如 `115-1`），勾選課程同步。下載教材時需逐份核准。
- 右上角的 **搜尋課程與文件** 可找課程、作業和已下載的教材。

## 繼續任務與管理本機資料

- 任務中斷時可按 **從中斷點繼續**。若登入失效，先恢復連線再續跑。畫面若提示 **重新執行**，舊草稿仍可在 **歷史任務** 找到。
- 附件、草稿和快取保存在本機。若要清除資料，在 **連線管理 → 本機資料與診斷** 先預覽清除範圍，再確認清除目前帳號資料。
