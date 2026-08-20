#Requires -Version 5.1
<#
.SYNOPSIS
    Builds, tests, publishes and releases the Wols CA Print Service.

.DESCRIPTION
    The authoritative pipeline entrypoint. Every step has to pass before the next
    one starts; the first failure stops the run and nothing is committed or pushed.

      0. Ask the IDE (CLion / Visual Studio) to save all open files.
      1. Verify the preconditions (python, docker daemon, git, registry login).
      2. Check the syntax of every Python file and validate the shipped JSON files.
      3. Build the Debian container image.
      4. Print test: a real job through the whole chain, but to a *virtual*
         printer - the output queue writes PDF files into build\printtest\out
         instead of sending anything to the physical printer.
      5. Run all diagnostics phases inside the container (self-test).
      6. Bump the commit number, commit and push to GitHub.
      7. Push the container package to the registry (GHCR by default).
      8. With -Release: cut the release from changesFixes.md, tag it, push the
         release image tags, branch off to release/vX.Y and freeze that branch.

    Every mutating step is guarded by ShouldProcess, so -WhatIf is a full dry run.
    If a step between 6 and 8 writes generated files and then fails, those files
    are restored, so a failed run never burns a version number.

.PARAMETER CommitMessage
    Description appended to the automated commit message. Asked for when omitted,
    unless -NonInteractive is used.

.PARAMETER Release
    Cut a release: run tools/release.py, tag vX.Y, publish the release image tags
    and branch off to release/vX.Y (frozen).

.PARAMETER Major
    With -Release: raise x by one and reset y to 0.

.PARAMETER NonInteractive
    Never prompt; use the default commit message and assume "yes".

.PARAMETER SkipPrintTest
    Skip step 4 (the virtual print test).

.PARAMETER SkipTests
    Skip step 5 (the container self-test).

.PARAMETER SkipGit
    Do not bump, commit, push, tag or branch.

.PARAMETER SkipPush
    Do not push the container image to the registry.

.PARAMETER NoBump
    Reuse the current BUILD_NUMBER instead of raising it.

.PARAMETER Registry
    Image repository without a tag. Default ghcr.io/wolsca/wols_ca_printservice.

.PARAMETER TestDocument
    PDF to use for the print test. Without it the newest PDF in the TestPrint
    folder is taken, and only when that folder is empty a document is generated
    (tools/make_test_pdf.py). Several pages matter: three pages are two sheets,
    so a front side, a flip and a back side.

.PARAMETER TestPages
    Number of pages of the generated test document. Default 3.

.PARAMETER FlipWaitSeconds
    How long to wait at the flip prompt before pressing Continue, so the state
    can be followed in Home Assistant and the web app. Default 30.

.EXAMPLE
    .\build_and_release.ps1 -CommitMessage "Watcher handles moved files"

.EXAMPLE
    .\build_and_release.ps1 -SkipPush -SkipGit          # local verification only

.EXAMPLE
    .\build_and_release.ps1 -Release -CommitMessage "Booklet mode per queue"
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param (
    [string]$CommitMessage = "",
    [switch]$Release,
    [switch]$Major,
    [switch]$NonInteractive,
    [switch]$SkipPrintTest,
    [switch]$SkipTests,
    [switch]$SkipGit,
    [switch]$SkipPush,
    [switch]$NoBump,
    [string]$Registry = "ghcr.io/wolsca/wols_ca_printservice",
    [string]$TestDocument = "",
    [int]$TestPages = 3,
    [int]$FlipWaitSeconds = 30
)

$ErrorActionPreference = "Stop"

$RepoRoot = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = (Get-Location).Path }
Set-Location $RepoRoot

$PackageDir   = Join-Path $RepoRoot "Wols_CA_PrintService"
$Dockerfile   = Join-Path $RepoRoot "deploy\docker\Dockerfile.debian"
$EnvFile      = Join-Path $RepoRoot "deploy\docker\.env.local"
$VersionFile  = Join-Path $RepoRoot "VERSION"
$BuildFile    = Join-Path $RepoRoot "BUILD_NUMBER"
$ReleasedFile = Join-Path $RepoRoot ".version-released"
$ChangesFile  = Join-Path $RepoRoot "changesFixes.md"
$NotesFile    = Join-Path $RepoRoot "RELEASE_NOTES.md"
$ChangelogFile= Join-Path $RepoRoot "CHANGELOG.md"
$BuildDir     = Join-Path $RepoRoot "build"
$PrintTestDir = Join-Path $BuildDir "printtest\out"
$PrintInDir   = Join-Path $BuildDir "printtest\in"
$LogDir       = Join-Path $BuildDir "logs"
$TestPrintDir = Join-Path $RepoRoot "TestPrint"
# Results live in a subfolder, so a result is never mistaken for a test document.
$TestResultDir = Join-Path $TestPrintDir "Results"
$TokenFile    = Join-Path $RepoRoot ".github_token"
# Home Assistant add-on manifests. The release add-on follows the releases, the
# test add-on every commit build, so the Supervisor only offers an update when a
# release is cut.
$AddonConfig     = Join-Path $RepoRoot "wolsca_print_service\config.yaml"
$AddonTestConfig = Join-Path $RepoRoot "wolsca_print_service_test\config.yaml"

# Files this script regenerates; their content is captured before the first write.
$GeneratedFiles = @($VersionFile, $BuildFile, $ReleasedFile, $ChangesFile, $NotesFile, $ChangelogFile,
                    $AddonConfig, $AddonTestConfig)

$LocalImage = "wolsca-print-service:test"
$ContainerName = "wolsca-pipeline"

# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------
function Write-Step { param([string]$Message) Write-Host "[INFO] $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "  -> $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[WARNING] $Message" -ForegroundColor Yellow }
function Write-Fail { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red }

function Get-FileSnapshot {
    param([string[]]$Paths)
    $Snapshot = @{}
    foreach ($Path in $Paths) {
        if (Test-Path $Path) { $Snapshot[$Path] = [System.IO.File]::ReadAllBytes($Path) }
        else { $Snapshot[$Path] = $null }
    }
    return $Snapshot
}

function Restore-FileSnapshot {
    param([hashtable]$Snapshot)
    if ($null -eq $Snapshot) { return }
    Write-Warn "Rolling back the generated files to their previous content..."
    foreach ($Path in $Snapshot.Keys) {
        try {
            if ($null -eq $Snapshot[$Path]) {
                if (Test-Path $Path) { Remove-Item $Path -Force }
            } else {
                [System.IO.File]::WriteAllBytes($Path, $Snapshot[$Path])
            }
            Write-Host "     restored $(Split-Path $Path -Leaf)"
        } catch {
            Write-Fail "Could not restore $Path : $($_.Exception.Message)"
        }
    }
}

function Get-GitHubToken {
    foreach ($Name in @("GITHUB_TOKEN", "GH_TOKEN")) {
        $Value = [System.Environment]::GetEnvironmentVariable($Name)
        if (-not [string]::IsNullOrWhiteSpace($Value)) {
            return [pscustomobject]@{ Token = $Value.Trim(); Source = "`$env:$Name" }
        }
    }
    if (Test-Path $TokenFile) {
        $Value = (Get-Content $TokenFile -Raw).Trim()
        if (-not [string]::IsNullOrWhiteSpace($Value)) {
            return [pscustomobject]@{ Token = $Value; Source = ".github_token file" }
        }
    }
    return $null
}

function Write-TextFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content
    )
    # Set-Content -Encoding utf8 writes a BOM on PowerShell 5.1; YAML must stay
    # BOM-less or the Supervisor cannot parse the manifest.
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $Utf8NoBom)
}

function Set-AddonVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Version
    )
    # The add-on version must be exactly the published image tag, otherwise the
    # Supervisor gets a 404 when it pulls the image.
    if (-not (Test-Path $Path)) {
        Write-Warn "Add-on manifest $Path not found; version not synchronised."
        return
    }
    $Text = (Get-Content $Path -Raw) -replace '(?m)^version:\s*".*"', "version: `"$Version`""
    Write-TextFile -Path $Path -Content $Text
    Write-Ok "$(Split-Path (Split-Path $Path -Parent) -Leaf)/config.yaml -> version $Version"
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$Arguments = @(),
        [string]$What = ""
    )
    # Native tools (docker, git) write progress to stderr; with a stopping
    # preference PowerShell would turn that into a terminating error, so only the
    # exit code decides here.
    $Previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Command @Arguments
    } finally {
        $ErrorActionPreference = $Previous
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$(if ($What) { $What } else { "$Command $($Arguments -join ' ')" }) failed with exit code $LASTEXITCODE."
    }
}

function Get-PythonCommand {
    # An interpreter that also has the runtime dependencies is preferred, because
    # only then the import check can run locally instead of just in the container.
    $Fallback = $null
    foreach ($Candidate in @("python", "python3", "py")) {
        if (-not (Get-Command $Candidate -ErrorAction SilentlyContinue)) { continue }
        $Probe = & $Candidate -c "import sys; print(sys.version_info[0])" 2>$null
        if ($LASTEXITCODE -ne 0 -or "$Probe".Trim() -ne "3") { continue }
        if (-not $Fallback) { $Fallback = $Candidate }
        & $Candidate -c "import paho.mqtt.client, pypdf" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Command = $Candidate; HasDependencies = $true }
        }
    }
    if ($Fallback) {
        return [pscustomobject]@{ Command = $Fallback; HasDependencies = $false }
    }
    return $null
}

function Remove-PipelineContainer {
    $Previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { docker rm -f $ContainerName 2>&1 | Out-Null } finally { $ErrorActionPreference = $Previous }
}

function Get-ContainerStatus {
    $Previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $Json = docker exec $ContainerName curl -s --max-time 5 http://127.0.0.1:8080/api/status 2>$null
    } finally {
        $ErrorActionPreference = $Previous
    }
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Json)) { return $null }
    try { return ($Json | ConvertFrom-Json) } catch { return $null }
}

function Wait-ForState {
    param(
        [Parameter(Mandatory = $true)][string[]]$States,
        [int]$TimeoutSeconds = 120
    )
    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $Deadline) {
        $Status = Get-ContainerStatus
        if ($null -ne $Status -and $States -contains $Status.state) { return $Status }
        Start-Sleep -Seconds 2
    }
    return $null
}

$Failures = 0
$Snapshot = $null

# -------------------------------------------------------------------------
# 0. Ask the IDE to save all open files
# -------------------------------------------------------------------------
Write-Step "Saving all modified files in the IDE..."
$SaveTriggered = $false
try {
    $Wshell = New-Object -ComObject WScript.Shell
    foreach ($Ide in @("CLion", "Microsoft Visual Studio", "PyCharm")) {
        if ($Wshell.AppActivate($Ide)) {
            $Wshell.SendKeys("^+s")   # Save All
            Start-Sleep -Milliseconds 500
            $SaveTriggered = $true
            Write-Ok "'Save All' triggered in $Ide."
            break
        }
    }
} catch {
    Write-Verbose "SendKeys not available: $($_.Exception.Message)"
}
if (-not $SaveTriggered) {
    Write-Warn "Could not trigger 'Save All' - the build may use stale sources."
}

# -------------------------------------------------------------------------
# 1. Preconditions - fail fast before anything is written
# -------------------------------------------------------------------------
Write-Step "Verifying the preconditions..."

foreach ($Required in @($Dockerfile, $VersionFile, $BuildFile, $PackageDir)) {
    if (-not (Test-Path $Required)) {
        Write-Fail "Required path not found: $Required"
        exit 1
    }
}

$PythonInfo = Get-PythonCommand
if (-not $PythonInfo) {
    Write-Fail "No Python 3 interpreter found on PATH (tried python, python3, py)."
    exit 1
}
$Python = $PythonInfo.Command
Write-Ok "Python interpreter: $Python$(if (-not $PythonInfo.HasDependencies) { ' (without the runtime dependencies)' })"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Fail "'docker' was not found on PATH."
    exit 1
}
docker version --format '{{.Server.Version}}' | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Fail "The Docker daemon is not reachable. Start Docker Desktop and retry."
    exit 1
}
Write-Ok "Docker daemon reachable."

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Fail "'git' was not found on PATH."
    exit 1
}
$GitBranch = "$(git rev-parse --abbrev-ref HEAD 2>$null)".Trim()
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Not inside a git working tree."
    exit 1
}
if ($GitBranch -eq "HEAD") {
    Write-Warn "Detached HEAD - a commit would not advance any branch."
    if (-not $SkipGit -and -not $NonInteractive) {
        if ((Read-Host "Continue anyway? [y/N]") -notmatch '^(y|yes)$') { exit 1 }
    }
} else {
    Write-Ok "On branch '$GitBranch'."
}

if ($Release -and $GitBranch -like "release/*") {
    Write-Fail "Already on a release branch ('$GitBranch'); release branches are frozen."
    exit 1
}

if (-not $SkipPush) {
    $RegistryHost = ($Registry -split '/')[0]
    $DockerConfig = Join-Path $env:USERPROFILE ".docker\config.json"
    $HasLogin = $false
    if (Test-Path $DockerConfig) {
        try {
            $Auths = (Get-Content $DockerConfig -Raw | ConvertFrom-Json)
            if ($Auths.PSObject.Properties.Name -contains 'auths') {
                $HasLogin = ($Auths.auths.PSObject.Properties.Name -contains $RegistryHost)
            }
        } catch { Write-Verbose "Could not parse $DockerConfig." }
    }
    if (-not $HasLogin) {
        Write-Fail "No $RegistryHost credentials found. Run 'docker login $RegistryHost' or use -SkipPush."
        exit 1
    }
    Write-Ok "$RegistryHost credentials present."
}

if (-not (Test-Path $EnvFile)) {
    Write-Warn "deploy\docker\.env.local not found; the container runs without MQTT credentials."
    Write-Warn "Copy deploy\docker\.env.local.example and fill it in for a complete self-test."
}

New-Item -ItemType Directory -Force -Path $PrintTestDir, $PrintInDir, $LogDir | Out-Null


# -------------------------------------------------------------------------
# 2. Syntax check of every Python file, plus the shipped JSON files
# -------------------------------------------------------------------------
Write-Step "Checking the syntax of all Python files..."
try {
    Invoke-Native $Python @("-m", "compileall", "-q", "Wols_CA_PrintService", "tools") "compileall"
    Write-Ok "All Python files compile."

    # A syntax check alone misses a missing import or a broken module-level call.
    if ($PythonInfo.HasDependencies) {
        Push-Location $PackageDir
        try {
            Invoke-Native $Python @("-c", "import config, version, mqtt_service, diagnostics, admin, updater, file_watcher, hardware_dispatcher, pdf_processor, web_app, installer, main; print('imports ok')") "module import check"
        } finally {
            Pop-Location
        }
        Write-Ok "All modules import."
    } else {
        Write-Warn "paho-mqtt/pypdf are missing locally; the import check runs inside the container only."
        Write-Warn "Install them with: $Python -m pip install -r requirements.txt"
    }

    Invoke-Native $Python @("-c", "import json; json.load(open('Wols_CA_PrintService/web_strings.json', encoding='utf-8')); json.load(open('deploy/debian/WolsCAPrintService.linux.json', encoding='utf-8')); json.load(open('repository.json', encoding='utf-8')); print('json ok')") "JSON validation"
    Write-Ok "The shipped JSON files are valid."

    # The add-on manifests decide whether Home Assistant can install the image at
    # all, so an incomplete manifest has to fail here, not in HA.
    Invoke-Native $Python @((Join-Path $RepoRoot "tools\check_addons.py")) "add-on manifest check"
    Write-Ok "The Home Assistant add-on manifests are consistent."
} catch {
    Write-Fail $_.Exception.Message
    exit 1
}

$CurrentVersion = "$(& $Python (Join-Path $RepoRoot 'tools\bump_version.py') --show)".Trim()
Write-Ok "Current version: $CurrentVersion"

# -------------------------------------------------------------------------
# 3. Build the Debian container image
# -------------------------------------------------------------------------
if ($PSCmdlet.ShouldProcess($LocalImage, "docker build")) {
    Write-Step "Building the Debian container image $LocalImage..."
    try {
        Invoke-Native "docker" @("build", "-f", "deploy/docker/Dockerfile.debian", "-t", $LocalImage, ".") "docker build"
    } catch {
        Write-Fail $_.Exception.Message
        exit 1
    }
    Write-Ok "Image $LocalImage built."
}

# -------------------------------------------------------------------------
# 4. Print test through the whole chain, to a virtual PDF printer
# -------------------------------------------------------------------------
if ($SkipPrintTest) {
    Write-Warn "-SkipPrintTest specified: the print chain is not exercised."
} elseif ($PSCmdlet.ShouldProcess("virtual printer", "print test")) {
    Write-Step "Print test: printing to the virtual PDF printer (no paper is used)..."
    Get-ChildItem -Path $PrintTestDir -Filter *.pdf -ErrorAction SilentlyContinue | Remove-Item -Force

    # A booklet only shows its real behaviour with several pages: three pages are
    # two sheets, so a front side, a flip and a back side.
    $Document = $null
    if ($TestDocument) {
        if (-not (Test-Path $TestDocument)) {
            Write-Fail "The test document $TestDocument does not exist."
            exit 1
        }
        $Document = (Resolve-Path $TestDocument).Path
    } elseif (Test-Path $TestPrintDir) {
        # The documents in TestPrint\ are the reference material for the test.
        $Candidate = Get-ChildItem -Path $TestPrintDir -Filter *.pdf -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($Candidate) { $Document = $Candidate.FullName }
    }

    if ($Document) {
        # The name must survive 'lp', so no spaces or quotes in the container.
        $DocumentName = (Split-Path $Document -Leaf) -replace '[^A-Za-z0-9._-]', '_'
        Copy-Item $Document (Join-Path $PrintInDir $DocumentName) -Force
        Write-Ok "Using the test document $DocumentName from $(Split-Path $Document -Parent)."
    } else {
        $DocumentName = "booklet-test.pdf"
        Invoke-Native $Python @((Join-Path $RepoRoot "tools\make_test_pdf.py"),
            (Join-Path $PrintInDir $DocumentName), "--pages", "$TestPages") "make_test_pdf"
        Write-Ok "Generated a $TestPages-page test document."
    }

    Remove-PipelineContainer
    $PrintOk = $false
    try {
        $RunArgs = @(
            "run", "-d", "--name", $ContainerName,
            "-e", "WOLSCA_POLL_WATCHER=1",
            "-e", "WOLSCA_VIRTUAL_OUTPUT=1",
            "-e", "WOLSCA_VIRTUAL_OUTPUT_DIR=/var/spool/wolsca/PrintOut",
            "-v", "${PrintTestDir}:/var/spool/wolsca/PrintOut",
            "-v", "${PrintInDir}:/var/spool/wolsca/PrintTest:ro"
        )
        if (Test-Path $EnvFile) { $RunArgs += @("--env-file", $EnvFile) }
        $RunArgs += @($LocalImage, "service")
        Invoke-Native "docker" $RunArgs "docker run"

        Write-Host "     waiting for the service to come up..."
        if ($null -eq (Wait-ForState -States @("IDLE") -TimeoutSeconds 150)) {
            throw "The service did not reach IDLE inside the container."
        }
        Write-Ok "Service is up; submitting a job to the booklet intake queue."

        Invoke-Native "docker" @("exec", $ContainerName, "bash", "-lc",
            "lp -d WolsCA_Booklet -t PipelineProbe '/var/spool/wolsca/PrintTest/$DocumentName'") "lp"

        # The queue is picked up within a few seconds; the state only tells the
        # whole story once the job has actually started.
        if ($null -eq (Wait-ForState -States @("PROCESSING", "PRINTING", "WAITING_FOR_FLIP", "ERROR") -TimeoutSeconds 120)) {
            throw "The job was never picked up from the drop folder."
        }

        # Booklet and duplex wait for the sheets to be turned over; the pipeline
        # confirms that automatically, so both sides are really printed.
        $Completed = $false
        $Deadline = (Get-Date).AddSeconds(300)
        while ((Get-Date) -lt $Deadline) {
            $Status = Get-ContainerStatus
            if ($null -eq $Status) { Start-Sleep -Seconds 2; continue }
            switch ($Status.state) {
                "ERROR" { throw "The service reported an error: $($Status.detail)" }
                "WAITING_FOR_FLIP" {
                    Write-Host "     flip requested ($($Status.detail))"
                    Write-Host "     waiting $FlipWaitSeconds s, as a person would, then pressing Continue..."
                    Start-Sleep -Seconds $FlipWaitSeconds
                    Invoke-Native "docker" @("exec", $ContainerName, "curl", "-s", "-X", "POST",
                        "http://127.0.0.1:8080/api/resume") "resume"
                    Start-Sleep -Seconds 5
                }
                # COMPLETED is short-lived: the service returns to IDLE at once.
                "COMPLETED" { $Completed = $true }
                "IDLE" { $Completed = $true }
                default { Start-Sleep -Seconds 3 }
            }
            if ($Completed) { break }
        }
        if (-not $Completed) { throw "The job did not complete within the timeout." }
        Write-Ok "The service reports the job as COMPLETED."

        $Deadline = (Get-Date).AddSeconds(60)
        $Pdfs = @()
        while ((Get-Date) -lt $Deadline) {
            $Pdfs = @(Get-ChildItem -Path $PrintTestDir -Filter *.pdf -ErrorAction SilentlyContinue)
            if ($Pdfs.Count -ge 2) { break }
            Start-Sleep -Seconds 3
        }
        if ($Pdfs.Count -lt 1) {
            throw "No PDF arrived in $PrintTestDir - the chain dropped the job."
        }
        # The result is kept next to the test document, named after the version
        # and commit number of the build that produced it.
        New-Item -ItemType Directory -Force -Path $TestResultDir | Out-Null
        $Index = 0
        foreach ($Pdf in ($Pdfs | Sort-Object Name)) {
            $Index++
            $Side = if ($Pdf.Name -like "front_*") { "front" }
                    elseif ($Pdf.Name -like "back_*") { "back" }
                    else { "part$Index" }
            $ResultName = "$([System.IO.Path]::GetFileNameWithoutExtension($DocumentName))-$CurrentVersion-$Side.pdf"
            Copy-Item $Pdf.FullName (Join-Path $TestResultDir $ResultName) -Force
            Write-Ok "Printed $Side to TestPrint\Results\$ResultName ($([int]($Pdf.Length / 1024)) kB)"
        }
        $PrintOk = $true
    } catch {
        Write-Fail $_.Exception.Message
    } finally {
        $LogPath = Join-Path $LogDir "print-test.log"
        $ErrorActionPreference = "Continue"
        docker logs $ContainerName 2>&1 | Out-File -FilePath $LogPath -Encoding utf8
        $ErrorActionPreference = "Stop"
        Write-Host "     container log written to $LogPath"
        Remove-PipelineContainer
    }
    if (-not $PrintOk) {
        Write-Fail "The print test failed; nothing is committed or pushed."
        exit 1
    }
}

# -------------------------------------------------------------------------
# 5. All diagnostics phases inside the container
# -------------------------------------------------------------------------
if ($SkipTests) {
    Write-Warn "-SkipTests specified: the container self-test is not run."
} elseif ($PSCmdlet.ShouldProcess($LocalImage, "self-test --all")) {
    Write-Step "Running all self-test phases inside the container..."
    $TestArgs = @(
        "run", "--rm",
        "-e", "WOLSCA_POLL_WATCHER=1",
        "-e", "WOLSCA_VIRTUAL_OUTPUT=1",
        "-e", "WOLSCA_VIRTUAL_OUTPUT_DIR=/var/spool/wolsca/PrintOut",
        "-v", "${PrintTestDir}:/var/spool/wolsca/PrintOut"
    )
    if (Test-Path $EnvFile) { $TestArgs += @("--env-file", $EnvFile) }
    # Every phase except 'chain': the print test above already exercises the
    # whole chain, with a real multi-page booklet and the flip.
    $TestArgs += @($LocalImage, "self-test",
                   "system,config,admin,permissions,update,cups,printer,network")

    $TestLog = Join-Path $LogDir "self-test.log"
    $ErrorActionPreference = "Continue"
    docker @TestArgs 2>&1 | Tee-Object -FilePath $TestLog
    $TestExit = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    Write-Host "     self-test log written to $TestLog"
    if ($TestExit -ne 0) {
        Write-Fail "The self-test failed (exit code $TestExit); nothing is committed or pushed."
        exit 1
    }
    Write-Ok "All self-test phases passed."
}

# -------------------------------------------------------------------------
# Steps 6 to 8 are transactional: a failure restores the generated files.
# -------------------------------------------------------------------------
$Snapshot = Get-FileSnapshot -Paths $GeneratedFiles
$NewVersion = $CurrentVersion
$ReleaseNumber = $null

try {
    # ---------------------------------------------------------------------
    # 6. Bump, commit and push
    # ---------------------------------------------------------------------
    if ($SkipGit) {
        Write-Warn "-SkipGit specified: no bump, commit or tag."
    } else {
        if ($Release) {
            Write-Step "Cutting the release from changesFixes.md..."
            $ReleaseArgs = @((Join-Path $RepoRoot "tools\release.py"))
            if ($Major) { $ReleaseArgs += "--major" }
            if ($PSCmdlet.ShouldProcess("RELEASE_NOTES.md", "cut release")) {
                Invoke-Native $Python $ReleaseArgs "tools/release.py"
            }
            $ReleaseNumber = "$(Get-Content $VersionFile -Raw)".Trim()
            Write-Ok "Release number: $ReleaseNumber"
        }

        if ($NoBump) {
            $KeptBuild = "$(Get-Content $BuildFile -Raw)".Trim()
            Write-Step "-NoBump specified: keeping build number $KeptBuild."
        } elseif ($PSCmdlet.ShouldProcess($BuildFile, "bump the commit number")) {
            Invoke-Native $Python @((Join-Path $RepoRoot "tools\bump_version.py"), "--build") "bump_version"
        }
        $NewVersion = "$(& $Python (Join-Path $RepoRoot 'tools\bump_version.py') --show)".Trim()
        Write-Ok "Version to publish: $NewVersion"

        # Home Assistant add-on manifests: the test add-on tracks every commit
        # build, the release add-on only a real release. That is what makes the
        # Supervisor offer an update on releases only.
        if ($PSCmdlet.ShouldProcess($AddonTestConfig, "synchronise the add-on version")) {
            Set-AddonVersion -Path $AddonTestConfig -Version "build-$NewVersion"
        }
        if ($Release -and $PSCmdlet.ShouldProcess($AddonConfig, "synchronise the add-on version")) {
            Set-AddonVersion -Path $AddonConfig -Version $NewVersion
        }

        if ([string]::IsNullOrWhiteSpace($CommitMessage) -and -not $NonInteractive) {
            Write-Host ""
            Write-Host "=================================================================" -ForegroundColor Cyan
            Write-Host " Build, print test and self-test succeeded. Time to commit!      " -ForegroundColor Cyan
            Write-Host "=================================================================" -ForegroundColor Cyan
            $CommitMessage = Read-Host "Enter a description (or press Enter for the default text)"
        }
        $FinalMessage = if ($Release) { "Release $NewVersion" } else { "Automated build $NewVersion" }
        if (-not [string]::IsNullOrWhiteSpace($CommitMessage)) { $FinalMessage += " - $CommitMessage" }

        if ($PSCmdlet.ShouldProcess($GitBranch, "git commit and push")) {
            Write-Step "Committing and pushing..."
            Invoke-Native "git" @("add", "-A") "git add"
            $Staged = @(git diff --cached --name-only)
            if ($Staged.Count -eq 0) {
                Write-Warn "Nothing to commit - the working tree is unchanged."
            } else {
                Invoke-Native "git" @("commit", "-m", $FinalMessage) "git commit"
                Invoke-Native "git" @("push") "git push"
                Write-Ok "Pushed $NewVersion to '$GitBranch'."
            }
        }
    }

    # ---------------------------------------------------------------------
    # 7. Push the container package
    # ---------------------------------------------------------------------
    if ($SkipPush) {
        Write-Warn "-SkipPush specified: the image stays local."
    } else {
        # Every commit gets a test package; a release gets the version and latest.
        $Tags = @("$Registry`:build-$NewVersion", "$Registry`:test")
        if ($Release) { $Tags += @("$Registry`:$NewVersion", "$Registry`:latest") }
        if ($ReleaseNumber) { $Tags += "$Registry`:$ReleaseNumber" }

        Write-Step "Publishing the container package to $Registry..."
        foreach ($Tag in $Tags) {
            if (-not $PSCmdlet.ShouldProcess($Tag, "docker tag and push")) { continue }
            Invoke-Native "docker" @("tag", $LocalImage, $Tag) "docker tag"
            Invoke-Native "docker" @("push", $Tag) "docker push"
            Write-Ok "Pushed $Tag"
        }
        Write-Host "[SUCCESS] Container package $NewVersion is live on $Registry." -ForegroundColor Green
    }

    # ---------------------------------------------------------------------
    # 8. Release: tag, branch off and freeze
    # ---------------------------------------------------------------------
    if ($Release -and -not $SkipGit) {
        $Tag = "v$ReleaseNumber"
        $ReleaseBranch = "release/$Tag"

        if ($PSCmdlet.ShouldProcess($Tag, "create and push the git tag")) {
            Write-Step "Tagging the release as $Tag..."
            Invoke-Native "git" @("tag", "-a", $Tag, "-m", "Release $Tag ($NewVersion)") "git tag"
            Invoke-Native "git" @("push", "origin", $Tag) "git push tag"
            Write-Ok "Tag $Tag pushed."
        }

        if ($PSCmdlet.ShouldProcess($ReleaseBranch, "branch off and push")) {
            Write-Step "Branching off to $ReleaseBranch..."
            Invoke-Native "git" @("branch", $ReleaseBranch, $Tag) "git branch"
            Invoke-Native "git" @("push", "origin", $ReleaseBranch) "git push branch"
            Write-Ok "Release branch $ReleaseBranch pushed; the work continues on '$GitBranch'."
        }

        # Freezing needs the GitHub API: the branch is locked and nobody, not even
        # an administrator, can push to it any more.
        $TokenInfo = Get-GitHubToken
        if ($null -eq $TokenInfo) {
            Write-Warn "No GitHub token found; $ReleaseBranch was NOT frozen."
            Write-Warn "Set `$env:GITHUB_TOKEN and freeze it in Settings > Branches, or rerun with a token."
        } elseif ($PSCmdlet.ShouldProcess($ReleaseBranch, "freeze through the GitHub API")) {
            Write-Step "Freezing $ReleaseBranch..."
            $Remote = "$(git config --get remote.origin.url)".Trim()
            if ($Remote -match 'github\.com[:/](?<owner>[^/]+)/(?<repo>[^/.]+)') {
                $Owner = $Matches['owner']
                $Repo  = $Matches['repo']
                $Headers = @{
                    "Authorization"        = "Bearer $($TokenInfo.Token)"
                    "Accept"               = "application/vnd.github+json"
                    "X-GitHub-Api-Version" = "2022-11-28"
                }
                $Body = @{
                    required_status_checks           = $null
                    enforce_admins                   = $true
                    required_pull_request_reviews    = $null
                    restrictions                     = $null
                    allow_force_pushes               = $false
                    allow_deletions                  = $false
                    lock_branch                      = $true
                } | ConvertTo-Json -Depth 5
                try {
                    Invoke-RestMethod -Method Put -Headers $Headers -ContentType "application/json" `
                        -Uri "https://api.github.com/repos/$Owner/$Repo/branches/$([uri]::EscapeDataString($ReleaseBranch))/protection" `
                        -Body $Body | Out-Null
                    Write-Ok "$ReleaseBranch is locked (read-only, no force push, no deletion)."
                } catch {
                    Write-Warn "Could not freeze $ReleaseBranch : $($_.Exception.Message)"
                    Write-Warn "A token with 'repo'/'administration' scope is required (public repositories only on the free plan)."
                    $Failures++
                }
            } else {
                Write-Warn "Could not derive owner/repository from '$Remote'; $ReleaseBranch was not frozen."
                $Failures++
            }
        }
    }
} catch {
    Write-Fail $_.Exception.Message
    Restore-FileSnapshot -Snapshot $Snapshot
    exit 1
}

if ($Failures -gt 0) {
    Write-Fail "$Failures step(s) failed after the package was published."
    exit 1
}

Write-Host ""
if ($Release) {
    Write-Host "[SUCCESS] Release $NewVersion completed (tag v$ReleaseNumber, branch release/v$ReleaseNumber frozen)." -ForegroundColor Green
} else {
    Write-Host "[SUCCESS] Build $NewVersion committed, pushed and published." -ForegroundColor Green
}
exit 0
