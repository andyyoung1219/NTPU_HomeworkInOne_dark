"""Recover writing output using facts already read by the workbench."""

import csv
import io
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

WINE_VARIABLES = {
    "fixed acidity": "Nonvolatile acidity of the sample.",
    "volatile acidity": "Acidity contributed by volatile acids.",
    "citric acid": "Amount of citric acid in the sample.",
    "residual sugar": "Sugar remaining after fermentation.",
    "chlorides": "Chloride content of the sample.",
    "free sulfur dioxide": "Sulfur dioxide present in its free form.",
    "total sulfur dioxide": "Combined free and bound sulfur dioxide.",
    "density": "Mass per unit volume of the wine.",
    "pH": "Measure of the sample's acidity.",
    "sulphates": "Sulphate content of the sample.",
    "alcohol": "Alcohol content of the sample.",
    "quality": "Ordinal sensory score on the repository's 0–10 scale.",
}


def cell(value):
    return " ".join(str(value).split()).replace("|", r"\|")


def table(headers, rows):
    def line(values):
        return "| " + " | ".join(map(cell, values)) + " |"

    return "\n".join([line(headers), line(["---"] * len(headers)), *(line(row) for row in rows)])


def wine_methods():
    return (
        "### Research questions and proposed analysis\n\n"
        "1. **Do wines form reproducible chemical profiles?** Standardize the 11 measurements within each training fold, "
        "then compare k-means with hierarchical clustering across candidate cluster counts. Check silhouette and bootstrap "
        "stability, then compare quality distributions across clusters after fitting. Stable differences would help prioritize "
        "batches for follow-up tasting.\n\n"
        "2. **How accurately can measurements predict the sensory score?** Compare a median-score baseline with "
        "regularized ordinal classification and a tree-based model. Use stratified group folds so identical chemical profiles "
        "stay together, with grouped folds and explicit class support if stratification is infeasible. Report mean absolute "
        "error, quadratic weighted kappa and per-score recall. This would test whether the models can screen common scores "
        "without missing rare low-quality wines.\n\n"
        "3. **Which measurements or combinations explain model decisions?** Compare permutation importance across held-out "
        "folds and extract shallow decision rules. Require support and lift over the overall rate before treating a rule as "
        "potentially useful. Repeated patterns could guide quality-control checks and motivate controlled experiments.\n\n"
        "### Data preparation and limitations\n\n"
        "Inspect ranges and duplicate profiles first. Fit imputation, scaling and model tuning on training folds only. Keep "
        "quality out of the clustering features. Sensory ratings are subjective and extreme scores are rare. The dataset "
        "covers one region and lacks production metadata. Associations do not establish causality, and generalization to "
        "other producers would require independent data. The proposed analyses will evaluate whether routine chemical tests "
        "can support interpretable screening before expert tasting."
    )


def dataset_proposal(engine, user, task, unit):
    """Draft a proposal from a single actual dataset, keeping the subject scoped."""
    plan = task["completion_plan"]
    if plan.get("deliverable_type") != "proposal" or unit["kind"] != "writing":
        return None
    data = task.get("completion_data", {})
    entries = data.get("datasets", []) + data.get("attachment_profiles", [])
    chosen = [item for item in entries if item["file_id"] in unit["source_ids"]] or entries
    if len(chosen) != 1:
        return None
    dataset = chosen[0]
    ref = dataset["file_id"]
    if ref not in task["source_hashes"]:
        return None
    from backend.agent.completion import profile_csv
    from backend.agent.completion_units import verified_bytes

    workspace = engine.study.workspace(user, task["id"])
    expected = task["source_hashes"][ref]
    path = workspace / "input" / (expected + ".csv")
    raw = verified_bytes(engine.study, workspace,
                         {"path": str(path.relative_to(engine.study.root)), "sha256": expected},
                         max_bytes=10_000_000)
    first = raw.partition(b"\n")[0]
    delimiter = dataset.get("delimiter", ";" if first.count(b";") > first.count(b",") else ",")
    profile = profile_csv(raw, delimiter)
    columns = profile.get("columns", [])
    attributes = profile.get("attributes", {})
    if not columns or any(column not in attributes for column in columns):
        return None
    examples = profile.get("examples", [])
    for dependency in unit.get("dependencies", []) + unit.get("optional_dependencies", []):
        result = task.get("tool_results", {}).get("unit-" + dependency, {})
        if result.get("status") != "verified":
            continue
        for artifact in result.get("artifacts", {}).values():
            if not Path(artifact["filename"]).stem.startswith(("sample_", "example_")):
                continue
            sample = verified_bytes(engine.study, workspace, artifact)
            try:
                records = list(csv.DictReader(io.StringIO(sample.decode("utf-8-sig"))))
            except (UnicodeError, csv.Error):
                continue
            if len(records) != 5 or any(set(columns) - row.keys() for row in records):
                continue
            remaining = {tuple(row[column] for column in columns) for row in records}
            for candidate in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=delimiter):
                remaining.discard(tuple(candidate[column] for column in columns))
                if not remaining:
                    break
            if not remaining:
                examples = records
                break
    english = plan.get("report_language") == "en"
    wine = ref in {"dataset:wine-white", "dataset:wine-red"} and set(columns) == WINE_VARIABLES.keys()
    name = dataset.get("title") or task["source_names"].get(ref, ref)
    sections = [f"### {'Dataset' if english else '資料集'}\n\n{name}"]
    if wine:
        sections.append("The samples come from Portuguese Vinho Verde. The proposal studies whether laboratory measurements can "
                        "reveal chemical profiles and support screening before sensory evaluation." if english else
                        "樣本來自葡萄牙 Vinho Verde 葡萄酒。這項提案研究能否從理化測量辨識化學特徵，並在感官評估前輔助篩檢。")
    links = []
    if url := dataset.get("url"):
        filename = PurePosixPath(urlsplit(url).path).name
        links.append(f"[{('Complete data file' if english else '完整資料檔')} ({filename})]({url})")
    if page := dataset.get("page"):
        links.append(f"[{'Source and data documentation' if english else '資料來源及說明'}]({page})")
    if links:
        sections.append(" · ".join(links) + (f" ({dataset['license']})" if dataset.get("license") else ""))
    sections.append(f"The data contains {profile['records']:,} records and {len(columns)} columns." if english else
                    f"資料共有 {profile['records']:,} 筆紀錄、{len(columns)} 個欄位。")
    labels = ["Attribute", "Type", "Missing", "Minimum", "Mean", "Maximum"] if english else ["欄位", "型態", "缺值", "最小值", "平均值", "最大值"]
    if wine and english:
        labels.insert(1, "Meaning")
    rows = []
    for name in columns[:20]:
        attribute = attributes[name]
        rows.append([name, *([WINE_VARIABLES[name]] if wine and english else []), attribute["type"],
                     attribute["missing"], *(f"{attribute[key]:.5g}" if key in attribute else "—"
                                             for key in ("minimum", "mean", "maximum"))])
    sections.append(table(labels, rows))
    if len(columns) > len(rows):
        sections.append("Remaining fields: " + ", ".join(columns[len(rows):]) if english else
                        "其餘欄位：" + "、".join(columns[len(rows):]))
    if examples:
        sections.append("### Example records" if english else "### 實際樣本")
        sections.append("These records come from the source data." if english else "下列紀錄摘自原始資料。")
        for offset in range(0, len(rows), 6):
            shown = columns[offset:min(offset + 6, len(rows))]
            sections.append(table(["Record" if english else "紀錄", *shown],
                                  [[index, *(str(row.get(key) or "")[:80] for key in shown)]
                                   for index, row in enumerate(examples[:5], 1)]))
    if wine and (distribution := attributes["quality"].get("frequencies")):
        sections.append(table(["Quality score", "Records"] if english else ["品質評分", "筆數"], sorted(distribution.items())))
    if wine and english:
        sections.append(wine_methods())
    return {"content": "\n\n".join(sections), "sources": [ref]}
