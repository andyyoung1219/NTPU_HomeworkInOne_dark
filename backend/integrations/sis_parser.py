"""Port of NTPUOne AcademicReportParser v4 / sis_probe contracts.

Source: b7f07f1f5b856b7016fce80eaa0983e2d401760f; see THIRD_PARTY_NOTICES.md.
Only structured, identity-checked data leaves this module. No computed grades.
"""

import re
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from lxml import html

from backend.p0.moodle import probe


def fail(code="SIS_SCHEMA_CHANGED"):
    raise probe.ProbeError(code)


def document(text):
    if not isinstance(text, str) or len(text.encode()) > 8 * 1024 * 1024:
        fail()
    try:
        root = html.document_fromstring(text)
    except (ValueError, html.etree.ParserError):
        fail()
    for node in root.xpath("//script|//style|//form|//input|//button|//iframe|//object|"
                           "//*[contains(concat(' ',normalize-space(@class),' '),' lang-en-US ')]"):
        node.drop_tree()
    return root


def text(node):
    return " ".join(" ".join(node.itertext()).split()) if node is not None else ""


def cls(node, name):
    return node.xpath(".//*[contains(concat(' ',normalize-space(@class),' '), $name)]",
                      name=f" {name} ")


def first_text(node, name):
    nodes = cls(node, name)
    return text(nodes[0]) if nodes else ""


def meetings(raw):
    """Exact full-line contracts; an unknown/partial schedule stays unknown."""
    result = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(
            r"\s*(?:(?:每週|每周|星期|週))?([一二三四五六日])\s*"
            r"([0-9]+(?:\s*[~～\-－、,，]\s*[0-9]+)*)\s*(?:@|\s)\s*(\S.*?)\s*", line)
        if not match:
            return [], "unknown"
        day, periods, room = match.groups()
        if re.search(r"每週|每周|星期|週|[一二三四五六日]\d+@", room):
            return [], "unknown"
        for segment in re.split(r"[、,，]", periods):
            limits = re.split(r"[~～\-－]", segment)
            start, end = int(limits[0]), int(limits[-1])
            if len(limits) > 2 or not 1 <= start <= end <= 13:
                return [], "unknown"
            result += [{"weekday": "一二三四五六日".index(day) + 1, "period": p,
                        "classroom": room} for p in range(start, end + 1)]
    keys = {(m["weekday"], m["period"]) for m in result}
    if not result or len(keys) != len(result):
        return [], "unknown"
    return result, "known"


def parse_enrolment(raw, identity):
    root = document(raw)
    rows = root.xpath(".//tr")
    headers = ["", "科目名稱(與組別)", "核課資訊", "授課老師", "上課時間@教室",
               "類別", "學分/時數", "開課系級", "備註"]
    if not 1 < len(rows) <= 301 or [text(c) for c in rows[0].xpath("./td|./th")] != headers:
        fail()
    terms = set()
    for link in root.xpath(".//a/@href"):
        parts = urlsplit(link)
        if parts.scheme or parts.netloc or parts.path != "../univer/course_check_m.dt_e":
            continue
        query = parse_qs(parts.query)
        if query.get("stud_no") != [identity]:
            fail("SIS_ACCOUNT_CHANGED")
        year, term = query.get("year1", []), query.get("term1", [])
        if len(year) != 1 or not re.fullmatch(r"1\d{2}", year[0]) or term not in (["1"], ["2"]):
            fail()
        terms.add((year[0], term[0]))
    if len(terms) != 1:
        fail()
    year, term = next(iter(terms))
    courses = []
    for row in rows[1:]:
        cells = row.xpath("./td")
        values = [text(c) for c in cells]
        if len(values) != 9 or values[2] != "已核准":
            fail()
        match = re.search(r"\(([A-Z]\d{4})\)\s*$", values[1])
        if not match:
            fail()
        # Preserve line boundaries from the official timetable, including <br>.
        for br in cells[4].xpath(".//br"):
            br.tail = "\n" + (br.tail or "")
        schedule = cells[4].text_content().strip()
        slots, status = meetings(schedule)
        code = match[1]
        courses.append({"id": f"{year}-{term}:{code}", "semester": f"{year}-{term}",
                        "year": year, "term": term, "code": code,
                        "name": values[1][:match.start()].strip(), "teacher": values[3] or None,
                        "schedule_raw": schedule, "meetings": slots, "schedule_status": status})
    if len({c["id"] for c in courses}) != len(courses):
        fail()
    return {"semester": f"{year}-{term}", "courses": courses}


def section(identifier, title):
    if not title:
        fail()
    return {"id": identifier, "title": title, "fields": [], "subjects": [], "notes": []}


def parse_report(raw, identity, kind):
    root = document(raw)
    if not identity or not re.search(r"(?<![\w])" + re.escape(identity) + r"(?![\w])", text(root)):
        fail("SIS_ACCOUNT_CHANGED")
    summary_issues = []
    if kind == "grades":
        sections = grades(root)
    elif kind == "rank":
        sections = ranks(root)
    elif kind == "graduation":
        sections, summary_issues = graduation(root)
    else:
        fail()
    if not sections or len({s["id"] for s in sections}) != len(sections):
        fail()
    report = {"kind": kind, "parser_version": 4, "sections": sections}
    if summary_issues:
        report["summary_issues"] = summary_issues
    return report


def grades(root):
    output = []
    for card in cls(root, "card"):
        if "my-4" not in card.get("class", "").split():
            continue
        year = first_text(card, "card-title")
        tables = cls(card, "table-striped")
        if len(tables) != 1:
            fail()
        rows = tables[0].xpath(".//tr")
        if not any([text(c) for c in r.xpath("./th")] ==
                   ["類別", "科目名稱", "學分", "成績", "學分", "成績"] for r in rows):
            fail()
        for semester in (1, 2):
            entry = section(f"grades:{year}:{semester}", f"{year} · 第{semester}學期")
            for row in rows:
                cells = row.xpath("./td")
                values = [text(c) for c in cells if f"sem{semester}bg" in c.get("class", "").split()]
                if not cells:
                    continue
                if len(cells) == 6 and "text-left" in cells[1].get("class", "").split():
                    if len(values) != 2:
                        fail()
                    if not any(values):
                        continue
                    if not text(cells[1]):
                        fail()
                    entry["subjects"].append({"title": text(cells[1]), "category": text(cells[0]),
                                              "credits": values[0], "grade": values[1]})
                elif any(values):
                    label = " ".join(text(c) for c in cells
                                     if not {"sem1bg", "sem2bg"} & set(c.get("class", "").split()))
                    if label:
                        entry["fields"].append({"label": label, "value": " / ".join(values)})
                elif len(cells) == 6:
                    fail()
            if entry["subjects"] or entry["fields"]:
                output.append(entry)
    return output


def ranks(root):
    output = []
    for container in cls(root, "container"):
        if "my-2" not in container.get("class", "").split():
            continue
        for card in container:
            if "card" not in card.get("class", "").split():
                continue
            heading = card.getprevious()
            while heading is not None and heading.tag != "h6":
                heading = heading.getprevious()
            year = text(heading)
            if not year:
                fail()
            for sheet in cls(card, "datasheets"):
                strong = sheet.xpath(".//strong")
                term = text(strong[0]) if strong else ""
                if not term and "card-body" not in sheet.get("class", "").split():
                    fail()
                entry = section(f"rank:{year}:{term}", f"{year} · {term}" if term else year)
                for rows in cls(sheet, "datarows"):
                    for row in rows.xpath("./li"):
                        parts = re.split("[：:]", text(row), maxsplit=1)
                        if parts[0]:
                            entry["fields"].append({"label": parts[0] if len(parts) == 2 else "排名資訊",
                                                    "value": parts[-1].strip()})
                if entry["fields"]:
                    output.append(entry)
    return output


class _SummaryCardError(ValueError):
    """Fixed descriptions of an unreadable summary card."""


def _summary_card(heading, identifier, label):
    parent = heading.getparent()
    while parent is not None and "flex_item_col" not in parent.get("class", "").split():
        parent = parent.getparent()
    boxes = cls(parent, "credit_box") if parent is not None else []
    if len(boxes) != 1:
        raise _SummaryCardError("學分區塊缺少或重複")
    box = boxes[0]
    detail_class = "credit_count" if identifier == "summary_total" else "total_credit"
    detail = first_text(parent, detail_class)
    # The explanation can contain other styled numbers within the amount box.
    if cls(box, detail_class):
        box = deepcopy(box)
        for node in cls(box, detail_class):
            node.tail = " " + (node.tail or "")
            node.drop_tree()
    values = cls(box, "main_credit")
    if len(values) == 1:
        value = text(values[0])
    elif not values:
        credit = r"([0-9０-９]+(?:[.．][0-9０-９]+)?)(?:\s*學分)?"
        pattern = credit if identifier == "summary_total" else rf"{credit}(?:\s*[/／]\s*{credit})?"
        match = re.fullmatch(pattern, text(box))
        if not match:
            raise _SummaryCardError("學分數字格式不明")
        value = match[1]
        if identifier != "summary_total" and match[2] is not None and not detail:
            detail = "/ " + match[2]
    else:
        raise _SummaryCardError("有多個學分數值欄")
    if not value:
        raise _SummaryCardError("學分數值欄是空白")
    entry = section(identifier, label)
    entry["fields"].append({"label": label, "value": value, "detail": detail or None})
    return entry


def graduation(root):
    output = []
    summary_issues = []
    headings = list(dict.fromkeys(h for row in cls(root, "row_progress") for h in cls(row, "grid_stat_item_title")))
    for label, identifier in [("已累計總學分", "summary_total"), ("系必修", "summary_required"),
                              ("系選修", "summary_option"), ("其他學分", "summary_other")]:
        matches = [h for h in headings if text(h) == label]
        if len(matches) > 1:
            summary_issues.append(f"{label}（卡片標題重複）")
        elif matches:
            try:
                output.append(_summary_card(matches[0], identifier, label))
            except _SummaryCardError as exc:
                summary_issues.append(f"{label}（{exc}）")
    entry = section("requirements", "畢業條件")
    for row in cls(root, "item_li"):
        label = first_text(row, "item_title")
        if label.startswith(("外語能力指標檢核", "校定程式設計能力", "學分學程、微學程")):
            value = text(row).removeprefix(label).strip()
            if value:
                entry["fields"].append({"label": label, "value": value})
    if entry["fields"]:
        output.append(entry)
    for identifier, title in [("tag_total", "校訂必修學分"), ("tag_chinese", "國文"),
                              ("tag_english", "英文"), ("tag_pe", "體育"),
                              ("tag_general", "通識"), ("mini_general", "通識分類")]:
        nodes = root.xpath(".//*[@id=$identifier]", identifier=identifier)
        if len(nodes) > 1:
            fail("SIS_GRADUATION_CATEGORY_FORMAT")
        if nodes:
            entry = section(identifier, title)
            for box in cls(nodes[0], "credit_box"):
                value = first_text(box, "main_credit")
                if value:
                    entry["fields"].append({"label": "學分進度", "value": value,
                                            "detail": first_text(box, "total_credit") or None})
            if entry["fields"]:
                output.append(entry)
    for identifier in ("required", "option", "common", "other"):
        nodes = root.xpath(".//section[@id=$identifier]", identifier=identifier)
        if len(nodes) != 1:
            fail("SIS_GRADUATION_CATEGORY_FORMAT")
        node = nodes[0]
        title = first_text(node, "collapse_title")
        if not title:
            fail("SIS_GRADUATION_CATEGORY_FORMAT")
        entry = section(identifier, title)
        entry["notes"] = list(dict.fromkeys(text(n) for n in cls(node, "illus_text") + cls(node, "collapse_ul_footer") if text(n)))
        for header in cls(node, "collapse_ul_header"):
            for item in cls(header, "collapse_col_item"):
                # Nested spans can style a field without adding a new field.
                spans = item.xpath(".//span")
                span_set = set(spans)
                values = [node for node in spans
                          if not any(parent in span_set for parent in node.iterancestors("span"))]
                if len(values) != 2:
                    fail("SIS_GRADUATION_METRIC_FORMAT")
                entry["fields"].append({"label": text(values[0]), "value": text(values[1])})
        for row in cls(node, "subject_li"):
            name = first_text(row, "subject_title")
            if not name:
                fail("SIS_GRADUATION_SUBJECT_FORMAT")
            entry["subjects"].append({"title": name, "details": list(dict.fromkeys(text(n) for n in cls(row, "subject_state")))})
        output.append(entry)
    if summary_issues and not any(entry["fields"] or entry["subjects"] for entry in output):
        fail("SIS_GRADUATION_SUMMARY_FORMAT")
    return output, summary_issues
