# ═══════════════════════════════════════════════════════════════
#  Tofu (豆腐) — Windows PowerShell One-Liner Installer
# ═══════════════════════════════════════════════════════════════
#
#  Usage:
#    irm https://raw.githubusercontent.com/rangehow/ToFu/main/install.ps1 | iex
#
#  Or download and run with options:
#    .\install.ps1 -Port 8080 -ApiKey "sk-xxx"
#
#  This script:
#    1. Checks for Python 3.10+
#    2. Clones the Tofu repository
#    3. Delegates to install.py for the actual setup
# ═══════════════════════════════════════════════════════════════

param(
    [string]$Dir = "$HOME\tofu",
    [int]$Port = 15000,
    [string]$ApiKey = "",
    [switch]$Docker,
    [switch]$NoLaunch,
    [switch]$SkipPlaywright,
    [switch]$Help
)

$ErrorActionPreference = "Stop"

if ($Help) {
    Write-Host @"

  Tofu (豆腐) — Windows Installer

  Usage: .\install.ps1 [OPTIONS]

  Options:
    -Dir PATH           Install directory (default: ~/tofu)
    -Port PORT          Server port (default: 15000)
    -ApiKey KEY         Pre-configure LLM API key
    -Docker             Use Docker instead of native install
    -NoLaunch           Install only, don't start the server
    -SkipPlaywright     Skip Playwright browser installation
    -Help               Show this help

"@
    exit 0
}

Write-Host ""
Write-Host "  🧈 Tofu (豆腐) — Self-Hosted AI Assistant" -ForegroundColor Cyan
Write-Host "  ───────────────────────────────────────────"
Write-Host ""

# ── Check Python ────────────────────────────────────────────
function Find-Python {
    foreach ($cmd in @("python", "python3", "py")) {
        $py = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($py) {
            try {
                $ver = & $py.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                $parts = $ver.Split(".")
                if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) {
                    return $py.Source
                }
            } catch {}
        }
    }
    # Check the Windows Python Launcher
    $py = Get-Command "py" -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $ver = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            $parts = $ver.Split(".")
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) {
                return "py"
            }
        } catch {}
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host "  ✗  Python 3.10+ is required but not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "     Download from: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "     Or install via: winget install Python.Python.3.12" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "     IMPORTANT: Check 'Add Python to PATH' during installation!" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
Write-Host "  ✓  Found Python: $python" -ForegroundColor Green

# ── Check Git ───────────────────────────────────────────────
$git = Get-Command "git" -ErrorAction SilentlyContinue
if (-not $git) {
    Write-Host "  ✗  Git is required but not found." -ForegroundColor Red
    Write-Host ""
    Write-Host "     Download from: https://git-scm.com/downloads" -ForegroundColor Yellow
    Write-Host "     Or install via: winget install Git.Git" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# ── Clone or update repository ──────────────────────────────
if (Test-Path "$Dir\server.py") {
    Write-Host "  ✓  Existing installation found at $Dir" -ForegroundColor Green
} else {
    Write-Host "  ℹ  Cloning repository to $Dir..." -ForegroundColor Blue
    git clone "https://github.com/rangehow/ToFu.git" $Dir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ✗  Failed to clone repository" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✓  Repository cloned" -ForegroundColor Green
}

# ── Delegate to install.py ──────────────────────────────────
$installPy = Join-Path $Dir "install.py"
if (-not (Test-Path $installPy)) {
    Write-Host "  ✗  install.py not found at $installPy" -ForegroundColor Red
    exit 1
}

$installArgs = @("$installPy", "--dir", "$Dir", "--port", "$Port")
if ($ApiKey) { $installArgs += @("--api-key", $ApiKey) }
if ($Docker) { $installArgs += "--docker" }
if ($NoLaunch) { $installArgs += "--no-launch" }
if ($SkipPlaywright) { $installArgs += "--skip-playwright" }

Write-Host ""
Write-Host "  ▸ Launching cross-platform installer..." -ForegroundColor Cyan
Write-Host ""

if ($python -eq "py") {
    & py -3 $installArgs
} else {
    & $python $installArgs
}
