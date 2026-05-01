# Пересборка образа conf-ui без кэша Docker и kubectl apply (см. start.ps1 -Action apply-rebuild).
param()

$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

$start = Join-Path $RepoRoot 'start.ps1'
& $start -Action apply-rebuild
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
