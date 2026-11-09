---
description: "웹 리포트 HTML 대시보드 360px 모바일 뷰포트 무오버플로우 UI 규격"
trigger: "glob"
globs: "**/reports/**, **/*.html, **/static/**"
---

# 웹 리포트 UI 규격 (텔레그램 인앱 웹 연동)

> 적용 대상: GitHub Pages 정적 리포트(`https://kkjhj1.github.io/kwDailyMarketReport/`), 텔레그램에서 연결되는 HTML 대시보드.
> `reports/`는 배포 폴더이므로 데모·테스트 실행 중 기록 금지(`telegram-safety.md` [TG-06]).

## 1. 360px 뷰포트 무오버플로우
- `360px` 뷰포트에서 가로 오버플로우 **제로**를 보장한다.
- 그리드 열은 `minmax(0, 1fr)`을 사용해 긴 텍스트·숫자가 열을 밀어내지 않도록 한다.

## 2. 터치 타깃
- 버튼·링크 등 터치 타깃은 **44px 이상**을 확보한다.

## 3. 모바일 3초 스캔
- 핵심 수치는 상단에 `이모지 + 핵심명사 + 수치(Bold)` 형태로 배치해 3초 내 파악이 가능하게 한다.
- 장황한 괄호 설명과 원본 URL 노출을 피한다.
