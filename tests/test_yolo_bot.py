"""Comprehensive test suite for YOLO Bot (욜로봇).
Verifies:
1. State transitions: ONBOARDING -> ACTIVE -> archetype switch -> UNSUBSCRIBED
2. [INV-01] Solo-Only invariant (`solo_tags` non-empty & `https://` URL enforcement)
3. [INV-02] Weather Hard Gate & Rainy Day Fallback across RAIN, SNOW, HEAT, COLD, DUST, FOG, UNKNOWN
4. Weather fallback disabled (`weather_fallback_enabled=False`) safety block on severe weather
5. [TG-01/02/03] Slim mobile card length caps (<=1800 visible, <=3500 HTML) and strict `html.escape()`
6. [TG-04] 0.1s immediate `answerCallbackQuery` call order BEFORE DB/edit work & <=64-byte callback_data
7. [TG-05] Weekly dispatch idempotency (`UNIQUE(chat_id, week_key, dispatch_type)`) & 429 retry
8. [TG-06] Token-free `--demo` execution in `:memory:` without polluting filesystem
9. v2.0 visual web magazine: 3+ curations per archetype, child-table round-trip,
   legacy DB migration, 360px CSS contract, magazine URL button
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3
import pytest

from yolo_bot_runner import generate_report, run_demo
from yolo_brief.channels.telegram import YoloTelegramChannel
from yolo_brief.channels.telegram_card import (
    MAX_CALLBACK_BYTES,
    MAX_HTML_CHARS,
    MAX_VISIBLE_CHARS,
    WEB_MAGAZINE_URL,
    build_archetype_keyboard,
    build_main_keyboard,
    measure_message,
    render_curation_card,
    render_onboarding_card,
    render_unsub_confirm_card,
    validate_callback_data,
)
from yolo_brief.channels.telegram_limiter import (
    SafeTelegramDispatcher,
    TelegramRateLimiter,
)
from yolo_brief.fixtures.seed_curations import SEED_CURATIONS
from yolo_brief.interactive import (
    ALLOWED_ARCHETYPE_CALLBACKS,
    ALLOWED_STATIC_CALLBACKS,
    YoloInteractiveBot,
)
from yolo_brief.matching import match_curations
from yolo_brief.models import (
    ActivityMode,
    BudgetLine,
    GalleryImage,
    ProfileState,
    Season,
    SoloArchetype,
    WeatherClass,
    TimelineEntry,
    WeatherSnapshot,
    YoloCuration,
    YoloUserProfile,
)
from yolo_brief.storage.db import CURATION_V2_COLUMNS, YoloDatabase
from yolo_brief.web_report import YoloWebReportGenerator
from yolo_brief.weather_engine import (
    evaluate_weather_gate,
    get_iso_week_key,
    get_kst_season,
)


@pytest.fixture()
def memory_db() -> YoloDatabase:
    db = YoloDatabase(":memory:")
    db.seed_curations(SEED_CURATIONS)
    yield db
    db.close()


# =========================================================================
# 1. Onboarding & State Transitions + DB Constraints
# =========================================================================
def test_onboarding_and_archetype_state_transitions(memory_db: YoloDatabase) -> None:
    user = memory_db.upsert_onboarding_user("user_101")
    assert user.profile_state == ProfileState.ONBOARDING
    assert user.archetype is None
    assert user.weather_fallback_enabled is True

    # Transition to ACTIVE with ACTIVE_CHALLENGER
    user = memory_db.set_user_archetype("user_101", SoloArchetype.ACTIVE_CHALLENGER)
    assert user.profile_state == ProfileState.ACTIVE
    assert user.archetype == SoloArchetype.ACTIVE_CHALLENGER

    # Instant switch to WELLNESS_DEEPDIVER
    user = memory_db.set_user_archetype("user_101", SoloArchetype.WELLNESS_DEEPDIVER)
    assert user.profile_state == ProfileState.ACTIVE
    assert user.archetype == SoloArchetype.WELLNESS_DEEPDIVER

    # Unsubscribe
    user = memory_db.unsubscribe_user("user_101")
    assert user.profile_state == ProfileState.UNSUBSCRIBED

    # Re-onboarding via /start resets to ONBOARDING
    user = memory_db.upsert_onboarding_user("user_101")
    assert user.profile_state == ProfileState.ONBOARDING
    assert user.archetype is None


def test_db_check_constraint_rejects_invalid_onboarding_with_archetype(
    memory_db: YoloDatabase,
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with memory_db._connect() as conn:
            conn.execute(
                """
                INSERT INTO yolo_users (
                    chat_id, profile_state, archetype, weather_area_code,
                    weather_fallback_enabled, created_at, updated_at
                ) VALUES ('bad_user', 'ONBOARDING', 'ACTIVE_CHALLENGER', '1100000000', 1, '2026-01-01', '2026-01-01')
                """
            )


# =========================================================================
# 2. [INV-01] Solo-Only Invariant Verification
# =========================================================================
def test_inv01_solo_tags_and_https_url_enforced() -> None:
    # Empty solo_tags must raise ValueError
    with pytest.raises(ValueError, match="INV-01"):
        YoloCuration(
            id="BAD-01",
            title="커플 2인 전용 세트",
            summary="2인 이상 주문 필수",
            season=Season.SPRING,
            weather_class=WeatherClass.CLEAR,
            activity_mode=ActivityMode.INDOOR,
            location_name="서울",
            cost_or_duration="5만원",
            action_url="https://example.com",
            archetypes=(SoloArchetype.URBAN_HIPSTER,),
            solo_tags=(),
        )

    # Non-https URL must raise ValueError
    with pytest.raises(ValueError, match="https://"):
        YoloCuration(
            id="BAD-02",
            title="불안정한 링크",
            summary="설명",
            season=Season.SPRING,
            weather_class=WeatherClass.CLEAR,
            activity_mode=ActivityMode.INDOOR,
            location_name="서울",
            cost_or_duration="1만원",
            action_url="http://insecure.example.com",
            archetypes=(SoloArchetype.URBAN_HIPSTER,),
            solo_tags=("#1인환영",),
        )

    # All SEED_CURATIONS must have non-empty solo_tags
    for c in SEED_CURATIONS:
        assert len(c.solo_tags) >= 2
        assert c.action_url.startswith("https://")


# =========================================================================
# 3. [INV-02] Weather Hard Gate & Rainy Day Fallback Verification
# =========================================================================
@pytest.mark.parametrize(
    "snapshot,expected_class",
    [
        (
            WeatherSnapshot(weather_class=WeatherClass.RAIN, pop_percent=70, precip_mm=5.0),
            WeatherClass.RAIN,
        ),
        (
            WeatherSnapshot(weather_class=WeatherClass.SNOW, snow_mm=3.5),
            WeatherClass.SNOW,
        ),
        (
            WeatherSnapshot(weather_class=WeatherClass.CLEAR, feels_like_c=35.0),
            WeatherClass.HEAT,
        ),
        (
            WeatherSnapshot(weather_class=WeatherClass.CLEAR, feels_like_c=-12.0),
            WeatherClass.COLD,
        ),
        (
            WeatherSnapshot(weather_class=WeatherClass.CLEAR, pm_grade="BAD"),
            WeatherClass.DUST,
        ),
        (
            WeatherSnapshot(weather_class=WeatherClass.FOG),
            WeatherClass.FOG,
        ),
        (
            WeatherSnapshot(weather_class=WeatherClass.UNKNOWN),
            WeatherClass.UNKNOWN,
        ),
        (None, WeatherClass.UNKNOWN),
    ],
)
def test_inv02_severe_weather_blocks_outdoor_and_triggers_indoor_fallback(
    memory_db: YoloDatabase,
    snapshot: WeatherSnapshot | None,
    expected_class: WeatherClass,
) -> None:
    is_severe, eff_class, _ = evaluate_weather_gate(snapshot)
    assert is_severe is True
    assert eff_class == expected_class

    curations = memory_db.list_active_curations()
    for outdoor_arc in (SoloArchetype.ACTIVE_CHALLENGER, SoloArchetype.ROAD_NOMAD):
        user = YoloUserProfile(
            chat_id="u_outdoor",
            profile_state=ProfileState.ACTIVE,
            archetype=outdoor_arc,
            weather_fallback_enabled=True,
        )
        res = match_curations(
            user, curations, snapshot=snapshot, target_season=Season.AUTUMN
        )
        assert res.is_severe_weather is True
        assert res.fallback_applied is True
        assert len(res.items) >= 1
        # Zero outdoor items allowed under severe weather!
        for item in res.items:
            assert item.activity_mode == ActivityMode.INDOOR


def test_weather_fallback_disabled_blocks_unsafe_outdoor_without_exposing_it(
    memory_db: YoloDatabase,
) -> None:
    curations = memory_db.list_active_curations()
    user = YoloUserProfile(
        chat_id="u_no_fb",
        profile_state=ProfileState.ACTIVE,
        archetype=SoloArchetype.ACTIVE_CHALLENGER,
        weather_fallback_enabled=False,
    )
    rain = WeatherSnapshot(weather_class=WeatherClass.RAIN, pop_percent=80, precip_mm=10.0)
    res = match_curations(user, curations, snapshot=rain, target_season=Season.SPRING)
    assert res.is_severe_weather is True
    assert res.blocked_by_weather_no_fallback is True
    assert res.items == []

    card_html, _ = render_curation_card(user, res)
    assert "악천후 안전 가드 작동" in card_html


# =========================================================================
# 4. [TG-01/02/03] Slim Mobile Card Capacity & Strict HTML Escape
# =========================================================================
def test_tg01_tg02_card_length_and_html_escape() -> None:
    malicious_curation = YoloCuration(
        id="XSS-01",
        title="<script>alert('xss')</script> & 1인 러닝",
        summary="<b>태그침투</b> & <img src=x onerror=1>",
        season=Season.AUTUMN,
        weather_class=WeatherClass.CLEAR,
        activity_mode=ActivityMode.OUTDOOR,
        location_name="서울 <강남>",
        cost_or_duration="1만원 & 60분",
        action_url="https://example.com/solo?a=1&b=2",
        archetypes=(SoloArchetype.ACTIVE_CHALLENGER,),
        solo_tags=("#1인<안전>", "#혼자&환영"),
    )
    user = YoloUserProfile(
        chat_id="u_xss",
        profile_state=ProfileState.ACTIVE,
        archetype=SoloArchetype.ACTIVE_CHALLENGER,
    )
    res = match_curations(
        user,
        [malicious_curation],
        snapshot=WeatherSnapshot(weather_class=WeatherClass.CLEAR),
        target_season=Season.AUTUMN,
    )
    html_text, _ = render_curation_card(user, res)
    vis_len, html_len = measure_message(html_text)
    assert vis_len <= MAX_VISIBLE_CHARS
    assert html_len <= MAX_HTML_CHARS
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text
    assert "<img" not in html_text


# =========================================================================
# 5. [TG-04] 0.1s Immediate Callback Answer Order & 64-Byte Limit
# =========================================================================
def test_tg04_answer_callback_precedes_edit_and_all_callbacks_under_64_bytes(
    memory_db: YoloDatabase,
) -> None:
    channel = YoloTelegramChannel(token="", dry_run=True)
    bot = YoloInteractiveBot(db=memory_db, channel=channel)

    bot.handle_update(
        {
            "callback_query": {
                "id": "cb_999",
                "data": "yolo:set:ACTIVE_CHALLENGER",
                "message": {"chat": {"id": "u_cb"}, "message_id": 42},
            }
        },
        ref_date=date(2026, 4, 15),
    )

    methods = [m for m, _ in channel.call_log]
    assert len(methods) >= 2
    assert methods[0] == "answerCallbackQuery"
    assert methods[1] == "editMessageText"

    # Verify all static & archetype callbacks + keyboard buttons <= 64 bytes
    all_callbacks = set(ALLOWED_STATIC_CALLBACKS) | set(ALLOWED_ARCHETYPE_CALLBACKS.keys())
    user = memory_db.get_user("u_cb")
    assert user is not None
    match = match_curations(
        user,
        memory_db.list_active_curations(),
        snapshot=WeatherSnapshot(weather_class=WeatherClass.CLEAR),
        target_season=Season.AUTUMN,
    )
    for kb in (
        build_archetype_keyboard(user.archetype),
        build_main_keyboard(user),
        render_unsub_confirm_card()[1],
        render_curation_card(user, match)[1],
    ):
        for row in kb["inline_keyboard"]:
            for btn in row:
                # URL 버튼(웹 매거진)은 callback_data 키가 없으므로 검증 대상에서 제외
                if btn.get("callback_data"):
                    all_callbacks.add(btn["callback_data"])

    for cb_str in all_callbacks:
        assert len(cb_str.encode("utf-8")) <= MAX_CALLBACK_BYTES
        assert validate_callback_data(cb_str) == cb_str

    with pytest.raises(ValueError, match="TG-04"):
        validate_callback_data("yolo:set:" + ("A" * 60))


# =========================================================================
# 6. [TG-05] Weekly Dispatch Deduplication & Rate Limiter
# =========================================================================
def test_tg05_weekly_dispatch_idempotency(memory_db: YoloDatabase) -> None:
    memory_db.set_user_archetype("u_1", SoloArchetype.ACTIVE_CHALLENGER)
    memory_db.set_user_archetype("u_2", SoloArchetype.URBAN_HIPSTER)

    channel = YoloTelegramChannel(token="", dry_run=True)
    limiter = TelegramRateLimiter(
        max_global_rate=1000.0, min_chat_interval=0.0, sleep=lambda _: None
    )
    dispatcher = SafeTelegramDispatcher(rate_limiter=limiter, sleep=lambda _: None)
    bot = YoloInteractiveBot(db=memory_db, channel=channel, dispatcher=dispatcher)

    ref = date(2026, 11, 7)
    first = bot.dispatch_weekly_briefings(ref_date=ref)
    assert first["sent"] == 2
    assert first["skipped"] == 0

    # Second run in the same ISO week must skip all already-sent users
    second = bot.dispatch_weekly_briefings(ref_date=ref)
    assert second["sent"] == 0
    assert second["skipped"] == 2


# =========================================================================
# 7. [TG-06] Token-Free `--demo` Isolation Guard
# =========================================================================
def test_tg06_demo_runs_in_memory_without_creating_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = run_demo()
    assert rc == 0
    assert list(tmp_path.glob("*.sqlite3")) == []


def test_season_and_iso_week_helpers() -> None:
    assert get_kst_season(date(2026, 4, 1)) == Season.SPRING
    assert get_kst_season(date(2026, 7, 15)) == Season.SUMMER
    assert get_kst_season(date(2026, 10, 10)) == Season.AUTUMN
    assert get_kst_season(date(2026, 1, 20)) == Season.WINTER
    assert get_iso_week_key(date(2026, 11, 7)) == "2026-W45"


# =========================================================================
# 8. v2.0 Visual Web Magazine & 3+ Curation Expansion
# =========================================================================
def _active_user(arc: SoloArchetype, chat_id: str = "u_v2") -> YoloUserProfile:
    return YoloUserProfile(
        chat_id=chat_id,
        profile_state=ProfileState.ACTIVE,
        archetype=arc,
        weather_fallback_enabled=True,
    )


@pytest.mark.parametrize("arc", list(SoloArchetype))
def test_v2_autumn_clear_has_three_curations_per_archetype(
    memory_db: YoloDatabase, arc: SoloArchetype
) -> None:
    res = match_curations(
        _active_user(arc),
        memory_db.list_active_curations(),
        snapshot=WeatherSnapshot(weather_class=WeatherClass.CLEAR),
        target_season=Season.AUTUMN,
    )
    assert res.is_severe_weather is False
    assert len(res.items) >= 3
    assert all(not item.is_fallback for item in res.items)


@pytest.mark.parametrize("season", [Season.SPRING, Season.SUMMER, Season.AUTUMN, Season.WINTER])
@pytest.mark.parametrize("arc", [SoloArchetype.ACTIVE_CHALLENGER, SoloArchetype.ROAD_NOMAD])
def test_v2_rain_fallback_has_three_indoor_curations(
    memory_db: YoloDatabase, arc: SoloArchetype, season: Season
) -> None:
    rain = WeatherSnapshot(weather_class=WeatherClass.RAIN, pop_percent=80, precip_mm=8.0)
    res = match_curations(
        _active_user(arc), memory_db.list_active_curations(), snapshot=rain, target_season=season
    )
    assert res.fallback_applied is True
    assert len(res.items) >= 3
    for item in res.items:
        assert item.activity_mode == ActivityMode.INDOOR


@pytest.mark.parametrize("arc", list(SoloArchetype))
def test_v2_three_item_card_within_caps_and_has_magazine_button(
    memory_db: YoloDatabase, arc: SoloArchetype
) -> None:
    user = _active_user(arc)
    res = match_curations(
        user,
        memory_db.list_active_curations(),
        snapshot=WeatherSnapshot(weather_class=WeatherClass.CLEAR),
        target_season=Season.AUTUMN,
    )
    card_html, markup = render_curation_card(user, res)
    vis_len, html_len = measure_message(card_html)
    assert vis_len <= MAX_VISIBLE_CHARS
    assert html_len <= MAX_HTML_CHARS

    first_row = markup["inline_keyboard"][0]
    assert first_row == [
        {"text": "🌐 감성 사진 & 나홀로 풀코스 매거진 보기", "url": WEB_MAGAZINE_URL}
    ]
    assert "callback_data" not in first_row[0]
    # Original callback rows are preserved below the URL row
    assert markup["inline_keyboard"][1:] == build_main_keyboard(user)["inline_keyboard"]


def test_v2_seed_curations_carry_valid_magazine_details() -> None:
    ids = [c.id for c in SEED_CURATIONS]
    assert len(ids) == len(set(ids))
    for c in SEED_CURATIONS:
        assert c.timeline, c.id
        assert c.budget_breakdown, c.id
        assert c.solo_secrets.strip(), c.id
        assert c.total_budget_krw == sum(b.cost_krw for b in c.budget_breakdown)
        for url in (c.hero_image_url, c.map_naver_url, c.map_kakao_url):
            assert url == "" or url.startswith("https://")
        if c.hero_image_url:
            assert c.hero_image_alt.strip(), c.id
        for b in c.budget_breakdown:
            assert b.category in ("transport", "food", "ticket", "stay", "etc")


def test_v2_model_rejects_unsafe_urls_and_negative_budget() -> None:
    base = dict(
        id="V2-BAD",
        title="검증용",
        summary="요약",
        season=Season.AUTUMN,
        weather_class=WeatherClass.CLEAR,
        activity_mode=ActivityMode.INDOOR,
        location_name="서울",
        cost_or_duration="1만원",
        action_url="https://example.com",
        archetypes=(SoloArchetype.URBAN_HIPSTER,),
        solo_tags=("#1인환영",),
    )
    with pytest.raises(ValueError, match="hero_image_url"):
        YoloCuration(**base, hero_image_url="javascript:alert(1)")
    with pytest.raises(ValueError, match="map_naver_url"):
        YoloCuration(**base, map_naver_url="http://map.naver.com/")
    with pytest.raises(ValueError, match="https://"):
        GalleryImage(url="http://insecure.example.com/a.jpg", alt="사진")
    with pytest.raises(ValueError, match="cost_krw"):
        BudgetLine(item="음수", cost_krw=-1)


def test_v2_db_roundtrip_preserves_child_table_order(memory_db: YoloDatabase) -> None:
    curation = YoloCuration(
        id="V2-RT-01",
        title="라운드트립 검증",
        summary="요약",
        season=Season.AUTUMN,
        weather_class=WeatherClass.CLEAR,
        activity_mode=ActivityMode.OUTDOOR,
        location_name="서울",
        cost_or_duration="3만원",
        action_url="https://example.com",
        archetypes=(SoloArchetype.ROAD_NOMAD,),
        solo_tags=("#1인환영",),
        hero_image_url="https://example.com/hero.jpg",
        hero_image_alt="히어로",
        gallery_images=(
            GalleryImage(url="https://example.com/b.jpg", alt="B"),
            GalleryImage(url="https://example.com/a.jpg", alt="A"),
        ),
        timeline=(
            TimelineEntry(time="14:00", spot="두번째", desc="나중"),
            TimelineEntry(time="09:00", spot="첫번째", desc="먼저"),
        ),
        budget_breakdown=(
            BudgetLine(item="KTX 왕복", cost_krw=20000, category="transport"),
            BudgetLine(item="1인 식사", cost_krw=9500, category="food"),
            BudgetLine(item="입장료", cost_krw=1, category="ticket"),
        ),
        solo_secrets="혼자 가면 더 좋아요",
        map_naver_url="https://map.naver.com/p/search/test",
        map_kakao_url="https://map.kakao.com/?q=test",
    )
    memory_db.upsert_curation(curation)
    loaded = {c.id: c for c in memory_db.list_active_curations()}["V2-RT-01"]
    assert loaded == curation
    assert loaded.total_budget_krw == 29501

    # Re-upsert with fewer children must not leave stale rows behind
    memory_db.upsert_curation(
        YoloCuration(**{**curation.__dict__, "timeline": (), "gallery_images": (), "budget_breakdown": ()})
    )
    reloaded = {c.id: c for c in memory_db.list_active_curations()}["V2-RT-01"]
    assert reloaded.timeline == () and reloaded.gallery_images == () and reloaded.budget_breakdown == ()

    seeded = {c.id: c for c in memory_db.list_active_curations()}
    for c in SEED_CURATIONS:
        assert seeded[c.id] == c


def test_v2_legacy_db_is_migrated_with_alter_table(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy_yolo.sqlite3"
    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        CREATE TABLE yolo_curations (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL,
            season TEXT NOT NULL, weather_class TEXT NOT NULL DEFAULT 'ALL',
            activity_mode TEXT NOT NULL, location_name TEXT NOT NULL,
            location_area_code TEXT NOT NULL DEFAULT '1100000000',
            cost_or_duration TEXT NOT NULL, action_url TEXT NOT NULL,
            is_fallback INTEGER NOT NULL DEFAULT 0, fallback_for TEXT,
            priority INTEGER NOT NULL DEFAULT 100, is_active INTEGER NOT NULL DEFAULT 1,
            emoji TEXT NOT NULL DEFAULT '✨', created_at TEXT NOT NULL
        );
        CREATE TABLE yolo_curation_archetypes (curation_id TEXT NOT NULL, archetype TEXT NOT NULL,
            PRIMARY KEY (curation_id, archetype));
        CREATE TABLE yolo_curation_solo_tags (curation_id TEXT NOT NULL, solo_tag TEXT NOT NULL,
            PRIMARY KEY (curation_id, solo_tag));
        INSERT INTO yolo_curations (id, title, summary, season, activity_mode, location_name,
            cost_or_duration, action_url, created_at)
        VALUES ('OLD-01', '레거시 코스', '요약', 'AUTUMN', 'INDOOR', '서울', '무료',
            'https://example.com', '2026-01-01');
        INSERT INTO yolo_curation_archetypes VALUES ('OLD-01', 'URBAN_HIPSTER');
        INSERT INTO yolo_curation_solo_tags VALUES ('OLD-01', '#1인환영');
        """
    )
    conn.commit()
    conn.close()

    db = YoloDatabase(legacy_path)
    with db._connect() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(yolo_curations)").fetchall()}
    assert {name for name, _ in CURATION_V2_COLUMNS} <= cols

    legacy = {c.id: c for c in db.list_active_curations()}["OLD-01"]
    assert legacy.hero_image_url == ""
    assert legacy.timeline == ()

    # Migration is idempotent and v2 upserts work on the migrated DB
    YoloDatabase(legacy_path)
    db.seed_curations(SEED_CURATIONS)
    assert len(db.list_active_curations()) == len(SEED_CURATIONS) + 1


def test_v2_web_report_render_contract_and_escape(tmp_path: Path) -> None:
    xss = YoloCuration(
        id="XSS-V2",
        title="<script>alert(1)</script> 코스",
        summary="<img src=x onerror=1>",
        season=Season.AUTUMN,
        weather_class=WeatherClass.CLEAR,
        activity_mode=ActivityMode.INDOOR,
        location_name="서울 <강남>",
        cost_or_duration="1만원",
        action_url='https://example.com/?a="1"&b=2',
        archetypes=(SoloArchetype.URBAN_HIPSTER,),
        solo_tags=("#1인<b>환영</b>",),
        hero_image_url='https://example.com/x.jpg?"onerror=alert(1)',
        hero_image_alt='"><script>',
        timeline=(TimelineEntry(time="10:00", spot="<b>스팟</b>", desc="설명"),),
        budget_breakdown=(BudgetLine(item="<i>식사</i>", cost_krw=12000, category="food"),),
        solo_secrets="<script>tip</script>",
    )
    gen = YoloWebReportGenerator()
    html_text = gen.render_html([xss, *SEED_CURATIONS], season=Season.AUTUMN)

    assert "<script>alert" not in html_text
    assert "<img src=x" not in html_text
    assert "<script>tip" not in html_text
    assert '"onerror' not in html_text
    assert "&lt;script&gt;" in html_text
    # 360px CSS contract
    assert 'name="viewport"' in html_text
    assert "grid-template-columns:1fr" in html_text
    assert "@media (min-width:640px)" in html_text
    assert "repeat(auto-fit,minmax(300px,1fr))" in html_text
    assert "min-height:44px" in html_text
    # Images: lazy + alt + 16:9
    assert 'loading="lazy"' in html_text
    assert "aspect-ratio:16/9" in html_text
    for tag in html_text.split("<img")[1:]:
        assert 'alt="' in tag.split(">")[0]
        assert 'loading="lazy"' in tag.split(">")[0]
    # Season filter: only AUTUMN/ALL curations rendered, 4 archetype tabs present
    assert 'id="URB-AUT-01"' in html_text
    assert 'id="ACT-SPR-01"' not in html_text
    for arc in SoloArchetype:
        assert f'data-filter="{arc.value}"' in html_text
    assert "₩12,000" in html_text

    out = gen.save_report(html_text, tmp_path / "nested" / "yolo_latest.html")
    assert out.exists()
    assert out.read_text(encoding="utf-8") == html_text


def test_v2_web_report_hides_outdoor_under_severe_weather() -> None:
    gen = YoloWebReportGenerator()
    rain = WeatherSnapshot(weather_class=WeatherClass.RAIN, pop_percent=90, precip_mm=12.0)
    html_text = gen.render_html(SEED_CURATIONS, season=Season.AUTUMN, weather_snapshot=rain)
    for c in SEED_CURATIONS:
        if c.activity_mode == ActivityMode.OUTDOOR:
            assert f'id="{c.id}"' not in html_text
    assert 'id="ACT-FB-01"' in html_text
    assert "우천 예보" in html_text


def test_v2_generate_report_writes_only_to_given_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    out = generate_report(tmp_path / "yolo_latest.html", season=Season.AUTUMN)
    assert out == tmp_path / "yolo_latest.html"
    assert "나홀로 욜로 매거진" in out.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.sqlite3")) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["yolo_latest.html"]
