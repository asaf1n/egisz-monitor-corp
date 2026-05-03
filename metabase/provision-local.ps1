#!/usr/bin/env powershell
# Локальный прототип: пересобрать образ Metabase (JSON из репозитория) и заново провижинить дашборды
# в уже запущенный Metabase на localhost (например port-forward из K8s или отдельный контейнер).
#
# Требования: Docker. Metabase должен отвечать по HTTP, в нём настроена БД витрины (egisz_reports) и выполнен sync схемы.
#
# Примеры:
#   .\metabase\provision-local.ps1
#   .\metabase\provision-local.ps1 -MetabaseUrl "http://127.0.0.1:3000" -AdminEmail "admin@egisz.local" -AdminPassword (Read-Host -AsSecureString)
#   # PS 7+: пароль из строки только для локальной отладки — ConvertTo-SecureString "egisz" -AsPlainText -Force
# Учётка по умолчанию (и в k8s Secret metabase-admin / start.ps1): admin@egisz.local / egisz

param(
    [string]$MetabaseUrl = "http://127.0.0.1:3000",
    # Пустые строки — намеренно: ниже подставляются METABASE_ADMIN_* / ADMIN_* из окружения, иначе как в k8s (стр. 10).
    [string]$AdminEmail = "",
    [SecureString]$AdminPassword = $null
)

$ErrorActionPreference = "Stop"
# Скрипт лежит в metabase/ → корень репозитория на уровень выше
$RepoRoot = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $RepoRoot "metabase_dashboards"))) {
    Write-Host "ERROR: Could not find metabase_dashboards/ under $RepoRoot" -ForegroundColor Red
    exit 1
}

$email = $AdminEmail
if (-not $email) { $email = $env:METABASE_ADMIN_EMAIL; if (-not $email) { $email = $env:ADMIN_EMAIL } }
if (-not $email) { $email = "admin@egisz.local" }

$pass = $null
if ($null -ne $AdminPassword) {
    $cred = New-Object System.Management.Automation.PSCredential("_", $AdminPassword)
    $pass = $cred.GetNetworkCredential().Password
}
if (-not $pass) { $pass = $env:METABASE_ADMIN_PASSWORD; if (-not $pass) { $pass = $env:ADMIN_PASSWORD } }
if (-not $pass) { $pass = "egisz" }

$image = "egisz-monitor-metabase:local"
Write-Host "[provision-local] Building $image from $RepoRoot ..." -ForegroundColor Cyan
docker build -f (Join-Path $RepoRoot "metabase\Dockerfile") -t $image $RepoRoot
if ($LASTEXITCODE -ne 0) { exit 1 }

# С хоста Windows контейнер обращается к Metabase на ПК через host.docker.internal
$mbInContainer = $MetabaseUrl
if ($MetabaseUrl -match "127\.0\.0\.1|localhost") {
    $mbInContainer = $MetabaseUrl -replace "127\.0\.0\.1|localhost", "host.docker.internal"
}

$dash = "/dashboards"
$scriptPath = "/app/setup-dashboards.sh"

Write-Host "[provision-local] METABASE_URL (inside container)=$mbInContainer" -ForegroundColor Cyan
Write-Host "[provision-local] ADMIN_EMAIL=$email" -ForegroundColor DarkGray

docker run --rm `
    --entrypoint /bin/bash `
    --add-host=host.docker.internal:host-gateway `
    -e METABASE_URL="$mbInContainer" `
    -e ADMIN_EMAIL="$email" `
    -e ADMIN_PASSWORD="$pass" `
    -e METABASE_DASHBOARDS_DIR="$dash" `
    -v "${RepoRoot}/metabase_dashboards:${dash}:ro" `
    -v "${RepoRoot}/metabase/setup-dashboards.sh:${scriptPath}:ro" `
    $image `
    $scriptPath

if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Host "[provision-local] Done. Откройте персональную коллекцию в Metabase и проверьте дашборды." -ForegroundColor Green

Write-Host ""
Write-Host "Полный сброс Metabase app DB в стеке (K8s) и повторная заливка дашбордов из JSON — из корня репозитория:" -ForegroundColor DarkGray
Write-Host ".\start.ps1 -Action reset-metabase" -ForegroundColor White
