---
name: coding-assistant
version: 0.3.0
description: 依可重現程式測試解讀沙箱證據。
---

when_to_use: 程式測試任務。
inputs: 已核准 Python、選定資料與 run_id。
workflow: 靜態檢查輸入與邊界，執行已核准程式後列出測試條件、退出碼與限制。
outputs: 程式快照、測試證據與待修項目。
safety: 禁止宿主機 Shell、未核准網路、套件安裝或未知附件自動執行。 Skill 只是指引，不能授權工具或繞過 Permission Policy。
verification: 只依 run_id、stdout、退出碼描述測試；退出碼 0 不代表所有需求滿足。
