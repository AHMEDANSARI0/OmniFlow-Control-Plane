[CmdletBinding()]
param(
    [string]$RepositoryUrl = "https://github.com/AHMEDANSARI0/OmniFlow-Control-Plane.git"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & git -C $Root @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($Arguments -join ' ')"
    }
}

try {
    Write-Host ""
    Write-Host "OmniFlow Control Plane - Private GitHub Publisher" -ForegroundColor Green

    if (-not (Get-Command git -CommandType Application -ErrorAction SilentlyContinue)) {
        throw "Git is not installed or is not available in PATH."
    }

    if ($RepositoryUrl -notmatch '^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$') {
        throw "RepositoryUrl must be a GitHub HTTPS .git URL."
    }

    $repositoryAlreadyInitialized = Test-Path -LiteralPath (
        Join-Path $Root ".git"
    )

    $forbidden = Get-ChildItem -LiteralPath $Root -Force -File |
        Where-Object {
            $_.Name -eq ".env" -or
            ($_.Name -like ".env.*" -and $_.Name -ne ".env.example") -or
            $_.Extension -in @(".dump", ".pem", ".key")
        }

    if ($forbidden) {
        throw "A forbidden secret/backup file exists in the deployment folder."
    }

    if (-not $repositoryAlreadyInitialized) {
        Invoke-Git -Arguments @("init")
    }

    Invoke-Git -Arguments @("branch", "-M", "main")
    Invoke-Git -Arguments @("add", "-A")
    Invoke-Git -Arguments @("diff", "--cached", "--check")

    & git -C $Root diff --cached --quiet
    $diffExitCode = $LASTEXITCODE
    if ($diffExitCode -eq 1) {
        $commitMessage = if ($repositoryAlreadyInitialized) {
            "Update OmniFlow Control Plane deployment"
        }
        else {
            "Deploy OmniFlow Control Plane API v1"
        }
        Invoke-Git -Arguments @("commit", "-m", $commitMessage)
    }
    elseif ($diffExitCode -ne 0) {
        throw "Git could not inspect the staged deployment files."
    }

    $existingRemote = & git -C $Root remote get-url origin 2>$null
    if ($LASTEXITCODE -ne 0) {
        Invoke-Git -Arguments @("remote", "add", "origin", $RepositoryUrl)
    }
    elseif (([string]$existingRemote).Trim() -cne $RepositoryUrl) {
        throw "Existing Git origin does not match RepositoryUrl."
    }

    Write-Host ""
    Write-Host "Pushing to GitHub. Authenticate locally if Git requests it."
    Invoke-Git -Arguments @("push", "-u", "origin", "main")

    Write-Host ""
    Write-Host "CONTROL PLANE GITHUB PUSH PASSED" -ForegroundColor Green
    Write-Host "Repository: $RepositoryUrl"
    Write-Host "Next: create a Render Blueprint from this private repository."
}
catch {
    Write-Host ""
    Write-Host "PUBLISH STOPPED" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "No credentials should be pasted into chat."
    exit 1
}
