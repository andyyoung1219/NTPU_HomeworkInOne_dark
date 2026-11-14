"""Render submission text separately from the workbench's review information."""

import hashlib
import re

HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
LEGACY_STAGE_NOTICE = (
    "This stage is incomplete. Check the assignment to confirm whether it is required.",
    "本階段尚未完成，請對照原題確認是否為必交成果。",
)
EMPTY_SUBMISSION_NOTICE = (
    "No submission content is available yet.", "目前尚無可繳交的內容。",
    "See the required actions for the items that need additional inputs or tools.",
    "需要額外資料或工具的項目列於使用者待辦。",
)
PLACEHOLDER = re.compile(
    r"^(?:[-*+]\s+)?(?:[^:\n.。;；]{0,60}[:：]\s*)?"
    r"(?:TODO|TBD|待填|待補|placeholder|to be completed|no content)[\s.。;；!！]*$",
    re.I,
)


def guided_notice(task):
    if task.get("runs"):
        return "> AI 協作草稿。程式執行範圍以附錄 run_id 與工具證據為準；推論與作業要求仍需本人核對。"
    if task.get("request", {}).get("mode") == "code_solution":
        return "> AI 產生的程式解題草稿。程式尚未執行，請審閱程式並驗證測試案例。"
    return "> AI 協作草稿。尚未執行程式或實驗；所有實作與數據須由本人完成及核對。"


def guided_review_block(model):
    lines = "\n".join(f"- TODO：{item}" for item in model["unresolved"])
    return f"## 待補與人工核對\n\n{lines}\n- TODO：核對所有題目要求、引用與未執行的實驗。"


def _generated_review_blocks(task, direct, saved_draft):
    model = task.get("report_model") or {}
    sections = model.get("sections", [])
    for section in sections:
        content = section.get("content", "")
        if content.startswith(LEGACY_STAGE_NOTICE):
            yield f"## {section['heading']}\n\n{content}"
    if not saved_draft:
        return
    if not direct:
        if "unresolved" in model:
            yield guided_review_block(model)
            yield guided_notice(task)

    # Older direct-completion drafts appended these exact bullets. Author text
    # can have the same heading, including names selected by the assignment.
    completion = model.get("completion") or task.get("completion") or {}
    actions = dict.fromkeys(item.strip() for group in (
        completion.get("required_actions", []), model.get("unresolved", []), completion.get("blockers", []),
    ) for item in group if isinstance(item, str) and item.strip())
    if not actions:
        return
    action_heading = (model.get("report_labels") or {}).get("actions_heading")
    headings = (action_heading,) if isinstance(action_heading, str) else ("使用者待辦", "Required user actions")
    body = "\n".join(f"- {action}" for action in actions)
    for heading in headings:
        if any(section["heading"] == heading and section["content"].strip() == body for section in sections):
            continue
        heading = re.sub(r"([\\`*_{}\[\]<>#|])", r"\\\1", heading.strip())
        yield f"## {heading}\n\n{body}"


def submission_markdown(task, markdown=None):
    """Remove generated review text while retaining the rest of the draft."""
    text = task.get("draft", "") if markdown is None else markdown
    direct = task.get("request", {}).get("mode") == "complete_assignment"
    blocks = {}
    for block in _generated_review_blocks(task, direct, saved_draft=markdown is None):
        lines = block.splitlines()
        blocks.setdefault(lines[0], []).append(lines)

    lines = text.splitlines(keepends=True)
    plain_lines = [line.rstrip("\r\n") for line in lines]
    output = []
    fence = None
    skip_until = 0
    for index, line in enumerate(lines):
        if index < skip_until:
            continue
        marker = FENCE.match(line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token.startswith(fence) and not line[marker.end():].strip():
                fence = None
        elif fence is None:
            match = next((block for block in blocks.get(plain_lines[index], ())
                          if plain_lines[index:index + len(block)] == block), None)
            if match:
                skip_until = index + len(match)
                continue
            if direct and PLACEHOLDER.fullmatch(line.strip()):
                continue
        output.append(line)
    result = "".join(output).rstrip()
    return result + "\n" if result else ""


def review_markdown(task):
    """A standalone list of actions and unresolved review items."""
    completion = task.get("completion") or {}
    model = task.get("report_model") or {}
    model_completion = model.get("completion") or {}
    actions = list(dict.fromkeys(
        action.strip()
        for group in (completion.get("required_actions"), completion.get("review_required"),
                      completion.get("blockers"), task.get("blockers"), model.get("unresolved"),
                      model_completion.get("required_actions"), model_completion.get("blockers"))
        for action in group or []
        if isinstance(action, str) and action.strip() and action != "RUBRIC_REVIEW_REQUIRED"
    ))
    if not actions:
        return ""
    return "# 作業待辦\n\n" + "\n".join(f"- {action}" for action in actions) + "\n"


def submission_export_stale(task, markdown, metadata, kind):
    if not metadata:
        return False
    recorded = metadata.get("submission_sha256")
    if recorded is None:
        # Older exports had no source hash and rendered the entire saved draft.
        changed = markdown.rstrip() != task.get("draft", "").rstrip()
    else:
        changed = recorded != hashlib.sha256(markdown.encode()).hexdigest()
    return changed or (kind == "zip" and not metadata.get("separate_review_checklist")
                       and task.get("request", {}).get("mode") == "complete_assignment"
                       and bool(review_markdown(task)))


def stale_submission_exports(task):
    artifacts = {**task.get("exports", {}), "pdf": task.get("qa") if task.get("pdf") else None}
    if not any(artifacts.values()):
        return []
    markdown = submission_markdown(task)
    return [kind for kind in ("pdf", "docx", "zip")
            if submission_export_stale(task, markdown, artifacts.get(kind), kind)]


def require_submission_content(task, markdown):
    """Keep generated status-only pages out of submission exports."""
    if task.get("request", {}).get("mode") != "complete_assignment":
        return
    from backend.services.study import StudyError

    body = [line.strip() for line in markdown.splitlines()
            if line.strip() and not HEADING.match(line) and not FENCE.match(line)]
    if not body or all(line in EMPTY_SUBMISSION_NOTICE or PLACEHOLDER.fullmatch(line) for line in body):
        raise StudyError("SUBMISSION_CONTENT_MISSING")
