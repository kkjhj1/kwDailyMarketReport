"""Season-matched destination photos, sendPhoto dispatcher, Track A opt-out & photo isolation (L2)."""
from __future__ import annotations

import concurrent.futures
import json
import re
import socket
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from travel_brief import destination_photos as dp
from travel_brief.channels.telegram import TELEGRAM_CAPTION_LIMIT, TelegramChannel
from travel_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter
from travel_brief.destination_photos import (
    CURATED_PHOTO_CITIES,
    copy_photo_assets,
    resolve_destination_photo,
)
from travel_brief.flight_engine import FlightEngine
from travel_brief.storage.db import TravelBriefDatabase
from travel_brief.web_report import WebReportGenerator, build_travel_snapshot

ROOT = Path(__file__).resolve().parent.parent
# Track A KST weekday gate fixtures (2026-11-17 = Tuesday, 2026-11-19 = Thursday)
from datetime import datetime as _dt  # noqa: E402

TUESDAY_KST = _dt(2026, 11, 17, 12, 0)
THURSDAY_KST = _dt(2026, 11, 19, 12, 0)
MONDAY_KST = _dt(2026, 11, 16, 12, 0)
CURATED = ("NRT", "KIX", "FUK", "DAD", "CXR", "BKK", "TPE", "GUM", "DPS", "SIN")
FORBIDDEN_WEATHER_WORDS = ("현재 날씨", "오늘 날씨", "실시간 기상")
REAL_LOOKING_TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd"


def _fast_dispatcher() -> SafeTelegramDispatcher:
    return SafeTelegramDispatcher(rate_limiter=TelegramRateLimiter(max_global_rate=1000, min_chat_interval=0))


def _tree_snapshot(path: Path) -> dict[str, tuple[int, int]]:
    if not path.exists():
        return {}
    return {
        str(p.relative_to(path)): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in path.rglob("*") if p.is_file()
    }


# ----------------------------------------------------------------------
# 1. destination_photos resolver
# ----------------------------------------------------------------------
def test_all_curated_cities_resolve_with_honest_season_captions():
    assert tuple(CURATED_PHOTO_CITIES) == CURATED
    for code in CURATED:
        for season, label in (
            ("BEST_SEASON", "여행 최적 시즌 참고 이미지"),
            ("CAUTION", "기상/우기 주의 시즌 참고 이미지"),
            ("NORMAL", "대표 여행지 참고 이미지"),
        ):
            photo = resolve_destination_photo(code, season, dep_date="2027-01-14")
            assert photo is not None, code
            assert photo.dest_code == code and photo.path.is_file()
            assert photo.path.name.startswith(code)  # never another city's file
            assert photo.caption == f"{CURATED_PHOTO_CITIES[code]} · {label}"
            assert not any(w in photo.caption + photo.season_tag for w in FORBIDDEN_WEATHER_WORDS)
    assert resolve_destination_photo("fuk", "BEST_SEASON", dest_name="후쿠오카").caption == \
        "후쿠오카 · 여행 최적 시즌 참고 이미지"
    assert resolve_destination_photo("DAD", "weird").season_status == "NORMAL"


@pytest.mark.parametrize("code", ["CTS", "OKA", "HAN", "XYZ", "", None, "ALL", "@JP"])
def test_non_curated_or_unknown_city_returns_none(code):
    assert resolve_destination_photo(code, "BEST_SEASON") is None


def test_missing_asset_returns_none_and_month_season_priority(tmp_path):
    assert resolve_destination_photo("FUK", asset_dir=tmp_path) is None  # curated but no file -> None
    (tmp_path / "FUK.jpg").write_bytes(b"base")
    (tmp_path / "FUK_caution.jpg").write_bytes(b"caution")
    (tmp_path / "FUK_m03.jpg").write_bytes(b"march")
    (tmp_path / "NRT.jpg").write_bytes(b"other city")
    assert resolve_destination_photo("FUK", "NORMAL", asset_dir=tmp_path).path.name == "FUK.jpg"
    assert resolve_destination_photo("FUK", "CAUTION", asset_dir=tmp_path).path.name == "FUK_caution.jpg"
    assert resolve_destination_photo("FUK", "CAUTION", "2027-03-02", asset_dir=tmp_path).path.name == "FUK_m03.jpg"
    assert resolve_destination_photo("KIX", asset_dir=tmp_path) is None  # NRT.jpg never substitutes


def test_copy_photo_assets_isolated_to_target_dir(tmp_path):
    src_snapshot = _tree_snapshot(dp.ASSET_DIR)
    target = tmp_path / "report_root" / "assets" / "travel"
    photos = [resolve_destination_photo("FUK"), None, resolve_destination_photo("FUK"), resolve_destination_photo("DAD")]
    copied = copy_photo_assets(target, photos)
    assert sorted(p.name for p in copied) == ["DAD.jpg", "FUK.jpg"]
    assert sorted(p.name for p in target.iterdir()) == ["DAD.jpg", "FUK.jpg"]
    assert (target / "FUK.jpg").read_bytes() == dp.ASSET_DIR.joinpath("FUK.jpg").read_bytes()
    copy_photo_assets(target, photos)  # idempotent
    assert _tree_snapshot(dp.ASSET_DIR) == src_snapshot  # source assets never modified
    assert copy_photo_assets(tmp_path / "empty", [None]) == []
    assert not (tmp_path / "empty").exists()


# ----------------------------------------------------------------------
# 2. TelegramChannel.send_photo & SafeTelegramDispatcher.send_photo_to_recipient
# ----------------------------------------------------------------------
class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, *a):
        raw, self._raw = self._raw, b""
        return raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_photo_multipart_payload(tmp_path):
    img = tmp_path / "FUK.jpg"
    img.write_bytes(b"\xff\xd8\xffJPEGDATA")
    captured = {}

    def opener(req, timeout=None):
        captured["req"], captured["timeout"] = req, timeout
        return _Resp({"ok": True, "result": {"message_id": 77}})

    tg = TelegramChannel("123:abc", opener=opener)
    markup = {"inline_keyboard": [[{"text": "🔄", "callback_data": "trv:r:FUK"}]]}
    msg_id = tg.send_photo("42", img, caption="<b>후쿠오카</b> · 참고", reply_markup=markup)
    assert msg_id == "77"
    req = captured["req"]
    assert req.full_url.endswith("/sendPhoto") and req.get_method() == "POST"
    ctype = req.get_header("Content-type")
    assert ctype.startswith("multipart/form-data; boundary=")
    body = req.data
    assert b'name="chat_id"\r\n\r\n42\r\n' in body
    assert '<b>후쿠오카</b> · 참고'.encode("utf-8") in body
    assert b'name="parse_mode"\r\n\r\nHTML\r\n' in body
    assert b'name="reply_markup"' in body and b"trv:r:FUK" in body
    assert b'name="photo"; filename="FUK.jpg"\r\nContent-Type: image/jpeg' in body
    assert b"\xff\xd8\xffJPEGDATA" in body
    assert body.rstrip().endswith(b"--")


def test_send_photo_rejects_over_limit_caption_without_slicing(tmp_path):
    img = tmp_path / "FUK.jpg"
    img.write_bytes(b"x")
    opener = MagicMock()
    tg = TelegramChannel("123:abc", opener=opener)
    ok_caption = "가" * TELEGRAM_CAPTION_LIMIT
    opener.return_value = _Resp({"ok": True, "result": {"message_id": 1}})
    assert tg.send_photo("1", img, caption=ok_caption) == "1"
    opener.reset_mock()
    with pytest.raises(ValueError):
        tg.send_photo("1", img, caption="<b>" + "가" * TELEGRAM_CAPTION_LIMIT + "</b>")
    opener.assert_not_called()
    # through the dispatcher: a plain FAILED (never UNKNOWN, never sliced)
    status, msg_id, err = _fast_dispatcher().send_photo_to_recipient(tg, "1", img, caption="x" * 1025)
    assert (status, msg_id) == ("FAILED", None) and "too long" in err
    opener.assert_not_called()


@pytest.mark.parametrize("exc", [
    socket.timeout("timed out"),
    TimeoutError("read timed out"),
    urllib.error.URLError("timed out"),
    ConnectionResetError("connection reset by peer"),
])
def test_send_photo_dispatcher_ambiguous_errors_are_unknown(exc):
    tg = MagicMock()
    tg.send_photo.side_effect = exc
    status, msg_id, err = _fast_dispatcher().send_photo_to_recipient(tg, "9", "x.jpg", caption="c")
    assert status == "UNKNOWN" and msg_id is None and "Ambiguous" in err
    assert tg.send_photo.call_count == 1  # never retried


def test_send_photo_dispatcher_429_retry_then_sent_and_403_deactivates():
    tg = MagicMock()
    tg.send_photo.side_effect = [RuntimeError("Telegram HTTP 429 (sendPhoto): retry after 1"), "55"]
    with patch("travel_brief.channels.telegram_limiter.time.sleep") as slept:
        status, msg_id, _ = _fast_dispatcher().send_photo_to_recipient(tg, "9", "x.jpg", caption="c")
    assert (status, msg_id) == ("SENT", "55") and slept.called
    db = MagicMock()
    tg.send_photo.side_effect = RuntimeError("Telegram HTTP 403 (sendPhoto): Forbidden: bot was blocked by the user")
    status, _, _ = _fast_dispatcher().send_photo_to_recipient(tg, "9", "x.jpg", db=db)
    assert status == "FAILED"
    db.deactivate_recipient.assert_called_once_with("9", reason="USER_BLOCKED_BOT")


# ----------------------------------------------------------------------
# 3. travel_bot_runner.run_briefing: Track A opt-out, photo isolation, demo/dry-run
# ----------------------------------------------------------------------
class _FakeDispatcher:
    photo_behaviour: object = "SENT"
    instances: list = []

    def __init__(self, *a, **kw):
        self.calls: list[tuple[str, str]] = []
        self.texts: dict[str, str] = {}
        _FakeDispatcher.instances.append(self)

    def send_photo_to_recipient(self, tg, chat_id, photo_path, caption="", parse_mode="HTML", db=None,
                                reply_markup=None):
        self.calls.append(("photo", str(chat_id)))
        assert len(caption) <= TELEGRAM_CAPTION_LIMIT
        assert not any(w in caption for w in FORBIDDEN_WEATHER_WORDS)
        b = _FakeDispatcher.photo_behaviour
        if isinstance(b, BaseException):
            raise b
        return b, ("p1" if b == "SENT" else None), (None if b == "SENT" else "boom")

    def send_text_to_recipient(self, tg, chat_id, text, parse_mode="HTML", db=None, reply_markup=None):
        self.calls.append(("text", str(chat_id)))
        self.texts[str(chat_id)] = text
        return "SENT", "t1", None


@pytest.fixture
def runner_env(tmp_path, monkeypatch):
    import travel_bot_runner as tbr

    monkeypatch.delenv("TRAVEL_LIVE_NAVER", raising=False)
    _FakeDispatcher.instances = []
    _FakeDispatcher.photo_behaviour = "SENT"
    db_path = str(tmp_path / "t.sqlite3")
    db = TravelBriefDatabase(db_path=db_path)
    for cid in ("101", "202", "303"):
        db.add_recipient(cid, name=f"u{cid}", topic="TRAVEL")
    db.set_weekly_brief_enabled("202", False)
    fuk = resolve_destination_photo("FUK", "BEST_SEASON", dest_name="후쿠오카")
    monkeypatch.setattr(tbr, "get_travel_bot_token", lambda: REAL_LOOKING_TOKEN)
    monkeypatch.setattr(tbr, "get_travel_chat_id", lambda: "")
    monkeypatch.setattr(tbr, "SafeTelegramDispatcher", _FakeDispatcher)
    monkeypatch.setattr(tbr, "resolve_top1_photo", lambda recs: fuk)
    return tbr, db, db_path, tmp_path


def test_weekly_opt_out_excluded_from_default_dispatch_but_override_works(runner_env):
    tbr, db, db_path, tmp = runner_env
    reports_before = _tree_snapshot(ROOT / "reports")
    assert tbr.run_briefing(db_path=db_path, output_dir=tmp / "out" / "travel", now_kst=THURSDAY_KST) is True
    calls = _FakeDispatcher.instances[-1].calls
    assert ("text", "202") not in calls and ("photo", "202") not in calls
    assert calls == [("photo", "101"), ("text", "101"), ("photo", "303"), ("text", "303")]
    # explicit admin override is sent as-is (even to an opted-out chat)
    assert tbr.run_briefing(db_path=db_path, output_dir=tmp / "out2" / "travel", recipients_override=["202"]) is True
    assert _FakeDispatcher.instances[-1].calls == [("photo", "202"), ("text", "202")]
    assert _tree_snapshot(ROOT / "reports") == reports_before


@pytest.mark.parametrize("behaviour", [
    RuntimeError("photo exploded"), "UNKNOWN", "FAILED", socket.timeout("timed out"),
])
def test_photo_failure_never_blocks_main_text_briefing(runner_env, behaviour):
    tbr, db, db_path, tmp = runner_env
    _FakeDispatcher.photo_behaviour = behaviour
    assert tbr.run_briefing(db_path=db_path, output_dir=tmp / "out" / "travel", now_kst=THURSDAY_KST) is True
    calls = _FakeDispatcher.instances[-1].calls
    assert [c for c in calls if c[0] == "text"] == [("text", "101"), ("text", "303")]
    assert [c for c in calls if c[0] == "photo"] == [("photo", "101"), ("photo", "303")]  # never retried


def test_track_a_kst_weekday_gate_and_daily_ledger(runner_env):
    tbr, db, db_path, tmp = runner_env
    # 101: default THU_1X · 202: OFF (opted out) · 303: TUE_THU_2X
    db.set_weekly_brief_freq("303", "TUE_THU_2X")
    assert db.get_travel_user_prefs("202")["weekly_brief_freq"] == "OFF"
    # Tuesday: only 2x subscribers
    assert tbr.run_briefing(db_path=db_path, output_dir=tmp / "tue" / "travel", now_kst=TUESDAY_KST) is True
    assert [c for c in _FakeDispatcher.instances[-1].calls if c[0] == "text"] == [("text", "303")]
    # Thursday: 1x and 2x
    assert tbr.run_briefing(db_path=db_path, output_dir=tmp / "thu" / "travel", now_kst=THURSDAY_KST) is True
    assert [c for c in _FakeDispatcher.instances[-1].calls if c[0] == "text"] == [("text", "101"), ("text", "303")]
    # same KST day again: the ledger claim blocks any automatic re-send
    n = len(_FakeDispatcher.instances)
    assert tbr.run_briefing(db_path=db_path, output_dir=tmp / "thu2" / "travel", now_kst=THURSDAY_KST) is False
    assert all(not inst.calls for inst in _FakeDispatcher.instances[n:])
    # any other weekday: nobody (and never a compensatory send)
    n = len(_FakeDispatcher.instances)
    assert tbr.run_briefing(db_path=db_path, output_dir=tmp / "mon" / "travel", now_kst=MONDAY_KST) is False
    assert all(not inst.calls for inst in _FakeDispatcher.instances[n:])
    assert tbr.weekly_freqs_for_weekday(1) == ("TUE_THU_2X",)
    assert tbr.weekly_freqs_for_weekday(3) == ("THU_1X", "TUE_THU_2X")
    assert all(tbr.weekly_freqs_for_weekday(d) == () for d in (0, 2, 4, 5, 6))


def test_track_a_region_theme_segments_single_collection_and_sparse_fallback(runner_env, monkeypatch):
    tbr, db, db_path, tmp = runner_env
    db.set_weekly_brief_region("101", "EU")
    db.set_weekly_brief_theme("101", "RESORT")  # EU has no resort city -> sparse fallback to EU ALL
    db.set_weekly_brief_region("303", "JP")
    calls: list[dict] = []
    real_search = FlightEngine.search_deals

    def counting_search(self, *a, **kw):
        calls.append(kw)
        return real_search(self, *a, **kw)

    monkeypatch.setattr(FlightEngine, "search_deals", counting_search)
    assert tbr.run_briefing(db_path=db_path, output_dir=tmp / "seg" / "travel", now_kst=THURSDAY_KST) is True
    extra_calls = [kw for kw in calls if kw.get("extra_destinations")]
    assert len(extra_calls) == 1  # ONE collection pass for every needed region (never per region/theme)
    assert extra_calls[0].get("pg_only") is True
    regions = {m.region_code for m in extra_calls[0]["extra_destinations"].values()}
    assert regions == {"EU", "JP"}
    texts = _FakeDispatcher.instances[-1].texts
    eu, jp = texts["101"], texts["303"]
    assert "해당 권역은 관광/휴양 중심이라 권역 전체 기준으로 추천합니다" in eu
    assert "region=EU" in eu and "유럽" in eu and "5박 이상 권장" in eu
    assert "region=JP" in jp and "region=EU" not in jp
    assert tbr.fits_tg01(eu) and tbr.fits_tg01(jp)
    # the web report of the same run carries the extra regions for `?region=` deep links
    root_html = (tmp / "seg" / "travel_latest.html").read_text(encoding="utf-8")
    assert 'data-filter="EU"' in root_html and 'data-filter="JP"' in root_html


def test_top1_caption_is_short_escaped_and_honest():
    import travel_bot_runner as tbr

    engine = FlightEngine(live_naver=False)
    recs = build_travel_snapshot(engine, top_n=3).recommendations
    photo = resolve_destination_photo("FUK", "CAUTION", dest_name="<후쿠오카>")
    caption = tbr.build_top1_photo_caption(recs[0], photo)
    assert len(caption) <= TELEGRAM_CAPTION_LIMIT
    assert "&lt;후쿠오카&gt;" in caption and "<후쿠오카>" not in caption
    assert "TOP 1" in caption and recs[0].best_deal.departure_date in caption
    assert not any(w in caption for w in FORBIDDEN_WEATHER_WORDS)
    assert set(re.findall(r"</?([a-z]+)", caption)) <= {"b", "i"}


@pytest.mark.parametrize("mode", ["demo", "dry_run"])
def test_demo_and_dry_run_never_send_photos_or_touch_reports(tmp_path, monkeypatch, mode):
    import travel_bot_runner as tbr

    monkeypatch.delenv("TRAVEL_LIVE_NAVER", raising=False)
    reports_before = _tree_snapshot(ROOT / "reports")
    prod_db = ROOT / "alerts.sqlite3"
    prod_db_before = (prod_db.stat().st_mtime_ns, prod_db.stat().st_size) if prod_db.exists() else None
    boom = MagicMock(side_effect=AssertionError("network send attempted in demo/dry-run"))
    monkeypatch.setattr(TelegramChannel, "send_photo", boom)
    monkeypatch.setattr(TelegramChannel, "send_text", boom)
    monkeypatch.setattr(SafeTelegramDispatcher, "send_photo_to_recipient", boom)
    monkeypatch.setattr(tbr, "get_travel_bot_token", boom)
    if mode == "demo":
        assert tbr.run_briefing(demo=True) is True
        demo_root = ROOT / "scratch" / "reports_demo"
    else:
        assert tbr.run_briefing(dry_run=True, db_path=str(tmp_path / "d.sqlite3")) is True
        demo_root = ROOT / "scratch" / "reports_demo"
    boom.assert_not_called()
    assert _tree_snapshot(ROOT / "reports") == reports_before
    if prod_db_before is not None:
        assert (prod_db.stat().st_mtime_ns, prod_db.stat().st_size) == prod_db_before
    root_html = (demo_root / "travel_latest.html").read_text(encoding="utf-8")
    for src in re.findall(r'<img class="top-card-photo" src="([^"]+)"', root_html):
        assert src.startswith("assets/travel/") and (demo_root / src).is_file()


# ----------------------------------------------------------------------
# 4. web_report: root vs nested relative paths, CSS, no broken <img>
# ----------------------------------------------------------------------
def _img_srcs(html_text: str) -> list[str]:
    return re.findall(r'<img class="top-card-photo" src="([^"]+)"', html_text)


@pytest.fixture(scope="module")
def offline_snapshot():
    engine = FlightEngine(live_naver=False)
    return engine, build_travel_snapshot(engine, top_n=3)


def test_web_report_root_vs_nested_relative_asset_paths(tmp_path, offline_snapshot):
    engine, snapshot = offline_snapshot
    gen = WebReportGenerator(engine=engine)
    expected = [
        p for p in gen.resolve_top_photos(snapshot) if p is not None
    ]
    assert expected, "offline TOP 3 should include at least one curated city"
    out = tmp_path / "reports" / "travel"
    dated, latest = gen.save_reports(output_dir=out, snapshot=snapshot)
    root_html = (tmp_path / "reports" / "travel_latest.html").read_text(encoding="utf-8")
    nested_html = latest.read_text(encoding="utf-8")
    assert dated.read_text(encoding="utf-8") == nested_html
    root_srcs, nested_srcs = _img_srcs(root_html), _img_srcs(nested_html)
    assert len(root_srcs) == len(nested_srcs) == len(expected)
    assert all(s.startswith("assets/travel/") for s in root_srcs)
    assert all(s.startswith("../assets/travel/") for s in nested_srcs)
    for s in root_srcs:
        assert (tmp_path / "reports" / s).is_file()
    for s in nested_srcs:
        assert (out / s).resolve().is_file()
    assert sorted(p.name for p in (tmp_path / "reports" / "assets" / "travel").iterdir()) == \
        sorted({p.filename for p in expected})
    for p in expected:
        assert p.caption in root_html
    assert not any(w in root_html for w in FORBIDDEN_WEATHER_WORDS)
    # flat output dir: everything relative to itself
    flat = tmp_path / "flat"
    _, flat_latest = gen.save_reports(output_dir=flat, snapshot=snapshot)
    assert all(s.startswith("assets/travel/") for s in _img_srcs(flat_latest.read_text(encoding="utf-8")))
    assert (flat / "assets" / "travel").is_dir()


def test_web_report_css_360px_rules(offline_snapshot):
    engine, snapshot = offline_snapshot
    html_text = WebReportGenerator(engine=engine).generate_html_report(snapshot, asset_rel_prefix="assets/travel")
    css = re.search(r"\.top-card-photo \{([^}]*)\}", html_text).group(1)
    for rule in ("display: block", "width: 100%", "max-width: 100%", "height: 180px", "object-fit: cover",
                 "border-radius"):
        assert rule in css, rule
    wrap = re.search(r"\.top-card-photo-wrap \{([^}]*)\}", html_text).group(1)
    assert "max-width: 100%" in wrap and "overflow: hidden" in wrap and "min-width: 0" in wrap
    mobile = html_text[html_text.index("@media (max-width: 768px)"):]
    assert re.search(r"\.top-cards-grid,[^{]*\{\s*grid-template-columns: minmax\(0, 1fr\)", mobile)
    assert ".top-card-photo {" in mobile


def test_web_report_no_broken_img_for_fallback_or_missing_assets(tmp_path, offline_snapshot):
    engine, snapshot = offline_snapshot
    gen = WebReportGenerator(engine=engine)
    first_code = snapshot.recommendations[0].destination.code
    real = resolve_destination_photo

    def first_is_fallback(code, *a, **kw):
        return None if code == first_code else real(code, *a, **kw)

    with patch("travel_brief.web_report.resolve_destination_photo", side_effect=first_is_fallback):
        html_text = gen.generate_html_report(snapshot, asset_rel_prefix="assets/travel")
    assert f"assets/travel/{first_code}" not in html_text
    n_expected = sum(1 for p in gen.resolve_top_photos(snapshot)[1:] if p is not None)
    assert len(_img_srcs(html_text)) == n_expected
    # no asset at all -> no <img>, no assets dir
    with patch("travel_brief.web_report.resolve_destination_photo", return_value=None):
        _, latest = gen.save_reports(output_dir=tmp_path / "r" / "travel", snapshot=snapshot)
    assert "<img" not in latest.read_text(encoding="utf-8")
    assert "<img" not in (tmp_path / "r" / "travel_latest.html").read_text(encoding="utf-8")
    assert not (tmp_path / "r" / "assets").exists()
    # default call (no prefix) never emits images
    assert "<img" not in gen.generate_html_report(snapshot)


def test_web_report_partial_asset_copy_failure_never_emits_broken_img(tmp_path, offline_snapshot):
    engine, snapshot = offline_snapshot
    gen = WebReportGenerator(engine=engine)
    photos = [p for p in gen.resolve_top_photos(snapshot) if p is not None]
    if len({p.filename for p in photos}) < 2:
        pytest.skip("offline TOP 3 needs >= 2 distinct curated photos for a partial-copy scenario")
    failing = photos[0].filename
    real_copy = dp.shutil.copy2

    def flaky_copy(src, dst, *a, **kw):
        if Path(dst).name == failing:
            raise OSError("simulated disk error")
        return real_copy(src, dst, *a, **kw)

    with patch.object(dp.shutil, "copy2", side_effect=flaky_copy):
        _, latest = gen.save_reports(output_dir=tmp_path / "r" / "travel", snapshot=snapshot)
    root_dir = tmp_path / "r"
    assert not (root_dir / "assets" / "travel" / failing).exists()
    nested_srcs = _img_srcs(latest.read_text(encoding="utf-8"))
    root_srcs = _img_srcs((root_dir / "travel_latest.html").read_text(encoding="utf-8"))
    assert nested_srcs and root_srcs
    assert all(failing not in s for s in nested_srcs + root_srcs)
    for s in root_srcs:
        assert (root_dir / s).is_file()
    for s in nested_srcs:
        assert (root_dir / "travel" / s).resolve().is_file()


# ----------------------------------------------------------------------
# 5. interactive: fresh city search photo; refresh / ALL / fallback skip
# ----------------------------------------------------------------------
class _SyncPool:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *a, **kw):
        self.submitted.append(getattr(fn, "__name__", str(fn)))
        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn(*a, **kw))
        except BaseException as exc:  # pragma: no cover - surfaced via future
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait=False, **kw):
        return None


@pytest.fixture
def photo_handler():
    from travel_brief.interactive import TravelInteractiveHandler

    h = TravelInteractiveHandler(bot_token=REAL_LOOKING_TOKEN, engine=FlightEngine(live_naver=False))
    assert h._photo_enabled
    h.tg = MagicMock()
    h.dispatcher = _fast_dispatcher()
    h.worker_pool = _SyncPool()
    h.ui_pool = _SyncPool()
    h._arm_watchdog = lambda *a, **kw: None
    h.edits = []

    def fake_edit(chat_id, message_id, request_seq, text, reply_markup=None, is_loading=False):
        h.edits.append((message_id, text, is_loading))
        return True

    h._edit_if_latest = fake_edit
    yield h
    h.shutdown()


def _final_edits(h):
    return [e for e in h.edits if not e[2]]


def test_fresh_city_search_sends_best_effort_photo_after_briefing(photo_handler):
    h = photo_handler
    h._launch_search("c1", 10, "FUK", "u1", is_refresh=False)
    assert len(_final_edits(h)) == 1
    assert h.tg.send_photo.call_count == 1
    kwargs = h.tg.send_photo.call_args.kwargs
    assert Path(kwargs["photo_path"]).name.startswith("FUK") and kwargs["recipient"] == "c1"
    assert "후쿠오카" in kwargs["caption"] and "참고 이미지" in kwargs["caption"]
    assert not any(w in kwargs["caption"] for w in FORBIDDEN_WEATHER_WORDS)
    assert h.ui_pool.submitted[-1] == "_send_city_photo"


def test_photo_failure_does_not_affect_city_briefing(photo_handler):
    h = photo_handler
    h.tg.send_photo.side_effect = socket.timeout("timed out")
    h._launch_search("c1", 11, "DAD", "u1", is_refresh=False)
    assert len(_final_edits(h)) == 1 and "⚠️" not in _final_edits(h)[0][1][:2]
    assert h.tg.send_photo.call_count == 1  # UNKNOWN is never retried
    h.tg.send_photo.side_effect = RuntimeError("Telegram HTTP 400: bad photo")
    h._launch_search("c1", 12, "NRT", "u1", is_refresh=False)
    assert len(_final_edits(h)) == 2


@pytest.mark.parametrize("target,is_refresh", [("FUK", True), ("ALL", False), ("ALL", True), ("CTS", False)])
def test_refresh_all_and_fallback_skip_photo(photo_handler, target, is_refresh):
    h = photo_handler
    h._launch_search("c1", 20, target, "u1", is_refresh=is_refresh)
    assert len(_final_edits(h)) == 1  # briefing (or error card) still delivered
    h.tg.send_photo.assert_not_called()
    assert "_send_city_photo" not in h.ui_pool.submitted


def test_fallback_city_never_gets_other_city_photo(photo_handler):
    assert photo_handler.resolve_city_photo("CTS") is None
    assert photo_handler.resolve_city_photo("OKA") is None
    photo = photo_handler.resolve_city_photo("KIX")
    assert photo is not None and photo.dest_code == "KIX"


def test_dummy_token_handler_never_uploads_photo():
    from travel_brief.interactive import TravelInteractiveHandler

    h = TravelInteractiveHandler(bot_token="dummy", engine=FlightEngine(live_naver=False))
    try:
        assert h._photo_enabled is False
        h.tg = MagicMock()
        assert h._submit_city_photo("c1", "FUK", None) is False
        h.tg.send_photo.assert_not_called()
    finally:
        h.shutdown()
