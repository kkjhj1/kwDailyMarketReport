"""Season-profile matched destination reference photos (curated 10 cities only).

[DATA-01] Honesty contract:
- Photos are *reference images* matched to the recommendation's season profile
  (`BEST_SEASON` / `NORMAL` / `CAUTION`). They are NOT live weather photos, so captions never say
  "현재 날씨" / "오늘 날씨" / "실시간 기상".
- Only the 10 curated cities have assets. Any other (fallback) city resolves to `None`; a photo of a
  different city is NEVER substituted, and callers must omit `<img>` / `sendPhoto` cleanly.

Asset lookup per city (first existing file wins), all under `travel_brief/assets/travel/`:
1. `<CODE>_m<MM>.jpg`        (departure-month specific, optional)
2. `<CODE>_<season>.jpg`     (`best_season` / `normal` / `caution`, optional)
3. `<CODE>.jpg`              (city representative image)
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional, Union

logger = logging.getLogger(__name__)

ASSET_DIR = Path(__file__).resolve().parent / "assets" / "travel"
ASSET_SUBDIR = "assets/travel"

CURATED_PHOTO_CITIES: dict[str, str] = {
    "NRT": "도쿄",
    "KIX": "오사카",
    "FUK": "후쿠오카",
    "DAD": "다낭",
    "CXR": "나트랑",
    "BKK": "방콕",
    "TPE": "타이베이",
    "GUM": "괌",
    "DPS": "발리",
    "SIN": "싱가포르",
}

SEASON_LABELS: dict[str, str] = {
    "BEST_SEASON": "여행 최적 시즌 참고 이미지",
    "CAUTION": "기상/우기 주의 시즌 참고 이미지",
    "NORMAL": "대표 여행지 참고 이미지",
}
SEASON_TAGS: dict[str, str] = {
    "BEST_SEASON": "🌤️ 최적 시즌",
    "CAUTION": "☔ 기상 주의 시즌",
    "NORMAL": "📷 대표 풍경",
}
_SEASON_FILE_SUFFIX = {"BEST_SEASON": "best_season", "CAUTION": "caution", "NORMAL": "normal"}

DateLike = Union[date, datetime, str, None]


@dataclass(frozen=True)
class DestinationPhoto:
    dest_code: str
    path: Path
    season_status: str
    caption: str
    season_tag: str

    @property
    def filename(self) -> str:
        return self.path.name

    def rel_src(self, asset_rel_prefix: str = ASSET_SUBDIR) -> str:
        """Relative URL for an HTML file, e.g. `assets/travel/NRT.jpg` or `../assets/travel/NRT.jpg`."""
        prefix = (asset_rel_prefix or ASSET_SUBDIR).replace("\\", "/").rstrip("/")
        return f"{prefix}/{self.filename}"


def _normalize_season(season_status: Optional[str]) -> str:
    s = (season_status or "NORMAL").strip().upper()
    return s if s in SEASON_LABELS else "NORMAL"


def _month_of(dep_date: DateLike) -> Optional[int]:
    if dep_date is None:
        return None
    if isinstance(dep_date, (date, datetime)):
        return dep_date.month
    try:
        return datetime.strptime(str(dep_date).strip()[:10], "%Y-%m-%d").month
    except ValueError:
        return None


def season_caption(dest_code: str, season_status: Optional[str] = "NORMAL", dest_name: str = "") -> str:
    code = (dest_code or "").strip().upper()
    name = (dest_name or "").strip() or CURATED_PHOTO_CITIES.get(code, code)
    return f"{name} · {SEASON_LABELS[_normalize_season(season_status)]}"


def resolve_destination_photo(
    dest_code: Optional[str],
    season_status: Optional[str] = "NORMAL",
    dep_date: DateLike = None,
    dest_name: str = "",
    *,
    asset_dir: Optional[Path] = None,
) -> Optional[DestinationPhoto]:
    """Returns the curated season-matched reference photo, or `None` (non-curated city / missing file)."""
    code = (dest_code or "").strip().upper()
    if code not in CURATED_PHOTO_CITIES:
        return None
    base = Path(asset_dir) if asset_dir is not None else ASSET_DIR
    season = _normalize_season(season_status)
    candidates: list[str] = []
    month = _month_of(dep_date)
    if month is not None:
        candidates.append(f"{code}_m{month:02d}.jpg")
    candidates.append(f"{code}_{_SEASON_FILE_SUFFIX[season]}.jpg")
    candidates.append(f"{code}.jpg")
    for name in candidates:
        path = base / name
        if path.is_file():
            return DestinationPhoto(
                dest_code=code,
                path=path,
                season_status=season,
                caption=season_caption(code, season, dest_name),
                season_tag=SEASON_TAGS[season],
            )
    return None


def copy_photo_assets(dest_dir: Union[str, Path], photos: Iterable[Optional[DestinationPhoto]]) -> list[Path]:
    """Copies the used photos into `dest_dir` (the report's own `assets/travel/` folder). Unchanged files
    (same size) are skipped. Best-effort: a failed copy is logged and skipped, never raised."""
    target = Path(dest_dir)
    copied: list[Path] = []
    seen: set[str] = set()
    for photo in photos:
        if photo is None or photo.filename in seen:
            continue
        seen.add(photo.filename)
        dst = target / photo.filename
        try:
            target.mkdir(parents=True, exist_ok=True)
            if dst.is_file() and dst.stat().st_size == photo.path.stat().st_size:
                copied.append(dst)
                continue
            shutil.copy2(photo.path, dst)
            copied.append(dst)
        except OSError as exc:
            logger.warning("photo asset copy failed (%s -> %s): %s", photo.path, dst, exc)
    return copied
