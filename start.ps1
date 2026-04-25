#!/usr/bin/env powershell
# Local full stack in Kubernetes (namespace egisz-corp). Firebird stays on Windows host.
# Requires: Docker, kubectl; optional: kind (https://kind.sigs.k8s.io/) for auto cluster create.
#
# deploy: kind cluster egisz-local if needed, docker build, load images into kind, apply manifests,
# schema for egisz_reports, airflow DB, Metabase default admin + provision dashboards.

param(
    [ValidateSet("deploy", "reset-deploy", "build", "apply", "status", "web", "forward", "stop-forward", "compose-up", "compose-down", "test", "help")]
    [string]$Action = "deploy",
    [switch]$SkipKindCluster,
    # With -Action compose-down: also remove Compose volumes (wipes corp Postgres data volume).
    [switch]$RemoveComposeVolumes,
    # With -Action web / forward: do not bind localhost:5432 (skip if another Postgres already uses 5432).
    [switch]$SkipPostgresPortForward,
    # With -Action web / forward: kubectl port-forward in background (hidden kubectl.exe, no extra PS windows). PIDs: .egisz-corp-port-forward.pids; stop: -Action stop-forward
    [switch]$BackgroundPortForward,
    # With -Action deploy / apply / reset-deploy: after smoke tests, start port-forward (default: same as web -BackgroundPortForward). Use -BackgroundPortForward:$false for three visible PS windows.
    [switch]$WithPortForward
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

$script:KindClusterName = "egisz-local"
$script:ComposeProject = "egisz-monitor-corp"

function Get-ComposeFilePath {
    return (Join-Path $Root "docker-compose.yml")
}

function Warn-IfNestedUnderSiblingMonitor {
    $leaf = Split-Path $Root -Leaf
    $parent = Split-Path $Root -Parent
    if (-not $parent) { return }
    $parentLeaf = Split-Path $parent -Leaf
    if ($leaf -eq "egisz-monitor-corp" -and $parentLeaf -eq "egisz-monitor") {
        Write-Warning ("Clone is nested under {0}. Use a standalone folder, e.g. C:\Users\...\egisz-monitor-corp, so Docker paths and IDE roots are not mixed with the other repo." -f $parent)
    }
}

function Invoke-CorpDockerCompose {
    param(
        [Parameter(Mandatory)][string[]]$ComposeArgs
    )
    Warn-IfNestedUnderSiblingMonitor
    $composeFile = Get-ComposeFilePath
    if (-not (Test-Path $composeFile)) {
        Write-Host "ERROR: Missing $composeFile" -ForegroundColor Red
        exit 1
    }
    $prev = $env:COMPOSE_PROJECT_NAME
    $env:COMPOSE_PROJECT_NAME = $script:ComposeProject
    try {
        docker compose --project-directory $Root -f $composeFile @ComposeArgs
        if ($LASTEXITCODE -ne 0) { exit 1 }
    } finally {
        if ($null -eq $prev) {
            Remove-Item Env:COMPOSE_PROJECT_NAME -ErrorAction SilentlyContinue
        } else {
            $env:COMPOSE_PROJECT_NAME = $prev
        }
    }
}

function Invoke-CorpComposeUp {
    Write-Banner "Docker Compose (corp Postgres only)"
    Invoke-CorpDockerCompose -ComposeArgs @("up", "-d", "--build")
    Write-Host "[compose] Up. DB volume name: egisz_monitor_corp_postgres_data (see docker volume ls)" -ForegroundColor Green
}

function Invoke-CorpComposeDown {
    $args = @("down")
    if ($RemoveComposeVolumes) { $args += "-v" }
    Write-Banner "Docker Compose down"
    Invoke-CorpDockerCompose -ComposeArgs $args
    Write-Host "[compose] Down complete." -ForegroundColor Green
}

function Invoke-PythonTests {
    Write-Banner "pytest"
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        & $venvPy -m pip install -q -e ".[dev]"
        if ($LASTEXITCODE -ne 0) { exit 1 }
        & $venvPy -m pytest
        if ($LASTEXITCODE -ne 0) { exit 1 }
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m pip install -q -e ".[dev]"
        if ($LASTEXITCODE -ne 0) { exit 1 }
        py -3 -m pytest
        if ($LASTEXITCODE -ne 0) { exit 1 }
    } else {
        Write-Host "ERROR: No .venv\Scripts\python.exe and no py launcher. Install Python 3.10+." -ForegroundColor Red
        exit 1
    }
    Write-Host "[test] OK" -ForegroundColor Green
}

function Write-Banner([string]$Title, [string]$Color = "Cyan") {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor $Color
    Write-Host $Title -ForegroundColor $Color
    Write-Host "========================================" -ForegroundColor $Color
}

function Write-Utf8NoBom([string]$Path, [string]$Content) {
    [System.IO.File]::WriteAllText($Path, $Content, [System.Text.UTF8Encoding]::new($false))
}

function Show-Help {
    Write-Host @'
egisz-monitor-corp\start.ps1

  deploy (default)  kind (if needed) + docker build + kubectl apply + DB schema + smoke HTTP + summary (optional: -WithPortForward)
  reset-deploy      delete namespace egisz-corp (full reset), then same as deploy + smoke tests (optional: -WithPortForward)
  build             docker build only (K8s images)
  apply             kubectl only (images already built / loaded into kind if needed) (optional: -WithPortForward)
  compose-up        Postgres from THIS repo only: docker compose --project-directory <repo>
  compose-down      docker compose down (use -RemoveComposeVolumes to wipe corp DB volume)
  status            kubectl get pods,svc -n egisz-corp
  web | forward     port-forward to localhost (Postgres 5432, conf-ui 8080, Metabase 3000); opens browser (Docker Desktop)
  stop-forward      stop background kubectl port-forwards started with web -BackgroundPortForward
  SkipPostgresPortForward   only with web/forward: omit Postgres forward if port 5432 is busy on the host
  BackgroundPortForward     only with web/forward: hidden kubectl.exe (no extra PS windows); PIDs in .egisz-corp-port-forward.pids
  test              pip install -e ".[dev]" && pytest
  help

Parameters:
  -SkipKindCluster         do not run kind create; kubectl cluster-info must work
  -RemoveComposeVolumes    only with compose-down: docker compose down -v
  -WithPortForward         with deploy / apply / reset-deploy: after smoke, kubectl port-forward to localhost + open browser (default: background kubectl). Optional -BackgroundPortForward:$false for three PS windows instead.

Compose always uses COMPOSE_PROJECT_NAME=egisz-monitor-corp and repo root; DB volume is
  egisz_monitor_corp_postgres_data (not the sibling egisz-monitor stack).

K8s from your PC (Docker Desktop / kind): browser uses NodePorts 30808 (conf-ui) and 30300 (Metabase), not 8080/3000 on localhost unless you port-forward. Inside the cluster, services stay on 8080 / 3000 / 5432 (see deploy summary).

Generated each deploy/apply (under k8s\):
  Postgres: database egisz_reports, user egisz, password egisz
  Metabase: admin@egisz.local / egisz  (MB_PASSWORD_COMPLEXITY=weak in k8s/metabase.yaml)
  Web config file: k8s\local\egisz_corp.yaml (Firebird: host.docker.internal)

Edit k8s\local\egisz_corp.yaml for your Firebird alias and credentials on Windows.
'@
}

function Invoke-DockerBuild {
    Warn-IfNestedUnderSiblingMonitor
    Write-Host "[Docker] Building egisz-conf-ui (Config UI)..." -ForegroundColor Yellow
    docker build -f docker/web/Dockerfile -t egisz-conf-ui:latest $Root
    if ($LASTEXITCODE -ne 0) { exit 1 }
    docker tag egisz-conf-ui:latest egisz-conf-ui:corp-web
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Write-Host "[Docker] Building egisz-corp-metabase..." -ForegroundColor Yellow
    docker build -f metabase/Dockerfile -t egisz-corp-metabase:latest $Root
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Write-Host "[Docker] OK" -ForegroundColor Green
}

function Test-KubectlResponds {
    # Do not use PowerShell-native kubectl here: with $ErrorActionPreference=Stop, stderr from kubectl
    # becomes a terminating error and we never reach kind create.
    cmd /c 'kubectl cluster-info --request-timeout=8s 1>nul 2>nul'
    return ($LASTEXITCODE -eq 0)
}

function Ensure-LocalKubernetesCluster {
    if (Test-KubectlResponds) {
        Write-Host "[kubectl] Cluster API is reachable." -ForegroundColor Green
        return
    }
    if ($SkipKindCluster) {
        Write-Host "ERROR: kubectl has no cluster and -SkipKindCluster was set. Enable Kubernetes or pick a context." -ForegroundColor Red
        exit 1
    }
    if (-not (Get-Command kind -ErrorAction SilentlyContinue)) {
        Write-Host "ERROR: No Kubernetes API and kind.exe not found." -ForegroundColor Red
        Write-Host "       Install kind or enable Kubernetes in Docker Desktop." -ForegroundColor Red
        exit 1
    }
    $kindName = $script:KindClusterName
    $ctx = "kind-$kindName"
    $clusterLines = @(cmd /c 'kind get clusters 2>nul' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($clusterLines -contains $kindName) {
        cmd /c ('kubectl config use-context "' + $ctx + '" 1>nul 2>nul')
        if (Test-KubectlResponds) {
            Write-Host "[kind] Using existing cluster $kindName." -ForegroundColor Green
            return
        }
        Write-Host ("ERROR: kind cluster '{0}' exists but the API is not reachable (context {1})." -f $kindName, $ctx) -ForegroundColor Red
        Write-Host "       Start Docker / the kind control plane, or fix kubeconfig: kubectl config get-contexts" -ForegroundColor Red
        exit 1
    }
    Write-Host ('[kind] Creating cluster ' + $kindName + ' (first run may take several minutes)...') -ForegroundColor Cyan
    kind create cluster --name $kindName --wait 10m
    if ($LASTEXITCODE -ne 0) { exit 1 }
    cmd /c ('kubectl config use-context "' + $ctx + '" 1>nul 2>nul')
    if (-not (Test-KubectlResponds)) {
        Write-Host "ERROR: kind cluster created but cluster-info still fails." -ForegroundColor Red
        exit 1
    }
    Write-Host "[kind] Cluster $kindName is ready." -ForegroundColor Green
}

function Invoke-KindLoadImagesIfNeeded {
    $ctx = (cmd /c 'kubectl config current-context 2>nul' | Select-Object -First 1)
    if (-not $ctx) { return }
    $ctx = $ctx.Trim()
    if ($ctx -notmatch '^kind-') { return }
    $name = $ctx -replace '^kind-', ''
    Write-Host "[kind] Loading local images into cluster $name..." -ForegroundColor Cyan
    kind load docker-image egisz-conf-ui:corp-web --name $name
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kind load docker-image egisz-corp-metabase:latest --name $name
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Write-Host "[kind] Images loaded." -ForegroundColor Green
}

function New-LocalDeployArtifactFiles {
    $pg = @'
apiVersion: v1
kind: Secret
metadata:
  name: postgres-credentials
  namespace: egisz-corp
type: Opaque
stringData:
  POSTGRES_USER: "egisz"
  POSTGRES_PASSWORD: "egisz"
  POSTGRES_DB: "egisz_reports"
'@
    $mb = @'
apiVersion: v1
kind: Secret
metadata:
  name: metabase-admin
  namespace: egisz-corp
type: Opaque
stringData:
  email: "admin@egisz.local"
  password: "egisz"
'@
    $pgPath = Join-Path $Root "k8s\postgres\postgres-credentials.yaml"
    $mbPath = Join-Path $Root "k8s\metabase-admin-secret.yaml"
    Write-Utf8NoBom $pgPath $pg
    Write-Utf8NoBom $mbPath $mb
    Write-Host "[local] Wrote k8s\postgres\postgres-credentials.yaml and k8s\metabase-admin-secret.yaml" -ForegroundColor Green
}

function Wait-KubectlJobSucceeded {
    param(
        [Parameter(Mandatory)][string]$Namespace,
        [Parameter(Mandatory)][string]$JobName,
        [int]$TimeoutSec = 300
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSec)
    while ([DateTime]::UtcNow -lt $deadline) {
        $json = kubectl -n $Namespace get job $JobName -o json 2>$null
        if ($LASTEXITCODE -ne 0) {
            Start-Sleep -Seconds 2
            continue
        }
        $job = $json | ConvertFrom-Json
        $succeeded = $job.status.succeeded
        if ($null -ne $succeeded -and [int]$succeeded -ge 1) {
            return $true
        }
        $conds = @()
        if ($job.status.conditions) { $conds = @($job.status.conditions) }
        foreach ($c in $conds) {
            if ($c.type -eq 'Failed' -and $c.status -eq 'True') {
                Write-Host ('ERROR: Job ' + $JobName + ' failed: ' + $c.reason + ' - ' + $c.message) -ForegroundColor Red
                kubectl -n $Namespace logs "job/$JobName" --tail=120 2>$null
                return $false
            }
        }
        Start-Sleep -Seconds 2
    }
    Write-Host ('ERROR: Timeout waiting for job ' + $JobName + ' (' + $TimeoutSec + 's).') -ForegroundColor Red
    kubectl -n $Namespace logs "job/$JobName" --tail=120 2>$null
    return $false
}

function Publish-ConfUiImageToDockerDesktopK8s {
    $ctx = (kubectl config current-context 2>$null | Out-String).Trim()
    if ($ctx -ne "docker-desktop") { return }
    kubectl -n egisz-corp get deploy conf-ui -o name 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { return }
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")
    $img = "egisz-conf-ui:corp-web-$stamp"
    docker tag egisz-conf-ui:latest $img
    if ($LASTEXITCODE -ne 0) { return }
    kubectl -n egisz-corp set image deployment/conf-ui "conf-ui=$img"
    if ($LASTEXITCODE -ne 0) { return }
    Write-Host "[docker-desktop] conf-ui -> $img (local image digest refresh)" -ForegroundColor DarkGray
}

function Publish-MetabaseImageToDockerDesktopK8s {
    $ctx = (kubectl config current-context 2>$null | Out-String).Trim()
    if ($ctx -ne "docker-desktop") { return }
    kubectl -n egisz-corp get deploy metabase -o name 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { return }
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")
    $img = "egisz-corp-metabase:latest-$stamp"
    docker tag egisz-corp-metabase:latest $img
    if ($LASTEXITCODE -ne 0) { return }
    kubectl -n egisz-corp set image deployment/metabase "metabase=$img"
    if ($LASTEXITCODE -ne 0) { return }
    Write-Host "[docker-desktop] metabase -> $img (local image digest refresh)" -ForegroundColor DarkGray
}

function Invoke-PostgresSchemaInit {
    Write-Host "[kubectl] ConfigMap + Job: apply schema to egisz_reports..." -ForegroundColor Cyan
    $sql1 = Join-Path $Root "sql\001_schema.sql"
    $sql2 = Join-Path $Root "sql\002_etl_state.sql"
    if (-not (Test-Path $sql1) -or -not (Test-Path $sql2)) {
        Write-Host "ERROR: Missing sql\001_schema.sql or sql\002_etl_state.sql" -ForegroundColor Red
        exit 1
    }
    # Do not pipe ConfigMap YAML through PowerShell: it can transcode UTF-8 SQL aliases to '?'.
    # Create the ConfigMap directly from files so Russian UI column names stay byte-exact.
    kubectl -n egisz-corp delete configmap/egisz-reports-schema --ignore-not-found
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kubectl -n egisz-corp create configmap egisz-reports-schema `
        --from-file=001_schema.sql=$sql1 `
        --from-file=002_etl_state.sql=$sql2
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kubectl -n egisz-corp delete job/egisz-reports-schema-init --ignore-not-found
    kubectl apply -f (Join-Path $Root "k8s\postgres\egisz-reports-schema-job.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    if (-not (Wait-KubectlJobSucceeded -Namespace egisz-corp -JobName egisz-reports-schema-init -TimeoutSec 300)) {
        Write-Host "ERROR: Schema job did not succeed. Logs: kubectl -n egisz-corp logs job/egisz-reports-schema-init" -ForegroundColor Red
        exit 1
    }
    Write-Host "[kubectl] DWH schema applied." -ForegroundColor Green
}

function Invoke-PostgresAirflowDbInit {
    $jobFile = Join-Path $Root "k8s\postgres\airflow-metadata-init-job.yaml"
    Write-Host "[kubectl] Job: create database airflow..." -ForegroundColor Cyan
    kubectl -n egisz-corp delete job/airflow-metadata-db-init --ignore-not-found
    kubectl apply -f $jobFile
    if ($LASTEXITCODE -ne 0) { exit 1 }
    if (-not (Wait-KubectlJobSucceeded -Namespace egisz-corp -JobName airflow-metadata-db-init -TimeoutSec 300)) {
        Write-Host "ERROR: Airflow DB job did not succeed. Logs: kubectl -n egisz-corp logs job/airflow-metadata-db-init" -ForegroundColor Red
        exit 1
    }
    Write-Host "[kubectl] Database airflow ready." -ForegroundColor Green
}

function Invoke-WebConfigSecret {
    $cfg = Join-Path $Root "k8s\local\egisz_corp.yaml"
    if (-not (Test-Path $cfg)) {
        Write-Host "ERROR: Missing $cfg" -ForegroundColor Red
        exit 1
    }
    Write-Host "[kubectl] Secret egisz-corp-conf-ui-config from k8s\local\egisz_corp.yaml..." -ForegroundColor Cyan
    kubectl -n egisz-corp create secret generic egisz-corp-conf-ui-config `
        --from-file="egisz_corp.yaml=$cfg" `
        --dry-run=client -o yaml | kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

function Invoke-ResetK8sNamespace {
    Write-Host "[kubectl] Full reset: deleting namespace egisz-corp..." -ForegroundColor Cyan
    kubectl delete namespace egisz-corp --ignore-not-found
    $maxWait = 72
    for ($i = 0; $i -lt $maxWait; $i++) {
        cmd /c 'kubectl get namespace egisz-corp -o name 1>nul 2>nul'
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[kubectl] Namespace egisz-corp is gone." -ForegroundColor Green
            return
        }
        Start-Sleep -Seconds 5
    }
    Write-Host "WARN: namespace egisz-corp still exists after wait; continuing apply may fail." -ForegroundColor Yellow
}

function Invoke-KubectlApply {
    param([switch]$ResetNamespace)

    Ensure-LocalKubernetesCluster
    if (-not (Test-KubectlResponds)) {
        Write-Host "ERROR: kubectl cluster-info failed." -ForegroundColor Red
        exit 1
    }

    if ($ResetNamespace) {
        Invoke-ResetK8sNamespace
    }

    New-LocalDeployArtifactFiles

    Write-Host "[kubectl] Namespace..." -ForegroundColor Cyan
    kubectl apply -f (Join-Path $Root "k8s\postgres\namespace.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }

    kubectl apply -f (Join-Path $Root "k8s\postgres\postgres-credentials.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kubectl apply -f (Join-Path $Root "k8s\postgres\postgres-statefulset.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kubectl apply -f (Join-Path $Root "k8s\postgres\postgres-service.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host "[kubectl] Waiting for Postgres (up to 10m, first PVC bind can be slow)..." -ForegroundColor Cyan
    kubectl -n egisz-corp rollout status statefulset/postgres --timeout=600s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: StatefulSet postgres not Ready." -ForegroundColor Red
        Write-Host "--- kubectl -n egisz-corp describe pod -l app.kubernetes.io/name=postgres ---" -ForegroundColor Yellow
        cmd /c 'kubectl -n egisz-corp describe pod -l app.kubernetes.io/name=postgres 2>&1'
        Write-Host "--- kubectl -n egisz-corp get events (last 25) ---" -ForegroundColor Yellow
        cmd /c 'kubectl -n egisz-corp get events --sort-by=.lastTimestamp 2>&1' | Select-Object -Last 25
        exit 1
    }

    Invoke-PostgresSchemaInit
    Invoke-PostgresAirflowDbInit

    kubectl apply -f (Join-Path $Root "k8s\metabase-admin-secret.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kubectl apply -f (Join-Path $Root "k8s\metabase.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Publish-MetabaseImageToDockerDesktopK8s

    Invoke-WebConfigSecret

    # Remove legacy corp-web objects if upgrading without namespace reset.
    kubectl -n egisz-corp delete deployment/corp-web service/corp-web --ignore-not-found
    kubectl -n egisz-corp delete secret/egisz-corp-web-config --ignore-not-found

    kubectl apply -f (Join-Path $Root "k8s\conf-ui.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Publish-ConfUiImageToDockerDesktopK8s

    Write-Host "[kubectl] Waiting for Metabase (first start can take several minutes)..." -ForegroundColor Cyan
    kubectl -n egisz-corp rollout status deployment/metabase --timeout=600s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: Metabase not Ready in 10m. Check: kubectl -n egisz-corp logs deploy/metabase" -ForegroundColor Yellow
    }

    Write-Host "[kubectl] Waiting for conf-ui (Config UI)..." -ForegroundColor Cyan
    kubectl -n egisz-corp rollout status deployment/conf-ui --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: conf-ui not Ready in 3m. Check: kubectl -n egisz-corp describe deploy/conf-ui" -ForegroundColor Yellow
    }

    Write-Host "[kubectl] Apply finished." -ForegroundColor Green
}

function Invoke-K8sSmokeTests {
    # NodePort на 127.0.0.1 из IDE/агента часто недоступен; проверяем сервисы по DNS внутри кластера (тот же kube API).
    Write-Banner "Smoke tests (in-cluster HTTP)"
    $ns = "egisz-corp"
    $targets = @(
        @{ Name = "conf-ui"; Url = "http://conf-ui.$ns.svc.cluster.local:8080/" },
        @{ Name = "metabase-health"; Url = "http://metabase.$ns.svc.cluster.local:3000/api/health" }
    )
    foreach ($t in $targets) {
        $pod = "smoke-" + [guid]::NewGuid().ToString("n").Substring(0, 12)
        cmd /c ('kubectl -n ' + $ns + ' run ' + $pod + ' --rm -i --restart=Never --image=curlimages/curl:latest --command -- curl -sf -o /dev/null --max-time 30 ' + $t.Url)
        if ($LASTEXITCODE -ne 0) {
            Write-Host ("ERROR: smoke failed for {0} ({1})" -f $t.Name, $t.Url) -ForegroundColor Red
            exit 1
        }
        Write-Host ('[smoke] OK {0} -> {1}' -f $t.Name, $t.Url) -ForegroundColor Green
    }
    Write-Host ""
    Write-Host "From this PC's browser: apply/deploy only update the cluster; they do NOT start port-forward." -ForegroundColor Yellow
    Write-Host "  .\start.ps1 -Action web -BackgroundPortForward" -ForegroundColor White
    Write-Host "  then http://127.0.0.1:8080/ (Config UI) and http://127.0.0.1:3000/ (Metabase)." -ForegroundColor White
    Write-Host ""
}

function Show-K8sNetworkLegend {
    Write-Host ""
    Write-Host "==================================================================" -ForegroundColor Yellow
    Write-Host " Docker Desktop (Windows): NodePort on 127.0.0.1 often does NOT work." -ForegroundColor Yellow
    Write-Host " Open Config UI + Metabase in your browser with standard ports:" -ForegroundColor Yellow
    Write-Host "   .\start.ps1 -Action web -BackgroundPortForward   (recommended: no extra PS windows)" -ForegroundColor White
    Write-Host "   .\start.ps1 -Action web   (alias: forward; opens 3 PS windows with port-forward)" -ForegroundColor DarkGray
    Write-Host "==================================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "------------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host "Ports: why 30300 / 30808 and not 3000 / 8080 in the browser" -ForegroundColor Cyan
    Write-Host "  In the pod: Metabase listens on 3000, Config UI on 8080 (container ports)." -ForegroundColor Gray
    Write-Host "  Kubernetes Service maps those to ClusterIP:3000 and ClusterIP:8080 for in-cluster traffic." -ForegroundColor Gray
    Write-Host "  NodePort publishes a host-facing port in range 30000-32767 (here 30300, 30808) so you can" -ForegroundColor Gray
    Write-Host "  open the stack from Windows without port-forward. Use the NodePort URLs below on the host." -ForegroundColor Gray
    Write-Host "------------------------------------------------------------------" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Endpoints (ClusterIP is the virtual IP of the Service inside the cluster):" -ForegroundColor Cyan
    $rows = @(
        @{ Name = "conf-ui"; Dns = "conf-ui.egisz-corp.svc.cluster.local"; SvcPort = 8080; NodePort = 30808; Url = "http://127.0.0.1:30808/" },
        @{ Name = "metabase"; Dns = "metabase.egisz-corp.svc.cluster.local"; SvcPort = 3000; NodePort = 30300; Url = "http://127.0.0.1:30300/" },
        @{ Name = "postgres"; Dns = "postgres.egisz-corp.svc.cluster.local"; SvcPort = 5432; NodePort = 30432; Url = "127.0.0.1:30432 (TCP, psql/DBeaver)" }
    )
    foreach ($r in $rows) {
        $ip = (cmd /c ('kubectl -n egisz-corp get svc ' + $r.Name + ' -o jsonpath={.spec.clusterIP} 2>nul'))
        if ($null -eq $ip) { $ip = "" }
        $ip = $ip.Trim()
        if (-not $ip) { $ip = "(no Service or namespace missing)" }
        Write-Host ("  {0,-10}  Cluster-IP: {1,-15}  in-cluster: {2}:{3}" -f $r.Name, $ip, $r.Dns, $r.SvcPort) -ForegroundColor White
        Write-Host ("  {0,-10}  from this PC:  {1}  (NodePort {2} -> pod port {3})" -f "", $r.Url, $r.NodePort, $r.SvcPort) -ForegroundColor DarkCyan
        Write-Host ""
    }
    Write-Host "If NodePort URLs do not load, use port-forward (then browser uses 8080 / 3000 on localhost):" -ForegroundColor Yellow
    Write-Host "  kubectl -n egisz-corp port-forward svc/conf-ui 8080:8080    -> http://127.0.0.1:8080/" -ForegroundColor Gray
    Write-Host "  kubectl -n egisz-corp port-forward svc/metabase 3000:3000   -> http://127.0.0.1:3000/" -ForegroundColor Gray
    Write-Host "  (if 3000 is busy on the host, use e.g. 3001:3000 and open http://127.0.0.1:3001/ )" -ForegroundColor DarkGray
    Write-Host ""
}

function Get-CorpPortForwardPidFile {
    return (Join-Path $Root ".egisz-corp-port-forward.pids")
}

function Invoke-CorpStopPortForward {
    param([switch]$Quiet)
    $pidFile = Get-CorpPortForwardPidFile
    if (-not (Test-Path $pidFile)) {
        if (-not $Quiet) { Write-Host "No saved port-forward PIDs ($pidFile). Nothing to stop." -ForegroundColor DarkGray }
        return
    }
    $ids = @(Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^\d+$' })
    foreach ($id in $ids) {
        Stop-Process -Id ([int]$id) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    if (-not $Quiet) { Write-Host "Stopped background port-forward (kubectl) processes." -ForegroundColor Green }
}

function Invoke-CorpPortForwardIfRequestedAfterK8s {
    param(
        [bool]$BackgroundSwitchPresent,
        [bool]$BackgroundEnabled
    )
    if (-not $WithPortForward) { return }
    if ($BackgroundSwitchPresent -and -not $BackgroundEnabled) {
        Invoke-CorpWebPortForward
    } else {
        Invoke-CorpWebPortForward -ForceBackground
    }
}

function Invoke-CorpWebPortForward {
    param(
        [switch]$ForceBackground
    )
    $useBackground = [bool]($ForceBackground -or $BackgroundPortForward)
    Write-Banner "EGISZ Corp - standard ports (kubectl port-forward)"
    if (-not (Test-KubectlResponds)) {
        Write-Host "ERROR: kubectl cluster-info failed." -ForegroundColor Red
        exit 1
    }
    cmd /c 'kubectl -n egisz-corp get deploy conf-ui metabase 1>nul 2>nul'
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: namespace egisz-corp or deployments missing. Run: .\start.ps1 -Action deploy" -ForegroundColor Red
        exit 1
    }
    $pfPg = "kubectl -n egisz-corp port-forward svc/postgres 5432:5432"
    $pfConf = "kubectl -n egisz-corp port-forward svc/conf-ui 8080:8080"
    $pfMeta = "kubectl -n egisz-corp port-forward svc/metabase 3000:3000"

    if ($useBackground) {
        $kc = Get-Command kubectl -ErrorAction SilentlyContinue
        if (-not $kc) {
            Write-Host "ERROR: kubectl not found on PATH." -ForegroundColor Red
            exit 1
        }
        $kubectlExe = $kc.Source
        Write-Host "Background port-forward (hidden kubectl.exe, no extra PowerShell windows)." -ForegroundColor Cyan
        Write-Host "  Stop: .\start.ps1 -Action stop-forward" -ForegroundColor Gray
        Invoke-CorpStopPortForward -Quiet
        $startedPids = New-Object System.Collections.ArrayList
        if (-not $SkipPostgresPortForward) {
            cmd /c 'kubectl -n egisz-corp get svc postgres 1>nul 2>nul'
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  Postgres -> localhost:5432" -ForegroundColor Gray
                $pp = Start-Process -FilePath $kubectlExe -ArgumentList @('-n', 'egisz-corp', 'port-forward', 'svc/postgres', '5432:5432') -WindowStyle Hidden -PassThru
                if (-not $pp -or $pp.Id -lt 1) {
                    Write-Host "ERROR: could not start kubectl port-forward for postgres." -ForegroundColor Red
                    exit 1
                }
                [void]$startedPids.Add($pp.Id)
                Start-Sleep -Milliseconds 800
            } else {
                Write-Host "WARN: svc/postgres not found; skipping Postgres port-forward." -ForegroundColor Yellow
            }
        } else {
            Write-Host "SkipPostgresPortForward: conf-ui + Metabase only." -ForegroundColor Yellow
        }
        Write-Host "  conf-ui -> http://127.0.0.1:8080/" -ForegroundColor Gray
        $pc = Start-Process -FilePath $kubectlExe -ArgumentList @('-n', 'egisz-corp', 'port-forward', 'svc/conf-ui', '8080:8080') -WindowStyle Hidden -PassThru
        if (-not $pc -or $pc.Id -lt 1) {
            Write-Host "ERROR: could not start kubectl port-forward for conf-ui." -ForegroundColor Red
            exit 1
        }
        [void]$startedPids.Add($pc.Id)
        Start-Sleep -Milliseconds 500
        Write-Host "  Metabase -> http://127.0.0.1:3000/" -ForegroundColor Gray
        $pm = Start-Process -FilePath $kubectlExe -ArgumentList @('-n', 'egisz-corp', 'port-forward', 'svc/metabase', '3000:3000') -WindowStyle Hidden -PassThru
        if (-not $pm -or $pm.Id -lt 1) {
            Write-Host "ERROR: could not start kubectl port-forward for metabase." -ForegroundColor Red
            exit 1
        }
        [void]$startedPids.Add($pm.Id)
        $pidFile = Get-CorpPortForwardPidFile
        Set-Content -LiteralPath $pidFile -Value (($startedPids | ForEach-Object { $_.ToString() }) -join "`n") -Encoding ascii
        Write-Host ("[forward] PIDs saved to {0}" -f $pidFile) -ForegroundColor DarkGray
    } else {
        if (-not $SkipPostgresPortForward) {
            cmd /c 'kubectl -n egisz-corp get svc postgres 1>nul 2>nul'
            if ($LASTEXITCODE -eq 0) {
                Write-Host "Starting three PowerShell windows (leave them open while you use the stack)." -ForegroundColor Cyan
                Write-Host "  Window 1: $pfPg   -> localhost:5432 = Postgres in cluster (DBeaver/psql)" -ForegroundColor Gray
                Write-Host "  Window 2: $pfConf" -ForegroundColor Gray
                Write-Host "  Window 3: $pfMeta" -ForegroundColor Gray
                Start-Process powershell.exe -ArgumentList @('-NoExit', '-NoProfile', '-Command', $pfPg)
                Start-Sleep -Milliseconds 800
            } else {
                Write-Host "WARN: svc/postgres not found; skipping Postgres port-forward." -ForegroundColor Yellow
                Write-Host "Starting two PowerShell windows (leave them open while you use the apps)." -ForegroundColor Cyan
                Write-Host "  Window 1: $pfConf" -ForegroundColor Gray
                Write-Host "  Window 2: $pfMeta" -ForegroundColor Gray
            }
        } else {
            Write-Host "SkipPostgresPortForward: starting two windows (conf-ui + Metabase only)." -ForegroundColor Yellow
            Write-Host "  Window 1: $pfConf" -ForegroundColor Gray
            Write-Host "  Window 2: $pfMeta" -ForegroundColor Gray
        }
        Start-Process powershell.exe -ArgumentList @('-NoExit', '-NoProfile', '-Command', $pfConf)
        Start-Sleep -Seconds 1
        Start-Process powershell.exe -ArgumentList @('-NoExit', '-NoProfile', '-Command', $pfMeta)
    }
    Write-Host "Waiting for localhost listeners (up to 45s)..." -ForegroundColor Cyan
    $ok8080 = $false
    $ok3000 = $false
    $prevEa = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    for ($i = 0; $i -lt 45; $i++) {
        try {
            if (-not $ok8080) {
                $r = Invoke-WebRequest -Uri "http://127.0.0.1:8080/" -UseBasicParsing -TimeoutSec 2
                if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { $ok8080 = $true }
            }
        } catch { }
        try {
            if (-not $ok3000) {
                $r2 = Invoke-WebRequest -Uri "http://127.0.0.1:3000/api/health" -UseBasicParsing -TimeoutSec 2
                if ([int]$r2.StatusCode -eq 200) { $ok3000 = $true }
            }
        } catch { }
        if ($ok8080 -and $ok3000) { break }
        Start-Sleep -Seconds 1
    }
    $ErrorActionPreference = $prevEa
    if (-not $ok8080) {
        if ($useBackground) {
            Write-Host "WARN: http://127.0.0.1:8080/ did not respond yet (kubectl still starting or port in use)." -ForegroundColor Yellow
        } else {
            Write-Host "WARN: http://127.0.0.1:8080/ did not respond yet (check the port-forward window for errors)." -ForegroundColor Yellow
        }
    } else {
        Write-Host '[check] OK http://127.0.0.1:8080/ (Config UI)' -ForegroundColor Green
    }
    if (-not $ok3000) {
        Write-Host "WARN: http://127.0.0.1:3000/api/health did not respond yet (Metabase may still be starting)." -ForegroundColor Yellow
    } else {
        Write-Host '[check] OK http://127.0.0.1:3000/api/health (Metabase)' -ForegroundColor Green
    }
    Write-Host "Opening default browser..." -ForegroundColor Cyan
    Start-Process "http://127.0.0.1:8080/"
    Start-Sleep -Milliseconds 400
    Start-Process "http://127.0.0.1:3000/"
    if ($useBackground) {
        Write-Host "Done. Port-forward runs in the background; stop: .\start.ps1 -Action stop-forward" -ForegroundColor Green
    } else {
        Write-Host "Done. Close the port-forward PowerShell windows when finished." -ForegroundColor Green
    }
}

function Show-DeployInfo {
    Write-Banner 'Services (namespace egisz-corp)'
    $prevEa = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        kubectl -n egisz-corp get pods,svc -o wide
    } finally {
        $ErrorActionPreference = $prevEa
    }
    Show-K8sNetworkLegend
    Write-Host "Credentials (local dev):" -ForegroundColor Cyan
    Write-Host "  Postgres: db=egisz_reports user=egisz pass=egisz (in cluster: postgres:5432)" -ForegroundColor White
    Write-Host '  Metabase: admin@egisz.local / egisz  (MB_PASSWORD_COMPLEXITY=weak; dashboards via provision.sh in metabase pod logs)' -ForegroundColor White
    Write-Host "  Firebird: host.docker.internal:3050 in k8s\local\egisz_corp.yaml (rebuild conf-ui image for libfbclient)" -ForegroundColor White
    Write-Host "  Standard ports on PC:  .\start.ps1 -Action web   (optional: web -BackgroundPortForward, no extra PS windows)" -ForegroundColor White
    Write-Host ""
    Write-Host "ETL: kubectl -n egisz-corp exec -it deploy/conf-ui -- egisz-corp sync" -ForegroundColor Yellow
    Write-Banner "Complete" Green
}

switch ($Action) {
    "help" { Show-Help }
    "build" { Invoke-DockerBuild }
    "compose-up" { Invoke-CorpComposeUp }
    "compose-down" { Invoke-CorpComposeDown }
    "test" { Invoke-PythonTests }
    "apply" {
        Warn-IfNestedUnderSiblingMonitor
        Ensure-LocalKubernetesCluster
        Invoke-KindLoadImagesIfNeeded
        Invoke-KubectlApply
        Show-DeployInfo
        Invoke-K8sSmokeTests
        Invoke-CorpPortForwardIfRequestedAfterK8s -BackgroundSwitchPresent:$PSBoundParameters.ContainsKey('BackgroundPortForward') -BackgroundEnabled:$BackgroundPortForward
    }
    "status" {
        kubectl -n egisz-corp get pods,svc -o wide
        Show-K8sNetworkLegend
    }
    "web" {
        Warn-IfNestedUnderSiblingMonitor
        Invoke-CorpWebPortForward
    }
    "forward" {
        Warn-IfNestedUnderSiblingMonitor
        Invoke-CorpWebPortForward
    }
    "stop-forward" {
        Warn-IfNestedUnderSiblingMonitor
        Invoke-CorpStopPortForward
    }
    "deploy" {
        Warn-IfNestedUnderSiblingMonitor
        Write-Banner 'egisz-monitor-corp K8s deploy (local)'
        Ensure-LocalKubernetesCluster
        Invoke-DockerBuild
        Invoke-KindLoadImagesIfNeeded
        Invoke-KubectlApply
        Show-DeployInfo
        Invoke-K8sSmokeTests
        Invoke-CorpPortForwardIfRequestedAfterK8s -BackgroundSwitchPresent:$PSBoundParameters.ContainsKey('BackgroundPortForward') -BackgroundEnabled:$BackgroundPortForward
    }
    "reset-deploy" {
        Warn-IfNestedUnderSiblingMonitor
        Write-Banner 'egisz-monitor-corp K8s reset-deploy (clean namespace)'
        Ensure-LocalKubernetesCluster
        Invoke-DockerBuild
        Invoke-KindLoadImagesIfNeeded
        Invoke-KubectlApply -ResetNamespace
        Show-DeployInfo
        Invoke-K8sSmokeTests
        Invoke-CorpPortForwardIfRequestedAfterK8s -BackgroundSwitchPresent:$PSBoundParameters.ContainsKey('BackgroundPortForward') -BackgroundEnabled:$BackgroundPortForward
    }
}
