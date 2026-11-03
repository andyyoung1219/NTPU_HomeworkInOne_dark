"""Synthetic SIS contracts, matching the pinned upstream's observed selectors."""

import json
from urllib.parse import parse_qs

import httpx
import pytest

from backend.integrations.sis import (
    ORIGIN,
    TARGETS,
    SISClient,
    identity_key,
    portal_redirect_url,
    validate_bridge,
)
from backend.integrations.sis_parser import meetings, parse_enrolment, parse_report
from backend.integrations.transport import ReadTransport
from backend.p0.moodle import probe

IDENTITY = "SYNTHETIC_ID"


def enrolment(identity=IDENTITY, schedule="一1-3@合成教室", semester="115-1", count=5):
    headers = ["", "科目名稱(與組別)", "核課資訊", "授課老師", "上課時間@教室", "類別", "學分/時數", "開課系級", "備註"]
    rows = [headers] + [["", f"合成課程{i}(U{1000+i})", "已核准", "合成教師", schedule, "必修", "3/3", "測試", ""] for i in range(count)]
    year, term = semester.split("-")
    return ("<table>" + "".join("<tr>" + "".join("<td>" + c + "</td>" for c in row) + "</tr>" for row in rows) +
            f'</table><a href="../univer/course_check_m.dt_e?stud_no={identity}&year1={year}&term1={term}">確認</a>')


def grade_html(identity=IDENTITY):
    return f"""<div>本人 {identity} 歷年成績</div>
    <div class='card my-4'><div class='card-title'>115學年度<span class='lang-en-US'>IGNORE_ENGLISH</span></div>
    <table class='table table-striped'><tr><th>類別</th><th>科目名稱</th><th>學分</th><th>成績</th><th>學分</th><th>成績</th></tr>
    <tr><td>必修</td><td class='text-left'>合成課程</td><td class='sem1bg'>3</td><td class='sem1bg'>A</td><td class='sem2bg'>2</td><td class='sem2bg'>88</td></tr>
    <tr><td colspan='2'>學期平均</td><td class='sem1bg' colspan='2'>87.5</td><td class='sem2bg' colspan='2'>88</td></tr></table></div>
    <script>PRIVATE_SCRIPT_SECRET</script><input value='PRIVATE_INPUT_SECRET'>"""


def graduation_html():
    return f"""<div>本人 {IDENTITY} 畢業進度</div>
    <div class='row_progress'><div class='flex_item_col'><div class='grid_stat_item_title'>已累計總學分</div><div class='credit_count'>官方說明</div><div class='credit_box'><div>123</div></div></div>
    <div class='flex_item_col'><div class='grid_stat_item_title'>系必修</div><div class='credit_box'>50 / 60</div></div></div>
    <ul class='item_ul'><li class='item_li'><div class='item_title'>外語能力指標檢核</div>未處理</li></ul>
    <div id='tag_total'><div class='credit_box'><span class='main_credit'>12.5</span><span class='total_credit'>28</span></div></div>""" + "".join(
        f"<section id='{kind}'><div class='collapse_title'>{kind}</div><div class='collapse_ul_header'><div class='collapse_col_item'><span>應修</span><span>3</span></div></div><li class='subject_li'><div class='subject_title'>合成課程</div><div class='subject_state'>已修</div></li></section>"
        for kind in ("required", "option", "common", "other"))


def test_five_course_timetable_identity_term_and_unknown_schedule():
    value = parse_enrolment(enrolment(), IDENTITY)
    assert value["semester"] == "115-1" and len(value["courses"]) == 5
    first = value["courses"][0]
    assert first["id"] == "115-1:U1000" and first["teacher"] == "合成教師"
    assert first["meetings"] == [{"weekday": 1, "period": p, "classroom": "合成教室"} for p in (1, 2, 3)]
    unknown = parse_enrolment(enrolment(schedule="待定／另行通知"), IDENTITY)["courses"][0]
    assert unknown["meetings"] == [] and unknown["schedule_status"] == "unknown"
    assert meetings("一1@A\n三3,4@B")[1] == "known"
    assert len(parse_enrolment(enrolment(schedule="一1@A<br>三3@B"), IDENTITY)["courses"][0]["meetings"]) == 2


@pytest.mark.parametrize("schedule", ["一1@A 週三4 B", "週一1-14 A", "一3-1@A", "一1,1@A", "未知", "週一1 A\n錯誤"])
def test_partial_or_ambiguous_schedule_is_unknown(schedule):
    assert meetings(schedule) == ([], "unknown")


@pytest.mark.parametrize("raw,code", [
    (enrolment(identity="OTHER"), "SIS_ACCOUNT_CHANGED"),
    (enrolment().replace("核課資訊", "changed"), "SIS_SCHEMA_CHANGED"),
    (enrolment().replace("已核准", "退選"), "SIS_SCHEMA_CHANGED"),
    (enrolment().replace("U1001", "U1000"), "SIS_SCHEMA_CHANGED"),
])
def test_enrolment_rejects_unknown_schema_identity_and_duplicates(raw, code):
    with pytest.raises(probe.ProbeError, match=code):
        parse_enrolment(raw, IDENTITY)


def test_grades_preserve_official_values_and_separate_semesters():
    report = parse_report(grade_html(), IDENTITY, "grades")
    assert len(report["sections"]) == 2
    assert [s["subjects"][0]["grade"] for s in report["sections"]] == ["A", "88"]
    assert report["sections"][0]["fields"][0]["value"] == "87.5"
    assert "SECRET" not in json.dumps(report) and "IGNORE_ENGLISH" not in json.dumps(report)
    with pytest.raises(probe.ProbeError, match="SIS_ACCOUNT_CHANGED"):
        parse_report(grade_html("OTHER"), IDENTITY, "grades")
    with pytest.raises(probe.ProbeError, match="SIS_SCHEMA_CHANGED"):
        parse_report(grade_html().replace("科目名稱", "NEW_HEADER"), IDENTITY, "grades")


def test_rank_and_cumulative_preserve_school_statement():
    raw = f"""<p>{IDENTITY}</p><div class='container my-2'><h6>115學年</h6><div class='card'><div class='datasheets'><strong>第一學期</strong><ul class='datarows'><li>平均成績：87</li><li>排名：3 / 30</li></ul></div></div>
    <h6>累計成績</h6><div class='card'><div class='card-body datasheets'><ul class='datarows'><li>平均成績：85</li></ul></div></div></div>"""
    result = parse_report(raw, IDENTITY, "rank")
    assert result["sections"][0]["fields"][1]["value"] == "3 / 30"
    assert result["sections"][1]["title"] == "累計成績"


def test_graduation_never_confuses_total_category_or_eligibility():
    result = {s["id"]: s for s in parse_report(graduation_html(), IDENTITY, "graduation")["sections"]}
    assert result["summary_total"]["fields"][0]["value"] == "123"
    assert result["summary_required"]["fields"][0]["value"] == "50"
    assert result["summary_required"]["fields"][0]["detail"] == "/ 60"
    assert result["tag_total"]["fields"][0]["value"] == "12.5"
    assert result["requirements"]["fields"][0]["value"] == "未處理"
    for raw, code in (
        (graduation_html().replace("id='required'", "id='changed'"), "SIS_GRADUATION_CATEGORY_FORMAT"),
        (graduation_html().replace("<span>3</span>", "<span>3</span><span>9</span>"), "SIS_GRADUATION_METRIC_FORMAT"),
    ):
        with pytest.raises(probe.ProbeError, match=code):
            parse_report(raw, IDENTITY, "graduation")


def test_graduation_reads_amount_separately_from_detail_inside_same_card():
    raw = graduation_html().replace(
        "<div class='credit_count'>官方說明</div><div class='credit_box'><div>123</div>",
        "<div class='credit_box'><div>１２３</div><div class='credit_count'>123 = <span class='main_credit'>50</span> + 73</div>",
    ).replace("<div class='credit_box'>50 / 60</div>",
              "<div class='credit_box'>50<span class='total_credit'>/ 60</span></div>")
    raw = "<div class='row_progress'>" + raw + "</div>"
    report = parse_report(raw, IDENTITY, "graduation")
    sections = {s["id"]: s for s in report["sections"]}
    assert "summary_issues" not in report
    assert sections["summary_total"]["fields"][0] == {
        "label": "已累計總學分", "value": "１２３", "detail": "123 = 50 + 73"}
    assert sections["summary_required"]["fields"][0] == {"label": "系必修", "value": "50", "detail": "/ 60"}


class FakeClock:
    def __init__(self):
        self.value = 0
        self.delays = []

    def now(self):
        return self.value

    def sleep(self, delay):
        self.delays.append(delay)
        self.value += delay


def transport(handler):
    clock = FakeClock()
    return ReadTransport(httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False), clock.now, clock.sleep)


def school_handler(requests, descriptor_override=None, page_identity=IDENTITY):
    def handler(request):
        requests.append(request)
        path = request.url.path
        if request.url.host == "my.ntpu.edu.tw":
            if path == "/login" and request.method == "GET":
                return httpx.Response(200, text="<form method='POST' action='/login'><input name='_token' value='synthetic-csrf'><input name='username'><input name='password'></form>")
            if path == "/login":
                assert parse_qs(request.content.decode())["_token"] == ["synthetic-csrf"]
                return httpx.Response(302, headers={"location": "/"})
            if path == "/":
                menus = [{"url": "https://ohs01.ntpu.edu.tw" + p, "project_id": 10} for p in TARGETS.values()]
                return httpx.Response(200, text="<a href='/logout'>登出</a><script>var arr = " + json.dumps(menus) + ";</script>")
            target = request.url.params["url"].replace("ohs01.ntpu.edu.tw", "cof.ntpu.edu.tw")
            descriptor = {"form_url": "https://cof.ntpu.edu.tw/pls/pm/stud_system.signin",
                          "base_url": target, "formData": {"id": IDENTITY, "pw": "bridge-secret"}}
            descriptor.update(descriptor_override or {})
            return httpx.Response(200, json=descriptor)
        if path.endswith("signin"):
            assert request.url.host == "cof.ntpu.edu.tw" and request.method == "POST"
            return httpx.Response(200, text="OK")
        return httpx.Response(200, content=enrolment(identity=page_identity).encode("big5"), headers={"content-type": "text/html; charset=big5"})
    return handler


def test_normal_csrf_login_dynamic_verified_bridge_and_logout():
    requests = []
    client = SISClient(transport(school_handler(requests)))
    try:
        client.login(IDENTITY, "synthetic-password")
        result = client.read("timetable")
        assert result["identity_key"] == identity_key(IDENTITY)
        assert len(result["courses"]) == 5
        assert "bridge-secret" not in json.dumps(result)
        client.reset()
        with pytest.raises(probe.ProbeError, match="SIS_LOGIN_REQUIRED"):
            client.read("timetable")
    finally:
        client.close()


def test_graduation_reads_big5_extensions_and_nested_credit_fields():
    raw = (graduation_html().replace("合成課程", "合成課程 €")
           .replace("50 / 60", "50 學分 ／ 60 學分")
           .replace("<span>應修</span><span>3</span>", "<span>應修</span><div><span><span>3</span></span></div>"))
    body = raw.encode("cp950")
    with pytest.raises(UnicodeDecodeError):
        body.decode("big5")
    handler = school_handler([])

    def serve_graduation(request):
        if request.url.host == "cof.ntpu.edu.tw" and request.url.params.get("func") == "9_m":
            return httpx.Response(200, content=body, headers={"content-type": "text/html; charset=big5"})
        return handler(request)

    client = SISClient(transport(serve_graduation))
    try:
        client.login(IDENTITY, "synthetic-password")
        result = client.read("graduation")
        sections = {s["id"]: s for s in result["sections"]}
        assert sections["summary_required"]["fields"] == [{"label": "系必修", "value": "50", "detail": "/ 60"}]
        assert sections["required"]["fields"] == [{"label": "應修", "value": "3"}]
        assert sections["required"]["subjects"][0]["title"] == "合成課程 €"
    finally:
        client.close()


@pytest.mark.parametrize("change,reason", [
    ({"form_url": "https://evil.example/pls/pm/stud_system.signin"}, "LOGIN_HOST"),
    ({"form_url": "https://cof.ntpu.edu.tw:444/pls/pm/stud_system.signin"}, "LOGIN_URL_FORMAT"),
    ({"form_url": "https://cof.ntpu.edu.tw/pls/pm/stud_system.signin?next=secret"}, "LOGIN_ROUTE"),
    ({"form_url": None}, "LOGIN_URL_FORMAT"),
    ({"form_url": "https://[broken"}, "LOGIN_URL_FORMAT"),
    ({"form_url": "https://cof.ntpu.edu.tw:bad/pls/pm/stud_system.signin"}, "LOGIN_URL_FORMAT"),
    ({"form_url": "http://cof.ntpu.edu.tw/pls/pm/stud_system.signin"}, "LOGIN_URL_FORMAT"),
    ({"form_url": "https://secret@cof.ntpu.edu.tw/pls/pm/stud_system.signin"}, "LOGIN_URL_FORMAT"),
    ({"form_url": "https://cof.ntpu.edu.tw/pls/pm/stud_system.signin#secret"}, "LOGIN_URL_FORMAT"),
    ({"form_url": "https://cof.ntpu.edu.tw/\npls/pm/stud_system.signin"}, "LOGIN_URL_FORMAT"),
    ({"form_url": "https://cof.ntpu.edu.tw\\@evil.example/pls/pm/stud_system.signin"}, "LOGIN_URL_FORMAT"),
    ({"base_url": None}, "REPORT_URL_FORMAT"),
    ({"base_url": "https://evil.example" + TARGETS["timetable"]}, "REPORT_HOST"),
    ({"base_url": "https://ohs01.ntpu.edu.tw" + TARGETS["timetable"]}, "HOST_MISMATCH"),
    ({"base_url": "https://cof.ntpu.edu.tw" + TARGETS["grades"]}, "REPORT_ROUTE"),
    ({"base_url": "https://cof.ntpu.edu.tw" + TARGETS["timetable"] + "&func=5_m"}, "REPORT_ROUTE"),
    ({"formData": []}, "FIELDS_FORMAT"),
    ({"formData": {"id": IDENTITY}}, "FIELDS_MISSING"),
    ({"formData": {"id": IDENTITY, "pw": "secret", "extra": "secret"}}, "FIELDS_EXTRA"),
    ({"formData": {"id": IDENTITY, "pw": ""}}, "CREDENTIALS_EMPTY"),
    ({"formData": {"id": 123, "pw": "secret"}}, "CREDENTIALS_EMPTY"),
])
def test_unknown_bridge_stops_before_sending_credentials(change, reason):
    requests = []
    client = SISClient(transport(school_handler(requests, change)))
    try:
        client.login(IDENTITY, "synthetic-password")
        with pytest.raises(probe.ProbeError) as caught:
            client.read("timetable")
        assert caught.value.code == "SIS_BRIDGE_" + reason
        assert all(r.url.host == "my.ntpu.edu.tw" for r in requests)
    finally:
        client.close()


@pytest.mark.parametrize("host", ["cof.ntpu.edu.tw", "ohs01.ntpu.edu.tw"])
@pytest.mark.parametrize("kind", TARGETS)
def test_bridge_equivalent_https_origin_and_each_report(host, kind):
    fields = {"id": IDENTITY, "pw": "synthetic-secret"}
    login, report, returned = validate_bridge({
        "form_url": f"HTTPS://{host.upper()}:443/pls/pm/stud_system.signin",
        "base_url": f"https://{host}:443" + TARGETS[kind], "formData": fields,
    }, kind)
    assert login == f"https://{host}/pls/pm/stud_system.signin"
    assert report == f"https://{host}" + TARGETS[kind]
    assert returned == fields


def test_equivalent_bridge_url_completes_read_with_canonical_requests():
    requests = []
    client = SISClient(transport(school_handler(requests, {
        "form_url": "HTTPS://COF.NTPU.EDU.TW:443/pls/pm/stud_system.signin",
        "base_url": "https://COF.NTPU.EDU.TW:443" + TARGETS["timetable"],
    })))
    try:
        client.login(IDENTITY, "synthetic-password")
        assert len(client.read("timetable")["courses"]) == 5
        assert str(requests[-2].url) == "https://cof.ntpu.edu.tw/pls/pm/stud_system.signin"
        assert str(requests[-1].url) == "https://cof.ntpu.edu.tw" + TARGETS["timetable"]
    finally:
        client.close()


def test_rejected_bridge_discards_fields_without_echoing_response():
    fields = {"id": IDENTITY, "pw": "synthetic-secret"}
    with pytest.raises(probe.ProbeError) as caught:
        validate_bridge({"form_url": "https://secret.example", "formData": fields}, "timetable")
    assert fields == {}
    assert str(caught.value) == "SIS_BRIDGE_LOGIN_HOST"


def test_unknown_portal_redirect_never_followed():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/capture"})

    client = SISClient(transport(handler))
    with pytest.raises(probe.ProbeError, match="SIS_REDIRECT_REQUIRES_REVIEW"):
        client.login(IDENTITY, "synthetic-password")
    assert len(requests) == 1 and requests[0].url == ORIGIN + "/login"
    client.close()


@pytest.mark.parametrize("location", [ORIGIN, ORIGIN + "/", "/", "https://my.ntpu.edu.tw:443"])
def test_login_accepts_equivalent_portal_home_redirect_without_replaying_password(location):
    requests = []
    handler = school_handler(requests)

    def redirecting(request):
        response = handler(request)
        if request.method == "POST" and request.url.path == "/login":
            return httpx.Response(302, headers={"location": location})
        return response

    client = SISClient(transport(redirecting))
    try:
        client.login(IDENTITY, "synthetic-password")
        assert client.state == "connected"
        assert requests[-1].url == ORIGIN + "/" and requests[-1].method == "GET"
        assert requests[-1].content == b""
        assert len([r for r in requests if r.method == "POST"]) == 1
    finally:
        client.close()


@pytest.mark.parametrize("location", [
    "", "https://evil.example/", "http://my.ntpu.edu.tw/", ORIGIN + "/logout",
    ORIGIN + "/?token=secret", ORIGIN + "/#secret", "https://my.ntpu.edu.tw:444/",
    "https://name:secret@my.ntpu.edu.tw/", "https://my.ntpu.edu.tw.evil.example/",
    "https://my.ntpu.edu.tw:invalid/", "https://my.ntpu.edu.tw\\@evil.example/",
])
def test_portal_canonicalization_does_not_allow_other_destinations(location):
    with pytest.raises(probe.ProbeError, match="SIS_REDIRECT_REQUIRES_REVIEW"):
        portal_redirect_url(ORIGIN + "/login", location)


def test_429_blocks_subsequent_calls_until_retry_after_and_503_backoff():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"retry-after": "120"}) if len(requests) == 1 else httpx.Response(200)

    reader = transport(handler)
    for _ in range(2):
        with pytest.raises(probe.ProbeError, match="RATE_LIMITED"):
            reader.request("GET", ORIGIN)
    assert len(requests) == 1
    reader.sleep(120)
    assert reader.request("GET", ORIGIN).status_code == 200
    reader.close()
    responses = iter([503, 502, 200])
    reader = transport(lambda request: httpx.Response(next(responses)))
    assert reader.request("GET", ORIGIN).status_code == 200
    assert reader.clock() == 3
    reader.close()


def test_transport_never_retries_login_and_bounds_body():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503)

    reader = transport(handler)
    assert reader.request("POST", ORIGIN, retry=False).status_code == 503
    assert len(requests) == 1
    reader.close()
    reader = transport(lambda request: httpx.Response(200, content=b"too large"))
    with pytest.raises(probe.ProbeError, match="RESPONSE_TOO_LARGE"):
        reader.request("GET", ORIGIN, max_bytes=3)
    reader.close()


def test_expected_account_is_checked_before_forwarding_bridge_credentials():
    requests = []
    client = SISClient(transport(school_handler(requests)))
    try:
        client.login(IDENTITY, "synthetic-password")
        client.expected_identity_key = identity_key("OTHER")
        with pytest.raises(probe.ProbeError, match="SIS_ACCOUNT_CHANGED"):
            client.read("timetable")
        assert all(request.url.host == "my.ntpu.edu.tw" for request in requests)
        assert client.state == "disconnected"
    finally:
        client.close()
