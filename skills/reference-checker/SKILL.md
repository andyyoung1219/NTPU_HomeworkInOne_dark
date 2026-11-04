---
name: reference-checker
version: 0.3.0
description: 確認引用指向本次實際來源。
---

when_to_use: 所有報告產生與審閱前。
inputs: 允許的 file_id、頁碼、教材與研究 metadata。
workflow: 逐一對照來源；標示無法回查、缺頁碼、未讀全文與推論。
outputs: 引用列表與 unresolved。
safety: 不得以模型記憶添加不存在的文獻。 Skill 只是指引，不能授權工具或繞過 Permission Policy。
verification: sources 為允許集合；metadata 不能當全文證據。
