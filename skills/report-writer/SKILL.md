---
name: report-writer
version: 0.1.0
description: 產生可審閱的繁體中文草稿。
---

適用：使用者核准資料傳送後的報告草稿。
輸入：已授權題目、需求與文件節錄。
流程：提供 title、sections、sources、unresolved 的 JSON；使用繁體中文。
輸出：有結構的報告草稿；章節內容可包含 Markdown 表格。
安全：沒有工具執行證據時不得宣稱完成實驗或編造數值；不自動繳交作業。
驗證：未執行內容標記 TODO；來源僅使用輸入 file_id；引文保留頁碼。
