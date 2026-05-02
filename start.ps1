#!/usr/bin/env powershell
# Local full stack in Kubernetes (namespace egisz-monitor). Firebird stays on Windows host.
# Requires: Docker, kubectl; optional: kind (https://kind.sigs.k8s.io/) for auto cluster create.
#
# deploy (-Action deploy): kind cluster egisz-local if needed, docker build (conf-ui + Metabase), load images,
# apply manifests, schema for egisz_reports, airflow DB; DROP/CREATE �� ���������� Metabase (metabase) + provision ���������;
# rollout restart; verify. Does not clear Firebird / ETL log export state.
# �� ��������� ��� ����������: ��� apply � ��� ������ �� Metabase; ���������� ������ Config UI + kubectl apply ���������� ����������/��������.
# apply: Config UI (Flask) + kubectl apply; does not reset Metabase app DB. Always rollout restart conf-ui and Metabase. Metabase DB reset only on deploy / reset-deploy / reset-metabase.
# apply-rebuild: �� ��, ��� apply, �� docker build conf-ui ������ � --no-cache (������� �����).

param(
    [ValidateSet("deploy", "reset-deploy", "reset-metabase", "build", "apply", "apply-rebuild", "start", "restart-metabase", "restart-conf-ui", "restart-web", "status", "verify", "web", "forward", "stop-forward", "metabase-provision-local", "test", "help")]
    [string]$Action = "apply",
    [switch]$SkipKindCluster,
    # With -Action web / forward: do not bind localhost:5432 (skip if another Postgres already uses 5432).
    [switch]$SkipPostgresPortForward,
    # With -Action web | forward | deploy | apply | apply-rebuild | start | reset-deploy | verify: pass -BackgroundPortForward:$false to open separate PowerShell windows for each kubectl port-forward instead of hidden background kubectl.
    [switch]$BackgroundPortForward,
    # With deploy / apply / apply-rebuild / start / reset-deploy / verify: also forward Postgres to localhost:5432 (��� -Action web ��� ConfAndMetabaseOnly). �� ��������� ������ 8080+3000, ����� �� ������������� � ��������� Postgres.
    [switch]$IncludePostgresPortForward,
    # With -Action build or apply: docker build --no-cache (build: conf-ui + Metabase; apply: conf-ui only). apply-rebuild ������ --no-cache ��� conf-ui.
    [switch]$DockerNoCache
)

$ErrorActionPreference = "Stop"
# PS 7.3+ ��� $ErrorActionPreference=Stop ������������ stderr �������� ������ (docker build,
# kubectl exec, etc.) � terminating error, ���� ���� exit-code = 0. ���������������� buildkit
# ����� �#0 building with desktop-linux� � stderr � ��� �������. ��������� ������: ���� ���
# ��������� $LASTEXITCODE ����� ������ �������� �������.
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$Root = $PSScriptRoot
Set-Location $Root

# ���������� ������ �������� ������� (docker, kubectl, kind): ��������� ��������� stderr
# � RemoteException �� PS 5.1 + $ErrorActionPreference=Stop. ���������� exit-code � $LASTEXITCODE.
# �������������: Invoke-Native docker build @nc -f docker/web/Dockerfile -t egisz-conf-ui:latest $Root
function Invoke-Native {
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # 2>&1 ���������� stderr � �������� pipeline; ErrorRecord-������� �������� � ������� �����
        # ToString() � ����� Write-Host ���������� �System.Management.Automation.RemoteException�.
        & $args[0] $args[1..($args.Count - 1)] 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                [Console]::Error.WriteLine($_.Exception.Message)
            } else {
                Write-Host $_
            }
        }
    } finally {
        $ErrorActionPreference = $prevEAP
    }
}

$script:KindClusterName = "egisz-local"

function Write-NestedSiblingMonitorWarning {
    $leaf = Split-Path $Root -Leaf
    $parent = Split-Path $Root -Parent
    if (-not $parent) { return }
    $parentLeaf = Split-Path $parent -Leaf
    if ($leaf -eq "egisz-monitor-corp" -and $parentLeaf -eq "egisz-monitor") {
        Write-Warning ("Clone is nested under {0}. Use a standalone folder, e.g. C:\Users\...\egisz-monitor-corp, so Docker paths and IDE roots are not mixed with the other repo." -f $parent)
    }
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
    Write-Host "`[test] OK" -ForegroundColor Green
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

  apply | start (default)   ��� ���������� = ���� �����: kind (if needed) + docker build ������ Config UI + kubectl apply + �����/������� �� �����������; �� Metabase � Postgres �� ������������; rollout Metabase+conf-ui + smoke + verify + port-forward 8080/3000 (+ �������). ����� start �� �� �����.
  deploy            kind + docker build (conf-ui + Metabase) + kubectl apply + DB schema + DROP/CREATE �� Metabase (metabase) + provision ��������� + restart + verify + port-forward 8080/3000 + browser � ������������ ��� �������� Metabase ��� ��������� ������� ��������� ��� �������������
  reset-deploy      remove legacy Compose Postgres volume if present; delete namespace egisz-monitor; docker build --no-cache; then ��� deploy (������� ����� �� Metabase) + smoke + verify + port-forward
  reset-metabase    ������ DROP/CREATE �� Metabase (��� ������� deploy). ������� egisz_reports �� ���������. ���� ������ JSON ���������: ������� .\start.ps1 -Action build
  build             docker build only (K8s images); use -DockerNoCache for --no-cache
  apply-rebuild     ��� apply, �� conf-ui ������ � docker build --no-cache (���������� apply -DockerNoCache)
  restart-metabase  ������ kubectl rollout restart deployment/metabase + �������� Ready (����� ��� � ��������; ����� build � apply ��� ���� action).
  restart-conf-ui   ������ rollout restart deployment/conf-ui + �������� Ready.
  restart-web       rollout restart Metabase � conf-ui + �������� ����� (��� docker build � ��� apply ����������).
  metabase-provision-local  docker build Metabase + setup-dashboards.sh � Metabase �� localhost:3000 (��. metabase/provision-local.ps1 - ���������)
  status            kubectl get pods,svc -n egisz-monitor
  web | forward     port-forward only (������ �� �������). ����� ������ UI: .\start.ps1 ��� -Action apply (��� build + deploy ��� ����� Metabase)
  stop-forward      stop background kubectl port-forwards (saved PIDs from web/forward or deploy/apply/reset-deploy)
  test              pip install -e ".[dev]" && pytest
  verify            ������ �������� � ��������: Postgres (�������) + Metabase (�������� � ����� ������ ���������); kubectl exec � ��� metabase (��. metabase/verify-corp-stack.sh)
  help

  SkipPostgresPortForward   only with web/forward: omit Postgres forward if port 5432 is busy on the host
  BackgroundPortForward     port-forward runs in background by default for web|forward|deploy|apply|apply-rebuild|start|reset-deploy|verify; pass -BackgroundPortForward:$false for separate kubectl windows

Parameters:
  -SkipKindCluster         do not run kind create; kubectl cluster-info must work
  -DockerNoCache           with -Action build or apply: pass --no-cache to docker build (conf-ui; ��� build ��� � Metabase). apply-rebuild ������ ��� ���� ��� conf-ui.
  -IncludePostgresPortForward   with deploy / apply / apply-rebuild / start / reset-deploy / verify: also forward Postgres to localhost:5432 (�� ��������� ������ 8080+3000)
  (deploy/apply/start/apply-rebuild/reset-deploy: kubectl port-forward 8080+3000 on localhost before smoke/verify; + Postgres: -IncludePostgresPortForward or .\start.ps1 -Action web. Foreground kubectl windows: -BackgroundPortForward:$false)

����� ����������� Docker Desktop ���� � namespace ����� ����������� ����; ������ ��������� .\start.ps1 ��� port-forward �� localhost. �����������: ����������� ������� Windows � ������ .\start.ps1 ��� ����� � �������, ���� ����� �������������� ������� ������.

K8s from your PC (Docker Desktop / kind): after deploy/apply/start, start.ps1 runs port-forward so http://127.0.0.1:8080 and :3000 work. Web Services use LoadBalancer where supported (Docker Desktop often exposes :8080 / :3000 on localhost directly).

Generated each deploy/apply (under k8s\):
  Postgres: database egisz_reports, user egisz, password egisz
  Metabase: admin@egisz.local / egisz  (MB_PASSWORD_COMPLEXITY=weak in k8s/metabase.yaml)
  Web config file: k8s\local\egisz_monitor.yaml (Firebird: host.docker.internal)

Edit k8s\local\egisz_monitor.yaml for your Firebird alias and credentials on Windows.
'@
}

function Invoke-DockerBuildConfUi {
    param([switch]$DockerNoCache)
    $nc = @()
    if ($DockerNoCache) {
        $nc = @("--no-cache")
        Write-Host "`[Docker] Building Config UI with --no-cache..." -ForegroundColor Yellow
    }
    Write-Host "`[Docker] Building egisz-conf-ui (Config UI)..." -ForegroundColor Yellow
    Invoke-Native docker build @nc -f docker/web/Dockerfile -t egisz-conf-ui:latest $Root
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Invoke-Native docker tag egisz-conf-ui:latest egisz-conf-ui:sync-web
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Write-Host "`[Docker] egisz-conf-ui OK" -ForegroundColor Green
}

function Invoke-DockerBuild {
    param([switch]$DockerNoCache)
    Invoke-DockerBuildConfUi -DockerNoCache:$DockerNoCache
    Write-Host "`[Docker] Building egisz-monitor-metabase..." -ForegroundColor Yellow
    $nc = @()
    if ($DockerNoCache) {
        $nc = @("--no-cache")
    }
    Invoke-Native docker build @nc -f metabase/Dockerfile -t egisz-monitor-metabase:latest $Root
    if ($LASTEXITCODE -ne 0) { exit 1 }
    # :k8s-v15 + :local = ��� �� digest, ��� :latest. � k8s/metabase.yaml ����� � :k8s-v15 (bump v16� ��� ����� ��������/���������), ����� kubelet Docker Desktop ������ ������ digest ��� ����� ����.
    Invoke-Native docker tag egisz-monitor-metabase:latest egisz-monitor-metabase:k8s-v15
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Invoke-Native docker tag egisz-monitor-metabase:latest egisz-monitor-metabase:local
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Write-Host "`[Docker] OK" -ForegroundColor Green
}

function Test-KubectlResponds {
    # Do not use PowerShell-native kubectl here: with $ErrorActionPreference=Stop, stderr from kubectl
    # becomes a terminating error and we never reach kind create.
    cmd /c 'kubectl cluster-info --request-timeout=8s 1>nul 2>nul'
    return ($LASTEXITCODE -eq 0)
}

function Initialize-LocalKubernetesCluster {
    if (Test-KubectlResponds) {
        Write-Host "`[kubectl] Cluster API is reachable." -ForegroundColor Green
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
            Write-Host "`[kind] Using existing cluster $kindName." -ForegroundColor Green
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
    Write-Host "`[kind] Cluster $kindName is ready." -ForegroundColor Green
}

function Invoke-KindLoadImagesIfNeeded {
    $ctx = (cmd /c 'kubectl config current-context 2>nul' | Select-Object -First 1)
    if (-not $ctx) { return }
    $ctx = $ctx.Trim()
    if ($ctx -notmatch '^kind-') { return }
    $name = $ctx -replace '^kind-', ''
    Write-Host "`[kind] Loading local images into cluster $name..." -ForegroundColor Cyan
    kind load docker-image egisz-conf-ui:sync-web --name $name
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kind load docker-image egisz-monitor-metabase:latest --name $name
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kind load docker-image egisz-monitor-metabase:k8s-v15 --name $name
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kind load docker-image egisz-monitor-metabase:local --name $name
    if ($LASTEXITCODE -ne 0) { exit 1 }
    Write-Host "`[kind] Images loaded." -ForegroundColor Green
}

function New-LocalDeployArtifactFiles {
    $pg = @'
apiVersion: v1
kind: Secret
metadata:
  name: postgres-credentials
  namespace: egisz-monitor
type: Opaque
stringData:
  POSTGRES_USER: "egisz"
  POSTGRES_PASSWORD: "egisz"
  POSTGRES_DB: "egisz_reports"
'@
    $mb = @'
# Metabase UI / API ����������� (��������� � k8s/metabase-admin-secret.example.yaml).
apiVersion: v1
kind: Secret
metadata:
  name: metabase-admin
  namespace: egisz-monitor
type: Opaque
stringData:
  email: "admin@egisz.local"
  password: "egisz"
'@
    $pgPath = Join-Path $Root "k8s\postgres\postgres-credentials.yaml"
    $mbPath = Join-Path $Root "k8s\metabase-admin-secret.yaml"
    Write-Utf8NoBom $pgPath $pg
    Write-Utf8NoBom $mbPath $mb
    Write-Host "`[local] Wrote k8s\postgres\postgres-credentials.yaml and k8s\metabase-admin-secret.yaml" -ForegroundColor Green
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
    kubectl -n egisz-monitor get deploy conf-ui -o name 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { return }
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMddHHmmss")
    $img = "egisz-conf-ui:sync-web-$stamp"
    docker tag egisz-conf-ui:latest $img
    if ($LASTEXITCODE -ne 0) { return }
    kubectl -n egisz-monitor set image deployment/conf-ui "conf-ui=$img"
    if ($LASTEXITCODE -ne 0) { return }
    kubectl -n egisz-monitor get cronjob egisz-monitor-sync -o name 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        kubectl -n egisz-monitor set image cronjob/egisz-monitor-sync "sync=$img"
        if ($LASTEXITCODE -eq 0) {
            Write-Host "`[docker-desktop] cronjob/egisz-monitor-sync sync -> $img (aligned with conf-ui)" -ForegroundColor DarkGray
        }
    }
    Write-Host "`[docker-desktop] conf-ui -> $img (local image digest refresh)" -ForegroundColor DarkGray
}

function Invoke-ResetMetabaseApplicationDatabase {
    <#
      Metabase ������ UI/�������� � Postgres (�� metabase). ���������� deployment �� ���������� � �
      ��� �������� Metabase ����� �������� ��� �� (������� egisz_reports �� �������������).
      -AsDeployStep: ���������� �� deploy/reset-deploy (��� ������� ������� � ��� ���������� rollout status �����).
    #>
    param(
        [switch]$AsDeployStep
    )
    $ns = "egisz-monitor"
    if (-not $AsDeployStep) {
        Write-NestedSiblingMonitorWarning
        Write-Banner "reset-metabase (application DB only)" Cyan
    } else {
        Write-Host "`[kubectl] ����� �� ���������� Metabase (DROP/CREATE metabase � Postgres)..." -ForegroundColor Cyan
    }
    kubectl -n $ns get deploy metabase -o name 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: deployment/metabase not found in $ns. Run deploy first." -ForegroundColor Red
        exit 1
    }
    kubectl -n $ns get secret postgres-credentials -o name 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: secret postgres-credentials not found." -ForegroundColor Red
        exit 1
    }
    $encUser = kubectl -n $ns get secret postgres-credentials -o jsonpath="{.data.POSTGRES_USER}"
    if (-not $encUser) {
        Write-Host "ERROR: POSTGRES_USER missing in postgres-credentials." -ForegroundColor Red
        exit 1
    }
    $pgUser = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($encUser))
    $pgPod = kubectl -n $ns get pods -l app.kubernetes.io/name=postgres -o jsonpath="{.items[0].metadata.name}" 2>$null
    if (-not $pgPod) {
        Write-Host "ERROR: no pod with label app.kubernetes.io/name=postgres in $ns." -ForegroundColor Red
        exit 1
    }
    Write-Host "`[kubectl] Scaling deployment/metabase to 0..." -ForegroundColor Cyan
    kubectl -n $ns scale deployment/metabase --replicas=0
    if ($LASTEXITCODE -ne 0) { exit 1 }
    $deadline = (Get-Date).AddMinutes(4)
    $prevEaLoop = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        while ((Get-Date) -lt $deadline) {
            # kubectl prints "No resources found" to stderr when replica set is 0; with $ErrorActionPreference=Stop that would abort.
            $lines = kubectl -n $ns get pods -l app.kubernetes.io/name=metabase --no-headers 2>$null
            if (-not $lines) { break }
            Start-Sleep -Seconds 2
        }
    } finally {
        $ErrorActionPreference = $prevEaLoop
    }
    Write-Host "`[kubectl] Dropping database metabase on pod $pgPod (owner $pgUser)..." -ForegroundColor Cyan
    # Single-quoted here-string: in @"..."@ PowerShell would parse pg_backend_pid() as a function call.
    $sql = @'
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'metabase' AND pid <> pg_backend_pid();
DROP DATABASE IF EXISTS metabase;
CREATE DATABASE metabase OWNER 
'@ + $pgUser + ';'
    # psql NOTICE (e.g. database does not exist) goes to stderr; PS 7 + ErrorAction Stop treats it as terminating.
    $prevEaSql = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $sql | kubectl -n $ns exec -i $pgPod -- psql -U $pgUser -d postgres -v ON_ERROR_STOP=1
    } finally {
        $ErrorActionPreference = $prevEaSql
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: psql DROP/CREATE metabase failed (see above)." -ForegroundColor Red
        exit 1
    }
    Write-Host "`[kubectl] Scaling deployment/metabase to 1 (migrations + provision from image)..." -ForegroundColor Cyan
    kubectl -n $ns scale deployment/metabase --replicas=1
    if ($LASTEXITCODE -ne 0) { exit 1 }
    if (-not $AsDeployStep) {
        kubectl -n $ns rollout status deployment/metabase --timeout=600s
        if ($LASTEXITCODE -ne 0) {
            Write-Host "WARN: rollout status timed out; check: kubectl -n $ns get pods -l app.kubernetes.io/name=metabase" -ForegroundColor Yellow
        }
        Write-Host 'Done. Metabase UI: http://127.0.0.1:3000/ - dashboards 01-10 in Personal collection after provision (1-5 min).' -ForegroundColor Green
        Write-Host "Logs: kubectl -n $ns logs deploy/metabase --tail=80" -ForegroundColor Gray
    }
}

function Invoke-CorpRolloutRestartMetabaseOnly {
    $ns = "egisz-monitor"
    kubectl -n $ns get deploy metabase -o name 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: deployment/metabase not found in $ns." -ForegroundColor Red
        exit 1
    }
    Write-Host "`[kubectl] rollout restart deployment/metabase..." -ForegroundColor Cyan
    kubectl -n $ns rollout restart deployment/metabase
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

function Invoke-CorpRolloutRestartConfUiOnly {
    $ns = "egisz-monitor"
    kubectl -n $ns get deploy conf-ui -o name 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: deployment/conf-ui not found in $ns." -ForegroundColor Red
        exit 1
    }
    Write-Host "`[kubectl] rollout restart deployment/conf-ui..." -ForegroundColor Cyan
    kubectl -n $ns rollout restart deployment/conf-ui
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

function Wait-CorpMetabaseRollout {
    Write-Host "`[kubectl] Waiting for Metabase (up to 10m)..." -ForegroundColor Cyan
    kubectl -n egisz-monitor rollout status deployment/metabase --timeout=600s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: Metabase not Ready in 10m. Check: kubectl -n egisz-monitor logs deploy/metabase --tail=80" -ForegroundColor Yellow
    }
}

function Wait-CorpConfUiRollout {
    Write-Host "`[kubectl] Waiting for conf-ui (up to 3m)..." -ForegroundColor Cyan
    kubectl -n egisz-monitor rollout status deployment/conf-ui --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: conf-ui not Ready in 3m. Check: kubectl -n egisz-monitor describe deploy/conf-ui" -ForegroundColor Yellow
    }
}

function Invoke-CorpRolloutRestartMetabaseAndConfUi {
    # ����� docker build + kind load / docker-desktop ����� :local � conf-ui ��� �� ����, �� ��� ������ ����
    # kubelet ����� ������� ������ ����. ���������� deployment (��� ��������� k8s) ����������� ����� � ����� ������� � provision.sh.
    # �� ������� Firebird, etl_state � �������� ����� FB � ������ web-���� � namespace egisz-monitor.
    param(
        [switch]$SkipMetabase
    )
    $ns = "egisz-monitor"
    if (-not $SkipMetabase) {
        kubectl -n $ns get deploy metabase -o name 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "`[kubectl] rollout restart deployment/metabase (����� �����, ����������� ��� ������ ����)..." -ForegroundColor Cyan
            kubectl -n $ns rollout restart deployment/metabase
            if ($LASTEXITCODE -ne 0) { exit 1 }
        }
    }
    kubectl -n $ns get deploy conf-ui -o name 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "`[kubectl] rollout restart deployment/conf-ui (����� ����� Config UI)..." -ForegroundColor Cyan
        kubectl -n $ns rollout restart deployment/conf-ui
        if ($LASTEXITCODE -ne 0) { exit 1 }
    }
}

function Invoke-PostgresEnsureAppRole {
    Write-Host "`[kubectl] Postgres: ���� egisz � �� �� Secret (��������� �role egisz does not exist� �� ������ ����)..." -ForegroundColor Cyan
    $pgPod = kubectl -n egisz-monitor get pods -l app.kubernetes.io/name=postgres -o jsonpath="{.items[0].metadata.name}" 2>$null
    if (-not $pgPod) {
        Write-Host "ERROR: pod postgres � egisz-monitor �� ������." -ForegroundColor Red
        exit 1
    }
    $shPath = Join-Path $Root "k8s\postgres\ensure-postgres-app-role.sh"
    if (-not (Test-Path $shPath)) {
        Write-Host "ERROR: ��� ����� $shPath" -ForegroundColor Red
        exit 1
    }
    # CRLF � .sh ��� � bash �invalid option name� � `set -o pipefail`. ����������� LF � ��� UTF-8 ��� BOM ����� stdin ������
    # (���� ������ �� PowerShell 5.1 ����� ��� UTF-16 � ������ bash).
    $tmpSh = Join-Path ([System.IO.Path]::GetTempPath()) ("egisz-ensure-role-{0}.sh" -f [guid]::NewGuid().ToString("n"))
    try {
        $shRaw = [System.IO.File]::ReadAllText($shPath)
        $shRaw = $shRaw -replace "`r`n", "`n" -replace "`r", "`n"
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
        [System.IO.File]::WriteAllText($tmpSh, $shRaw, $utf8NoBom)
        $kubectlExe = (Get-Command kubectl).Source
        $proc = Start-Process -FilePath $kubectlExe `
            -ArgumentList @('-n', 'egisz-monitor', 'exec', '-i', $pgPod, '--', 'bash', '-s') `
            -RedirectStandardInput $tmpSh `
            -Wait -PassThru -NoNewWindow
        if ($null -eq $proc -or $proc.ExitCode -ne 0) {
            Write-Host "ERROR: ensure-postgres-app-role.sh ���������� � �������." -ForegroundColor Red
            exit 1
        }
    } finally {
        Remove-Item -LiteralPath $tmpSh -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-PostgresSchemaInit {
    Write-Host "`[kubectl] ConfigMap + Job: apply warehouse DDL (sql/schema_apply_order.txt)..." -ForegroundColor Cyan
    $orderFile = Join-Path $Root "sql\schema_apply_order.txt"
    if (-not (Test-Path $orderFile)) {
        Write-Host "ERROR: Missing $orderFile" -ForegroundColor Red
        exit 1
    }
    $names = New-Object System.Collections.Generic.List[string]
    Get-Content -LiteralPath $orderFile -Encoding UTF8 | ForEach-Object {
        $parts = $_ -split '#', 2
        $line = $parts[0].Trim()
        if ($line.Length -gt 0) {
            [void]$names.Add($line)
        }
    }
    if ($names.Count -eq 0) {
        Write-Host "ERROR: No SQL filenames in schema_apply_order.txt" -ForegroundColor Red
        exit 1
    }
    foreach ($n in $names) {
        $p = Join-Path $Root "sql\$n"
        if (-not (Test-Path $p)) {
            Write-Host "ERROR: Listed in schema_apply_order.txt but file missing: $p" -ForegroundColor Red
            exit 1
        }
    }
    kubectl -n egisz-monitor delete configmap/egisz-reports-schema --ignore-not-found
    if ($LASTEXITCODE -ne 0) { exit 1 }
    $kubectlArgs = @(
        '-n', 'egisz-monitor', 'create', 'configmap', 'egisz-reports-schema',
        "--from-file=schema_apply_order.txt=$orderFile"
    )
    foreach ($n in $names) {
        $p = Join-Path $Root "sql\$n"
        $kubectlArgs += "--from-file=${n}=$p"
    }
    & kubectl $kubectlArgs
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kubectl -n egisz-monitor delete job/egisz-reports-schema-init --ignore-not-found
    kubectl apply -f (Join-Path $Root "k8s\postgres\egisz-reports-schema-job.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    if (-not (Wait-KubectlJobSucceeded -Namespace egisz-monitor -JobName egisz-reports-schema-init -TimeoutSec 300)) {
        Write-Host "ERROR: Schema job did not succeed. Logs: kubectl -n egisz-monitor logs job/egisz-reports-schema-init" -ForegroundColor Red
        exit 1
    }
    Write-Host "`[kubectl] DWH schema applied." -ForegroundColor Green
}

function Invoke-PostgresAirflowDbInit {
    $jobFile = Join-Path $Root "k8s\postgres\airflow-metadata-init-job.yaml"
    Write-Host "`[kubectl] Job: create database airflow..." -ForegroundColor Cyan
    kubectl -n egisz-monitor delete job/airflow-metadata-db-init --ignore-not-found
    kubectl apply -f $jobFile
    if ($LASTEXITCODE -ne 0) { exit 1 }
    if (-not (Wait-KubectlJobSucceeded -Namespace egisz-monitor -JobName airflow-metadata-db-init -TimeoutSec 300)) {
        Write-Host "ERROR: Airflow DB job did not succeed. Logs: kubectl -n egisz-monitor logs job/airflow-metadata-db-init" -ForegroundColor Red
        exit 1
    }
    Write-Host "`[kubectl] Database airflow ready." -ForegroundColor Green
}

function Invoke-WebConfigSecret {
    $cfg = Join-Path $Root "k8s\local\egisz_monitor.yaml"
    if (-not (Test-Path $cfg)) {
        Write-Host "ERROR: Missing $cfg" -ForegroundColor Red
        exit 1
    }
    Write-Host "`[kubectl] Secret egisz-monitor-conf-ui-config from k8s\local\egisz_monitor.yaml..." -ForegroundColor Cyan
    kubectl -n egisz-monitor create secret generic egisz-monitor-conf-ui-config `
        --from-file="egisz_monitor.yaml=$cfg" `
        --dry-run=client -o yaml | kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

function Invoke-RemoveLegacyComposePostgresVolume {
    # ����� ������ ����������� docker-compose � ����� egisz_monitor_corp_postgres_data � �������, ���� �������.
    $vol = "egisz_monitor_corp_postgres_data"
    cmd /c "docker volume inspect $vol 1>nul 2>nul"
    if ($LASTEXITCODE -ne 0) { return }
    Write-Host "`[docker] Removing legacy Compose volume $vol (standalone Postgres removed from project)..." -ForegroundColor Yellow
    cmd /c "docker volume rm $vol 2>nul"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: could not remove volume $vol (stop containers using it: docker volume ls)." -ForegroundColor Yellow
    } else {
        Write-Host "`[docker] Volume $vol removed." -ForegroundColor Green
    }
}

function Invoke-ResetK8sNamespace {
    Write-Host "`[kubectl] Full reset: deleting namespace egisz-monitor..." -ForegroundColor Cyan
    kubectl delete namespace egisz-monitor --ignore-not-found
    $maxWait = 72
    for ($i = 0; $i -lt $maxWait; $i++) {
        cmd /c 'kubectl get namespace egisz-monitor -o name 1>nul 2>nul'
        if ($LASTEXITCODE -ne 0) {
            Write-Host "`[kubectl] Namespace egisz-monitor is gone." -ForegroundColor Green
            return
        }
        Start-Sleep -Seconds 5
    }
    Write-Host "WARN: namespace egisz-monitor still exists after wait; continuing apply may fail." -ForegroundColor Yellow
}

function Invoke-KubectlApply {
    param(
        [switch]$ResetNamespace,
        # deploy / reset-deploy: DROP/CREATE �� metabase ����� apply Metabase (������� �� ���������). apply � ��� ����� �����.
        [switch]$ResetMetabaseAppDb
    )

    Initialize-LocalKubernetesCluster
    if (-not (Test-KubectlResponds)) {
        Write-Host "ERROR: kubectl cluster-info failed." -ForegroundColor Red
        exit 1
    }

    if ($ResetNamespace) {
        Invoke-ResetK8sNamespace
    }

    New-LocalDeployArtifactFiles

    Write-Host "`[kubectl] Namespace..." -ForegroundColor Cyan
    kubectl apply -f (Join-Path $Root "k8s\postgres\namespace.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }

    kubectl apply -f (Join-Path $Root "k8s\postgres\postgres-credentials.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kubectl apply -f (Join-Path $Root "k8s\postgres\postgres-statefulset.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kubectl apply -f (Join-Path $Root "k8s\postgres\postgres-service.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }

    Write-Host "`[kubectl] Waiting for Postgres (up to 10m, first PVC bind can be slow)..." -ForegroundColor Cyan
    kubectl -n egisz-monitor rollout status statefulset/postgres --timeout=600s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: StatefulSet postgres not Ready." -ForegroundColor Red
        Write-Host "--- kubectl -n egisz-monitor describe pod -l app.kubernetes.io/name=postgres ---" -ForegroundColor Yellow
        cmd /c 'kubectl -n egisz-monitor describe pod -l app.kubernetes.io/name=postgres 2>&1'
        Write-Host "--- kubectl -n egisz-monitor get events (last 25) ---" -ForegroundColor Yellow
        cmd /c 'kubectl -n egisz-monitor get events --sort-by=.lastTimestamp 2>&1' | Select-Object -Last 25
        exit 1
    }

    Invoke-PostgresEnsureAppRole

    Invoke-PostgresSchemaInit
    Invoke-PostgresAirflowDbInit

    kubectl apply -f (Join-Path $Root "k8s\metabase-admin-secret.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }
    kubectl apply -f (Join-Path $Root "k8s\metabase.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }

    if ($ResetMetabaseAppDb) {
        Invoke-ResetMetabaseApplicationDatabase -AsDeployStep
    }

    Invoke-WebConfigSecret

    kubectl apply -f (Join-Path $Root "k8s\conf-ui.yaml")
    if ($LASTEXITCODE -ne 0) { exit 1 }

    # CronJob (k8s/etl-cron.yaml): по умолчанию suspend:true — kubectl patch при включении.
    $cronYaml = Join-Path $Root "k8s\etl-cron.yaml"
    if (Test-Path $cronYaml) {
        kubectl apply -f $cronYaml
        if ($LASTEXITCODE -ne 0) { exit 1 }
        Write-Host "`[kubectl] cronjob/egisz-monitor-sync: по умолчанию suspend=true (см. k8s/etl-cron.yaml и auto_sync в конфиге)." -ForegroundColor DarkGray
    }

    Publish-ConfUiImageToDockerDesktopK8s

    if ($ResetMetabaseAppDb) {
        Invoke-CorpRolloutRestartMetabaseAndConfUi -SkipMetabase
    } else {
        Invoke-CorpRolloutRestartMetabaseAndConfUi
    }
    Wait-CorpMetabaseRollout
    Wait-CorpConfUiRollout

    Write-Host "`[kubectl] Apply finished." -ForegroundColor Green
}

function Invoke-ConfUiFirebirdDriverSelfTest {
    cmd /c 'kubectl -n egisz-monitor get deploy conf-ui -o name 1>nul 2>nul'
    if ($LASTEXITCODE -ne 0) { return }
    Write-Host "`[verify] conf-ui pod: Firebird client library + firebird.driver..." -ForegroundColor Cyan
    # -c conf-ui: ��������� �Defaulted container � out of: conf-ui, seed-config (init)� �� kubectl
    # �� stderr � ����� PowerShell � $ErrorActionPreference=Stop ������ �� ���������� NOTICE.
    # Avoid nested " inside -c: Windows kubectl often truncates argv and breaks print("...").
    kubectl -n egisz-monitor exec deploy/conf-ui -c conf-ui -- python -c "from firebird.driver import fbapi; fbapi.load_api()"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Firebird driver check in conf-ui failed (see docker/web/Dockerfile libfbclient2)." -ForegroundColor Red
        exit 1
    }
    Write-Host "`[verify] conf-ui Firebird driver OK (libfbclient loaded; sync uses k8s/local/egisz_monitor.yaml in Secret)." -ForegroundColor Green
}

function Test-MetabaseVerifyFatalOutput {
    param([int]$ExitCode, [string]$Text)
    if ($ExitCode -eq 2 -or $ExitCode -eq 3) { return $true }
    if ($Text -match '(?i)jq:\s+error') { return $true }
    if ($Text -match '(?i)compile error') { return $true }
    return $false
}

function Invoke-MetabaseVerifyCorpStackInPod {
    # ��� ����� � pipe �� Out-String � ����� � Windows PowerShell �������� ��� ������ kubectl.
    $prevEa = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $lines = @(kubectl -n egisz-monitor exec deploy/metabase -- /bin/bash /app/verify-corp-stack.sh 2>&1)
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEa
    if ($lines.Count -eq 0) {
        $out = ""
    } else {
        $out = ($lines | ForEach-Object { "$_" }) -join "`n"
    }
    @{ Code = $code; Out = $out }
}

function Invoke-K8sCorpStackVerify {
    Write-Banner "Full stack verify (Postgres DWH + Metabase EGISZ)"
    if (-not (Test-KubectlResponds)) {
        Write-Host "ERROR: kubectl cluster-info failed." -ForegroundColor Red
        exit 1
    }
    cmd /c 'kubectl -n egisz-monitor get deploy metabase 1>nul 2>nul'
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: deployment/metabase not found in egisz-monitor. Run deploy first." -ForegroundColor Red
        exit 1
    }
    $max = 90
    $maxRecovery = 60
    $prevEaVerify = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $verifyOk = $false
    for ($i = 0; $i -lt $max; $i++) {
        $r = Invoke-MetabaseVerifyCorpStackInPod
        if ($r.Code -eq 0) {
            $okOut = $r.Out.TrimEnd()
            if ($okOut) { Write-Host $okOut }
            $verifyOk = $true
            break
        }
        $trimmed = $r.Out.TrimEnd()
        if ($trimmed) { Write-Host $trimmed }
        if (Test-MetabaseVerifyFatalOutput -ExitCode $r.Code -Text $r.Out) {
            $ErrorActionPreference = $prevEaVerify
            Write-Host "ERROR: verify: ��������� ������ (jq/������ � ������); ������� �� �������. ������������ ����� Metabase: .\start.ps1 -Action build, ����� apply ��� rollout restart deployment/metabase." -ForegroundColor Red
            exit 1
        }
        Write-Host ("`[verify] attempt {0}/{1} failed; retry in 10s (provision may still be running)..." -f ($i + 1), $max) -ForegroundColor Yellow
        Start-Sleep -Seconds 10
    }
    if ($verifyOk) {
        $ErrorActionPreference = $prevEaVerify
        Write-Host "`[verify] OK (Postgres tables + Metabase dashboards in personal collection root)" -ForegroundColor Green
        Write-Host "  Metabase: open Personal collection in sidebar - dashboards are on that page." -ForegroundColor DarkGray
        Invoke-ConfUiFirebirdDriverSelfTest
        return
    }
    Write-Host "`[verify] Checks still failing (stale pod or dashboards mismatch vs JSON in image)." -ForegroundColor Yellow
    Write-Host "`[verify] rollout restart Metabase + conf-ui (does not clear Firebird or etl_state); waiting Ready..." -ForegroundColor Cyan
    Invoke-CorpRolloutRestartMetabaseAndConfUi
    kubectl -n egisz-monitor rollout status deployment/metabase --timeout=600s
    if ($LASTEXITCODE -ne 0) {
        $ErrorActionPreference = $prevEaVerify
        Write-Host "ERROR: Metabase not Ready after recovery restart." -ForegroundColor Red
        exit 1
    }
    kubectl -n egisz-monitor rollout status deployment/conf-ui --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARN: conf-ui not Ready after recovery; Metabase verify will still run." -ForegroundColor Yellow
    }
    $verifyOk = $false
    for ($j = 0; $j -lt $maxRecovery; $j++) {
        $r = Invoke-MetabaseVerifyCorpStackInPod
        if ($r.Code -eq 0) {
            $okOut = $r.Out.TrimEnd()
            if ($okOut) { Write-Host $okOut }
            $verifyOk = $true
            break
        }
        $trimmed = $r.Out.TrimEnd()
        if ($trimmed) { Write-Host $trimmed }
        if (Test-MetabaseVerifyFatalOutput -ExitCode $r.Code -Text $r.Out) {
            $ErrorActionPreference = $prevEaVerify
            Write-Host "ERROR: verify (����� recovery): ��������� ������; ��. ����. ������������ ����� Metabase (.\start.ps1 -Action build)." -ForegroundColor Red
            exit 1
        }
        Write-Host ("`[verify] recovery attempt {0}/{1} failed; retry in 10s..." -f ($j + 1), $maxRecovery) -ForegroundColor Yellow
        Start-Sleep -Seconds 10
    }
    if ($verifyOk) {
        $ErrorActionPreference = $prevEaVerify
        Write-Host "`[verify] OK after recovery restart (Postgres + Metabase dashboards)" -ForegroundColor Green
        Write-Host "  Metabase: open Personal collection in sidebar - dashboards are on that page." -ForegroundColor DarkGray
        Invoke-ConfUiFirebirdDriverSelfTest
        return
    }
    $ErrorActionPreference = $prevEaVerify
    Write-Host ("ERROR: verify failed after {0} attempts, recovery restart, then {1} more attempts. Logs: kubectl -n egisz-monitor logs deploy/metabase --tail=200" -f $max, $maxRecovery) -ForegroundColor Red
    exit 1
}

function Invoke-K8sSmokeTests {
    # HTTP из временного пода по in-cluster DNS сервисов (не с хоста Windows).
    Write-Banner "Smoke tests (in-cluster HTTP)"
    $ns = "egisz-monitor"
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
    Write-Host "Next: deploy/apply/reset-deploy always start port-forward to http://127.0.0.1:8080/ and :3000/ before smoke/verify (same as real use)." -ForegroundColor DarkGray
    Write-Host ""
}

function Show-K8sNetworkLegend {
    Write-Host ""
    Write-Host "==================================================================" -ForegroundColor Yellow
    Write-Host " From Windows: Services conf-ui and metabase use LoadBalancer." -ForegroundColor Yellow
    Write-Host " Docker Desktop often binds http://127.0.0.1:8080 and http://127.0.0.1:3000 directly." -ForegroundColor Yellow
    Write-Host " If LB stays Pending or ports conflict: .\start.ps1 -Action web (port-forward 8080 / 3000)." -ForegroundColor Yellow
    Write-Host "   .\start.ps1 -Action web -BackgroundPortForward:`$false   (visible kubectl windows)" -ForegroundColor DarkGray
    Write-Host "==================================================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Endpoints (ClusterIP is the virtual IP of the Service inside the cluster):" -ForegroundColor Cyan
    $rows = @(
        @{ Name = "conf-ui"; Dns = "conf-ui.egisz-monitor.svc.cluster.local"; SvcPort = 8080; HostHint = "http://127.0.0.1:8080/ (typical with Docker Desktop LB)" },
        @{ Name = "metabase"; Dns = "metabase.egisz-monitor.svc.cluster.local"; SvcPort = 3000; HostHint = "http://127.0.0.1:3000/ (typical with Docker Desktop LB)" },
        @{ Name = "postgres"; Dns = "postgres.egisz-monitor.svc.cluster.local"; SvcPort = 5432; HostHint = "127.0.0.1:30432 TCP (NodePort -> 5432)" }
    )
    foreach ($r in $rows) {
        $ip = (cmd /c ('kubectl -n egisz-monitor get svc ' + $r.Name + ' -o jsonpath={.spec.clusterIP} 2>nul'))
        if ($null -eq $ip) { $ip = "" }
        $ip = $ip.Trim()
        if (-not $ip) { $ip = "(no Service or namespace missing)" }
        Write-Host ("  {0,-10}  Cluster-IP: {1,-15}  in-cluster: {2}:{3}" -f $r.Name, $ip, $r.Dns, $r.SvcPort) -ForegroundColor White
        Write-Host ("  {0,-10}  from this PC:  {1}" -f "", $r.HostHint) -ForegroundColor DarkCyan
        Write-Host ""
    }
    Write-Host "If host URLs do not respond, use port-forward:" -ForegroundColor Yellow
    Write-Host "  kubectl -n egisz-monitor port-forward svc/conf-ui 8080:8080    -> http://127.0.0.1:8080/" -ForegroundColor Gray
    Write-Host "  kubectl -n egisz-monitor port-forward svc/metabase 3000:3000   -> http://127.0.0.1:3000/" -ForegroundColor Gray
    Write-Host "  (if 3000 is busy on the host, use e.g. 3001:3000 and open http://127.0.0.1:3001/ )" -ForegroundColor DarkGray
    Write-Host ""
}

function Get-CorpPortForwardPidFile {
    return (Join-Path $Root ".egisz-monitor-port-forward.pids")
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
        [bool]$BackgroundEnabled,
        # After deploy/apply: forward only conf-ui + Metabase (8080, 3000), not Postgres � avoids localhost:5432 clashes.
        [switch]$ConfAndMetabaseOnly,
        [switch]$SkipBrowserOpens
    )
    if ($BackgroundSwitchPresent -and -not $BackgroundEnabled) {
        Invoke-CorpWebPortForward -ConfAndMetabaseOnly:$ConfAndMetabaseOnly -SkipBrowserOpens:$SkipBrowserOpens
    } else {
        Invoke-CorpWebPortForward -ForceBackground -ConfAndMetabaseOnly:$ConfAndMetabaseOnly -SkipBrowserOpens:$SkipBrowserOpens
    }
}

function Invoke-CorpWebPortForward {
    param(
        [switch]$ForceBackground,
        [switch]$ConfAndMetabaseOnly,
        [switch]$SkipBrowserOpens
    )
    $useBackground = [bool]($ForceBackground -or $BackgroundPortForward)
    $forwardPostgres = (-not $ConfAndMetabaseOnly) -and (-not $SkipPostgresPortForward)
    Write-Banner "EGISZ Corp - standard ports (kubectl port-forward)"
    if (-not (Test-KubectlResponds)) {
        Write-Host "ERROR: kubectl cluster-info failed." -ForegroundColor Red
        exit 1
    }
    cmd /c 'kubectl -n egisz-monitor get deploy conf-ui metabase 1>nul 2>nul'
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: namespace egisz-monitor or deployments missing. Run: .\start.ps1 (apply) or .\start.ps1 -Action deploy for full images + Metabase DB reset." -ForegroundColor Red
        exit 1
    }
    $pfPg = "kubectl -n egisz-monitor port-forward svc/postgres 5432:5432"
    $pfConf = "kubectl -n egisz-monitor port-forward svc/conf-ui 8080:8080"
    $pfMeta = "kubectl -n egisz-monitor port-forward svc/metabase 3000:3000"

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
        if ($forwardPostgres) {
            cmd /c 'kubectl -n egisz-monitor get svc postgres 1>nul 2>nul'
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  Postgres -> localhost:5432" -ForegroundColor Gray
                $pp = Start-Process -FilePath $kubectlExe -ArgumentList @('-n', 'egisz-monitor', 'port-forward', 'svc/postgres', '5432:5432') -WindowStyle Hidden -PassThru
                if (-not $pp -or $pp.Id -lt 1) {
                    Write-Host "ERROR: could not start kubectl port-forward for postgres." -ForegroundColor Red
                    exit 1
                }
                [void]$startedPids.Add($pp.Id)
                Start-Sleep -Milliseconds 800
            } else {
                Write-Host "WARN: svc/postgres not found; skipping Postgres port-forward." -ForegroundColor Yellow
            }
        } elseif ($ConfAndMetabaseOnly) {
            Write-Host "Postgres port-forward skipped (deploy/apply default: conf-ui + Metabase only)." -ForegroundColor DarkGray
        } else {
            Write-Host "SkipPostgresPortForward: conf-ui + Metabase only." -ForegroundColor Yellow
        }
        Write-Host "  conf-ui -> http://127.0.0.1:8080/" -ForegroundColor Gray
        $pc = Start-Process -FilePath $kubectlExe -ArgumentList @('-n', 'egisz-monitor', 'port-forward', 'svc/conf-ui', '8080:8080') -WindowStyle Hidden -PassThru
        if (-not $pc -or $pc.Id -lt 1) {
            Write-Host "ERROR: could not start kubectl port-forward for conf-ui." -ForegroundColor Red
            exit 1
        }
        [void]$startedPids.Add($pc.Id)
        Start-Sleep -Milliseconds 500
        Write-Host "  Metabase -> http://127.0.0.1:3000/" -ForegroundColor Gray
        $pm = Start-Process -FilePath $kubectlExe -ArgumentList @('-n', 'egisz-monitor', 'port-forward', 'svc/metabase', '3000:3000') -WindowStyle Hidden -PassThru
        if (-not $pm -or $pm.Id -lt 1) {
            Write-Host "ERROR: could not start kubectl port-forward for metabase." -ForegroundColor Red
            exit 1
        }
        [void]$startedPids.Add($pm.Id)
        $pidFile = Get-CorpPortForwardPidFile
        Set-Content -LiteralPath $pidFile -Value (($startedPids | ForEach-Object { $_.ToString() }) -join "`n") -Encoding ascii
        Write-Host ("`[forward] PIDs saved to {0}" -f $pidFile) -ForegroundColor DarkGray
    } else {
        if ($forwardPostgres) {
            cmd /c 'kubectl -n egisz-monitor get svc postgres 1>nul 2>nul'
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
            if ($ConfAndMetabaseOnly) {
                Write-Host "Postgres port-forward skipped (conf-ui + Metabase only)." -ForegroundColor DarkGray
            } else {
                Write-Host "SkipPostgresPortForward: starting two windows (conf-ui + Metabase only)." -ForegroundColor Yellow
            }
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
    if (-not $SkipBrowserOpens) {
        Write-Host "Opening default browser..." -ForegroundColor Cyan
        Start-Process "http://127.0.0.1:8080/"
        Start-Sleep -Milliseconds 400
        Start-Process "http://127.0.0.1:3000/"
    }
    if ($useBackground) {
        Write-Host "Done. Port-forward runs in the background; stop: .\start.ps1 -Action stop-forward" -ForegroundColor Green
    } else {
        Write-Host "Done. Close the port-forward PowerShell windows when finished." -ForegroundColor Green
    }
}

function Show-DeployInfo {
    Write-Banner 'Services (namespace egisz-monitor)'
    $prevEa = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        kubectl -n egisz-monitor get pods,svc -o wide
    } finally {
        $ErrorActionPreference = $prevEa
    }
    Show-K8sNetworkLegend
    Write-Host "Credentials (local dev):" -ForegroundColor Cyan
    Write-Host "  Postgres: db=egisz_reports user=egisz pass=egisz (in cluster: postgres:5432)" -ForegroundColor White
    Write-Host "  Metabase: admin@egisz.local / egisz � �������� 01�10 � ����� Personal collection (-Action deploy ��� reset-metabase ���������� �� ���������� metabase)" -ForegroundColor White
    Write-Host "  Config UI: Flask via .\start.ps1 -Action apply; conf-ui only: restart-conf-ui; Metabase: restart-metabase / restart-web" -ForegroundColor White
    Write-Host "  Firebird: host.docker.internal:3050 � k8s\local\egisz_monitor.yaml" -ForegroundColor White
    Write-Host "  Standard ports: apply/start/deploy always port-forward 8080/3000; or: .\start.ps1 -Action web" -ForegroundColor White
    Write-Host ""
    Write-Host "ETL: kubectl -n egisz-monitor exec -it deploy/conf-ui -- egisz-monitor sync" -ForegroundColor Yellow
    Write-Banner "Complete" Green
}

switch ($Action) {
    "help" { Show-Help }
    "build" { Write-NestedSiblingMonitorWarning; Invoke-DockerBuild -DockerNoCache:$DockerNoCache }
    "metabase-provision-local" {
        Write-NestedSiblingMonitorWarning
        $prov = Join-Path $Root "metabase\provision-local.ps1"
        if (-not (Test-Path $prov)) {
            Write-Host "ERROR: Missing $prov" -ForegroundColor Red
            exit 1
        }
        & $prov
        if ($LASTEXITCODE -ne 0) { exit 1 }
    }
    "test" { Invoke-PythonTests }
    "verify" {
        Write-NestedSiblingMonitorWarning
        Invoke-CorpPortForwardIfRequestedAfterK8s -BackgroundSwitchPresent:$PSBoundParameters.ContainsKey('BackgroundPortForward') -BackgroundEnabled:$BackgroundPortForward -ConfAndMetabaseOnly:$(-not $IncludePostgresPortForward) -SkipBrowserOpens
        Invoke-K8sCorpStackVerify
    }
    { $_ -in @("apply", "start") } {
        Write-NestedSiblingMonitorWarning
        Initialize-LocalKubernetesCluster
        if ($_ -eq "start") {
            Write-Host "`[start] �� ��, ��� apply: Config UI �� �������� ����, ������ Metabase �����������..." -ForegroundColor Cyan
        } else {
            Write-Host "`[apply] ���������� ������ Config UI �� �������� ���� (Metabase �� �������)..." -ForegroundColor Cyan
        }
        Invoke-DockerBuildConfUi -DockerNoCache:$DockerNoCache
        Invoke-KindLoadImagesIfNeeded
        Invoke-KubectlApply
        Show-DeployInfo
        Invoke-CorpPortForwardIfRequestedAfterK8s -BackgroundSwitchPresent:$PSBoundParameters.ContainsKey('BackgroundPortForward') -BackgroundEnabled:$BackgroundPortForward -ConfAndMetabaseOnly:$(-not $IncludePostgresPortForward)
        Invoke-K8sSmokeTests
        Invoke-K8sCorpStackVerify
    }
    "apply-rebuild" {
        Write-NestedSiblingMonitorWarning
        Initialize-LocalKubernetesCluster
        Write-Host "`[apply-rebuild] Config UI: docker build --no-cache + kubectl apply (Metabase �� ������������)..." -ForegroundColor Cyan
        Invoke-DockerBuildConfUi -DockerNoCache:$true
        Invoke-KindLoadImagesIfNeeded
        Invoke-KubectlApply
        Show-DeployInfo
        Invoke-CorpPortForwardIfRequestedAfterK8s -BackgroundSwitchPresent:$PSBoundParameters.ContainsKey('BackgroundPortForward') -BackgroundEnabled:$BackgroundPortForward -ConfAndMetabaseOnly:$(-not $IncludePostgresPortForward)
        Invoke-K8sSmokeTests
        Invoke-K8sCorpStackVerify
    }
    "restart-metabase" {
        Write-NestedSiblingMonitorWarning
        Initialize-LocalKubernetesCluster
        if (-not (Test-KubectlResponds)) {
            Write-Host "ERROR: kubectl cluster-info failed." -ForegroundColor Red
            exit 1
        }
        Write-Banner "restart-metabase"
        Invoke-CorpRolloutRestartMetabaseOnly
        Wait-CorpMetabaseRollout
        Write-Host "`[restart-metabase] ������." -ForegroundColor Green
    }
    "restart-conf-ui" {
        Write-NestedSiblingMonitorWarning
        Initialize-LocalKubernetesCluster
        if (-not (Test-KubectlResponds)) {
            Write-Host "ERROR: kubectl cluster-info failed." -ForegroundColor Red
            exit 1
        }
        Write-Banner "restart-conf-ui"
        Invoke-CorpRolloutRestartConfUiOnly
        Wait-CorpConfUiRollout
        Write-Host "`[restart-conf-ui] ������." -ForegroundColor Green
    }
    "restart-web" {
        Write-NestedSiblingMonitorWarning
        Initialize-LocalKubernetesCluster
        if (-not (Test-KubectlResponds)) {
            Write-Host "ERROR: kubectl cluster-info failed." -ForegroundColor Red
            exit 1
        }
        Write-Banner "restart-web (Metabase + conf-ui)"
        Invoke-CorpRolloutRestartMetabaseAndConfUi
        Wait-CorpMetabaseRollout
        Wait-CorpConfUiRollout
        Write-Host "`[restart-web] ������." -ForegroundColor Green
    }
    "status" {
        kubectl -n egisz-monitor get pods,svc -o wide
        Show-K8sNetworkLegend
    }
    "web" {
        Write-NestedSiblingMonitorWarning
        # Same default as deploy/apply: hidden kubectl (+ .egisz-monitor-port-forward.pids); -BackgroundPortForward:$false = separate PS windows
        if ($PSBoundParameters.ContainsKey('BackgroundPortForward') -and -not $BackgroundPortForward) {
            Invoke-CorpWebPortForward
        } else {
            Invoke-CorpWebPortForward -ForceBackground
        }
    }
    "forward" {
        Write-NestedSiblingMonitorWarning
        if ($PSBoundParameters.ContainsKey('BackgroundPortForward') -and -not $BackgroundPortForward) {
            Invoke-CorpWebPortForward
        } else {
            Invoke-CorpWebPortForward -ForceBackground
        }
    }
    "stop-forward" {
        Write-NestedSiblingMonitorWarning
        Invoke-CorpStopPortForward
    }
    "deploy" {
        Write-NestedSiblingMonitorWarning
        Write-Banner 'egisz-monitor-corp K8s deploy (local)'
        Initialize-LocalKubernetesCluster
        Invoke-DockerBuild
        Invoke-KindLoadImagesIfNeeded
        Invoke-KubectlApply -ResetMetabaseAppDb
        Show-DeployInfo
        Invoke-CorpPortForwardIfRequestedAfterK8s -BackgroundSwitchPresent:$PSBoundParameters.ContainsKey('BackgroundPortForward') -BackgroundEnabled:$BackgroundPortForward -ConfAndMetabaseOnly:$(-not $IncludePostgresPortForward)
        Invoke-K8sSmokeTests
        Invoke-K8sCorpStackVerify
    }
    "reset-deploy" {
        Write-NestedSiblingMonitorWarning
        Write-Banner 'egisz-monitor-corp K8s reset-deploy (clean namespace)'
        Invoke-RemoveLegacyComposePostgresVolume
        Initialize-LocalKubernetesCluster
        Invoke-DockerBuild -DockerNoCache
        Invoke-KindLoadImagesIfNeeded
        Invoke-KubectlApply -ResetNamespace -ResetMetabaseAppDb
        Show-DeployInfo
        Invoke-CorpPortForwardIfRequestedAfterK8s -BackgroundSwitchPresent:$PSBoundParameters.ContainsKey('BackgroundPortForward') -BackgroundEnabled:$BackgroundPortForward -ConfAndMetabaseOnly:$(-not $IncludePostgresPortForward)
        Invoke-K8sSmokeTests
        Invoke-K8sCorpStackVerify
    }
    "reset-metabase" {
        Invoke-ResetMetabaseApplicationDatabase
    }
}

