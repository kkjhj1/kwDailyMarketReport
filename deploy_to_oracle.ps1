param(
    [string]$KeyPath = "$PSScriptRoot\참고파일\오라클서버\ssh-key-2026-11-06.key",
    [string]$ServerIp = "161.33.196.211",
    [string]$User = "ubuntu"
)

$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "🚀 [Oracle Cloud OCI] 배포 파이프라인 동기화 시작" -ForegroundColor Cyan
Write-Host "   Target: $User@$ServerIp (Port: 8002)" -ForegroundColor Gray
Write-Host "============================================================" -ForegroundColor Cyan

if (-not (Test-Path $KeyPath)) {
    $found = Get-ChildItem -Path "$PSScriptRoot" -Filter "ssh-key-2026-11-06.key" -Recurse | Select-Object -First 1
    if ($found) {
        $KeyPath = $found.FullName
    } else {
        Write-Error "SSH 키 파일을 찾을 수 없습니다: $KeyPath"
        exit 1
    }
}

$resolvedKey = (Resolve-Path $KeyPath).Path

Write-Host "📡 [1/3] 오라클 서버에 연결하여 최신 소스코드 동기화 (git pull)..." -ForegroundColor Yellow

$remoteSyncScript = "cd /home/ubuntu/kwDailyMarketReport && git fetch origin main && git reset --hard origin/main && ./venv/bin/python -m py_compile admin_server.py telegram_bot_runner.py market_brief/web_report.py market_brief/briefing_scheduler.py"

ssh -i "$resolvedKey" -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$User@$ServerIp" "$remoteSyncScript"

if ($LASTEXITCODE -ne 0) {
    Write-Error "오라클 서버 소스코드 동기화 실패"
    exit 1
}

Write-Host "⚙️ [2/3] systemd 멀티봇 서비스(multibot) 재기동..." -ForegroundColor Yellow

$remoteRestartScript = "sudo systemctl restart multibot && sleep 3 && sudo systemctl is-active multibot"

ssh -i "$resolvedKey" -o StrictHostKeyChecking=no "$User@$ServerIp" "$remoteRestartScript"

Write-Host "🩺 [3/3] 오라클 서버 헬스체크 및 상태 검증..." -ForegroundColor Yellow

$remoteHealthScript = "curl -s http://127.0.0.1:8002/healthz && echo ''"

ssh -i "$resolvedKey" -o StrictHostKeyChecking=no "$User@$ServerIp" "$remoteHealthScript"

Write-Host "============================================================" -ForegroundColor Green
Write-Host "🎉 [배포 완료] 오라클 클라우드 서버에 최신 코드가 성공적으로 반영되었습니다!" -ForegroundColor Green
Write-Host "   접속 URL: http://${ServerIp}:8002" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
