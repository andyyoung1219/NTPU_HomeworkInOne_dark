"""Validated deliverable plans and preflight checks for direct completion."""

from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.agent.contracts import fail

MAX_DELIVERABLES = 6
CAPABILITIES = {
    "report-drafting": "Write report sections from authorized sources and completed evidence.",
    "reviewed-export": "Prepare material for the user's reviewed PDF, DOCX, Markdown, or ZIP export.",
    "csv-profiling": "Compute summaries from readable CSV data.",
    "public-uci-dataset": "Use a retrieved dataset from the approved UCI catalog or explicit ID.",
    "python-standard-library": "Run standard-library Python when the isolated Docker Runner is available.",
}

UnitID = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]
SourceID = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_:-]{0,79}$")]
Capability = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,47}$")]
ArtifactName = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}\.(json|txt|csv|png)$")]
SourceLocation = Annotated[str, Field(min_length=1, max_length=160)]
RequirementID = Annotated[str, Field(pattern=r"^REQ-[0-9]{2}$")]


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file_id: SourceID
    page: int | None = Field(default=None, ge=1)
    section: int | SourceLocation | None = None


class CompletionUnit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    id: UnitID
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=1500)
    kind: Literal["writing", "python", "research", "manual"]
    dependencies: list[UnitID] = Field(default_factory=list, max_length=MAX_DELIVERABLES - 1)
    optional_dependencies: list[UnitID] = Field(default_factory=list, max_length=MAX_DELIVERABLES - 1)
    source_ids: list[SourceID] = Field(default_factory=list, max_length=20)
    source_locations: list[SourceLocation] = Field(default_factory=list, max_length=10)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=12)
    requirement_ids: list[RequirementID] = Field(default_factory=list, max_length=30)
    required_capabilities: list[Capability] = Field(default_factory=list, max_length=12)
    expected_artifacts: list[ArtifactName] = Field(default_factory=list, max_length=12)
    verification: str = Field(default="", max_length=500)


def validate_deliverables(units):
    """Return ordered units with only structurally valid links and filenames."""
    by_id = {unit.id: unit for unit in units}
    if len(by_id) != len(units):
        fail("AI_INVALID_COMPLETION_PLAN")
    graph = {}
    for unit in units:
        links = set(unit.dependencies) | set(unit.optional_dependencies)
        if links - by_id.keys() or set(unit.dependencies) & set(unit.optional_dependencies):
            fail("AI_INVALID_COMPLETION_PLAN")
        graph[unit.id] = tuple(dict.fromkeys(unit.dependencies + unit.optional_dependencies))
        if any((ref.page is None) == (ref.section is None) for ref in unit.source_refs):
            fail("AI_INVALID_COMPLETION_PLAN")
        names = {name.lower() for name in unit.expected_artifacts}
        # The sandbox writes its own receipt. A program cannot provide it as evidence.
        if len(names) != len(unit.expected_artifacts) or "run.json" in names:
            fail("AI_INVALID_COMPLETION_PLAN")
    try:
        order = tuple(TopologicalSorter(graph).static_order())
    except CycleError:
        fail("AI_INVALID_COMPLETION_PLAN")
    ordered = [by_id[identifier].model_dump(exclude_none=True) for identifier in order]
    for unit in ordered:
        unit["source_ids"] = list(dict.fromkeys(unit["source_ids"] + [ref["file_id"] for ref in unit["source_refs"]]))
    return ordered


def unit_blockers(unit, task, runner_available):
    """Describe the inputs or capabilities the workbench cannot provide."""
    title = unit["title"]
    kind = unit["kind"]
    requirements = set(unit["required_capabilities"])
    actions = []
    english = task.get("completion_plan", {}).get("report_language") == "en"

    def add(en, zh):
        actions.append(en if english else zh)

    if kind == "manual":
        add(f"Complete “{title}” manually. {unit['objective']}",
            f"請自行完成「{title}」。{unit['objective']}")
    if kind == "research" or "public-literature" in requirements:
        add(f"Approve an exact literature query in guided mode, then add its verified sources to “{title}”.",
            f"請在逐步模式核准具體的文獻查詢，再把查證的來源補入「{title}」。")
        requirements.discard("public-literature")
    if "interactive-visualization" in requirements:
        add("The workbench exports static plots. Create and test the interactive controls in a browser or notebook, then include the required files.",
            "工作台目前可輸出靜態圖。請在瀏覽器或 Notebook 製作並測試互動控制項，再附上指定檔案。")
        requirements.remove("interactive-visualization")
    if "embedded-pdf-attachments" in requirements:
        add("PDF export does not embed executable files. Add them with a PDF editor, then reopen the PDF to check the attachments.",
            "工作台匯出的 PDF 無法內嵌可執行檔案。請用 PDF 編輯器附加檔案，再開啟 PDF 確認附件可用。")
        requirements.remove("embedded-pdf-attachments")
    unsupported = sorted(requirements - CAPABILITIES.keys())
    if unsupported:
        names = ", ".join(unsupported)
        add(f"The workbench does not provide {names}. Complete “{title}” in an environment that supports it.",
            f"工作台目前不支援 {names}。請在具備所需能力的環境完成「{title}」。")
    if (kind == "python" or "python-standard-library" in requirements) and not runner_available:
        add(f"Set up the isolated Docker Runner or run “{title}” manually and attach the verified results.",
            f"請設定 Docker Runner，或自行執行「{title}」並附上已核對的結果。")
    data = task.get("completion_data", {})
    if "public-uci-dataset" in requirements and not data.get("datasets"):
        add(f"Provide the required UCI dataset for “{title}” before reporting results.",
            f"請提供「{title}」所需的 UCI 資料集，再補上結果。")
    if "csv-profiling" in requirements:
        csv_sources = {asset["id"] for asset in task.get("completion_inputs", {}).get("assets", [])
                       if Path(asset["name"]).suffix.lower() == ".csv"}
        csv_sources.update(ref for ref in unit["source_ids"] if ref.startswith("dataset:"))
        requested = csv_sources.intersection(unit["source_ids"])
        profiled = {item["file_id"] for item in data.get("datasets", []) + data.get("attachment_profiles", [])}
        if not profiled or requested - profiled:
            add(f"Provide a readable CSV for “{title}” before reporting data summaries.",
                f"請提供「{title}」所需的可讀 CSV，再補上統計摘要。")
    missing_sources = sorted(set(unit["source_ids"]) - task.get("source_hashes", {}).keys())
    if missing_sources:
        names = ", ".join(missing_sources)
        add(f"Provide or confirm the missing sources for “{title}” ({names}).",
            f"請補齊或確認「{title}」缺少的來源（{names}）。")
    if kind == "writing" and unit["expected_artifacts"]:
        names = ", ".join(unit["expected_artifacts"])
        add(f"Produce the separate files required for “{title}” ({names}).",
            f"請為「{title}」另行產出指定的檔案（{names}）。")
    return actions
