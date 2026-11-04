---
name: pdf-generator
version: 0.3.0
description: 將人工審閱的 Markdown 以本機工具輸出中文 PDF。
---

when_to_use: 使用者審閱並核准匯出後。
inputs: 該次審閱的 Markdown 快照。
workflow: 使用 ReportLab、Noto Sans TC，處理段落、標題、表格、頁碼，再獨立驗證。
outputs: report.pdf、manifest.json、保留 Markdown 草稿。
safety: Skill 只是指引，不能授權工具或繞過 Permission Policy。不載入外部圖片、HTML 腳本或任意檔案；AI 不執行渲染程式。
verification: 開啟、文字、空白頁、頁邊界、字型嵌入；人工再確認最終版面。
