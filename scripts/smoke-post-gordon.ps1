#!/usr/bin/env pwsh
# Post-Gordon smoke: kubectl get/top, /healthz, conf-ui logs (FB timeout -> RuntimeError in logs).
# Requires: kubectl + namespace egisz-monitor; healthz: port-forward или LoadBalancer на :8080.

param(
    [string]$Namespace = "egisz-monitor",
    [string]$ConfUiUrl = "http://127.0.0.1:8080"
)

$ErrorActionPreference = "Continue"

Write-Host "=== kubectl get pods,svc -n $Namespace ===" -ForegroundColor Cyan
kubectl get pods,svc -n $Namespace 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Warning "kubectl failed or namespace missing; stopping."
    exit 0
}

Write-Host "`n=== kubectl top pod -n $Namespace (needs metrics-server) ===" -ForegroundColor Cyan
$topOut = kubectl top pod -n $Namespace 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host $topOut
} else {
    Write-Warning "kubectl top unavailable (common without metrics-server)."
}

Write-Host "`n=== GET $ConfUiUrl/healthz ===" -ForegroundColor Cyan
try {
    $r = Invoke-WebRequest -Uri "$ConfUiUrl/healthz" -UseBasicParsing -TimeoutSec 5
    Write-Host "Status:" $r.StatusCode $r.Content
} catch {
    Write-Warning "healthz failed: run .\start.ps1 (apply) for port-forward, kubectl port-forward svc/conf-ui 8080:8080, or open Config UI via LoadBalancer. $($_.Exception.Message)"
}

Write-Host "`n=== kubectl logs deploy/conf-ui (tail 40; look for Firebird query timeout) ===" -ForegroundColor Cyan
kubectl logs -n $Namespace deploy/conf-ui -c conf-ui --tail=40 2>$null

Write-Host "`nManual: run full sync from UI; on FB timeout expect RuntimeError ... timeout ... in logs." -ForegroundColor Yellow
