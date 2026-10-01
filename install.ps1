<#
.SYNOPSIS
    Install Agentboard and create a `agentboard` command.

.DESCRIPTION
    irm <url>/install.ps1 | iex          install from a published source
    .\install.ps1                         install from this checkout
    .\install.ps1 -Uninstall              remove it again

    Installs into %LOCALAPPDATA%\Agentboard and puts a launcher in
    %LOCALAPPDATA%\Microsoft\WindowsApps, which is on PATH by default on
    Windows 10 and 11. Nothing is written elsewhere.
#>
[CmdletBinding()]
param(
    # A git URL, a .zip URL, or a local path. Defaults to the published
    # project; override it to install a fork or a branch.
    [string]$Source = $(if ($env:AGENTBOARD_SRC) { $env:AGENTBOARD_SRC }
                        else { 'https://github.com/Tataneeeeeeeeeee/Claude-Dashboard/archive/refs/heads/main.zip' }),
    [string]$Prefix = $(if ($env:AGENTBOARD_PREFIX) { $env:AGENTBOARD_PREFIX }
                        else { Join-Path $env:LOCALAPPDATA 'Agentboard' }),
    [string]$BinDir = $(if ($env:AGENTBOARD_BIN) { $env:AGENTBOARD_BIN }
                        else { Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps' }),
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Warn($text) { Write-Host "warning: $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "error: $text" -ForegroundColor Red; exit 1 }

$launcherCmd = Join-Path $BinDir 'agentboard.cmd'
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Agentboard.lnk'

# Before the rename the app was installed as `claude-dashboard`; remove
# those pieces so two copies never coexist. The app moves its own data
# directory on first run.
function Remove-Legacy {
    $legacyPrefix = Join-Path $env:LOCALAPPDATA 'ClaudeCodeDashboard'
    $legacyCmd = Join-Path $BinDir 'claude-dashboard.cmd'
    $legacyLink = Join-Path ([Environment]::GetFolderPath('Programs')) 'Claude Code Dashboard.lnk'
    if ((Test-Path $legacyCmd) -and (Select-String -Path $legacyCmd -Pattern 'written by install.ps1' -Quiet)) {
        Remove-Item $legacyCmd -Force -ErrorAction SilentlyContinue
        Write-Host "  removed the old launcher $legacyCmd"
    }
    Remove-Item $legacyLink -Force -ErrorAction SilentlyContinue
    if ((Test-Path (Join-Path $legacyPrefix 'app\run_app.py')) -and (Test-Path (Join-Path $legacyPrefix 'venv'))) {
        Remove-Item $legacyPrefix -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  removed the old installation $legacyPrefix"
    }
}

# ------------------------------------------------------------ uninstall

if ($Uninstall) {
    Write-Step 'Removing Agentboard'
    Remove-Item $launcherCmd -Force -ErrorAction SilentlyContinue
    Remove-Item $shortcut -Force -ErrorAction SilentlyContinue
    Remove-Item $Prefix -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Legacy
    Write-Host ''
    Write-Host 'Removed the application, its virtualenv and the launcher.'
    Write-Host 'Your data is untouched:'
    Write-Host '  %USERPROFILE%\.agentboard   this app''s config, cache, trash and backups'
    Write-Host '  %USERPROFILE%\.claude, .codex, .gemini, ...   each AI tool''s own files'
    exit 0
}

# --------------------------------------------------------------- python

Write-Step 'Checking Python'
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if (-not $found) { continue }
    $args = if ($candidate -eq 'py') { @('-3', '-c') } else { @('-c') }
    try {
        $version = & $candidate @args 'import sys; print("%d%02d" % sys.version_info[:2])' 2>$null
    } catch { continue }
    if ($version -and [int]$version -ge 311) { $python = $candidate; $pyArgs = if ($candidate -eq 'py') { @('-3') } else { @() }; break }
}
if (-not $python) {
    Fail 'Python 3.11 or newer is required. Install it from python.org or the Microsoft Store.'
}
Write-Host "  using $python"

# --------------------------------------------------------------- source

$work = $null
try {
    $scriptDir = if ($PSCommandPath) { Split-Path -Parent $PSCommandPath } else { $null }

    if ($scriptDir -and (Test-Path (Join-Path $scriptDir 'agentboard'))) {
        Write-Step 'Installing from this checkout'
        $sourceDir = $scriptDir
        Write-Host "  $sourceDir"
    }
    elseif ($Source) {
        Write-Step 'Fetching the source'
        $work = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP ("ccd-" + [guid]::NewGuid()))
        if ($Source -match '\.git$|^git@') {
            if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "git is needed to clone $Source" }
            git clone --depth 1 --quiet $Source (Join-Path $work 'src')
            $sourceDir = Join-Path $work 'src'
        }
        elseif ($Source -match '\.zip$') {
            $zip = Join-Path $work 'src.zip'
            Invoke-WebRequest -Uri $Source -OutFile $zip -UseBasicParsing
            Expand-Archive -Path $zip -DestinationPath (Join-Path $work 'unpacked') -Force
            $marker = Get-ChildItem -Path (Join-Path $work 'unpacked') -Recurse -Depth 2 `
                -Directory -Filter 'agentboard' | Select-Object -First 1
            if (-not $marker) { Fail "no agentboard directory inside $Source" }
            $sourceDir = $marker.Parent.FullName
        }
        elseif (Test-Path $Source) {
            $sourceDir = (Resolve-Path $Source).Path
        }
        else {
            Fail "cannot tell how to fetch '$Source' (expected a .git repo, a .zip or a path)"
        }
        Write-Host "  $Source"
    }
    else {
        Fail @"
no source to install from.

Run this script from inside a checkout, or tell it where to fetch from:

  `$env:AGENTBOARD_SRC = '<repo-or-zip>'; irm <url>/install.ps1 | iex

AGENTBOARD_SRC accepts a git URL, a .zip or a local path.
"@
    }

    if (-not (Test-Path (Join-Path $sourceDir 'agentboard'))) {
        Fail "$sourceDir does not look like the project (no agentboard\)"
    }

    # ----------------------------------------------------------- install

    Write-Step "Installing into $Prefix"
    Remove-Legacy
    $appDir = Join-Path $Prefix 'app'
    New-Item -ItemType Directory -Force -Path $Prefix, $BinDir | Out-Null
    Remove-Item $appDir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $appDir | Out-Null

    foreach ($item in @('agentboard', 'assets', 'examples', 'requirements.txt', 'run_app.py',
                        'README.md', 'SCHEMA.md', 'config.json')) {
        $path = Join-Path $sourceDir $item
        if (Test-Path $path) { Copy-Item $path -Destination $appDir -Recurse -Force }
    }
    Get-ChildItem -Path $appDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    Write-Step 'Creating the virtualenv'
    $venv = Join-Path $Prefix 'venv'
    Remove-Item $venv -Recurse -Force -ErrorAction SilentlyContinue
    & $python @pyArgs -m venv $venv
    $vpy = Join-Path $venv 'Scripts\python.exe'

    Write-Step 'Installing dependencies'
    & $vpy -m pip install --quiet --upgrade pip
    & $vpy -m pip install --quiet -r (Join-Path $appDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Fail 'dependency installation failed' }

    # ---------------------------------------------------------- launcher

    Write-Step 'Creating the launcher'
    $runner = Join-Path $appDir 'run_app.py'
    $pyw = Join-Path $venv 'Scripts\pythonw.exe'
    # pythonw.exe runs without a console window, which is what a desktop
    # application wants; python.exe is the fallback if it is missing.
    $exe = if (Test-Path $pyw) { $pyw } else { $vpy }
    @"
@echo off
REM Launcher for Agentboard, written by install.ps1.
REM
REM Opens the window and returns straight away, so the terminal stays free
REM and closing it does not close the app. `start` detaches, and pythonw
REM means no console window appears.
REM
REM Options that print to the terminal keep the foreground, because
REM pythonw would swallow their output: --check, --version, --headless,
REM --browser, --debug and --help. Add --foreground to stay attached.
setlocal

set "ARGS=%*"
set "FOREGROUND="

REM `shift` does not rewrite %*, so --foreground is removed by substitution.
if not "%ARGS%"=="%ARGS:--foreground=%" (
  set "FOREGROUND=1"
  set "ARGS=%ARGS:--foreground=%"
)

if not "%ARGS%"=="" (
  echo(%ARGS% | findstr /i /c:"--check" /c:"--version" /c:"--headless" /c:"--browser" /c:"--debug" /c:"--detect-providers" /c:"--help" >nul && set "FOREGROUND=1"
)

if defined FOREGROUND (
  "$vpy" "$runner" %ARGS%
  exit /b %errorlevel%
)

start "" "$exe" "$runner" %ARGS%
"@ | Set-Content -Path $launcherCmd -Encoding ASCII
    Write-Host "  $launcherCmd"

    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($shortcut)
        $link.TargetPath = $exe
        $link.Arguments = "`"$runner`""
        $link.WorkingDirectory = $appDir
        $link.IconLocation = Join-Path $appDir 'assets\icon.ico'
        $link.Description = 'Browse and compare the local history of your AI coding assistants'
        $link.Save()
        Write-Host "  $shortcut"
    } catch {
        Write-Warn "could not create the Start menu shortcut: $_"
    }

    # ------------------------------------------------------------ verify

    Write-Step 'Verifying'
    $reported = & $vpy $runner --version
    Write-Host "  $reported"

    # ---------------------------------------------------------- providers

    # Find every supported AI tool and point its adapter at its history.
    Write-Step 'Detecting AI tools'
    & $vpy $runner --detect-providers
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "provider detection failed; run 'agentboard --detect-providers' later, or use Settings > Providers."
    }

    Write-Host ''
    Write-Host 'Installed. Start it with:' -ForegroundColor Green
    Write-Host ''
    Write-Host '    agentboard'
    Write-Host ''
    if (($env:PATH -split ';') -notcontains $BinDir) {
        Write-Warn "$BinDir is not on your PATH, so the command will not be found yet."
        Write-Host "  Run it directly meanwhile: $launcherCmd"
        Write-Host ''
    }
    Write-Host 'The window opens and the terminal is handed straight back to you.'
    Write-Host ''
    Write-Host 'Other commands:'
    Write-Host '    agentboard --foreground         stay attached and watch the output'
    Write-Host '    agentboard --check              report the webview backend'
    Write-Host '    agentboard --headless --browser run without a native window'
    Write-Host '    agentboard --detect-providers   detect AI tools again'
    Write-Host '    install.ps1 -Uninstall                remove it again'
}
finally {
    if ($work) { Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue }
}
