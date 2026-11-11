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
- Phase 2 (파괴, [AUTH-01]): 사용자의 명확한 착수 승인("개발 진행해" 등, 단순 맞장구 제외) 후 Claude가 Single-Writer Lease로 구현. Gemini 파일 수정 금지, 대화형 작업 중 Claude 실패 시 임의 대체 금지(`IMPLEMENTATION_FAILED`/`BLOCKED` 보고; 무인 파이프라인 예외는 제6장 적용)
- 최종 판결: 착수 전 `git rev-parse HEAD`·`git status --short`로 기준점 고정, 반환 후 Gemini가 `git diff` Fresh Read로 시행령·불변조건 준수 판정. L3 배포·운영 DB 마이그레이션은 감사 누락/테스트 실패 시 중단(Fail-Closed)
- Handoff: 전체 소스 복사 금지, 대상 인터페이스만 1~2KB 압축. Writer는 `allowed_paths`만 수정, 완료 시 Drift Self-Report 제출

## 6. 무인 자율 파이프라인 (`auto_factory`) 조건부 예외 규약
- **적용 범위**: `git worktree`로 격리된 로컬 샌드박스 내 무인 파이프라인 작업에 한해 적용한다 (`main` 직접 수정 금지).
- **L0~L2 대타 코딩(Failover Writer) 허용**:
  - 1순위 `Claude`가 할당량 소진·Rate Limit·Cooldown 상태일 때, **L0~L2 작업에 한해** `GPT(codex exec -s workspace-write -c windows.sandbox=unelevated)` → `Gemini(agy -p <prompt> --mode accept-edits)` 순의 단일 Writer 대타 코딩을 허용한다. (구 `gemini -p --approval-mode auto_edit`는 무료 OAuth `UNSUPPORTED_CLIENT`로 퇴역; worktree 격리 + sanitized env 경계는 `win_job`/`worktree`가 강제한다.)
  - 전환 전 반드시 WIP 체크포인트 커밋 및 1~2KB 표준 JSON Handoff Contract를 생성·인계한다.
  - **L3 (운영 DB 마이그레이션·배포·실발송)**: 대타 코딩 및 감사 생략 절대 금지(`BLOCKED`, Fail-Closed).
- **교차 검증 & 단일 생존자 경계**:
  - `Coder ≠ Reviewer` 불변조건: 구현한 Provider와 다른 Provider가 최종 `git diff` 리뷰를 수행한다.
  - 가용 AI가 1개만 남은 경우: L0은 `Gemini` 단독 허용; L1/L2는 이미 유효한 감사·계획 완료 후 `Claude`만 남았을 때 `SINGLE_PROVIDER_REVIEW_REQUIRED`(자동 병합 금지)로 진행하며, `Codex`/`Gemini` 단독 생존 시 신규 L1/L2 착수 금지(`BLOCKED`).
- **필수 안전 게이트**:
  - `STOP.flag` 비상 킬 스위치, Windows Job Object(`KILL_ON_JOB_CLOSE`), rolling quota 70% 상한(30% 사람용 유보), 불변 `holdout_eval/` 분리, AST Anti-Cheating(테스트 삭제/`skip`/assert 축소 차단), `TELEGRAM_BOT_TOKEN=""` 격리 준수.
  - 최종 `main` 병합은 `task_id + target_sha + diff_sha256`가 결박된 사용자 승인 후에만 실행한다.

```powershell
# GPT 감사 (read-only)
Get-Content -Raw "scratch\handoff_to_gpt.md" -Encoding UTF8 | codex exec --skip-git-repo-check -s read-only -c windows.sandbox=unelevated -o "scratch\gpt_architecture.md" -
# Claude 구현 (Lease)
Get-Content -Raw "scratch\handoff_to_claude.md" -Encoding UTF8 | claude -p --tools "Read,Grep,Glob,Edit,Write,Bash" --permission-prompts none --no-session-persistence
```
