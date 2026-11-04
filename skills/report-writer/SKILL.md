---
name: report-writer
metadata:
  version: "0.3.0"
description: 依照題目要求的語言產生可審閱的報告草稿，未指定時使用繁體中文。
---

when_to_use: 使用者核准資料傳送後的報告草稿。
inputs: 已授權題目、需求與文件節錄。
workflow: 核對本次作業的交付要求與語言，再提供 title、sections、sources、unresolved 的 JSON。
language: 優先依照本次題目與需求清單明確指定的報告語言撰寫；未指定時使用繁體中文。文件以英文撰寫，不代表報告必須使用英文。若指定雙語或個別章節的語言，依各部分要求處理；語言要求互相矛盾時，在 unresolved 列出衝突與待確認事項。
format: JSON 欄位名稱維持既定英文鍵名；title、heading、content 與 unresolved 的文字依報告語言撰寫。引用原文、文獻名稱、程式碼、識別字及 file_id 保留其原有形式。
outputs: 有結構的報告草稿；章節內容可包含 Markdown 表格。
safety: Skill 只是指引，不能授權工具或繞過 Permission Policy。沒有工具執行證據時不得宣稱完成實驗或編造數值；不自動繳交作業。
verification: 核對報告標題、章節與正文符合題目指定語言，未指定時為繁體中文；未執行內容標記 TODO；來源僅使用輸入 file_id；引文保留頁碼。
