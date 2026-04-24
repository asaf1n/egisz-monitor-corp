#!/usr/bin/env powershell
# Полный деплой egisz-monitor-corp в Kubernetes (namespace egisz-corp).
# Требуется: Docker, kubectl, кластер с доступом (Docker Desktop / minikube / k8s).

param(
    [ValidateSet("deploy", "build", "apply", "status", "help")]
    [string]$Action = "deploy"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

function Write-Banner([string]$Title, [string]$Color = "Cyan") {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor $Color
    Write-Host $Title -ForegroundColor $Color
    Write-Host "========================================" -ForegroundColor $Color
}

function Show-Help {
    Write-Host @"
egisz-monitor-corp\start.ps1

  deploy (default)  docker build (web + metabase) + kubectl apply + краткая справка
  build             только docker build
  apply             только kubectl apply (образы уже собраны)
  status            kubectl get pods,svc -n egisz-corp
  help

Перед первым deploy:
  1) kubectl cluster-info
  2) Скопируйте k8s\postgres\postgres-secret.example.yaml -> postgres-credentials.yaml (имя Secret: postgres-credentials)
  3) Скопируйте k8s\metabase-admin-secret.example.yaml -> metabase-admin-secret.yaml -> kubectl apply -f ...
  4) Создайте секрет конфига для веба:
       kubectl -n egisz-corp create secret generic egisz-corp-web-config --from-file=egisz_corp.yaml=config\egisz_corp.yaml --dry-run=client -o yaml | kubectl apply -f -
     (подготовьте config\egisz_corp.yaml с реальными FB/PG для кластера)

Airflow (опционально): см. k8s\README.md и k8s\airflow\values-corp.example.yaml
"@
}

function Invoke-DockerBuild {
    Write-Host "[Docker] Building egisz-corp-web..." -ForegroundColor Yellow
    docker build -f docker/web/Dockerfile -t egisz-corp-web:latest $Root
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Write-Host "[Docker] Building egisz-corp-metabase..." -ForegroundColor Yellow
    docker build -f metabase/Dockerfile -t egisz-corp-metabase:latest $Root
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Write-Host "[Docker] OK" -ForegroundColor Green
}

function Invoke-KubectlApply {
    Write-Host "[kubectl] Namespace..." -ForegroundColor Cyan
    kubectl apply -f (Join-Path $Root "k8s\postgres\namespace.yaml")
    if (-not (Test-Path (Join-Path $Root "k8s\postgres\postgres-credentials.yaml"))) {
        Write-Host "ERROR: missing k8s\postgres\postgres-credentials.yaml (copy from postgres-secret.example.yaml)" -ForegroundColor Red
        exit 1
    }
    kubectl apply -f (Join-Path $Root "k8s\postgres\postgres-credentials.yaml")
    kubectl apply -f (Join-Path $Root "k8s\postgres\postgres-statefulset.yaml")
    kubectl apply -f (Join-Path $Root "k8s\postgres\postgres-service.yaml")
    kubectl apply -f (Join-Path $Root "k8s\postgres\airflow-metadata-init-job.yaml") 2>$null
    if (-not (Test-Path (Join-Path $Root "k8s\metabase-admin-secret.yaml"))) {
        Write-Host "WARN: k8s\metabase-admin-secret.yaml not found; Metabase pod may fail. Copy from metabase-admin-secret.example.yaml" -ForegroundColor Yellow
    } else {
        kubectl apply -f (Join-Path $Root "k8s\metabase-admin-secret.yaml")
    }
    kubectl apply -f (Join-Path $Root "k8s\metabase.yaml")
    kubectl apply -f (Join-Path $Root "k8s\web.yaml")
    Write-Host "[kubectl] Applied manifests" -ForegroundColor Green
}

function Show-DeployInfo {
    Write-Banner "Сервисы (namespace egisz-corp)"
    kubectl -n egisz-corp get pods,svc 2>$null
    Write-Host ""
    Write-Host "UI (port-forward):" -ForegroundColor Cyan
    Write-Host "  Web:      kubectl -n egisz-corp port-forward svc/corp-web 8080:8080   -> http://127.0.0.1:8080/" -ForegroundColor White
    Write-Host "  Metabase: kubectl -n egisz-corp port-forward svc/metabase 3001:3000 -> http://127.0.0.1:3001/" -ForegroundColor White
    Write-Host "  Postgres: kubectl -n egisz-corp port-forward svc/postgres 5432:5432" -ForegroundColor White
    Write-Host ""
    Write-Host "Внутри кластера:" -ForegroundColor Cyan
    Write-Host "  Postgres: postgres.egisz-corp.svc.cluster.local:5432" -ForegroundColor Gray
    Write-Host "  Metabase: http://metabase.egisz-corp.svc.cluster.local:3000" -ForegroundColor Gray
    Write-Host "  Web:      http://corp-web.egisz-corp.svc.cluster.local:8080" -ForegroundColor Gray
    Write-Host ""
    Write-Host "CLI / ETL в образе web: kubectl -n egisz-corp exec -it deploy/corp-web -- egisz-corp sync" -ForegroundColor Yellow
    Write-Banner "Complete" Green
}

switch ($Action) {
    "help" { Show-Help }
    "build" { Invoke-DockerBuild }
    "apply" { Invoke-KubectlApply; Show-DeployInfo }
    "status" { kubectl -n egisz-corp get pods,svc }
    "deploy" {
        Write-Banner "egisz-monitor-corp K8s deploy"
        Invoke-DockerBuild
        Invoke-KubectlApply
        Show-DeployInfo
    }
}
