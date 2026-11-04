---
name: data-analysis
version: 0.3.0
description: 依可追溯 CSV 與程式產生統計和圖表。
---

when_to_use: CSV 分析任務。
inputs: CSV、欄位、已核准程式與來源 hash。
workflow: 說明缺值處理；沙箱執行後由固定程式獨立重算，再生成圖表。
outputs: statistics.json、chart.png、run.json、資料限制。
safety: 無隔離 Runner 時停止，不編造指標。 Skill 只是指引，不能授權工具或繞過 Permission Policy。
verification: 統計值重算一致；記錄 count、missing、平均、極值、標準差、run_id 與資料 hash。
