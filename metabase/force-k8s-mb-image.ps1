#!/usr/bin/env powershell
# Восстановление: под Metabase ссылается на :local, но в кластере устаревший кэш / нет скриптов в /app.
# Собирает образ, тегирует :local, выставляет уникальный тег deployment и ждёт rollout.
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
Write-Host "[metabase] docker build + tag :local" -ForegroundColor Cyan
docker build -f metabase/Dockerfile -t egisz-corp-metabase:latest $Root
if ($LASTEXITCODE -ne 0) { exit 1 }
docker tag egisz-corp-metabase:latest egisz-corp-metabase:local
if ($LASTEXITCODE -ne 0) { exit 1 }
$stamp = [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")
$img = "egisz-corp-metabase:fix-$stamp"
docker tag egisz-corp-metabase:local $img
kubectl -n egisz-corp set image deployment/metabase "metabase=$img"
if ($LASTEXITCODE -ne 0) { exit 1 }
kubectl -n egisz-corp rollout status deployment/metabase --timeout=300s
if ($LASTEXITCODE -ne 0) { exit 1 }
kubectl -n egisz-corp exec deploy/metabase -- test -f /app/verify-corp-stack.sh
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: /app/verify-corp-stack.sh still missing in pod" -ForegroundColor Red
    exit 1
}
Write-Host "[metabase] OK: custom image in pod" -ForegroundColor Green
