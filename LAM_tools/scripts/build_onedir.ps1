[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [string]$BuildRoot = "D:\LAM_build",
    [switch]$NoClean
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$SpecPath = Join-Path $ProjectRoot "packaging\lam.spec"
$BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
$WorkPath = Join-Path $BuildRoot "work\pyinstaller"
$DistPath = Join-Path $BuildRoot "dist"
$LogPath = Join-Path $BuildRoot "logs\pyinstaller-build.log"

if ($BuildRoot -eq $ProjectRoot -or $BuildRoot.StartsWith($ProjectRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "BuildRoot must be independent from the source repository: $BuildRoot"
}
if ($BuildRoot.StartsWith("D:\ResearchLibrary\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "D:\ResearchLibrary must never be used as the PyInstaller output tree"
}
if (-not (Test-Path -LiteralPath $SpecPath -PathType Leaf)) {
    throw "PyInstaller spec is missing: $SpecPath"
}
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -and -not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
    throw "Python executable is unavailable: $PythonExe"
}

New-Item -ItemType Directory -Force -Path $BuildRoot, (Split-Path $LogPath -Parent) | Out-Null
if (-not $NoClean) {
    foreach ($target in @($WorkPath, $DistPath)) {
        $full = [System.IO.Path]::GetFullPath($target)
        if (-not $full.StartsWith($BuildRoot + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean an output outside BuildRoot: $full"
        }
        if (Test-Path -LiteralPath $full) {
            Remove-Item -LiteralPath $full -Recurse -Force
        }
    }
}
New-Item -ItemType Directory -Force -Path $WorkPath, $DistPath | Out-Null

& $PythonExe -c "import PyInstaller, sys; print(f'Python {sys.version.split()[0]}; PyInstaller {PyInstaller.__version__}; executable={sys.executable}')"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is unavailable through: $PythonExe"
}

$PyInstallerArgs = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--log-level", "INFO",
    "--workpath", $WorkPath,
    "--distpath", $DistPath
)
if (-not $NoClean) {
    $PyInstallerArgs += "--clean"
}
$PyInstallerArgs += $SpecPath

$oldHashSeed = $env:PYTHONHASHSEED
$env:PYTHONHASHSEED = "0"
$transcriptStarted = $false
try {
    Start-Transcript -LiteralPath $LogPath -Force | Out-Null
    $transcriptStarted = $true
}
catch {
    Write-Warning "PowerShell transcript is unavailable; continuing with host output logging: $($_.Exception.Message)"
}
try {
    Push-Location $ProjectRoot
    try {
        & $PythonExe @PyInstallerArgs
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller build failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
    $env:PYTHONHASHSEED = $oldHashSeed
}

Write-Host "LAM clean onedir build completed under: $DistPath"
