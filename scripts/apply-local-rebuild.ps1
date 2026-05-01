# Пересборка образа conf-ui без кэша Docker и kubectl apply (см. start.ps1 -Action apply-rebuild).
param(
    [switch]$SkipMetabaseRolloutRestart,
    [switch]$SkipPortForwardAfterDeploy
)

$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

$start = Join-Path $RepoRoot 'start.ps1'
$splat = @{ Action = 'apply-rebuild' }
if ($SkipMetabaseRolloutRestart) { $splat.SkipMetabaseRolloutRestart = $true }
if ($SkipPortForwardAfterDeploy) { $splat.SkipPortForwardAfterDeploy = $true }

& $start @splat
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
