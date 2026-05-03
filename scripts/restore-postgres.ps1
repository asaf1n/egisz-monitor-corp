#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Восстановление PostgreSQL из дампов -Fc после сброса / нового PVC.

.DESCRIPTION
    **Витрина (SchemaThenData):** после deploy примените DDL (Job egisz-reports-schema-init), затем:
    .\scripts\restore-postgres.ps1 -DwhDump '...\egisz_dwh_*.dump' -Mode DataOnly

    **Metabase:** `.\start.ps1 -Action deploy` (или `reset-deploy`) для DROP/CREATE БД приложения; при необходимости старого состояния UI — также -MetabaseDump.

    **Full:** только если дамп полный и нужен pg_restore без --data-only.

.EXAMPLE
    .\scripts\restore-postgres.ps1 -DwhDump 'I:\DB\egisz-monitor-backups\egisz_dwh_20260101_120000.dump' -Mode DataOnly
#>
param(
    [string]$Namespace = "egisz-monitor",
    [string]$PostgresPod = "",
    [Parameter(Mandatory = $true)]
    [string]$DwhDump,
    [string]$MetabaseDump = "",
    [ValidateSet("DataOnly", "Full")]
    [string]$Mode = "DataOnly"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $DwhDump)) {
    throw "DwhDump not found: $DwhDump"
}

function Get-PostgresPodName {
    if ($PostgresPod) { return $PostgresPod }
    $name = kubectl get pod -n $Namespace -l app.kubernetes.io/name=postgres `
        -o jsonpath="{.items[0].metadata.name}" 2>$null
    if (-not $name) { throw "Postgres pod not found in $Namespace" }
    return $name
}

$pod = Get-PostgresPodName

Write-Host "kubectl cp DWH dump -> pod $pod..."
kubectl cp $DwhDump "${Namespace}/${pod}:/tmp/egisz_dwh_restore.dump"
if ($LASTEXITCODE -ne 0) { throw "kubectl cp failed" }

if ($Mode -eq "DataOnly") {
    kubectl exec -n $Namespace $pod -- bash -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --data-only --no-owner /tmp/egisz_dwh_restore.dump; rc=$?; rm -f /tmp/egisz_dwh_restore.dump; if [ $rc -gt 1 ]; then exit $rc; fi; exit 0'
} else {
    kubectl exec -n $Namespace $pod -- bash -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner /tmp/egisz_dwh_restore.dump; rc=$?; rm -f /tmp/egisz_dwh_restore.dump; if [ $rc -gt 1 ]; then exit $rc; fi; exit 0'
}
if ($LASTEXITCODE -ne 0) { throw "pg_restore DWH failed (exit $LASTEXITCODE)" }

if ($MetabaseDump) {
    if (-not (Test-Path -LiteralPath $MetabaseDump)) { throw "MetabaseDump not found: $MetabaseDump" }
    kubectl cp $MetabaseDump "${Namespace}/${pod}:/tmp/metabase_app_restore.dump"
    if ($LASTEXITCODE -ne 0) { throw "kubectl cp metabase failed" }
    if ($Mode -eq "DataOnly") {
        kubectl exec -n $Namespace $pod -- bash -c 'pg_restore -U "$POSTGRES_USER" -d metabase --data-only --no-owner /tmp/metabase_app_restore.dump; rc=$?; rm -f /tmp/metabase_app_restore.dump; if [ $rc -gt 1 ]; then exit $rc; fi; exit 0'
    } else {
        kubectl exec -n $Namespace $pod -- bash -c 'pg_restore -U "$POSTGRES_USER" -d metabase --no-owner /tmp/metabase_app_restore.dump; rc=$?; rm -f /tmp/metabase_app_restore.dump; if [ $rc -gt 1 ]; then exit $rc; fi; exit 0'
    }
    if ($LASTEXITCODE -ne 0) { throw "pg_restore metabase failed (exit $LASTEXITCODE)" }
}

Write-Host "Готово. Проверка: kubectl exec -n $Namespace $pod -- bash -c 'pg_isready -U `$POSTGRES_USER -d `$POSTGRES_DB'"
