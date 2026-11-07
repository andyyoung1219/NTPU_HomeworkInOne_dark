# 作業網頁文字支援

2026-11-02 更新：從只取 intro、強制選附件，擴充為網頁文字與附件均可使用。

## 讀取來源

- `mod_assign_get_assignments.intro`：作業敘述。
- `mod_assign_get_assignments.activity`：活動指示（站台版本與開放狀態可能不同）。
- `mod_assign_get_submission_status.assignmentdata.activity`：本人可讀的活動指示。
- intro 沒有文字時，補查 `core_course_get_contents` 的 assign module description；
  以 cmid 和 assignment instance 同時比對，避免混入其他作業。

只抽取老師的題目欄位，不將學生提交文字、分數或回饋當成作業說明。
保留來源標籤、段落與表格文字；HTML script/style 不作為題目。
來源結構依 [Moodle 官方 externallib](https://github.com/moodle/moodle/blob/MOODLE_405_STABLE/mod/assign/externallib.php) 理解，站台未提供的欄位不猜測。

## 操作

查看作業時自動準備網頁文字與需求清單。有附件時可另外勾選並按「讀取作業文字與選定附件」。
不選附件就只使用網頁文字。AI 傳送仍須人工核准，文字與附件都有快照與來源 hash。
切換作業、變更選定附件、編輯節錄或需求清單會清除傳送核准。

## 驗證

- 33 項測試通過，含文字來源補查、來源去重、段落保留、無附件 API/AI 任務、
  文字快照、文字變更導致核准失效，以及空內容不冒稱已讀。
- 抽查一門已授權課程的四份作業時，校方 API 回傳的 intro 欄位均為空，
  課程內容 API 也沒有回傳 module description；畫面顯示未回傳文字的提示。
  這不等於已確認校方網頁沒有文字，也不會用 PDF 文字假填該欄位。
- 該課程仍可使用原有 PDF 附件；具有文字的情況以合成 Moodle 回應驗證。
