# P5 工程補強（尚未發布）

更新：2026-11-05。依使用者要求，先完成現有功能的工程補強，UI 改版、安裝包裝、Demo 與正式發布延後。版本仍為 0.4.0。

## 已實作

- 連線管理新增「本機資料與診斷」折疊區：範圍預覽、帳號與資料快照綁定、確認清除，以及診斷 JSON 下載。
- 清除目前帳號的 records、events、FTS 索引、assets、materials、workspaces、incoming；不影響其他帳號分區。執行中任務拒絕清除。
- 對應 Moodle Vault 憑證與記憶體校務連線一併清除。AI 專用 Profile 是應用程式共用資源，另行勾選才關閉 bridge 並清除；不處理日常瀏覽器 Profile。
- 清除先寫入持久紀錄；失敗後封鎖帳號資料的正常存取，保留重試入口，重啟續做。每次刪除前核對絕對路徑，拒絕 symlink／junction 與越界路徑。
- 新下載暫存依帳號隔離。舊共用 incoming 副本依資產 hash 辨識，其他帳號仍引用時保留。無法歸屬的舊孤立檔案、舊 P0 測試資料、手動備份與另存匯出檔不在清除範圍。此為邏輯與檔案清除，不宣稱磁碟鑑識不可復原。
- 診斷僅輸出目前帳號的任務序號、固定狀態／錯誤碼、產物存在狀態與最近事件；不輸出原文、標題、憑證、帳號、原始錯誤或檔案路徑。未知錯誤固定為 OTHER_ERROR／OTHER_CODE。
- SQLite schema 1 → 2 為新增清除紀錄表，保留原 records/events；遇到較新版本拒絕啟動，不再把未知版本覆寫成 1。
- 文字寫入使用獨立暫存檔、flush/fsync、原子替換與失敗清理。草稿修改先保存回復紀錄，task 更新與紀錄移除在同一交易提交；中斷時重啟恢復上一個成功草稿與程式檔。
- 所有帳號的中斷任務在啟動時標示需處理；匯出中斷且有草稿時回審閱，其餘回 partial。撤銷審閱核准，不自動呼叫 AI。
- PDF 匯出與資料清除使用同一服務鎖；PDF 下載檢查登錄 hash，文件下載限制在該任務工作區。保留原有 DOCX／ZIP 與工具產物 hash 檢查。
- 新增磁碟不足、本機檔案／資料庫不可用、草稿恢復與清除未完成的操作提示。

## 驗證

新增 tests/test_p5.py：帳號隔離、確認快照、執行中拒絕、刪除失敗後重啟、舊共用副本、AI Profile 選擇範圍、診斷去識別化、版本遷移與未來版本拒絕、原子寫入失敗、草稿交易失敗恢復、中斷重啟、活動帳號檔損壞、路徑／junction 拒絕、API 驗證及 PDF 異動。

- 初輪相關回歸：55 passed（34.09 秒）。
- 初輪完整回歸：352 passed、19 skipped（113.60 秒）；當時略過項目皆為 opt-in 真實 Docker 測試。證據：`.local/p5-full.xml`，保留為歷史結果。
- 逾時／磁碟錯誤分類、草稿恢復與 AI 重試補強回歸：40 passed（20.81 秒）。證據：`.local/p5-final-targeted.xml`。
- 最後 API／狀態檔防護回歸：27 passed（11.92 秒），涵蓋全部 19 項 P5 新增案例及核心 API。證據：`.local/p5-final-check.xml`。
- Ruff、repository privacy check、git diff --check 與離線 uv lock 一致性檢查通過。Python pip-audit 無已知漏洞、Node npm audit 0 項漏洞。
- 測試使用合成資料與 FakeAI，清除測試只操作 pytest 暫存資料及模擬 Vault；未清除本人資料、讀取校方資料或傳送 AI。
- 沙箱內 Windows asyncio socketpair 初始化受阻；相關 UI/API 測試改在沙箱外執行。
- **2026-11-05 10:24（臺北）真實 Docker 完整回歸：376 passed、0 failed、0 skipped（139.59 秒）**。啟用 `NTPU_TEST_RUNNER=1`，包含全部 19 項真實 Docker 案例；JUnit：`.local/p5-docker-full.xml`，去識別化摘要：`.local/p5-docker-validation.json`，兩者不進 Git。
- 環境：Windows x64、Docker Desktop Linux engine 29.9.2、`desktop-linux` context、`linux/amd64`。沿用既有不可變映像 `sha256:a0427090e94a6e1440661ce81813563263a7da41a41dfbc64a0063ef61613c2b`，未重新建置。
- 19 項涵蓋主機秘密／環境變數／Docker socket 不可讀、無網路、非 root／唯讀根目錄、檔案數與大小限制、逾時／取消、直接完成作業的真實執行證據，以及五類任務各三次回歸。全部 AI 回覆仍為合成替身；五類任務中的 CSV／程式模式實際執行生成程式，不把其他模式計為程式執行實驗。
- 測試結束以 `docker --context desktop-linux ps -a --filter name=ntpu-p3-` 核對，無遺留測試容器。檔案限制測試不代表完成 CPU／記憶體壓力測試。
- 起初 Docker 因 `docker_engine` pipe 存取被拒而啟動失敗；確認 Podman 無執行中容器後停止其 machine，再重新啟動 Docker 即恢復。Docker 保持可用供後續作業執行，Podman machine 維持停止。
- 真人 P2 核對、P3 AI 品質 Gate 與 DOCX 頁面視覺驗收仍待完成；先前已通過的歷史證據不當作本次重驗。

## 使用

重新啟動後開啟「連線管理 → 本機資料與診斷」。下載診斷需要目前帳號；清除須先預覽再勾選確認。任務執行中先取消並等待停止。遇到部分清除失敗，可重新預覽後重試，或重新啟動以接續。

沒有將本次工程補強標示為 P5 全部 Gate 通過，也沒有發布 v1.0。安裝與 Demo 包裝、UI 改版、P4 延後功能與跨平台真人驗收另行處理。


## 文件與研究擴充補充

本次後續完成 Word 可編輯公式／嵌圖／寬表、本機 OCR、NTPU AI 視覺輸入，以及主題／DOI 研究與更多 UCI 資料；已完成代表性五頁原生 Word 版面與合成圖的真實 AI 判讀驗證。原先 DOCX 待驗文字保留為歷史，新增範圍與證據以 [文件與研究擴充驗證](DOCUMENT_RESEARCH_VALIDATION.md) 為準；其他客戶端及本人實際作業品質仍需核對。
