"""Server-owned tool contracts. Neither model output nor skills grant authority."""

import hashlib
import json
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field


def fail(code):
    from backend.services.study import StudyError
    raise StudyError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AgentOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    selected_material_ids: list[str] = Field(default_factory=list, max_length=10)
    research_query: str = Field(default="", max_length=300)
    research_provider: Literal["crossref", "europepmc", "combined"] = "crossref"
    research_full_text: bool = False
    python_code: str = Field(default="", max_length=20000)
    csv_asset_id: str = Field(default="", max_length=100)
    csv_column: str = Field(default="", max_length=100)
    max_steps: int = Field(default=24, ge=1, le=40)
    max_retries: int = Field(default=1, ge=0, le=2)
    max_requests: int = Field(default=3, ge=1, le=5)
    tool_timeout: int = Field(default=60, ge=1, le=120)
    ai_timeout: int = Field(default=180, ge=1, le=300)
    deadline_seconds: int = Field(default=600, ge=10, le=900)
    max_prompt_chars: int = Field(default=60000, ge=1000, le=100000)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    objective: str
    tool: str
    required_inputs: dict
    expected_artifacts: list[str]
    approval_level: Literal["low", "medium", "high", "external"]


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": required or [], "additionalProperties": False}


TEXT = {"type": "string", "maxLength": 24000}
REGISTRY = {
    "read_assignment": ("low", schema()),
    "read_document": ("low", schema()),
    "search_course_materials": ("low", schema({"query": TEXT}, ["query"])),
    "web_research": ("external", schema({"query": {"type": "string", "minLength": 1,
                                                "maxLength": 300}}, ["query"])),
    "write_artifact": ("medium", schema({"name": {"enum": ["analysis.py"]}, "text": TEXT},
                                         ["name", "text"])),
    "run_python": ("high", schema()),
    "generate_chart": ("medium", schema()),
    "generate_report": ("medium", schema()),
    "prepare_completion": ("low", schema()),
    "plan_completion": ("medium", schema()),
    "gather_completion_data": ("external", schema()),
    "execute_completion": ("high", schema()),
    # These tools remain behind the separate human review/export endpoint.
    "render_pdf": ("medium", schema()),
    "validate_pdf": ("low", schema()),
}


def validate_step(step):
    parsed = Step.model_validate(step).model_dump()
    if parsed["tool"] not in REGISTRY:
        fail("TOOL_NOT_ALLOWED")
    level, contract = REGISTRY[parsed["tool"]]
    if parsed["approval_level"] != level:
        fail("TOOL_APPROVAL_MISMATCH")
    if not Draft202012Validator(contract).is_valid(parsed["required_inputs"]):
        fail("TOOL_INPUT_SCHEMA_INVALID")
    return parsed


CSV_CODE = '''import csv, json, math, statistics
from pathlib import Path
rows = list(csv.DictReader(Path({path!r}).open(encoding="utf-8-sig", newline="")))
column = {column!r}
values, missing = [], 0
for row in rows:
    value = row.get(column)
    if value is None:
        raise ValueError("CSV_COLUMN_NOT_FOUND")
    if not value.strip():
        missing += 1
        continue
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("CSV_NONFINITE_VALUE")
    values.append(number)
if not values:
    raise ValueError("CSV_NO_NUMERIC_VALUES")
result = dict(column=column, count=len(values), missing=missing,
              mean=statistics.mean(values), minimum=min(values), maximum=max(values),
              stdev=statistics.stdev(values) if len(values) > 1 else 0)
Path("/output/statistics.json").write_text(json.dumps(result), encoding="utf-8")
print(json.dumps(result))
'''


def make_plan(task):
    request = task["request"]
    options = request["agent"]
    steps = []

    def add(tool, objective, inputs=None, outputs=None):
        steps.append(validate_step({"id": f"s{len(steps) + 1}", "tool": tool,
            "objective": objective, "required_inputs": inputs or {},
            "expected_artifacts": outputs or [], "approval_level": REGISTRY[tool][0]}))

    if request["mode"] == "complete_assignment":
        add("prepare_completion", "讀取本次作業文字與全部支援附件")
        add("plan_completion", "依題目自主選擇方法、資料與交付內容")
        add("gather_completion_data", "取得選定公開資料並計算可驗證的統計")
        add("execute_completion", "依需要產生並在隔離環境執行程式")
        add("generate_report", "依實際完成成果撰寫完整報告", outputs=["report.md"])
        return {"version": 1, "steps": steps, "review_tools": ["render_pdf", "validate_pdf"],
                "limits": {key: options[key] for key in ("max_steps", "max_retries", "max_requests",
                           "tool_timeout", "ai_timeout", "deadline_seconds", "max_prompt_chars")}}

    add("read_assignment", "核對本次作業與要求快照")
    add("read_document", "讀取已核准的附件節錄，保留來源位置")
    if options["selected_material_ids"]:
        add("search_course_materials", "僅檢索本次已選的同課程教材", {"query": request["instruction"]})
    if request["allow_external_research"]:
        add("web_research", "依核准設定查詢公開文獻、摘要與可用開放全文節錄",
            {"query": options["research_query"]})
    if request["allow_code_execution"]:
        add("write_artifact", "保存供審閱的程式快照", {"name": "analysis.py", "text": options["python_code"]}, ["analysis.py"])
        add("run_python", "在無網路沙箱執行已核准程式與選定資料副本", outputs=["run.json"])
        if request["mode"] == "csv_analysis":
            add("generate_chart", "獨立重算 CSV 統計並產生數據圖表", outputs=["chart.png", "statistics.json"])
    add("generate_report", "依工具證據產生可審閱草稿，缺漏明確列出", outputs=["report.md"])
    return {"version": 1, "steps": steps, "review_tools": ["render_pdf", "validate_pdf"],
            "limits": {key: options[key] for key in ("max_steps", "max_retries", "max_requests",
                       "tool_timeout", "ai_timeout", "deadline_seconds", "max_prompt_chars")}}
