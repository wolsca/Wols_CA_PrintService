# Builds and runs the Debian test container with Docker Desktop.
#
#   .\tools\run-local-test.ps1                 # start the service (web app on :8080)
#   .\tools\run-local-test.ps1 -SelfTest       # run all self-test phases and exit
#   .\tools\run-local-test.ps1 -Shell          # shell inside the container, CUPS running
#   .\tools\run-local-test.ps1 -Broker 1.2.3.4 # override the broker address
#
# The MQTT credentials and the admin token are read from the git-ignored file
# deploy/docker/.env.local (copy .env.local.example once), so no secret ends up
# in the repository.
#
param(
    [switch]$SelfTest,
    [switch]$Shell,
    [switch]$NoBuild,
    [string]$Broker = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $image = "wolsca-print-service:test"
    if (-not $NoBuild) {
        Write-Host "Building $image ..."
        docker build -f deploy/docker/Dockerfile.debian -t $image .
    }

    $configDir = Join-Path $repoRoot "deploy\docker\config"
    New-Item -ItemType Directory -Force -Path $configDir | Out-Null

    $common = @(
        "--rm",
        "-e", "WOLSCA_POLL_WATCHER=1",
        "-v", "${configDir}:/etc/wolsca"
    )

    $envFile = Join-Path $repoRoot "deploy\docker\.env.local"
    if (Test-Path $envFile) {
        Write-Host "Using $envFile for the MQTT credentials."
        $common += @("--env-file", $envFile)
    }
    else {
        Write-Warning "deploy\docker\.env.local not found; copy .env.local.example and fill it in for MQTT."
    }
    if ($Broker) {
        $common += @("-e", "WOLSCA_MQTT_BROKER=$Broker")
    }

    if ($SelfTest) {
        docker run @common $image self-test
    }
    elseif ($Shell) {
        docker run @common -it -p 8080:8080 -p 6631:631 $image shell
    }
    else {
        Write-Host "Web app will be on http://localhost:8080/ , CUPS on http://localhost:6631/"
        docker run @common -p 8080:8080 -p 6631:631 --name wolsca $image service
    }
}
finally {
    Pop-Location
}
