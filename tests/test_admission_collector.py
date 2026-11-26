"""Unit tests for AdmissionCollector: offline sample data + live official-source parsers (fixture HTML)."""
import importlib
import os
import urllib.error
from datetime import datetime, timedelta
from email.message import Message
from unittest.mock import MagicMock, patch

from admission_brief.ai_curator import AICurator
from admission_brief.collector import (
    MOE_DETAIL_URL,
    MOE_LIST_URL,
    SEN_DETAIL_URL,
    SEN_LIST_URL,
    AdmissionCollector,
    is_official_url,
)
from admission_brief.models import (
    KST,
    AdmissionEvent,
    BriefingType,
    EventCategory,
    TargetGrade,
    TargetRegion,
)
from admission_brief.storage.db import AdmissionDatabase

NOW = datetime(2026, 11, 26, 9, 0, tzinfo=KST)
SEN_1069_URL = SEN_LIST_URL.format(bbs_sn="1069")
SEN_1068_URL = SEN_LIST_URL.format(bbs_sn="1068")
MOE_URL = MOE_LIST_URL.format(board_id="294")


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
class _FakeResp:
    def __init__(self, body: bytes, charset=None):
        self._body = body
        self.headers = Message()
        self.headers["Content-Type"] = f"text/html; charset={charset}" if charset else "text/html"

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Maps URL -> (bytes, charset) or Exception; records every call."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        value = self.pages.get(url)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise urllib.error.URLError(f"no fixture for {url}")
        body, charset = value
        return _FakeResp(body, charset)


def sen_1069_html(title_first="2027 정시 &amp; 수시 <strong>대입 설명회</strong> 안내"):
    return f"""
<table class="board"><thead><tr><th>번호</th><th>제목</th><th>작성일</th></tr></thead>
<tbody>
<tr><td>공지</td><td class="tit"><a href="/user/bbs/BD_selectBbs.do?q_bbsSn=1069&amp;q_bbsDocNo=20261120093011223&amp;q_clsfNo=1">
    <span class="ico_new">N</span> {title_first} </a></td><td>2026-11-20</td></tr>
<tr><td>2</td><td><a href='/user/bbs/BD_selectBbs.do?q_bbsSn = 1069&q_bbsDocNo = 20261101120000001'>11월 진학 상담 안내</a></td><td>2026.11.01</td></tr>
<tr><td>3</td><td><a href="/user/bbs/BD_selectBbs.do?q_bbsSn=1069&amp;q_bbsDocNo=20261027000000001">30일 경계 게시물</a></td><td>2026-10-27</td></tr>
<tr><td>4</td><td><a href="/user/bbs/BD_selectBbs.do?q_bbsSn=1069&amp;q_bbsDocNo=20261028000000001">29일 전 게시물</a></td><td>2026-10-28</td></tr>
<tr><td>5</td><td><a href="https://evil.example.com/user/bbs/BD_selectBbs.do?q_bbsSn=1069&amp;q_bbsDocNo=20261121000000001">피싱 링크 게시물</a></td><td>2026-11-21</td></tr>
<tr><td>6</td><td><a href="/user/bbs/BD_selectBbs.do?q_bbsSn=1069&amp;q_bbsDocNo=ABCD1234">문서번호 날짜 없음</a></td><td>2026-11-15</td></tr>
<tr><td>7</td><td><a href="/user/bbs/BD_selectBbs.do?q_bbsSn=1069&amp;q_bbsDocNo=20261399000000001">문서번호 날짜 오류</a></td><td>2026-11-16</td></tr>
<tr><td>8</td><td><a href="/user/bbs/BD_selectBbs.do?q_bbsSn=1069&amp;q_bbsDocNo=20261118000000001">동일 제목 공지</a></td><td>2026-11-18</td></tr>
<tr><td>9</td><td><a href="/user/bbs/BD_selectBbs.do?q_bbsSn=1069&amp;q_bbsDocNo=20261119000000001">동일 제목 공지</a></td><td>2026-11-19</td></tr>
<tr><td>10</td><td><a href="/user/bbs/BD_selectBbs.do?q_bbsSn=9999&amp;q_bbsDocNo=20261119000000002">다른 게시판 링크</a></td><td>2026-11-19</td></tr>
</tbody></table>
""".encode("utf-8")


def sen_1068_html():
    return """
<ul class="bbs_list">
  <li><a href="/user/bbs/BD_selectBbs.do?q_bbsSn=1068&amp;q_bbsDocNo=20261124100000001"><em>[고입]</em> 2027 후기고 진학 설명회</a><span class="date">2026-11-24</span></li>
</ul>
""".encode("utf-8")


def moe_html(encoding="utf-8"):
    meta = '<meta http-equiv="Content-Type" content="text/html; charset=euc-kr">' if encoding != "utf-8" else ""
    return f"""<html><head>{meta}</head><body><table><tbody>
<tr><td>100</td><td class="subject"><a href="#" onclick="goView('294','83001', '0', null, 'W', '1', 'N', ''); return false;">2027학년도 <em>수능</em> 성적 통지 안내</a></td><td>대입정책과</td><td>2026-11-24</td></tr>
<tr><td>99</td><td><a href="#" onclick="goView( &quot;294&quot; , &quot;83000&quot; ); return false;">고교학점제 운영 개선 방안 발표</a></td><td>교육과정정책과</td><td>2026-11-20</td></tr>
<tr><td>98</td><td><a href="#" onclick="goView(294, 82999)">디지털 혁신 포럼 개최</a></td><td>대입정책과 고교학점제 교육과정</td><td>2026-11-22</td></tr>
<tr><td>97</td><td><a href="#" onclick="goView(&#39;294&#39;,&#39;82998&#39;)">국가장학금 2차 신청 안내</a></td><td>장학정책과</td><td>2026-10-01</td></tr>
</tbody></table></body></html>""".encode(encoding)


def live_pages(**overrides):
    pages = {
        SEN_1069_URL: (sen_1069_html(), "utf-8"),
        SEN_1068_URL: (sen_1068_html(), None),
        MOE_URL: (moe_html(), "UTF-8"),
    }
    pages.update(overrides)
    return pages


def make_collector(pages, db=None):
    opener = FakeOpener(pages)
    collector = AdmissionCollector(db=db or AdmissionDatabase(db_path=":memory:"), curator=AICurator(), opener=opener)
    return collector, opener


# ----------------------------------------------------------------------
# Live parsers
# ----------------------------------------------------------------------
def test_sen_board_parses_entities_nested_tags_and_whitespace():
    collector, _ = make_collector(live_pages())
    events = collector.collect_sen_board("1069", TargetGrade.HIGH_3_N, EventCategory.BRIEFING, now_kst=NOW)
    by_id = {e.event_id: e for e in events}

    first = by_id["sen_1069_20261120093011223"]
    assert first.title == "2027 정시 & 수시 대입 설명회 안내"  # "new" badge span stripped
    assert first.source_url == SEN_DETAIL_URL.format(bbs_sn="1069", doc_no="20261120093011223")
    assert first.booking_url == first.source_url
    assert first.event_date_str == "2026-11-20"
    assert first.target_grade == TargetGrade.HIGH_3_N
    assert first.target_region == TargetRegion.SEOUL
    assert first.category == EventCategory.BRIEFING
    assert first.expires_at == datetime(2026, 12, 20, tzinfo=KST)

    # Single-quoted href with whitespace around '=' and raw '&'
    assert "sen_1069_20261101120000001" in by_id
    # Other-board links are ignored
    assert not any("20261119000000002" in eid for eid in by_id)


def test_sen_post_date_falls_back_to_row_date_when_doc_no_invalid():
    collector, _ = make_collector(live_pages())
    events = collector.collect_sen_board("1069", TargetGrade.HIGH_3_N, EventCategory.BRIEFING, now_kst=NOW)
    by_id = {e.event_id: e for e in events}
    assert by_id["sen_1069_ABCD1234"].event_date_str == "2026-11-15"
    assert by_id["sen_1069_20261399000000001"].event_date_str == "2026-11-16"


def test_sen_li_layout_for_middle_board():
    collector, _ = make_collector(live_pages())
    events = collector.collect_sen_board("1068", TargetGrade.MIDDLE, EventCategory.BRIEFING, now_kst=NOW)
    assert [e.event_id for e in events] == ["sen_1068_20261124100000001"]
    assert events[0].title == "[고입] 2027 후기고 진학 설명회"
    assert events[0].target_grade == TargetGrade.MIDDLE


def test_30_day_kst_boundary_and_expiry():
    collector, _ = make_collector(live_pages())
    events = collector.collect_sen_board("1069", TargetGrade.HIGH_3_N, EventCategory.BRIEFING, now_kst=NOW)
    ids = {e.event_id for e in events}
    assert "sen_1069_20261028000000001" in ids      # 29 days old -> fresh
    assert "sen_1069_20261027000000001" not in ids  # 30 days old -> expires 11-26 00:00 KST <= now
    for e in events:
        assert e.expires_at.tzinfo is not None
        assert e.expires_at > NOW
        assert not AdmissionDatabase._is_expired(e, NOW)

    # Naive "now" is interpreted as KST: just before midnight the 29-day post survives,
    # one minute after midnight on day 30 it does not.
    late = collector.collect_sen_board("1069", TargetGrade.HIGH_3_N, EventCategory.BRIEFING,
                                       now_kst=datetime(2026, 11, 26, 23, 59))
    assert "sen_1069_20261028000000001" in {e.event_id for e in late}
    next_day = collector.collect_sen_board("1069", TargetGrade.HIGH_3_N, EventCategory.BRIEFING,
                                           now_kst=datetime(2026, 11, 27, 0, 1))
    assert "sen_1069_20261028000000001" not in {e.event_id for e in next_day}


def test_sen_rejects_non_official_link_host():
    collector, _ = make_collector(live_pages())
    events = collector.collect_sen_board("1069", TargetGrade.HIGH_3_N, EventCategory.BRIEFING, now_kst=NOW)
    assert all("피싱" not in e.title for e in events)
    assert all(is_official_url(e.source_url) for e in events)


def test_sen_same_title_keeps_separate_ids():
    collector, _ = make_collector(live_pages())
    events = collector.collect_sen_board("1069", TargetGrade.HIGH_3_N, EventCategory.BRIEFING, now_kst=NOW)
    same = sorted(e.event_id for e in events if e.title == "동일 제목 공지")
    assert same == ["sen_1069_20261118000000001", "sen_1069_20261119000000001"]


def test_moe_quote_variants_title_only_filter_and_detail_url():
    collector, _ = make_collector(live_pages())
    events = collector.collect_moe_press("294", now_kst=NOW)
    by_id = {e.event_id: e for e in events}
    assert set(by_id) == {"moe_294_83001", "moe_294_83000"}
    # Keyword lives only in the department column -> must be excluded (title-only filter)
    assert "moe_294_82999" not in by_id
    # Keyword match but older than 30 days
    assert "moe_294_82998" not in by_id

    evt = by_id["moe_294_83001"]
    assert evt.title == "2027학년도 수능 성적 통지 안내"
    assert evt.source_url == MOE_DETAIL_URL.format(board_id="294", board_seq="83001")
    assert evt.source_url == (
        "https://www.moe.go.kr/boardCnts/viewRenew.do?boardID=294&boardSeq=83001"
        "&lev=0&statusYN=W&s=moe&m=020402&opType=N"
    )
    assert evt.event_date_str == "2026-11-24"
    assert evt.target_region == TargetRegion.ALL
    assert evt.expires_at == datetime(2026, 12, 24, tzinfo=KST)


def test_moe_cp949_meta_charset_decoding():
    collector, _ = make_collector(live_pages(**{MOE_URL: (moe_html("cp949"), None)}))
    events = collector.collect_moe_press("294", now_kst=NOW)
    assert {e.title for e in events} == {"2027학년도 수능 성적 통지 안내", "고교학점제 운영 개선 방안 발표"}


def test_is_official_url_allowlist():
    assert is_official_url("https://www.sen.go.kr/user/bbs/BD_selectBbs.do?q_bbsSn=1069")
    assert is_official_url("https://moe.go.kr/x")
    assert not is_official_url("http://www.sen.go.kr/")              # not https
    assert not is_official_url("https://sen.go.kr.evil.com/")        # suffix trick
    assert not is_official_url("https://evil.com/?u=https://www.moe.go.kr")
    assert not is_official_url("javascript:alert(1)")
    assert not is_official_url("")


def test_fetch_rejects_unofficial_url_without_network():
    collector, opener = make_collector({})
    assert collector.fetch_url_html("https://evil.example.com/") == ""
    assert collector.fetch_url_html("http://www.sen.go.kr/") == ""
    assert opener.calls == []


# ----------------------------------------------------------------------
# run_collection pipeline
# ----------------------------------------------------------------------
def test_run_collection_counts_and_saves():
    collector, opener = make_collector(live_pages())
    results = collector.run_collection(now_kst=NOW)
    assert set(opener.calls) == {SEN_1069_URL, SEN_1068_URL, MOE_URL}
    assert results["errors"] == []
    assert results["moe_count"] == 2
    assert results["sen_count"] == 7 + 1
    assert results["public_count"] == results["sen_count"] + results["moe_count"]
    assert results["total_saved"] == results["public_count"]
    assert "academy_count" not in results
    saved = collector.db.list_all_events()
    assert len(saved) == results["total_saved"]
    for e in saved:
        assert e.event_id.startswith(("sen_", "moe_"))
        assert is_official_url(e.source_url)
        assert e.expires_at is not None


def test_stable_id_upsert_on_title_change():
    db = AdmissionDatabase(db_path=":memory:")
    collector, _ = make_collector(live_pages(), db=db)
    first = collector.run_collection(now_kst=NOW)

    updated = live_pages(**{SEN_1069_URL: (sen_1069_html(title_first="[수정] 대입 설명회 일정 변경"), "utf-8")})
    collector2, _ = make_collector(updated, db=db)
    second = collector2.run_collection(now_kst=NOW)

    assert second["total_saved"] == first["total_saved"]
    assert len(db.list_all_events()) == first["total_saved"]
    assert db.get_event("sen_1069_20261120093011223").title == "[수정] 대입 설명회 일정 변경"


def test_one_source_failure_is_graceful_and_preserves_db():
    db = AdmissionDatabase(db_path=":memory:")
    existing = AdmissionEvent(
        event_id="sen_1069_existing", title="기존 수집 이벤트", organizer="서울시교육청",
        target_grade=TargetGrade.HIGH_3_N, event_type=BriefingType.BRIEFING_RECAP,
        event_date_str="2026-11-25",
    )
    db.save_event(existing)

    collector, _ = make_collector(live_pages(**{MOE_URL: urllib.error.URLError("timeout")}), db=db)
    results = collector.run_collection(now_kst=NOW)
    assert results["moe_count"] == 0
    assert results["sen_count"] == 8
    assert len(results["errors"]) == 1 and "moe_294" in results["errors"][0]
    assert db.get_event("sen_1069_existing") is not None

    # All sources down -> nothing saved, nothing wiped
    collector_down, _ = make_collector({}, db=db)
    down = collector_down.run_collection(now_kst=NOW)
    assert down["total_saved"] == 0 and len(down["errors"]) == 3
    assert db.get_event("sen_1069_existing") is not None
    assert len(db.list_all_events()) == 1 + results["total_saved"]


# ----------------------------------------------------------------------
# Offline sample data / --demo
# ----------------------------------------------------------------------
def test_sample_collection_is_offline():
    collector, opener = make_collector({})
    events = collector.collect_sample_events()
    assert len(events) >= 11
    result = collector.seed_sample_collection()
    assert result["total_saved"] == len(events)
    assert opener.calls == []


def test_demo_makes_zero_network_calls(capsys):
    def _no_network(*args, **kwargs):
        raise AssertionError("--demo must not hit the network")

    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "ADMISSION_TELEGRAM_BOT_TOKEN": ""}), \
            patch("urllib.request.urlopen", side_effect=_no_network) as urlopen_mock, \
            patch.object(AdmissionCollector, "run_collection", side_effect=_no_network) as run_mock, \
            patch.object(AdmissionCollector, "_fetch_html_strict", side_effect=_no_network) as fetch_mock:
        runner = importlib.import_module("admission_bot_runner")
        runner.run_demo()
    assert urlopen_mock.call_count == 0
    assert run_mock.call_count == 0
    assert fetch_mock.call_count == 0
    assert "[DEMO]" in capsys.readouterr().out


def test_collector_loads_categorized_sample_events():
    db = AdmissionDatabase(db_path=":memory:")
    collector = AdmissionCollector(db=db, curator=AICurator())
    collector.seed_sample_collection()

    by_cat = {}
    for e in db.list_all_events():
        by_cat.setdefault(e.category, []).append(e)
        assert e.credibility_score >= 70
        assert e.source_name and e.source_url and e.source_url.startswith("https://")
        assert e.urgency in {"NORMAL", "HIGH", "BREAKING"}
    assert len(by_cat[EventCategory.EXPO]) >= 3
    assert len(by_cat[EventCategory.BRIEFING]) >= 5
    assert len(by_cat[EventCategory.SEMINAR]) >= 3

    briefing_orgs = {e.organizer for e in by_cat[EventCategory.BRIEFING]}
    assert {"메가스터디", "종로학원", "시대인재", "대성마이맥", "이투스"} <= briefing_orgs
    assert any(e.is_online_live for e in db.list_all_events())


def test_collector_drops_low_credibility_spam():
    collector = AdmissionCollector(db=AdmissionDatabase(db_path=":memory:"), curator=AICurator())
    spam_post = {
        "title": "[마감임박] 대치동 1위 적중률 100% 비법 전수 사설 특강",
        "organizer": "정체불명 과외방",
        "target_grade": TargetGrade.MIDDLE,
        "target_region": TargetRegion.LOCAL,
        "event_type": BriefingType.TICKETING_ALERT,
        "event_date": "2026-11-20 18:00",
        "content": "마감임박 비법전수 파격할인",
    }
    assert collector._process_raw_post(spam_post) is None


def test_collector_megastudy_post_title_accurate():
    collector = AdmissionCollector(db=AdmissionDatabase(db_path=":memory:"), curator=AICurator())
    mega_event = next(e for e in collector.collect_major_academy_seminars() if e.organizer == "메가스터디")
    assert "수능 가채점 분석 및 2027 정시 합격 전략" in mega_event.title
    assert "수시 최종 지원" not in mega_event.title


def test_collector_events_respect_2027_admission_calendar():
    """No post-CSAT (가채점 / 수능 직후) event may be dated before the 2026-11-19 수능."""
    collector = AdmissionCollector(db=AdmissionDatabase(db_path=":memory:"), curator=AICurator())
    for e in collector.collect_sample_events():
        if "가채점" in e.title or "수능 직후" in e.title:
            assert e.event_date_str[:10] >= "2026-11-19", e.title


def test_collector_credibility_gate_is_70():
    curator = MagicMock(spec=AICurator)
    curator.clean_marketing_fluff.side_effect = lambda t: t
    collector = AdmissionCollector(db=AdmissionDatabase(db_path=":memory:"), curator=curator)
    post = {
        "title": "경계값 테스트 설명회",
        "organizer": "테스트",
        "target_grade": TargetGrade.ALL,
        "event_type": BriefingType.BRIEFING_RECAP,
        "event_date": "2026-12-01 10:00",
    }
    curator.score_credibility.return_value = 69
    assert collector._process_raw_post(post) is None
    curator.score_credibility.return_value = 70
    assert collector._process_raw_post(post) is not None
