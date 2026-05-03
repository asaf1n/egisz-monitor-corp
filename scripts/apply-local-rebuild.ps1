# Пересборка образа conf-ui без кэша Docker + kubectl apply (эквивалент: .\start.ps1 -Action apply -DockerNoCache).
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$start = Join-Path (Split-Path -Parent $here) "start.ps1"
& $start -Action apply -DockerNoCache
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
