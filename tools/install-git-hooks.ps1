# Points git at tools/git-hooks, so the commit number is raised on every commit.
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    git config core.hooksPath tools/git-hooks
    Write-Host "Git hooks enabled (core.hooksPath = tools/git-hooks)."
    Write-Host ("Current version: " + (python tools/bump_version.py --show))
}
finally {
    Pop-Location
}
