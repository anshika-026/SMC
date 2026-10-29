# FIRST-TIME SETUP ONLY: copy SMC\backend\data into the Docker volume smc_data
# (a COPY - the Windows folder is left untouched). Old app.db backups are skipped.
#
# Once the backend has run in Docker, the volume holds the LIVE data (new
# enrolments, cameras, captures, passwords). Seeding again would overwrite it
# with the older Windows copy, so this script refuses when the volume already
# has a database. -Force overrides that (only if you really mean to replace it).
#
# To copy the volume's data back out (backup), from the SMC folder:
#   docker run --rm -v smc_data:/data -v "${PWD}\backend\data-from-docker:/out" alpine cp -a /data/. /out/
param([switch]$Force)
$ErrorActionPreference = "Stop"

function Invoke-Docker {
    # Native commands don't stop the script on failure in Windows PowerShell 5.1:
    # check the exit code ourselves so a failure is never reported as success.
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "docker $($args[0]) failed (exit $LASTEXITCODE). Is Docker Desktop running?" }
}

$src = (Resolve-Path "$PSScriptRoot\..\backend\data").Path
Invoke-Docker info --format "{{.ServerVersion}}" | Out-Null

$existing = & docker volume ls --quiet --filter "name=^smc_data$"
if ($existing) {
    $hasDb = & docker run --rm -v smc_data:/data alpine sh -c "test -f /data/app.db && echo yes"
    if ($hasDb -eq "yes" -and -not $Force) {
        Write-Host "smc_data already holds a database (live data). Nothing copied."
        Write-Host "Re-seeding would overwrite it with $src. Use -Force only if you really mean that."
        exit 0
    }
}
Invoke-Docker volume create smc_data | Out-Null
Invoke-Docker run --rm -v smc_data:/data -v "${src}:/src:ro" alpine sh -c "cp -a /src/. /data/ && rm -f /data/*.bak-* && ls /data"
Write-Host "smc_data seeded from $src"
