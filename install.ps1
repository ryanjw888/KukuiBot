#Requires -Version 5.1
<#
.SYNOPSIS
    KukuiBot — One-line installer for Windows
.DESCRIPTION
    Usage: irm https://raw.githubusercontent.com/ryanjw888/KukuiBot/main/install.ps1 | iex
      or:  .\install.ps1 -Port 8080 -Dir C:\kukuibot
.PARAMETER Port
    Server port (default: 7000)
.PARAMETER Dir
    Data directory (default: $env:USERPROFILE\.kukuibot)
#>
param(
    [int]$Port = 0,
    [string]$Dir = ""
)

$ErrorActionPreference = 'Stop'

# =============================================
# 1. Parse parameters (equivalent to install.sh --port / --dir)
# =============================================

if (-not $Dir) { $Dir = if ($env:KUKUIBOT_HOME) { $env:KUKUIBOT_HOME } else { "$env:USERPROFILE\.kukuibot" } }
$KUKUIBOT_HOME = $Dir

# --- Interactive port selection (equivalent to install.sh interactive prompt) ---
if ($Port -eq 0 -and $env:KUKUIBOT_PORT) { $Port = [int]$env:KUKUIBOT_PORT }
if ($Port -eq 0) {
    Write-Host "`nKukuiBot Installation" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Select HTTPS port for KukuiBot:"
    Write-Host "  7000  - Default"
    Write-Host "  8443  - Alternative HTTPS"
    Write-Host "  Other - Custom port (1024-65535)"
    Write-Host ""
    $userPort = Read-Host "Enter port [7000]"
    $Port = if ($userPort) { [int]$userPort } else { 7000 }
    Write-Host ""
}

# =============================================
# 2. Pre-flight validation (equivalent to install.sh port/dir checks)
# =============================================

# Admin check (equivalent to install.sh sudo priming)
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[!] This installer should be run as Administrator for full functionality." -ForegroundColor Yellow
    Write-Host "    Some steps (mkcert root CA, scheduled tasks) may fail without elevation." -ForegroundColor Yellow
    Write-Host ""
}

# Port validation
if ($Port -lt 1024 -or $Port -gt 65535) {
    Write-Host "[X] Invalid port: $Port (must be 1024-65535)" -ForegroundColor Red
    exit 1
}

# Port in-use check (equivalent to install.sh lsof check)
$portInUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($portInUse) {
    $proc = Get-Process -Id $portInUse[0].OwningProcess -ErrorAction SilentlyContinue
    $procName = if ($proc) { "$($proc.ProcessName) (PID $($proc.Id))" } else { "unknown" }
    Write-Host "[X] Port $Port is already in use by: $procName" -ForegroundColor Red
    Write-Host "    Choose a different port with -Port or stop the conflicting process."
    exit 1
}

# Parent directory check
$parentDir = Split-Path $KUKUIBOT_HOME -Parent
if ($parentDir -and -not (Test-Path $parentDir)) {
    Write-Host "[X] Parent directory does not exist: $parentDir" -ForegroundColor Red
    exit 1
}

Write-Host "Installing KukuiBot..." -ForegroundColor Cyan
Write-Host "   Port: $Port"
Write-Host "   Data: $KUKUIBOT_HOME"
Write-Host ""

# Helper: check if a command exists
function Test-Command { param([string]$Name) return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

# Helper: check winget availability
function Assert-Winget {
    if (-not (Test-Command 'winget')) {
        Write-Host "[X] winget not found. Install App Installer from the Microsoft Store." -ForegroundColor Red
        Write-Host "    https://aka.ms/getwinget"
        exit 1
    }
}

# =============================================
# 3. Check/install Python 3.11+ (equivalent to install.sh Homebrew Python)
# =============================================

Assert-Winget

$needPython = $true
if (Test-Command 'python') {
    try {
        $pyMinor = & python -c "import sys; print(sys.version_info.minor)" 2>$null
        if ([int]$pyMinor -ge 11) { $needPython = $false }
        else { Write-Host "-> System Python is 3.$pyMinor (need 3.11+), installing..." }
    } catch { }
}

if ($needPython) {
    Write-Host "-> Installing Python 3.13 via winget..."
    winget install --id Python.Python.3.13 --accept-source-agreements --accept-package-agreements --silent
    # Refresh PATH
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Test-Command 'python')) {
        Write-Host "[X] Python not found after install. Restart your terminal and re-run." -ForegroundColor Red
        exit 1
    }
}
$pyVer = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
Write-Host "[OK] Python $pyVer" -ForegroundColor Green

# =============================================
# 4. Check/install Node.js 18+ (equivalent to install.sh Node check)
# =============================================

$needNode = $true
if (Test-Command 'node') {
    $nodeMajor = (& node -v) -replace 'v(\d+)\..*', '$1'
    if ([int]$nodeMajor -ge 18) { $needNode = $false }
    else { Write-Host "-> Node.js v$nodeMajor is too old (need >=18), upgrading..." }
}

if ($needNode) {
    Write-Host "-> Installing Node.js via winget..."
    winget install --id OpenJS.NodeJS --accept-source-agreements --accept-package-agreements --silent
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not (Test-Command 'node')) {
        Write-Host "[X] Node.js not found after install. Restart your terminal and re-run." -ForegroundColor Red
        exit 1
    }
}
$nodeVer = & node --version
Write-Host "[OK] Node.js $nodeVer" -ForegroundColor Green

# =============================================
# 5. Check/install Claude Code CLI (equivalent to install.sh find_claude + npm install)
# =============================================

$claudeBin = ""
if (Test-Command 'claude') {
    $claudeBin = (Get-Command claude).Source
} else {
    # Search common npm global locations
    $candidates = @(
        "$env:APPDATA\npm\claude.cmd",
        "$env:LOCALAPPDATA\npm\claude.cmd",
        "$env:ProgramFiles\nodejs\claude.cmd"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $claudeBin = $c; break }
    }
}

if (-not $claudeBin) {
    Write-Host "-> Installing Claude Code CLI..."
    try {
        npm install -g @anthropic-ai/claude-code 2>&1 | Select-Object -Last 1
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
        if (Test-Command 'claude') { $claudeBin = (Get-Command claude).Source }
    } catch {
        Write-Host "[!] Claude Code CLI install failed. Install manually: npm install -g @anthropic-ai/claude-code" -ForegroundColor Yellow
    }
}

if ($claudeBin) {
    $claudeVer = & $claudeBin --version 2>$null | Select-Object -First 1
    Write-Host "[OK] Claude Code CLI $claudeVer ($claudeBin)" -ForegroundColor Green
} else {
    Write-Host "[!] Claude Code CLI not found. Install manually: npm install -g @anthropic-ai/claude-code" -ForegroundColor Yellow
}

# =============================================
# 6. Check/install mkcert and ripgrep (equivalent to install.sh brew install mkcert ripgrep)
# =============================================

foreach ($pkg in @(@{name='mkcert'; cmd='mkcert'; id='FiloSottile.mkcert'}, @{name='ripgrep'; cmd='rg'; id='BurntSushi.ripgrep.MSVC'})) {
    if (-not (Test-Command $pkg.cmd)) {
        Write-Host "-> Installing $($pkg.name)..."
        winget install --id $pkg.id --accept-source-agreements --accept-package-agreements --silent
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    }
    Write-Host "[OK] $($pkg.name)" -ForegroundColor Green
}

# =============================================
# 7. Install root CA (equivalent to install.sh mkcert -install)
# =============================================

try {
    mkcert -install 2>$null
    Write-Host "[OK] Root CA trusted" -ForegroundColor Green
} catch {
    Write-Host "[!] mkcert -install failed. Run as Administrator to trust root CA." -ForegroundColor Yellow
}

# =============================================
# 8. Set up directories, clone/update repo (equivalent to install.sh clone section)
# =============================================

$SRC_DIR = "$KUKUIBOT_HOME\src"
$VENV_DIR = "$KUKUIBOT_HOME\venv"
$REPO_URL = if ($env:KUKUIBOT_REPO) { $env:KUKUIBOT_REPO } else { "https://github.com/ryanjw888/KukuiBot.git" }

if (-not (Test-Path $KUKUIBOT_HOME)) { New-Item -ItemType Directory -Path $KUKUIBOT_HOME -Force | Out-Null }

if (Test-Path "$SRC_DIR\.git") {
    Write-Host "-> Updating existing source at $SRC_DIR"
    Push-Location $SRC_DIR
    try {
        git fetch origin --quiet 2>$null
        $pulled = $false
        try { git pull --ff-only 2>$null; $pulled = $true } catch { }
        if (-not $pulled) {
            Write-Host "   Fast-forward failed, trying rebase..."
            try { git pull --rebase 2>$null; $pulled = $true } catch { }
        }
        if (-not $pulled) {
            Write-Host "   Rebase failed, performing clean reset..."
            git rebase --abort 2>$null
            git reset --hard origin/main
        }
    } finally { Pop-Location }
} else {
    Write-Host "-> Cloning KukuiBot to $SRC_DIR"
    git clone $REPO_URL $SRC_DIR
    if (-not $?) {
        Write-Host "[X] Git clone failed. Check your network and try again." -ForegroundColor Red
        exit 1
    }
}

# =============================================
# 9. Create venv, install Python deps (equivalent to install.sh venv + pip install)
# =============================================

if (-not (Test-Path $VENV_DIR)) {
    Write-Host "-> Creating virtual environment..."
    python -m venv $VENV_DIR
}
$PYTHON_BIN = "$VENV_DIR\Scripts\python.exe"
if (-not (Test-Path $PYTHON_BIN)) {
    Write-Host "[X] Virtual environment creation failed — $PYTHON_BIN not found" -ForegroundColor Red
    exit 1
}

Write-Host "-> Installing Python dependencies..."
# Filter out uvloop (Unix-only) from requirements
$reqFile = "$SRC_DIR\requirements.txt"
$filteredReqs = (Get-Content $reqFile) | Where-Object { $_ -notmatch '^\s*uvloop' }
$tmpReq = [System.IO.Path]::GetTempFileName()
$filteredReqs | Set-Content $tmpReq -Encoding UTF8
try {
    & $PYTHON_BIN -m pip install -q -r $tmpReq 2>&1 | Select-Object -Last 3
} finally {
    Remove-Item $tmpReq -ErrorAction SilentlyContinue
}
Write-Host "[OK] Python dependencies installed" -ForegroundColor Green

# =============================================
# 10. Install Playwright Chromium (equivalent to install.sh playwright install)
# =============================================

$playwrightOk = $false
try {
    & $PYTHON_BIN -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.stop()" 2>$null
    $playwrightOk = $true
} catch { }

if (-not $playwrightOk) {
    Write-Host "-> Installing Playwright Chromium browser..."
    & $PYTHON_BIN -m playwright install chromium 2>&1 | Select-Object -Last 1
    Write-Host "[OK] Playwright Chromium installed" -ForegroundColor Green
} else {
    Write-Host "[OK] Playwright Chromium already installed" -ForegroundColor Green
}

# =============================================
# 11. Seed Claude CLI settings (equivalent to install.sh Claude settings seed)
# =============================================

$claudeSettingsDir = "$env:USERPROFILE\.claude"
$claudeSettingsFile = "$claudeSettingsDir\settings.json"
if (-not (Test-Path $claudeSettingsFile)) {
    if (-not (Test-Path $claudeSettingsDir)) { New-Item -ItemType Directory -Path $claudeSettingsDir -Force | Out-Null }
    @{
        env = @{
            CLAUDE_AUTOCOMPACT_PCT_OVERRIDE = "90"
            CLAUDE_CODE_MAX_OUTPUT_TOKENS = "128000"
            MAX_THINKING_TOKENS = "0"
        }
    } | ConvertTo-Json -Depth 3 | Set-Content $claudeSettingsFile -Encoding UTF8
    Write-Host "[OK] Claude CLI settings created ($claudeSettingsFile)" -ForegroundColor Green
} else {
    Write-Host "[OK] Claude CLI settings already exist" -ForegroundColor Green
}

# =============================================
# 12. Seed default data files (equivalent to install.sh agent/workers/models copy)
# =============================================

Write-Host "-> Seeding default configuration files..."
foreach ($f in @('SOUL.md', 'USER.md', 'TOOLS.md', 'MEMORY.md')) {
    $dest = "$KUKUIBOT_HOME\$f"
    $src = "$SRC_DIR\agent\$f"
    if (-not (Test-Path $dest) -and (Test-Path $src)) { Copy-Item $src $dest }
}
foreach ($subdir in @('workers', 'models')) {
    $destDir = "$KUKUIBOT_HOME\$subdir"
    $srcDir = "$SRC_DIR\$subdir"
    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
    if (Test-Path $srcDir) {
        Get-ChildItem "$srcDir\*.md" -ErrorAction SilentlyContinue | ForEach-Object {
            $dest = "$destDir\$($_.Name)"
            if (-not (Test-Path $dest)) { Copy-Item $_.FullName $dest }
        }
    }
}
Write-Host "[OK] Configuration files ready" -ForegroundColor Green

# =============================================
# 13. Generate HTTPS certs (equivalent to install.sh mkcert cert generation)
# =============================================

$CERT_DIR = "$SRC_DIR\certs"
if (-not (Test-Path "$CERT_DIR\kukuibot.pem")) {
    Write-Host "-> Generating HTTPS certificates..."
    if (-not (Test-Path $CERT_DIR)) { New-Item -ItemType Directory -Path $CERT_DIR -Force | Out-Null }
    # Get LAN IP (equivalent to install.sh ipconfig getifaddr en0)
    $lanIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        $_.InterfaceAlias -notlike '*Loopback*' -and $_.PrefixOrigin -ne 'WellKnown'
    } | Select-Object -First 1).IPAddress
    $certNames = @('localhost', '127.0.0.1')
    if ($lanIP) { $certNames += $lanIP }
    & mkcert -cert-file "$CERT_DIR\kukuibot.pem" -key-file "$CERT_DIR\kukuibot-key.pem" @certNames
    $caRoot = & mkcert -CAROOT
    if (Test-Path "$caRoot\rootCA.pem") { Copy-Item "$caRoot\rootCA.pem" "$CERT_DIR\rootCA.pem" -ErrorAction SilentlyContinue }
    Write-Host "   Certificates: $CERT_DIR"
}
Write-Host "[OK] HTTPS certs ready" -ForegroundColor Green

# =============================================
# 14. Create Windows Scheduled Task for server (equivalent to install.sh launchd plist)
# =============================================

# NOTE: Privileged helper is macOS-specific (sudoers, Spotlight, AF_UNIX socket).
# It is not needed on Windows — OS-level privilege escalation uses different mechanisms.
Write-Host "[OK] Privileged helper: not needed on Windows (macOS-specific)" -ForegroundColor Green

Write-Host "-> Setting up services..."

$serverTaskName = "KukuiBot-Server"
$serverLogFile = "$KUKUIBOT_HOME\logs\kukuibot-server.log"
if (-not (Test-Path "$KUKUIBOT_HOME\logs")) { New-Item -ItemType Directory -Path "$KUKUIBOT_HOME\logs" -Force | Out-Null }

# Remove existing task if present
schtasks /Delete /TN $serverTaskName /F 2>$null

# Build a wrapper script that sets env vars and launches the server
$serverWrapper = "$KUKUIBOT_HOME\start-server.cmd"
@"
@echo off
set KUKUIBOT_HOME=$KUKUIBOT_HOME
set KUKUIBOT_PORT=$Port
set HOME=$env:USERPROFILE
$(if ($claudeBin) { "set CLAUDE_BIN=$claudeBin" })
cd /d "$SRC_DIR"
"$PYTHON_BIN" server.py >> "$serverLogFile" 2>&1
"@ | Set-Content $serverWrapper -Encoding ASCII

# Create scheduled task: runs at logon, restarts on failure (equivalent to launchd RunAtLoad + KeepAlive)
schtasks /Create /TN $serverTaskName `
    /TR "`"$serverWrapper`"" `
    /SC ONLOGON `
    /RL HIGHEST `
    /F | Out-Null

Write-Host "[OK] KukuiBot server task created (port $Port, runs at logon)" -ForegroundColor Green

# Start the server now
Write-Host "-> Starting KukuiBot server..."
Start-Process -FilePath "cmd.exe" -ArgumentList "/c `"$serverWrapper`"" -WindowStyle Hidden

# =============================================
# 15. Create Windows Scheduled Task for hourly backup (equivalent to install.sh crontab)
# =============================================

$backupTaskName = "KukuiBot-Backup"
$backupScript = "$SRC_DIR\backup.sh"

# Only create backup task if backup.sh exists and git-bash is available
$gitBash = ""
if (Test-Path "C:\Program Files\Git\bin\bash.exe") { $gitBash = "C:\Program Files\Git\bin\bash.exe" }
elseif (Test-Path "C:\Program Files (x86)\Git\bin\bash.exe") { $gitBash = "C:\Program Files (x86)\Git\bin\bash.exe" }

if ($gitBash -and (Test-Path $backupScript)) {
    schtasks /Delete /TN $backupTaskName /F 2>$null
    schtasks /Create /TN $backupTaskName `
        /TR "`"$gitBash`" `"$backupScript`"" `
        /SC HOURLY `
        /F | Out-Null
    Write-Host "[OK] Hourly backup task created" -ForegroundColor Green
} else {
    Write-Host "[!] Backup task skipped (requires Git Bash). Run backup.sh manually." -ForegroundColor Yellow
}

# =============================================
# 16. Verify server started (equivalent to install.sh lsof verify loop)
# =============================================

Write-Host "-> Waiting for server to start..."
$serverOk = $false
for ($i = 0; $i -lt 8; $i++) {
    Start-Sleep -Seconds 2
    $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listening) { $serverOk = $true; break }
}

if ($serverOk) {
    Write-Host "[OK] KukuiBot server running on port $Port" -ForegroundColor Green
} else {
    Write-Host "[!] Server didn't start within expected time" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Diagnostic steps:"
    Write-Host "    1. Check server logs:     Get-Content '$serverLogFile' -Tail 50"
    Write-Host "    2. Check scheduled tasks: schtasks /Query /TN $serverTaskName"
    Write-Host "    3. Test manually:         cd $SRC_DIR; & '$PYTHON_BIN' server.py"
    Write-Host ""
}

# =============================================
# 17. Print summary (equivalent to install.sh final banner)
# =============================================

$lanIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.InterfaceAlias -notlike '*Loopback*' -and $_.PrefixOrigin -ne 'WellKnown'
} | Select-Object -First 1).IPAddress
if (-not $lanIP) { $lanIP = "<your-ip>" }

Write-Host ""
Write-Host ("=" * 55)
Write-Host "  KukuiBot installation complete!" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Access URLs:"
Write-Host "    Local:  https://localhost:$Port"
Write-Host "    LAN:    https://${lanIP}:$Port"
Write-Host ""
Write-Host "  Configuration:"
Write-Host "    Data dir:   $KUKUIBOT_HOME"
Write-Host "    Source:     $SRC_DIR"
Write-Host "    Python:     $PYTHON_BIN"
Write-Host "    Node.js:    $(if (Test-Command 'node') { (Get-Command node).Source }) ($nodeVer)"
if ($claudeBin) {
    Write-Host "    Claude CLI: $claudeBin"
}
Write-Host ""
Write-Host "  Manage:"
Write-Host "    Stop:       schtasks /End /TN $serverTaskName"
Write-Host "    Start:      schtasks /Run /TN $serverTaskName"
Write-Host "    Logs:       Get-Content '$serverLogFile' -Tail 50 -Wait"
Write-Host "    Uninstall:  schtasks /Delete /TN $serverTaskName /F; schtasks /Delete /TN $backupTaskName /F"
Write-Host ("=" * 55)

# =============================================
# 18. Open browser (equivalent to install.sh open command)
# =============================================

if ($serverOk) {
    Write-Host ""
    Write-Host "-> Opening KukuiBot in your browser..."
    Start-Sleep -Seconds 1
    Start-Process "https://localhost:$Port"
} else {
    Write-Host ""
    Write-Host "-> Server needs troubleshooting before accessing web interface."
    Write-Host "   Review the diagnostic steps above and check logs."
}
