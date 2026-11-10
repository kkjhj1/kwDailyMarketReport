# PROJECT CONSTITUTION: Multi-Bot Intelligence Platform (v3.0 Slim)

## 1. 개요 & 운영 인프라
- 프로젝트: `kwDailyMarketReport` / `market_brief_starter` (Multi-Bot Platform v2.0)
- 운영 서버: OCI VM `161.33.196.211`, `systemd` `multibot.service`, 포트 `8002`, 24/7
- 웹 리포트: 오라클 클라우드 VM 자체 서빙 (`http://161.33.196.211:8002/reports/`)
- Render Cloud: 퇴역 완료, 사용 금지
- 봇: `telegram_bot_runner.py`(시황, 06:30 KST·±3% 긴급), `travel_bot_runner.py`, `admission_bot_runner.py`, `portal_bot_runner.py`(FREE/GREEN/RED/BLACK), 관리자 콘솔 `admin_server.py`

## 2. 포트 가드 (전역 [DEV-PORT])
- 기본 `ADMIN_PORT=8002`. 충돌 시 `find_available_port(8002)`로 8003+ 전환하고 `[PORT FALLBACK]` + 실제 `http://127.0.0.1:<PORT>` 출력
- 격리: `pension_platform` 8000 / `market_brief_starter` 8002 / 신규 8003+

## 3. 시행령 위임 (`.agents/rules/`)
- `telegram-safety.md`: [TG-01]~[TG-07] 메시지 용량·escape·콜백·429·`--demo`·제로 브레이킹
- `data-delivery-integrity.md`: [PROD-LIVE] 실발송 사전 승인, 15분 정시 윈도우, 재기동 보상 발송 금지, Dual-DB SSOT, 긴급변동 감시 일원화
- `web-report-ui.md`: 360px 무오버플로우, 44px 터치 타깃
- 해당 영역 작업 시 반드시 전문을 읽고 준수한다.

## 4. 절대 금지선
- 운영 DB(`*.sqlite3`), 배포 폴더(`reports/`), `.env` 임의 수정 금지
- 실사용자 텔레그램 실발송 금지(Deny-by-Default). 개발·테스트는 `--demo`/Dry-run만 사용하며, 발송·재발송은 반드시 사용자 사전 승인 후 진행
- 서버 재기동/배포 시 정시 15분 윈도우 외 보상성 자동 재발송(Compensatory Redelivery) 절대 금지
- 코드 수정 후 `python -m py_compile` + 관련 테스트 실측 통과 전 완료 보고 금지

## 5. 멀티 AI 2단계 파이프라인
- 라우팅: L0(문구·주석) Gemini 단독 / L1(단일 파일 국소 수정) Gemini→Claude 직행 / L2·L3(로직·템플릿·UI·DB·대규모 리팩토링) 아래 2단계
- Phase 1 (비파괴): Gemini 요구정리 → `scratch/handoff_to_gpt.md` → GPT 감사(read-only, 승인 대기 없이 자동) → `scratch/gpt_architecture.md` → Gemini 확정 계획서 보고. 감사 산출물 없는 계획 제안·승인 요청 금지(`AUDIT_INCOMPLETE`)
- Phase 2 (파괴, [AUTH-01]): 사용자의 명확한 착수 승인("개발 진행해" 등, 단순 맞장구 제외) 후 Claude가 Single-Writer Lease로 구현. Gemini 파일 수정 금지, Claude 실패 시 대체 금지(`IMPLEMENTATION_FAILED`/`BLOCKED` 보고)
- 최종 판결: 착수 전 `git rev-parse HEAD`·`git status --short`로 기준점 고정, 반환 후 Gemini가 `git diff` Fresh Read로 시행령·불변조건 준수 판정. L3 배포·운영 DB 마이그레이션은 감사 누락/테스트 실패 시 중단(Fail-Closed)
- Handoff: 전체 소스 복사 금지, 대상 인터페이스만 1~2KB 압축. Claude는 `allowed_paths`만 수정, 완료 시 Drift Self-Report 제출

```powershell
# GPT 감사 (read-only)
Get-Content -Raw "scratch\handoff_to_gpt.md" -Encoding UTF8 | codex exec --skip-git-repo-check -s read-only -o "scratch\gpt_architecture.md" -
# Claude 구현 (Lease)
Get-Content -Raw "scratch\handoff_to_claude.md" -Encoding UTF8 | claude -p --tools "Read,Grep,Glob,Edit,Write,Bash" --permission-prompts none --no-session-persistence
```
