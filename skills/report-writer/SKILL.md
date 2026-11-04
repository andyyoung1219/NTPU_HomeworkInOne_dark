---
name: report-writer
version: 0.3.0
description: 產生可審閱的繁體中文草稿。
---

when_to_use: 使用者核准資料傳送後的報告草稿。
inputs: 已授權題目、需求與文件節錄。
workflow: 提供 title、sections、sources、unresolved 的 JSON；使用繁體中文。
outputs: 有結構的報告草稿；章節內容可包含 Markdown 表格。
safety: Skill 只是指引，不能授權工具或繞過 Permission Policy。沒有工具執行證據時不得宣稱完成實驗或編造數值；不自動繳交作業。
verification: 未執行內容標記 TODO；來源僅使用輸入 file_id；引文保留頁碼。
