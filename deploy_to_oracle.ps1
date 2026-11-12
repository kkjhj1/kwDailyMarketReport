param(
    [string]$KeyPath = "$PSScriptRoot\참고파일\오라클서버\ssh-key-2026-11-06.key",
    [string]$ServerIp = "161.33.196.211",
    [string]$User = "ubuntu"
)

$ErrorActionPreference = "Stop"
# PowerShell 7.3+: native 명령의 non-zero exit 를 예외로 바꾸지 않음 (명시적 $LASTEXITCODE 검사만으로 판정)
$PSNativeCommandUseErrorActionPreference = $false

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "🚀 [Oracle Cloud OCI] 배포 파이프라인 동기화 시작" -ForegroundColor Cyan
Write-Host "   Target: $User@$ServerIp (Port: 8002)" -ForegroundColor Gray
Write-Host "============================================================" -ForegroundColor Cyan

function Fail-Deploy {
    param([string]$Message)
    Write-Host "❌ $Message" -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------
# [0/3] 로컬 Pre-flight: SSH 키 / 도구 / known_hosts 호스트키 고정 검증
# ------------------------------------------------------------
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    $found = Get-ChildItem -Path "$PSScriptRoot" -Filter "ssh-key-2026-11-06.key" -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        $KeyPath = $found.FullName
    } else {
        Fail-Deploy "SSH 키 파일을 찾을 수 없습니다: $KeyPath"
    }
}

$resolvedKey = (Resolve-Path -LiteralPath $KeyPath).Path

foreach ($tool in @("ssh", "ssh-keygen")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Fail-Deploy "필수 도구를 찾을 수 없습니다: $tool (OpenSSH 클라이언트 설치 필요)"
    }
}

& ssh-keygen -F $ServerIp *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ known_hosts 에 $ServerIp 호스트키가 고정(pinning)되어 있지 않습니다." -ForegroundColor Red
    Write-Host "   OCI 콘솔에서 서버 호스트키 지문을 확인한 뒤 수동으로 known_hosts 에 등록하세요." -ForegroundColor Gray
    Write-Host "   (예: ssh-keyscan -t ed25519 $ServerIp | ssh-keygen -lf -  로 지문 대조 후 추가)" -ForegroundColor Gray
    Write-Host "   StrictHostKeyChecking=no 는 사용하지 않습니다." -ForegroundColor Gray
    exit 1
}

$sshArgs = @(
    "-i", $resolvedKey,
    "-o", "StrictHostKeyChecking=yes",
    "-o", "BatchMode=yes",
    "-o", "IdentitiesOnly=yes",
    "-o", "ConnectTimeout=15"
)

function Invoke-RemoteBash {
    param(
        [Parameter(Mandatory = $true)][string]$Script,
        [Parameter(Mandatory = $true)][string]$FailMessage
    )
    # CRLF -> LF, 잔여 CR 제거 (원격 bash 에 \r 이 섞이지 않도록)
    $normalized = ($Script -replace "`r`n", "`n") -replace "`r", ""
    $normalized = "`n" + $normalized + "`n"
    # stdin 으로 BOM 이 송출되지 않도록 BOM 없는 UTF-8 지정 (함수 스코프 한정)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $normalized | & ssh @sshArgs "$User@$ServerIp" "tr -d '\r' | bash -s" | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Fail-Deploy "$FailMessage (exit=$LASTEXITCODE)"
    }
}

# ------------------------------------------------------------
# 원격 bash 스크립트 (single-quoted here-string: PowerShell 확장 없음, ASCII 전용)
# ------------------------------------------------------------
$remoteSyncScript = @'
{
set -Eeuo pipefail
APP_DIR=/home/ubuntu/kwDailyMarketReport
cd "$APP_DIR"

echo "[preflight] checking passwordless sudo"
sudo -n true

echo "[preflight] auditing working tree (outside reports/)"
git update-index -q --refresh >/dev/null 2>&1 || true
DIRTY_BYTES=$(git diff --name-only -z HEAD -- ':!reports/' | wc -c)
if [ "$DIRTY_BYTES" -ne 0 ]; then
  echo "ERROR: tracked changes outside reports/ detected; refusing to reset:" >&2
  git diff --name-only HEAD -- ':!reports/' >&2
  exit 1
fi
# Untracked .py at ANY depth. List to a temp file first so grep -q cannot
# SIGPIPE git under pipefail (which would mask a match as "no match").
UNTRACKED_LIST=$(mktemp)
git ls-files -z --others --exclude-standard > "$UNTRACKED_LIST"
if grep -zq '\.py$' "$UNTRACKED_LIST"; then
  echo "ERROR: unexpected untracked Python sources detected; refusing to reset:" >&2
  grep -z '\.py$' "$UNTRACKED_LIST" | tr '\0' '\n' >&2 || true
  rm -f "$UNTRACKED_LIST"
  exit 1
fi
rm -f "$UNTRACKED_LIST"

echo "[preflight] git fetch origin main"
git fetch origin main

echo "[preflight] waiting for running cron/bot runner processes (max 20s)"
RUN_USER=${USER:-$(id -un)}
WAITED=0
while pgrep -u "$RUN_USER" -f 'run_cron\.sh|_bot_runner\.py' >/dev/null 2>&1; do
  if [ "$WAITED" -ge 20 ]; then
    echo "ERROR: run_cron.sh / *_bot_runner.py still running after 20s; aborting before stopping multibot" >&2
    pgrep -a -u "$RUN_USER" -f 'run_cron\.sh|_bot_runner\.py' >&2 || true
    exit 1
  fi
  sleep 1
  WAITED=$((WAITED + 1))
done

echo "[quiesce] stopping multibot"
sudo -n systemctl stop multibot

mkdir -p backups
BAK=$(mktemp -d "$APP_DIR/backups/deploy_reports_XXXXXX")
die() { echo "ERROR: $* (BACKUP PRESERVED AT: $BAK; multibot is STOPPED)" >&2; exit 1; }
trap 'echo "BACKUP PRESERVED AT: $BAK (multibot is STOPPED)" >&2' ERR

git diff --name-only --diff-filter=AMR -z HEAD -- reports/ > "$BAK/.paths_dirty" || die "git diff (dirty reports) failed"
git diff --name-only --diff-filter=AMR -z HEAD origin/main -- reports/ > "$BAK/.paths_incoming" || die "git diff (incoming reports) failed"
printf '%s\0' reports/latest.html > "$BAK/.paths_explicit"

declare -A SEEN=()
BACKUP_FILES=()
for LIST in "$BAK/.paths_dirty" "$BAK/.paths_incoming" "$BAK/.paths_explicit"; do
  while IFS= read -r -d '' f; do
    [ -n "$f" ] || continue
    [ -z "${SEEN[$f]+x}" ] || continue
    SEEN[$f]=1
    if [ -f "$f" ] && [ ! -L "$f" ]; then
      BACKUP_FILES+=("$f")
    fi
  done < "$LIST"
done

: > "$BAK/MANIFEST.sha256"
for f in ${BACKUP_FILES[@]+"${BACKUP_FILES[@]}"}; do
  cp -a --parents "$f" "$BAK/" || die "backup copy failed: $f"
  sha256sum "$f" >> "$BAK/MANIFEST.sha256" || die "sha256sum failed: $f"
done

if [ -f reports/latest.html ]; then
  LATEST_SHA=$(sha256sum reports/latest.html | cut -d' ' -f1)
else
  LATEST_SHA=none
fi
echo "[backup] dir=$BAK latest_sha256=$LATEST_SHA files=${#BACKUP_FILES[@]}"

echo "[sync] git reset --hard origin/main"
git reset --hard origin/main || die "git reset --hard origin/main failed"

mkdir -p reports
if [ -d "$BAK/reports" ]; then
  cp -a "$BAK/reports/." reports/ || die "restore of reports/ failed"
fi
if [ "${#BACKUP_FILES[@]}" -gt 0 ]; then
  [ -s "$BAK/MANIFEST.sha256" ] || die "backup manifest is empty although files were backed up"
  sha256sum -c --quiet "$BAK/MANIFEST.sha256" || die "restored reports/ checksum mismatch"
fi
echo "[restore] reports/ restored and verified"

chmod +x run_cron.sh || die "chmod +x run_cron.sh failed"

grep -q '^REPORT_BASE_URL=' .env || die "REPORT_BASE_URL not found in .env"
sed -i 's|^REPORT_BASE_URL=.*|REPORT_BASE_URL=http://161.33.196.211:8002|' .env || die "sed on .env failed"

echo "[compile] full repository py_compile"
find . \( -path './venv' -o -path './.git' -o -path './backups' -o -path './scratch' -o -path '*/__pycache__' -o -path './.pytest_cache' \) -prune -o -type f -name '*.py' -print0 \
  | sort -z > "$BAK/.pyfiles" || die "collecting python files failed"
mapfile -d '' PY_FILES < "$BAK/.pyfiles"
[ "${#PY_FILES[@]}" -gt 0 ] || die "no python files collected"
./venv/bin/python -m py_compile "${PY_FILES[@]}" || die "py_compile failed"
echo "[compile] OK files=${#PY_FILES[@]}"

POST_DIRTY=$(git diff --name-only -z HEAD -- ':!reports/' | wc -c)
[ "$POST_DIRTY" -eq 0 ] || die "working tree outside reports/ is dirty after reset"

trap - ERR
rm -rf "$BAK"
echo "[step1] OK head=$(git rev-parse --short HEAD)"
} </dev/null
'@

$remoteRestartScript = @'
{
set -euo pipefail
sudo -n systemctl restart multibot
sudo -n systemctl is-active --quiet multibot
echo "[restart] multibot is active"
} </dev/null
'@

$remoteHealthScript = @'
{
set -euo pipefail
cd /home/ubuntu/kwDailyMarketReport
CHECK_PY='
import json, sys
d = json.load(sys.stdin)
s = d.get("briefing_scheduler") or {}
ok = (
    d.get("status") == "HEALTHY"
    and d.get("bot_workers_enabled") is True
    and not d.get("failed_workers")
    and len(d.get("active_workers") or []) > 0
    and s.get("is_running") is True
    and not s.get("has_crashed")
)
sys.exit(0 if ok else 1)
'
BODY=""
HEALTHY=0
for i in $(seq 1 20); do
  if BODY=$(curl -fsS --max-time 3 http://127.0.0.1:8002/healthz 2>/dev/null) \
     && printf '%s' "$BODY" | ./venv/bin/python -c "$CHECK_PY"; then
    HEALTHY=1
    break
  fi
  sleep 1
done
if [ "$HEALTHY" -ne 1 ]; then
  echo "ERROR: /healthz did not become HEALTHY within 20s" >&2
  echo "last body: ${BODY:-<none>}" >&2
  exit 1
fi
printf '%s' "$BODY" | ./venv/bin/python -c 'import json,sys; print(json.dumps(json.load(sys.stdin), indent=2, ensure_ascii=True))'
if [ -f reports/latest.html ]; then
  sha256sum reports/latest.html
else
  echo "reports/latest.html: not present"
fi
sudo -n systemctl is-active --quiet multibot
echo "[health] OK"
} </dev/null
'@

# ------------------------------------------------------------
# [0/3] 로컬 Pre-flight: 원격 bash 스크립트 문법 검증 (bash -n, SSH 연결 전 Fail-Closed)
# ------------------------------------------------------------
$bashExe = $null
$gitBash = Join-Path $env:ProgramFiles "Git\bin\bash.exe"
if (Test-Path -LiteralPath $gitBash -PathType Leaf) {
    $bashExe = $gitBash
} else {
    $bashCmd = Get-Command bash -ErrorAction SilentlyContinue
    if ($bashCmd) { $bashExe = $bashCmd.Source }
}
if (-not $bashExe) {
    Fail-Deploy "bash 를 찾을 수 없어 원격 스크립트 문법 검증(bash -n)을 수행할 수 없습니다 (Git for Windows 설치 필요)"
}

$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
foreach ($entry in @(
        @{ Name = "remoteSyncScript"; Body = $remoteSyncScript },
        @{ Name = "remoteRestartScript"; Body = $remoteRestartScript },
        @{ Name = "remoteHealthScript"; Body = $remoteHealthScript }
    )) {
    $lfBody = ($entry.Body -replace "`r`n", "`n") -replace "`r", ""
    $lfBody | & $bashExe -n | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Fail-Deploy "원격 bash 스크립트 문법 오류: $($entry.Name) (bash -n exit=$LASTEXITCODE)"
    }
}
Write-Host "✅ [0/3] 로컬 Pre-flight 통과 (SSH 키 / known_hosts / bash -n)" -ForegroundColor Green

Write-Host "📡 [1/3] 오라클 서버에 연결하여 사전점검·리포트 백업·최신 소스코드 동기화 및 전체 컴파일 검증..." -ForegroundColor Yellow
Invoke-RemoteBash -Script $remoteSyncScript -FailMessage "오라클 서버 소스코드 동기화 실패 (multibot 상태 및 백업 경로를 확인하세요)"

Write-Host "⚙️ [2/3] systemd 멀티봇 서비스(multibot) 재기동..." -ForegroundColor Yellow
Invoke-RemoteBash -Script $remoteRestartScript -FailMessage "multibot 서비스 재기동 실패"

Write-Host "🩺 [3/3] 오라클 서버 헬스체크 및 상태 검증..." -ForegroundColor Yellow
Invoke-RemoteBash -Script $remoteHealthScript -FailMessage "오라클 서버 헬스체크 실패"

Write-Host "============================================================" -ForegroundColor Green
Write-Host "🎉 [배포 완료] 오라클 클라우드 서버에 최신 코드가 성공적으로 반영되었습니다!" -ForegroundColor Green
Write-Host "   접속 URL: http://${ServerIp}:8002" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
