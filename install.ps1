# LiteView one-command installer for Windows. Run in PowerShell:
#
#   irm https://raw.githubusercontent.com/NameeJain/LiteView/master/install.ps1 | iex
#
# Works on locked-down corporate laptops: no admin needed for install,
# no .exe to block, no Python installer — uses portable embedded Python.
# Only needs admin elevation for WDA hook (SeDebugPrivilege).

& {
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # makes Invoke-WebRequest much faster

$Repo = 'NameeJain/LiteView'
$Dir = Join-Path $env:LOCALAPPDATA 'LiteView'
$Port = 8765
$Log = Join-Path $HOME '.liteview.log'
$PyVer = '3.12.7'  # embedded Python version to download

function Say($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Find-Python {
    # 1. Check our own portable Python first
    $portable = Join-Path $Dir "python-$PyVer\python.exe"
    if (Test-Path $portable) { return $portable }
    # 2. Check system Python
    $probes = @(
        { py -3 -c 'import sys; print(sys.executable)' },
        { python -c 'import sys; print(sys.executable)' }
    )
    foreach ($probe in $probes) {
        try {
            $exe = & $probe 2>$null | Select-Object -Last 1
            if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
        } catch {}
    }
    return $null
}

function Install-PortablePython {
    $pyDir = Join-Path $Dir "python-$PyVer"
    if (Test-Path (Join-Path $pyDir 'python.exe')) { return (Join-Path $pyDir 'python.exe') }

    Say "Downloading portable Python $PyVer (no install needed)..."
    $pyZip = Join-Path $env:TEMP "python-$PyVer-embed-amd64.zip"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip" -OutFile $pyZip -UseBasicParsing

    New-Item -ItemType Directory -Force $pyDir | Out-Null
    Expand-Archive $pyZip $pyDir -Force
    Remove-Item $pyZip -Force -ErrorAction SilentlyContinue

    # Enable pip: uncomment "import site" in python3XX._pth
    $pth = Get-ChildItem $pyDir -Filter 'python*._pth' | Select-Object -First 1
    if ($pth) {
        $content = Get-Content $pth.FullName
        $content = $content -replace '^#\s*import site', 'import site'
        # Also add Lib\site-packages so pip-installed packages are found
        $content += 'Lib\site-packages'
        Set-Content $pth.FullName $content
    }

    # Bootstrap pip
    Say 'Bootstrapping pip...'
    $getPip = Join-Path $env:TEMP 'get-pip.py'
    Invoke-WebRequest 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPip -UseBasicParsing
    & (Join-Path $pyDir 'python.exe') $getPip --no-warn-script-location 2>&1 | Out-Null
    Remove-Item $getPip -Force -ErrorAction SilentlyContinue

    return (Join-Path $pyDir 'python.exe')
}

function Find-Tailscale {
    @((Get-Command tailscale -ErrorAction SilentlyContinue).Source,
      (Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe')) |
        Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

function Get-TailscaleState($exe) {
    try {
        $json = (& $exe status --json 2>$null) -join "`n"
        if ($json) { return ($json | ConvertFrom-Json).BackendState }
    } catch {}
    return $null
}

try {
    # ---- stop a running copy so its files can be replaced ----------------------
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$Dir*host.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    # ---- download LiteView -----------------------------------------------------
    Say "Downloading LiteView to $Dir ..."
    $zip = Join-Path $env:TEMP 'liteview.zip'
    $unpacked = Join-Path $env:TEMP 'liteview-unpacked'
    Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/master.zip" -OutFile $zip -UseBasicParsing
    Remove-Item $unpacked -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive $zip $unpacked -Force
    New-Item -ItemType Directory -Force $Dir | Out-Null
    $src = Get-ChildItem $unpacked -Directory | Select-Object -First 1
    Copy-Item (Join-Path $src.FullName '*') $Dir -Recurse -Force
    Remove-Item $zip, $unpacked -Recurse -Force -ErrorAction SilentlyContinue

    # ---- Python ----------------------------------------------------------------
    $py = Find-Python
    if (-not $py) {
        # No system Python — download portable embedded Python (just a zip, no installer)
        $py = Install-PortablePython
    }
    Say "Using Python: $py"

    # ---- install packages -------------------------------------------------------
    Say 'Installing Python packages (first time takes a minute)...'
    $pyDir = Split-Path $py
    $pip = Join-Path $pyDir 'Scripts\pip.exe'
    # For portable Python, pip is in Scripts subfolder
    if (-not (Test-Path $pip)) { $pip = Join-Path $pyDir 'Scripts\pip3.exe' }

    # If using portable Python (no venv needed - install directly)
    $isPortable = $py -like "*$Dir*python-*"
    if ($isPortable) {
        & $py -m pip install --disable-pip-version-check -q -r (Join-Path $Dir 'requirements.txt') 2>&1 | Out-Host
        if ($LASTEXITCODE) { throw 'Installing Python packages failed.' }
        $runPy = $py
        $runPyw = Join-Path (Split-Path $py) 'pythonw.exe'
        if (-not (Test-Path $runPyw)) { $runPyw = $py }
    } else {
        # System Python — use venv as before
        $venvPy = Join-Path $Dir '.venv\Scripts\python.exe'
        $venvPyw = Join-Path $Dir '.venv\Scripts\pythonw.exe'
        if (-not (Test-Path $venvPy)) {
            & $py -m venv (Join-Path $Dir '.venv')
            if ($LASTEXITCODE) { throw 'Creating the Python virtual environment failed.' }
        }
        & $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Dir 'requirements.txt')
        if ($LASTEXITCODE) { throw 'Installing Python packages failed.' }
        $runPy = $venvPy
        $runPyw = $venvPyw
    }

    # ---- capture-bypass binaries ------------------------------------------------
    Say 'Downloading capture-bypass (DRM-video bypass)...'
    $cbDir = Join-Path $Dir 'capture-bypass'
    New-Item -ItemType Directory -Force $cbDir | Out-Null
    $cbZip     = Join-Path $env:TEMP 'cb.zip'
    $cbUnpacked = Join-Path $env:TEMP 'cb-unpacked'
    try {
        Invoke-WebRequest 'https://github.com/Londopy/capture-bypass/releases/download/v3.6.5/capture-bypass-3.6.5-portable-x64.zip' `
            -OutFile $cbZip -UseBasicParsing
        Remove-Item $cbUnpacked -Recurse -Force -ErrorAction SilentlyContinue
        Expand-Archive $cbZip $cbUnpacked -Force
        foreach ($f in @('payload_dll.dll','payload_dll_persistent.dll')) {
            $src = Get-ChildItem $cbUnpacked -Recurse -Filter $f | Select-Object -First 1
            if ($src) { Copy-Item $src.FullName (Join-Path $cbDir $f) -Force }
        }
        Remove-Item $cbZip, $cbUnpacked -Recurse -Force -ErrorAction SilentlyContinue
        Say 'capture-bypass ready.'
    } catch {
        Warn "capture-bypass download failed: $($_.Exception.Message)"
    }

    # ---- Tailscale: install and sign in -------------------------------------------
    $tailscale = Find-Tailscale
    if (-not $tailscale) {
        Say 'Installing Tailscale...'
        try {
            winget install -e --id Tailscale.Tailscale --silent `
                --accept-package-agreements --accept-source-agreements | Out-Host
        } catch {}
        $tailscale = Find-Tailscale
        if (-not $tailscale) {
            Warn 'Could not install Tailscale automatically. Install it from https://tailscale.com/download'
            Warn 'and re-run this command to enable access over the internet.'
        }
    }
    $tsConnected = $false
    if ($tailscale) {
        for ($i = 0; $i -lt 15 -and -not (Get-TailscaleState $tailscale); $i++) { Start-Sleep -Seconds 2 }
        if ((Get-TailscaleState $tailscale) -ne 'Running') {
            Say 'Connecting this computer to Tailscale...'
            Warn 'If a link appears below, open it and sign in with the SAME Tailscale account'
            Warn 'you use on the other computer.'
            & $tailscale up --unattended
            if ($LASTEXITCODE -and (Get-TailscaleState $tailscale) -ne 'Running') { & $tailscale up }
        }
        $tsConnected = (Get-TailscaleState $tailscale) -eq 'Running'
        if (-not $tsConnected) {
            Warn 'Tailscale is not connected. LiteView will only work on the local network.'
        }
    }

    $hostPy = Join-Path $Dir 'host.py'
    $hostArgs = @("`"$hostPy`"")
    if ($tsConnected) { $hostArgs += '--tailscale-only' }
    $argLine = $hostArgs -join ' '

    # ---- firewall ----------------------------------------------------------------
    if (-not (Get-NetFirewallRule -DisplayName 'LiteView' -ErrorAction SilentlyContinue)) {
        Say 'Opening the firewall for LiteView...'
        try {
            $baseExe = & $runPy -c 'import sys; print(sys._base_executable)' 2>$null
            if (-not $baseExe) { $baseExe = $runPy }
            $baseDir = Split-Path $baseExe
            $programs = @((Join-Path $baseDir 'python.exe'), (Join-Path $baseDir 'pythonw.exe'))
            $cmd = "foreach (`$p in @('" + (($programs | ForEach-Object { $_ -replace "'", "''" }) -join "','") + "')) { " +
                   "if (Test-Path `$p) { New-NetFirewallRule -DisplayName 'LiteView' -Direction Inbound -Action Allow -Protocol TCP " +
                   "-LocalPort $Port -RemoteAddress LocalSubnet,100.64.0.0/10 -Program `$p -Profile Any | Out-Null } }"
            $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
            Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden `
                -ArgumentList '-NoProfile', '-EncodedCommand', $encoded
        } catch {
            Warn 'Firewall prompt was declined. If connecting fails, re-run and click Yes.'
        }
    }

    # ---- start at login ----------------------------------------------------------
    Say 'Making LiteView start automatically when you log in...'
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut(
        (Join-Path ([Environment]::GetFolderPath('Startup')) 'LiteView.lnk'))
    $shortcut.TargetPath = $runPyw
    $shortcut.Arguments = $argLine
    $shortcut.WorkingDirectory = $Dir
    $shortcut.Save()

    # ---- start now ---------------------------------------------------------------
    Say 'Starting LiteView...'
    $proc = Start-Process $runPy -ArgumentList "`"$hostPy`"" -WorkingDirectory $Dir -PassThru
    Start-Sleep -Seconds 4
    if ($proc.HasExited) {
        Warn "LiteView stopped right after starting. Last lines of $Log :"
        if (Test-Path $Log) { Get-Content $Log -Tail 15 | Out-Host }
        return
    }

    Write-Host ''
    Write-Host 'LiteView is running! On the other computer, open:' -ForegroundColor Green
    $showArgs = @($hostPy, '--show-address')
    if ($tsConnected) { $showArgs += '--tailscale-only' }
    & $runPy @showArgs
    Write-Host ''
    Write-Host "It starts automatically at every login. Log file: $Log"
    Write-Host "To stop it: Task Manager -> end 'python.exe'. To remove from startup:"
    Write-Host '  Remove-Item "$([Environment]::GetFolderPath(''Startup''))\LiteView.lnk"'
} catch {
    Write-Host "LiteView install failed: $($_.Exception.Message)" -ForegroundColor Red
}
}
