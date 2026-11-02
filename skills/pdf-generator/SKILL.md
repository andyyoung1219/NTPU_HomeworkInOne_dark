---
name: pdf-generator
version: 0.1.0
description: 將人工審閱的 Markdown 以本機工具輸出中文 PDF。
---

適用：使用者審閱並核准匯出後。
輸入：該次審閱的 Markdown 快照。
流程：使用 ReportLab、Noto Sans TC，處理段落、標題、表格、頁碼，再獨立驗證。
輸出：report.pdf、manifest.json、保留 Markdown 草稿。
安全：不載入外部圖片、HTML 腳本或任意檔案；AI 不執行渲染程式。
驗證：開啟、文字、空白頁、頁邊界、字型嵌入；人工再確認最終版面。
