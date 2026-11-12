"""Select bounded source excerpts for direct-completion requests."""

import json
import re
from itertools import zip_longest

ASSIGNMENT = "assignment-description"
SOURCE_HEADER = re.compile(r"^\[來源 ([A-Za-z0-9][A-Za-z0-9_:-]{0,79}) / 頁或節 ([^\]\r\n]{1,80})\]\r?\n", re.M)
LOCATION_HINT = re.compile(
    r"(?:\b(?:pages?|pp?\.?|sections?|sec\.?)\s*|(?:頁|節)\s*)"
    r"(?P<after>\d+|(?:equation|image)-\d+)\b|(?:第\s*)?(?P<before>\d+)\s*[頁節]",
    re.I,
)


def snapshot_chunks(snapshot):
    if "source_chunks" in snapshot:
        return snapshot["source_chunks"]
    excerpt = snapshot.get("excerpt", "")
    headers = list(SOURCE_HEADER.finditer(excerpt))
    return [{"file_id": header[1], "location": header[2],
             "text": excerpt[header.end():headers[index + 1].start() if index + 1 < len(headers) else None].strip()}
            for index, header in enumerate(headers)]


def chunk_ref(chunk):
    if chunk.get("page") is not None:
        return chunk["file_id"], "page", str(chunk["page"])
    if chunk.get("section") is not None:
        return chunk["file_id"], "section", str(chunk["section"])
    return chunk["file_id"], "location", str(chunk.get("location", "?"))


def matches_ref(left, right):
    return (left[0] == right[0] and left[2] == right[2]
            and (left[1] == right[1] or "location" in (left[1], right[1])))


def interleave_sources(chunks, *, last_early=False):
    groups = {}
    for chunk in chunks:
        groups.setdefault(chunk["file_id"], []).append(chunk)
    if last_early:
        for group in groups.values():
            if len(group) > 2:
                group.insert(1, group.pop())
    return [chunk for row in zip_longest(*groups.values()) for chunk in row if chunk is not None]


def wire_width(text):
    return len(json.dumps(text, ensure_ascii=False).encode("utf-16-le", errors="surrogatepass")) // 2 - 2


def wire_clip(text, limit):
    if wire_width(text) <= limit:
        return text
    low, high = 0, min(len(text), limit)
    while low < high:
        middle = (low + high + 1) // 2
        if wire_width(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def excerpt_text(chunks, limit, supplement_limit=1200):
    def render(items, budget):
        sections, limited = [], False
        for index, chunk in enumerate(items):
            allowance = budget // min(8, len(items) - index)
            section, clipped = render_chunk(chunk, allowance)
            if not section:
                return sections, True, budget
            sections.append(section)
            limited |= clipped
            budget -= wire_width(section) + 4  # JSON-escaped separator between sections.
        return sections, limited, budget

    def render_chunk(chunk, allowance):
        file_id, _, location = chunk_ref(chunk)
        header = f"[來源 {file_id} / 頁或節 {location}]\n"
        available = allowance - wire_width(header) - 4
        if available <= 0:
            return "", True
        text = chunk["text"].strip()
        excerpt = wire_clip(text, available)
        return header + excerpt, len(excerpt) < len(text) or chunk.get("truncated", False)

    primary = [chunk for chunk, supplementary in chunks if not supplementary]
    supplemental = [chunk for chunk, supplementary in chunks if supplementary]
    reserved = min(limit, supplement_limit) if primary else limit
    supplement, supplement_limited, leftover = render(supplemental, reserved)
    main, main_limited, _ = render(primary, limit - reserved + leftover)
    return "\n\n".join(main + supplement), main_limited or supplement_limited


def unit_context(snapshot, unit, compact=False):
    """Carry relevant task text forward using source IDs and page/section references."""
    chunks = [chunk for chunk in snapshot_chunks(snapshot) if chunk["text"].strip()]
    source_refs = [chunk_ref(ref) for ref in unit.get("source_refs", [])]
    source_ids = set(unit.get("source_ids", [])) | {ref[0] for ref in source_refs}
    referenced = {ref[0] for ref in source_refs}
    hints = {(match["after"] or match["before"]).lower() for value in unit.get("source_locations", [])
             for match in LOCATION_HINT.finditer(value)}
    hints.update(value.strip().lower() for value in unit.get("source_locations", []))

    def selection(chunk):
        ref = chunk_ref(chunk)
        file_id = ref[0]
        if file_id in referenced:
            if any(matches_ref(ref, requested) for requested in source_refs):
                return 0
            return 3 if file_id == ASSIGNMENT else None
        if file_id == ASSIGNMENT and file_id not in source_ids:
            return 3
        if not source_ids or file_id in source_ids:
            return 1 if ref[2].lower() in hints else 2
        return None

    groups = {}
    for chunk in chunks:
        if (priority := selection(chunk)) is not None:
            groups.setdefault(priority, []).append(chunk)
    selected = [(chunk, priority == 3) for priority in sorted(groups)
                for chunk in interleave_sources(groups[priority])]
    limit = 4000 if compact else 10000
    if chunks:
        excerpt, limited = excerpt_text(selected, limit)
    else:
        raw = snapshot.get("excerpt", "")
        excerpt = wire_clip(raw, limit)
        limited = len(excerpt) < len(raw)

    requirements = []
    requirement_ids = set(unit.get("requirement_ids", []))
    for requirement in snapshot.get("requirements", []):
        ref = requirement.get("source", {})
        if ref.get("file_id") and (requirement["id"] in requirement_ids or selection(ref) is not None):
            requirements.append({**requirement, "text": wire_clip(requirement["text"], 300)})
    requirements.sort(key=lambda item: item["id"] not in requirement_ids)
    count = 4 if compact else 8
    assets = [asset for asset in snapshot.get("assets", []) if not source_ids or asset["id"] in source_ids]
    missing_locations = [ref for ref in unit.get("source_refs", [])
                         if not any(matches_ref(chunk_ref(ref), chunk_ref(chunk)) for chunk in chunks)]
    return {"excerpt": excerpt, "assets": assets, "requirements": requirements[:count],
            "context_limited": limited or len(requirements) > count, "missing_locations": missing_locations}


def planning_context(snapshot):
    """Preview sources throughout the assignment while keeping planning requests bounded."""
    chunks = interleave_sources([chunk for chunk in snapshot_chunks(snapshot) if chunk["text"].strip()], last_early=True)
    if not chunks:
        result = unit_context(snapshot, {})
    else:
        excerpt, limited = excerpt_text([(chunk, chunk["file_id"] == ASSIGNMENT) for chunk in chunks], 10000,
                                        supplement_limit=3600)
        result = {"excerpt": excerpt, "assets": snapshot.get("assets", []), "context_limited": limited}
    result["requirements"] = [{"id": item["id"], "text": wire_clip(item["text"], 220), "source": item["source"]}
                              for item in snapshot.get("requirements", [])]
    index = []
    available = 8000
    for chunk in chunks:
        file_id, key, location = chunk_ref(chunk)
        entry = {"file_id": file_id, key: chunk.get(key, location),
                 "preview": " ".join(chunk["text"].split())[:110]}
        if chunk.get("truncated"):
            entry["truncated"] = True
        available -= len(json.dumps(entry, ensure_ascii=False).encode("utf-16-le")) // 2 + 2
        if available < 0:
            break
        index.append(entry)
    result.update(source_index=index, source_index_limited=len(index) < len(chunks),
                  assignment_context_limited=any(chunk["file_id"] == ASSIGNMENT and
                      chunk["text"].strip() not in result["excerpt"] for chunk in chunks))
    return result
