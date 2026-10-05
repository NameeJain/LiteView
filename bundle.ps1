# Creates a fully self-contained LiteView bundle (Python + all dependencies).
# Run on your personal computer, then transfer the zip to the target laptop.
#
# Usage:
#   .\bundle.ps1           # creates bundle zip + starts HTTP server
#   .\bundle.ps1 -NoServe  # just creates the zip, no server

param([switch]$NoServe)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Dir = $PSScriptRoot  # LiteView directory
$BundleDir = Join-Path $env:TEMP 'liteview-bundle'
$OutZip = Join-Path $Dir 'LiteView-portable.zip'
$PyVer = '3.12.7'

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# ---- Step 1: Download embedded Python ----------------------------------------
Say "Downloading portable Python $PyVer..."
$pyDir = Join-Path $BundleDir "python"
$pyZip = Join-Path $env:TEMP "python-$PyVer-embed-amd64.zip"

Remove-Item $BundleDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $BundleDir | Out-Null
New-Item -ItemType Directory -Force $pyDir | Out-Null

if (-not (Test-Path $pyZip)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip" -OutFile $pyZip -UseBasicParsing
}
Expand-Archive $pyZip $pyDir -Force

# Enable site-packages and pip
$pth = Get-ChildItem $pyDir -Filter 'python*._pth' | Select-Object -First 1
if ($pth) {
    $content = Get-Content $pth.FullName
    $content = $content -replace '^#\s*import site', 'import site'
    $content += 'Lib\site-packages'
    Set-Content $pth.FullName $content
}

# Bootstrap pip
Say 'Bootstrapping pip...'
$getPip = Join-Path $env:TEMP 'get-pip.py'
if (-not (Test-Path $getPip)) {
    Invoke-WebRequest 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing
}
& (Join-Path $pyDir 'python.exe') $getPip --no-warn-script-location 2>&1 | Out-Null

# Install all dependencies into portable Python
Say 'Installing dependencies into portable Python...'
& (Join-Path $pyDir 'python.exe') -m pip install --disable-pip-version-check -q `
    aiohttp pillow pynput dxcam eye3 2>&1 | Out-Host

# ---- Step 2: Copy LiteView files --------------------------------------------
Say 'Copying LiteView files...'
foreach ($f in @('host.py', 'viewer.html', 'thirdeye.py', 'requirements.txt')) {
    $src = Join-Path $Dir $f
    if (Test-Path $src) { Copy-Item $src $BundleDir -Force }
}

# Copy capture-bypass DLLs if available
$cbSrc = Join-Path $Dir 'capture-bypass'
if (Test-Path $cbSrc) {
    $cbDst = Join-Path $BundleDir 'capture-bypass'
    New-Item -ItemType Directory -Force $cbDst | Out-Null
    Copy-Item (Join-Path $cbSrc '*') $cbDst -Force -ErrorAction SilentlyContinue
}

# ---- Step 3: Create launcher script -----------------------------------------
Say 'Creating launcher...'
@'
@echo off
:: LiteView portable launcher — run as Administrator for full WDA bypass
cd /d "%~dp0"
echo Starting LiteView...
python\python.exe host.py
pause
'@ | Set-Content (Join-Path $BundleDir 'START.bat')

# Also create a PowerShell launcher for auto-elevation
@'
# LiteView launcher with auto-elevation
$Dir = $PSScriptRoot
$py = Join-Path $Dir 'python\python.exe'
$host_py = Join-Path $Dir 'host.py'

# Check if running as admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host 'Requesting Administrator privileges for WDA bypass...' -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-NoExit -Command cd '$Dir'; & '$py' '$host_py'"
    exit
}

& $py $host_py
'@ | Set-Content (Join-Path $BundleDir 'START.ps1')

# ---- Step 4: Zip everything -------------------------------------------------
Say 'Creating zip...'
if (Test-Path $OutZip) { Remove-Item $OutZip -Force }
Compress-Archive -Path (Join-Path $BundleDir '*') -DestinationPath $OutZip -CompressionLevel Optimal

$size = [math]::Round((Get-Item $OutZip).Length / 1MB, 1)
Say "Bundle ready: $OutZip ($size MB)"

# Cleanup temp
Remove-Item $BundleDir -Recurse -Force -ErrorAction SilentlyContinue

# ---- Step 5: Serve over HTTP ------------------------------------------------
if (-not $NoServe) {
    # Find local IP on the hotspot network
    $ip = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.*' } |
        Select-Object -First 1).IPAddress

    $servePort = 9876
    Write-Host ''
    Write-Host '=============================================' -ForegroundColor Green
    Write-Host " On the other laptop, open PowerShell and run:" -ForegroundColor Green
    Write-Host ''
    Write-Host "   irm http://${ip}:${servePort}/install | iex" -ForegroundColor White
    Write-Host ''
    Write-Host '=============================================' -ForegroundColor Green
    Write-Host ''
    Write-Host 'Serving... Press Ctrl+C to stop.' -ForegroundColor Yellow

    # Create a mini install script that downloads and extracts the zip
    $installScript = @"
`& {
`$ProgressPreference = 'SilentlyContinue'
`$Dir = Join-Path `$env:LOCALAPPDATA 'LiteView'
Write-Host '==> Downloading LiteView bundle...' -ForegroundColor Cyan
`$zip = Join-Path `$env:TEMP 'LiteView-portable.zip'
Invoke-WebRequest 'http://${ip}:${servePort}/bundle' -OutFile `$zip -UseBasicParsing
Write-Host '==> Extracting...' -ForegroundColor Cyan
Remove-Item `$Dir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force `$Dir | Out-Null
Expand-Archive `$zip `$Dir -Force
Remove-Item `$zip -Force
Write-Host '==> Done! Starting LiteView...' -ForegroundColor Green
Write-Host 'Run as Admin for WDA bypass:' -ForegroundColor Yellow
Write-Host "  cd `$Dir; .\START.bat" -ForegroundColor White
Write-Host 'Or with auto-elevation:' -ForegroundColor Yellow
Write-Host "  powershell -ExecutionPolicy Bypass -File `$Dir\START.ps1" -ForegroundColor White
Set-Location `$Dir
& (Join-Path `$Dir 'START.ps1')
}
"@

    # Simple HTTP server using .NET HttpListener
    $listener = New-Object System.Net.HttpListener
    $listener.Prefixes.Add("http://+:${servePort}/")
    try { $listener.Start() } catch {
        # If port binding fails (no admin), try localhost only
        $listener = New-Object System.Net.HttpListener
        $listener.Prefixes.Add("http://${ip}:${servePort}/")
        $listener.Start()
    }

    try {
        while ($true) {
            $ctx = $listener.GetContext()
            $path = $ctx.Request.Url.AbsolutePath
            $resp = $ctx.Response

            if ($path -eq '/install') {
                $bytes = [Text.Encoding]::UTF8.GetBytes($installScript)
                $resp.ContentType = 'text/plain'
                $resp.ContentLength64 = $bytes.Length
                $resp.OutputStream.Write($bytes, 0, $bytes.Length)
                Write-Host "  Served install script to $($ctx.Request.RemoteEndPoint)" -ForegroundColor Gray
            } elseif ($path -eq '/bundle') {
                $bytes = [IO.File]::ReadAllBytes($OutZip)
                $resp.ContentType = 'application/zip'
                $resp.ContentLength64 = $bytes.Length
                $resp.OutputStream.Write($bytes, 0, $bytes.Length)
                Write-Host "  Served bundle ($size MB) to $($ctx.Request.RemoteEndPoint)" -ForegroundColor Gray
            } else {
                $resp.StatusCode = 404
            }
            $resp.Close()
        }
    } finally {
        $listener.Stop()
    }
}
