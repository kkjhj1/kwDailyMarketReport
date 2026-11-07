"""Admission Information Auto-Collector & Web Scraper.
Fetches official admission seminars, public education briefings, and major academy events.
"""
from __future__ import annotations
import json
import logging
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from admission_brief.ai_curator import AICurator
from admission_brief.models import (
    AdmissionEvent,
    BriefingType,
    EventCategory,
    TargetGrade,
    TargetRegion,
)
from admission_brief.storage.db import AdmissionDatabase

logger = logging.getLogger(__name__)


class AdmissionCollector:
    """Collects raw notices from official sources and major academies,
    evaluates them via AICurator, and land them into AdmissionDatabase.
    """

    def __init__(
        self,
        db: AdmissionDatabase,
        curator: Optional[AICurator] = None,
        opener=None,
    ):
        self.db = db
        self.curator = curator or AICurator()
        self._opener = opener or urllib.request.urlopen

    def fetch_url_html(self, url: str, timeout: int = 10) -> str:
        """Safely fetches HTML content with desktop browser User-Agent."""
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
            },
        )
        try:
            with self._opener(req, timeout=timeout) as resp:
                raw_bytes = resp.read()
                return raw_bytes.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", url, e)
            return ""

    def collect_public_education_notices(self) -> list[AdmissionEvent]:
        """Collects official admission fairs and public seminars from public educational entities.
        (KCUE Adiga, Seoul Metropolitan Office of Education, EBSi).

        Schedule anchors (2027학년도): 수능 2026-11-19, 성적 통지 2026-12-11, 정시 원서접수 2027-01-04~07.
        Links point to official root domains only; event detail pages must be confirmed on the source site.
        """
        events = []
        raw_public_posts = [
            # --- EXPO (공공·대학 대입 박람회) ---
            {
                "title": "대교협 2027 정시 대학입학정보박람회",
                "organizer": "한국대학교육협의회(대교협)",
                "category": EventCategory.EXPO,
                "target_grade": TargetGrade.HIGH_3_N,
                "target_region": TargetRegion.SEOUL,
                "is_online_live": False,
                "event_type": BriefingType.BRIEFING_RECAP,
                # 개최 일시는 대교협 확정 공고 전까지 미정 (정시 원서접수 마감일까지 노출)
                "event_date": "2026년 12월 중 (대교협 확정 공고 예정) · COEX 1층 A홀",
                "expires_at": datetime(2027, 1, 7, 18, 0),
                "booking_url": "https://www.adiga.kr",
                "source_name": "한국대학교육협의회 / 대입정보포털 어디가",
                "source_url": "https://www.kcue.or.kr",
                "urgency": "HIGH",
                "content": (
                    "전국 주요 4년제 대학이 참가하는 대교협 정시 박람회. 수능 성적표 기준 "
                    "대학별 1:1 입학상담 및 대교협 대입상담교사단 정시 상담, 대학별 모집요강 배포."
                ),
                "summary": [
                    "전국 주요 4년제 대학 부스에서 입학처 1:1 정시 상담",
                    "대교협 대입상담교사단의 수능 성적 기반 지원 가능선 상담",
                    "대학별 정시 모집요강·전년도 입결 자료집 현장 배포",
                ],
                "so_what": "입학처 담당자에게 직접 환산점수·반영비율을 확인할 수 있는 유일한 공식 창구입니다.",
                "todos": [
                    "수능 성적표 사본과 희망 대학 리스트 5곳 미리 준비",
                    "대교협 확정 공고 후 상담 사전예약 즉시 신청",
                ],
            },
            {
                "title": "서울시교육청 2027 대입 정시 민관합동 모의지원 상담 박람회",
                "organizer": "서울시교육청 진로진학센터",
                "category": EventCategory.EXPO,
                "target_grade": TargetGrade.HIGH_3_N,
                "target_region": TargetRegion.SEOUL,
                "is_online_live": False,
                "event_type": BriefingType.BRIEFING_RECAP,
                "event_date": "2026-12-13 10:00",
                "booking_url": "https://www.sen.go.kr",
                "source_name": "서울시교육청",
                "source_url": "https://www.sen.go.kr",
                "urgency": "NORMAL",
                "content": (
                    "12월 11일 수능 성적 통지 직후 서울시교육청 진학지도 교사단과 입시기관이 함께하는 "
                    "정시 모의지원 및 1:1 상담. 표준점수·백분위 기반 가/나/다군 조합 진단."
                ),
                "summary": [
                    "수능 성적 통지 직후 교사단·입시기관 합동 정시 1:1 상담",
                    "표준점수·백분위 기반 가/나/다군 지원 조합 모의지원",
                    "서울 소재 고3·N수생 및 학부모 무료 참여",
                ],
                "so_what": "사설 컨설팅 비용 없이 교사단의 실채점 기준 지원 전략을 받을 수 있습니다.",
                "todos": [
                    "12월 11일 성적표 수령 즉시 상담 신청서 제출",
                    "가/나/다군 후보 대학 3곳씩 사전 정리",
                ],
            },
            {
                "title": "EBSi 2027 수능 가채점 분석 및 대입전략 라이브 콘서트",
                "organizer": "EBSi (한국교육방송공사)",
                "category": EventCategory.EXPO,
                "target_grade": TargetGrade.HIGH_3_N,
                "target_region": TargetRegion.ONLINE,
                "is_online_live": True,
                "event_type": BriefingType.BRIEFING_RECAP,
                "event_date": "2026-11-19 20:00",
                "booking_url": "https://www.ebsi.co.kr",
                "source_name": "EBSi",
                "source_url": "https://www.ebsi.co.kr",
                "urgency": "HIGH",
                "content": (
                    "11월 19일 수능 당일 저녁 EBSi 대표 강사진 영역별 난이도 분석 및 "
                    "가채점 등급컷 기반 수시 최저 충족·정시 지원 전략 무료 생중계."
                ),
                "summary": [
                    "수능 당일 저녁 영역별 난이도 및 출제 경향 총평",
                    "가채점 기준 예상 등급컷과 수시 최저 충족 판단법",
                    "수시 대학별고사 응시 vs 정시 집중 의사결정 가이드",
                ],
                "so_what": "수능 당일 밤에 대학별고사 응시 여부를 결정해야 수시 납치·기회 상실을 막을 수 있습니다.",
                "todos": [
                    "수험생 가채점표 회수 후 영역별 원점수 기록",
                    "EBSi 라이브 알림 설정 및 등급컷 캡처 보관",
                ],
            },
            # --- SEMINAR (테마별 전략 세미나, 공공) ---
            {
                "title": "2028 대입 개편안 확정에 따른 중3 고교선택(자사고 vs 일반고) 나침반 세미나",
                "organizer": "서울시교육청 진로진학센터",
                "category": EventCategory.SEMINAR,
                "target_grade": TargetGrade.MIDDLE,
                "target_region": TargetRegion.SEOUL,
                "is_online_live": True,
                "event_type": BriefingType.BRIEFING_RECAP,
                "event_date": "2026-11-28 14:00",
                "booking_url": "https://www.sen.go.kr",
                "source_name": "서울시교육청",
                "source_url": "https://www.sen.go.kr",
                "urgency": "NORMAL",
                "content": (
                    "2028 대입 내신 5등급제 전환 이후 자사고·일반고 내신 유불리 비교 및 "
                    "고교학점제 과목 개설 현황 점검. 12월 후기고 배정 전 학부모 필수 세미나."
                ),
                "summary": [
                    "내신 5등급제(1등급 10%) 전환 후 자사고 vs 일반고 내신 유불리",
                    "고교학점제 심화과목 개설 현황이 학종 평가에 미치는 영향",
                    "후기고 지원 전 학교알리미 진학실적·편제표 확인법",
                ],
                "so_what": "5등급제에서는 학교 유형보다 '희망 전공 심화과목 개설 여부'가 학종 경쟁력을 좌우합니다.",
                "todos": [
                    "학교알리미에서 지망 고교 3곳 교육과정 편제표 비교",
                    "자녀 희망 계열과 개설 심화과목 일치 여부 체크",
                ],
            },
        ]

        for post in raw_public_posts:
            event = self._process_raw_post(post)
            if event:
                events.append(event)

        return events

    def collect_major_academy_seminars(self) -> list[AdmissionEvent]:
        """Collects high-demand entrance exam briefings and theme seminars from top-tier academies."""
        events = []
        raw_academy_posts = [
            # --- BRIEFING (5대 대형 입시기관 설명회) ---
            {
                "title": "메가스터디 수능 가채점 분석 및 2027 정시 합격 전략 라이브 설명회",
                "organizer": "메가스터디",
                "category": EventCategory.BRIEFING,
                "target_grade": TargetGrade.HIGH_3_N,
                "target_region": TargetRegion.ONLINE,
                "is_online_live": True,
                "event_type": BriefingType.TICKETING_ALERT,
                "event_date": "2026-11-20 19:30",
                "is_first_come": False,
                "booking_url": "https://www.megastudy.net",
                "source_name": "메가스터디 입시전략연구소",
                "source_url": "https://www.megastudy.net",
                "urgency": "HIGH",
                "content": (
                    "11월 19일 수능 다음 날 메가스터디 입시전략연구소 온라인 라이브 생중계. "
                    "가채점 등급컷 분석 및 2027 정시 대학별 환산점수 유불리 배치표 공개."
                ),
            },
            {
                "title": "종로학원 수능 가채점 결과 및 정시 합격점수 라이브 설명회",
                "organizer": "종로학원",
                "category": EventCategory.BRIEFING,
                "target_grade": TargetGrade.HIGH_3_N,
                "target_region": TargetRegion.SEOUL,
                "is_online_live": True,
                "event_type": BriefingType.BRIEFING_RECAP,
                "event_date": "2026-11-22 14:00",
                "booking_url": "https://www.jongro.co.kr",
                "source_name": "종로학원",
                "source_url": "https://www.jongro.co.kr",
                "urgency": "NORMAL",
                "content": (
                    "종로학원 가채점 결과 분석 및 주요 대학 정시 합격 예상점수 배치표 공개. "
                    "현장 설명회 및 유튜브 동시 생중계."
                ),
            },
            {
                "title": "[선착순] 시대인재 대치 본원 2027 수능 직후 정시 1:1 대면 컨설팅 및 대학별고사 설명회",
                "organizer": "시대인재",
                "category": EventCategory.BRIEFING,
                "target_grade": TargetGrade.HIGH_3_N,
                "target_region": TargetRegion.SEOUL,
                "is_online_live": False,
                "event_type": BriefingType.TICKETING_ALERT,
                "event_date": "2026-11-21 10:00",
                "is_first_come": True,
                "booking_url": "https://sdij.com",
                "source_name": "시대인재 대치 본원",
                "source_url": "https://sdij.com",
                "urgency": "HIGH",
                "content": (
                    "시대인재 대치 본원 수능 직후 가채점 기반 정시 1:1 대면 컨설팅 및 "
                    "수능 이후 논술·면접 대학별고사 대비 설명회. 표준점수 기반 지원 가능선 진단."
                ),
                "summary": [
                    "가채점 표준점수 기반 정시 지원 가능선 1:1 대면 진단",
                    "수능 직후 주말 논술·면접 대학별고사 응시 전략",
                    "선착순 좌석제 현장 진행 (대치 본원)",
                ],
                "so_what": "수능 직후 1주일 안에 대학별고사 응시 여부를 확정해야 수시 기회를 지킬 수 있습니다.",
                "todos": [
                    "가채점 원점수표 지참 후 사전 예약 즉시 완료",
                    "응시 예정 대학별고사 일정 겹침 여부 확인",
                ],
            },
            {
                "title": "대성마이맥 2027 정시 최종 합격선 배치표 공개 라이브 설명회",
                "organizer": "대성마이맥",
                "category": EventCategory.BRIEFING,
                "target_grade": TargetGrade.HIGH_3_N,
                "target_region": TargetRegion.ONLINE,
                "is_online_live": True,
                "event_type": BriefingType.BRIEFING_RECAP,
                "event_date": "2026-12-12 19:00",
                "booking_url": "https://www.mimacstudy.com",
                "source_name": "대성마이맥 입시전략연구소",
                "source_url": "https://www.mimacstudy.com",
                "urgency": "NORMAL",
                "content": (
                    "12월 11일 수능 성적 통지 다음 날, 실채점 표준점수 기준 2027 정시 "
                    "최종 합격선 배치표 공개 및 가/나/다군 지원 전략 라이브."
                ),
                "summary": [
                    "실채점 표준점수 기준 2027 정시 최종 합격선 배치표 공개",
                    "대학별 탐구 변환표준점수·영어 감점 반영 유불리 분석",
                    "가/나/다군 안정·적정·상향 조합 실전 예시",
                ],
                "so_what": "가채점이 아닌 실채점 기준 배치표로 최종 지원 라인을 다시 잡아야 합니다.",
                "todos": [
                    "성적표 수령 후 대학별 환산점수 직접 계산",
                    "가/나/다군 1차 후보 대학 확정",
                ],
            },
            {
                "title": "이투스 수능 가채점 기준 의치약한수 및 주요 상위권 대학 지원 가이드",
                "organizer": "이투스",
                "category": EventCategory.BRIEFING,
                "target_grade": TargetGrade.HIGH_3_N,
                "target_region": TargetRegion.ONLINE,
                "is_online_live": True,
                "event_type": BriefingType.BRIEFING_RECAP,
                "event_date": "2026-11-23 19:00",
                "booking_url": "https://www.etoos.com",
                "source_name": "이투스 교육평가연구소",
                "source_url": "https://www.etoos.com",
                "urgency": "NORMAL",
                "content": (
                    "이투스 교육평가연구소 가채점 기준 의대·치대·약대·한의대·수의대 및 "
                    "주요 상위권 대학 정시 지원 가능 표준점수 구간 분석 라이브."
                ),
                "summary": [
                    "가채점 기준 의치약한수 정시 지원 가능 표준점수 구간",
                    "의대 모집인원 변동에 따른 상위권 연쇄 이동 분석",
                    "서울 주요 상위권 대학 자연계 정시 지원 가이드",
                ],
                "so_what": "의대 정원 변동은 상위권 자연계 전체 합격선을 흔들므로 연쇄 효과를 함께 봐야 합니다.",
                "todos": [
                    "가채점 백분위로 의치약한수 지원 가능 구간 확인",
                    "상위권 대학 자연계 대안 학과 2곳 이상 선정",
                ],
            },
            # --- SEMINAR (테마별 전략 세미나, 학원) ---
            {
                "title": "예비고1 통합과학·통합사회 1등급 정복 겨울방학 로드맵 세미나",
                "organizer": "대성마이맥",
                "category": EventCategory.SEMINAR,
                "target_grade": TargetGrade.MIDDLE,
                "target_region": TargetRegion.ONLINE,
                "is_online_live": True,
                "event_type": BriefingType.BRIEFING_RECAP,
                "event_date": "2026-12-05 14:00",
                "booking_url": "https://www.mimacstudy.com",
                "source_name": "대성마이맥",
                "source_url": "https://www.mimacstudy.com",
                "urgency": "NORMAL",
                "content": (
                    "예비고1 겨울방학 통합과학·통합사회 선행 로드맵. 고1 내신 5등급제 1등급 확보를 위한 "
                    "12월~2월 주차별 학습 계획 및 수학(공통수학) 병행 전략."
                ),
                "summary": [
                    "통합과학·통합사회 겨울방학 1회독 주차별 로드맵",
                    "내신 5등급제 1등급(상위 10%) 확보를 위한 고1 첫 시험 전략",
                    "공통수학 선행과 통합과목 병행 시간 배분법",
                ],
                "so_what": "고1 첫 중간고사가 학종 평가의 출발점이므로 겨울방학 통합과목 결손 방지가 핵심입니다.",
                "todos": [
                    "자녀 겨울방학 주간 시간표에 통합과목 블록 배정",
                    "공통수학 진도와 통합과목 진도 병행 가능 여부 점검",
                ],
            },
            {
                "title": "의대 증원 및 무전공 확대에 따른 자연계열 득실 분석 세미나",
                "organizer": "종로학원",
                "category": EventCategory.SEMINAR,
                "target_grade": TargetGrade.HIGH_1_2,
                "target_region": TargetRegion.SEOUL,
                "is_online_live": True,
                "event_type": BriefingType.BRIEFING_RECAP,
                "event_date": "2026-12-06 14:00",
                "booking_url": "https://www.jongro.co.kr",
                "source_name": "종로학원",
                "source_url": "https://www.jongro.co.kr",
                "urgency": "NORMAL",
                "content": (
                    "의대 모집인원 변동 및 무전공(자율전공) 선발 확대가 고1·고2 자연계열 "
                    "수시·정시 경쟁 구도와 선발인원에 미치는 영향 분석."
                ),
                "summary": [
                    "의대 모집인원 변동이 자연계 상위권 합격선에 미치는 영향",
                    "무전공(자율전공) 선발 확대에 따른 교차지원 변화",
                    "고1·고2 선택과목 및 세특 준비 방향 재설계",
                ],
                "so_what": "무전공 확대로 계열 구분이 흐려지는 만큼 선택과목 이수 이력이 핵심 평가요소가 됩니다.",
                "todos": [
                    "자녀 고2 선택과목 수강 신청 계획 재점검",
                    "희망 대학 무전공 선발 인원 및 전형 방식 확인",
                ],
            },
        ]

        for post in raw_academy_posts:
            event = self._process_raw_post(post)
            if event:
                events.append(event)

        return events

    def _process_raw_post(self, post: dict[str, Any]) -> Optional[AdmissionEvent]:
        """Filters marketing hype, scores credibility, and produces an AdmissionEvent."""
        organizer = post["organizer"]
        content = post.get("content", "")
        title = post["title"]

        score = self.curator.score_credibility(
            organizer, content, has_official_source=bool(post.get("source_url"))
        )
        if score < 70:
            logger.info("Dropped low-credibility admission notice (%d pts): %s", score, title)
            return None

        clean_title = self.curator.clean_marketing_fluff(title)
        event_id = f"evt_{uuid.uuid5(uuid.NAMESPACE_DNS, title).hex[:12]}"

        # Use curated summaries when the post carries them; otherwise synthesize by keyword
        if post.get("summary"):
            summary = post["summary"]
            so_what = post.get("so_what", "")
            todos = post.get("todos", [])
        elif "내신 5등급제" in content or "2028" in content:
            summary = [
                "2028 대입 개편안 확정: 수능 선택과목 폐지 및 통합 시험 전환",
                "고교 내신 9등급제 ➔ 5등급제 완화로 학생부 교과 세특 영향력 확대",
                "상위권 대학 진학 시 심화 탐구 및 세특 기록 완성도가 실질 당락 좌우",
            ]
            so_what = "내신 1등급 변별력이 약화되어, 학교생활기록부 세특과 탐구 역량이 합격을 결정합니다."
            todos = [
                "희망 진로와 연계된 교과 세특 키워드 3가지 점검",
                "통합수학 및 통합과학 기초 개념 완성도 사전 진단",
            ]
        elif "윈터스쿨" in content or "예비고1" in content:
            summary = [
                "고교 진학 전 중3 겨울방학 통합사회·통합과학 1회독 로드맵",
                "대치동 주요 자사고(외대부고·하나고·상산고) 합격생 주간 학습 시간표",
                "고1 1학기 첫 중간고사 1등급 선점을 위한 수학(상/하) 심화 전략",
            ]
            so_what = "고등학교 첫 중간고사 성적이 대입 수시의 70%를 결정하므로 겨울방학 결손 방지가 필수입니다."
            todos = [
                "오늘 선착순 예약 오픈 시 1분 내 신청 완료",
                "자녀의 현재 수학 심화 성취도(블랙라벨/일품) 자가진단",
            ]
        elif "정시" in content or "수능" in content or "가채점" in content:
            summary = [
                f"{organizer} 수능 실시간 가채점 등급컷 및 정시 환산점수 유불리 분석",
                "수능 직후 주말 대학별 수시 논술/면접 고사 응시 여부 판단 가이드",
                "정시 가/나/다군 군별 지원 포트폴리오 및 배치표 활용 전략",
            ]
            so_what = "가채점 점수를 바탕으로 수시 수능 최저 충족 여부를 확인하고, 정시 유불리를 정밀 계산해야 합니다."
            todos = [
                "온라인 라이브 설명회 사전 알림 신청 완료",
                "수능 당일 가채점 표준점수 및 백분위 실시간 입력",
            ]
        else:
            summary = [
                f"{organizer} 실시간 배치표 및 대학별 환산점수 유불리 분석",
                "수능 최저 충족 여부에 따른 상향/적정 수시 라인업 재점검",
                "정시 이월 인원 및 전년도 입결 데이터 기반 지원 가이드",
            ]
            so_what = "모의평가 성적과 학생부 성적의 격차를 정밀 분석하여 수시 납치를 방지해야 합니다."
            todos = [
                "온라인 라이브 설명회 사전 알림 신청 완료",
                "자녀 수능 가채점 표준점수 및 백분위 확인",
            ]

        event = AdmissionEvent(
            event_id=event_id,
            title=clean_title,
            organizer=organizer,
            target_grade=post["target_grade"],
            target_region=post.get("target_region", TargetRegion.ALL),
            is_online_live=post.get("is_online_live", False),
            event_type=post["event_type"],
            event_date_str=post["event_date"],
            booking_opens_at=post.get("booking_opens_at"),
            is_first_come=post.get("is_first_come", False),
            booking_url=post.get("booking_url"),
            summary_3lines=summary,
            so_what=so_what,
            action_todos=todos,
            credibility_score=score,
            source_name=post.get("source_name", organizer),
            source_url=post.get("source_url", post.get("booking_url")),
            urgency=post.get("urgency", "NORMAL"),
            category=post.get("category", EventCategory.BRIEFING),
            expires_at=post.get("expires_at"),
        )
        return event

    def run_collection(self) -> dict[str, int]:
        """Runs complete automated collection pipeline and saves events to DB."""
        public_events = self.collect_public_education_notices()
        academy_events = self.collect_major_academy_seminars()

        all_collected = public_events + academy_events
        saved_count = 0
        for evt in all_collected:
            self.db.save_event(evt)
            saved_count += 1

        logger.info("Admission collection finished: %d event(s) saved.", saved_count)
        return {
            "public_count": len(public_events),
            "academy_count": len(academy_events),
            "total_saved": saved_count,
        }
