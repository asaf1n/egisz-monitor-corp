#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Дампы PostgreSQL из пода StatefulSet в каталог на диске Windows.

.DESCRIPTION
    В поде уже заданы POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB (Secret postgres-credentials).
    Формат -Fc (custom) для pg_restore. По умолчанию: дамп основной БД витрины (POSTGRES_DB) и БД metabase.

.PARAMETER Namespace
    Kubernetes namespace (по умолчанию egisz-monitor).

.PARAMETER BackupDir
    Локальный каталог для .dump (по умолчанию I:\DB\egisz-monitor-backups).

.PARAMETER PostgresPod
    Имя пода; если пусто — первый pod с label app.kubernetes.io/name=postgres.

.PARAMETER SkipMetabase
    Не дампить БД metabase.

.EXAMPLE
    .\scripts\backup-postgres.ps1
    .\scripts\backup-postgres.ps1 -BackupDir 'D:\backups\egisz' -SkipMetabase
#>
param(
    [string]$Namespace = "egisz-monitor",
    [string]$BackupDir = "I:\DB\egisz-monitor-backups",
    [string]$PostgresPod = "",
    [switch]$SkipMetabase
)

$ErrorActionPreference = "Stop"

function Get-PostgresPodName {
    if ($PostgresPod) { return $PostgresPod }
    $name = kubectl get pod -n $Namespace -l app.kubernetes.io/name=postgres `
        -o jsonpath="{.items[0].metadata.name}" 2>$null
    if (-not $name) {
        throw "Postgres pod not found in namespace $Namespace (label app.kubernetes.io/name=postgres)."
    }
    return $name
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$pod = Get-PostgresPodName
$ts = Get-Date -Format "yyyyMMdd_HHmmss"

function Copy-DumpFromPod {
    param(
        [string]$RemotePath,
        [string]$LocalName
    )
    # Windows kubectl cp: destination must look like a "local" path; absolute I:\... often fails
    # ("one of src or dest must be a local file specification"). Copy from inside BackupDir with .\file.
    $nameOnly = "${LocalName}_${ts}.dump"
    $local = Join-Path $BackupDir $nameOnly
    Push-Location $BackupDir
    try {
        kubectl cp "${Namespace}/${pod}:${RemotePath}" ".\$nameOnly"
        if ($LASTEXITCODE -ne 0) { throw "kubectl cp failed: $RemotePath" }
    }
    finally {
        Pop-Location
    }
    kubectl exec -n $Namespace $pod -- rm -f $RemotePath
    Write-Host "Saved: $local"
    return $local
}

# 1) Витрина: POSTGRES_DB из Secret (часто egisz_reports)
Write-Host "Dumping POSTGRES_DB from pod $pod..."
kubectl exec -n $Namespace $pod -- bash -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/egisz_dwh.dump'
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed for POSTGRES_DB" }
Copy-DumpFromPod -RemotePath "/tmp/egisz_dwh.dump" -LocalName "egisz_dwh" | Out-Null

if (-not $SkipMetabase) {
    Write-Host "Dumping metabase application database..."
    kubectl exec -n $Namespace $pod -- bash -c 'pg_dump -U "$POSTGRES_USER" -d metabase -Fc -f /tmp/metabase_app.dump'
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed for metabase" }
    Copy-DumpFromPod -RemotePath "/tmp/metabase_app.dump" -LocalName "metabase_app" | Out-Null
}

Write-Host "Done. After full cluster reset see scripts/restore-postgres.ps1 (header + modes)."
