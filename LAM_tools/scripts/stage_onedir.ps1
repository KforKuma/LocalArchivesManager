[CmdletBinding()]
param(
    [string]$BuildRoot = "D:\LAM_build",
    [string]$ReleaseName = "",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$RepositoryRoot = (Resolve-Path -LiteralPath (Join-Path $ProjectRoot "..")).Path
$BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
$VersionFile = Join-Path $ProjectRoot "src\lam\versions.py"
$oldVersionFile = $env:LAM_VERSION_FILE
$env:LAM_VERSION_FILE = $VersionFile
try {
    $PackageVersion = (& $PythonExe -c "import os, runpy; print(runpy.run_path(os.environ['LAM_VERSION_FILE'])['PACKAGE_VERSION'])").Trim()
}
finally {
    $env:LAM_VERSION_FILE = $oldVersionFile
}
if (-not $PackageVersion) {
    throw "Could not read PACKAGE_VERSION from: $VersionFile"
}
if (-not $ReleaseName) {
    $ReleaseName = "LAM-$PackageVersion-windows-x64"
}
$Source = Join-Path $BuildRoot ("dist\LAM-" + $PackageVersion)
$Release = Join-Path $BuildRoot ("release\" + $ReleaseName)
$Packaging = Join-Path $ProjectRoot "packaging"

if ($BuildRoot.StartsWith("D:\ResearchLibrary\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Release staging must not use D:\ResearchLibrary as its output tree"
}
if (-not (Test-Path -LiteralPath (Join-Path $Source "lam.exe") -PathType Leaf)) {
    throw "Clean PyInstaller output is missing: $Source"
}
$releaseFull = [System.IO.Path]::GetFullPath($Release)
if (-not $releaseFull.StartsWith($BuildRoot + "\release\", [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe release target: $releaseFull"
}
if (Test-Path -LiteralPath $releaseFull) {
    Remove-Item -LiteralPath $releaseFull -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $releaseFull | Out-Null
Copy-Item -Path (Join-Path $Source "*") -Destination $releaseFull -Recurse -Force

$modelSource = Join-Path $Packaging "assets\easyocr-models"
$modelTarget = Join-Path $releaseFull "models\easyocr"
$popplerSource = Join-Path $Packaging "vendor\poppler"
$popplerTarget = Join-Path $releaseFull "vendor\poppler"
foreach ($required in @($modelSource, $popplerSource)) {
    if (-not (Test-Path -LiteralPath $required -PathType Container)) {
        throw "Prepared release asset is missing: $required"
    }
}
New-Item -ItemType Directory -Force -Path $modelTarget, $popplerTarget | Out-Null
Copy-Item -Path (Join-Path $modelSource "*") -Destination $modelTarget -Recurse -Force
Copy-Item -Path (Join-Path $popplerSource "*") -Destination $popplerTarget -Recurse -Force
Copy-Item -LiteralPath (Join-Path $Packaging "manifests\easyocr-models.json") -Destination (Join-Path $modelTarget "manifest.json") -Force
Copy-Item -LiteralPath (Join-Path $Packaging "manifests\poppler-windows.json") -Destination (Join-Path $popplerTarget "manifest.json") -Force

$documents = @(
    @{ Source = (Join-Path $RepositoryRoot "AGENTS.md"); Target = "AGENTS.md" },
    @{ Source = (Join-Path $RepositoryRoot "Workflows.md"); Target = "Workflows.md" },
    @{ Source = (Join-Path $RepositoryRoot "README.md"); Target = "README.md" },
    @{ Source = (Join-Path $RepositoryRoot "LICENSE"); Target = "LICENSE" },
    @{ Source = (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md"); Target = "THIRD_PARTY_NOTICES.md" },
    @{ Source = (Join-Path $ProjectRoot ".env.example"); Target = ".env.example" },
    @{ Source = (Join-Path $Packaging "templates\setup-lam.bat"); Target = "setup-lam.bat" },
    @{ Source = (Join-Path $Packaging "templates\open-lam-terminal.bat"); Target = "open-lam-terminal.bat" }
)
foreach ($item in $documents) {
    if (-not (Test-Path -LiteralPath $item.Source -PathType Leaf)) {
        throw "Required release document is missing: $($item.Source)"
    }
    Copy-Item -LiteralPath $item.Source -Destination (Join-Path $releaseFull $item.Target) -Force
}

# Public source documentation intentionally uses the maintainer's real library
# root in examples. The distributable copy must not disclose that development
# path, including package-resource and distribution-metadata copies collected
# under _internal. Source documents and package templates remain byte-identical.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$textNames = @("METADATA", "README", ".env.example")
$textExtensions = @(".md", ".txt", ".json", ".bat")
foreach ($path in Get-ChildItem -LiteralPath $releaseFull -Recurse -File) {
    if ($path.Name -notin $textNames -and $path.Extension.ToLowerInvariant() -notin $textExtensions) {
        continue
    }
    $text = [System.IO.File]::ReadAllText($path.FullName)
    $sanitized = $text.Replace("D:\ResearchLibrary", "C:\LAM_Library")
    $sanitized = $sanitized.Replace("D:/ResearchLibrary", "C:/LAM_Library")
    $sanitized = $sanitized.Replace("D:\LAM_build", "C:\LAM_Build")
    $sanitized = $sanitized.Replace("D:/LAM_build", "C:/LAM_Build")
    if ($sanitized -ne $text) {
        [System.IO.File]::WriteAllText($path.FullName, $sanitized, $utf8NoBom)
    }
}

& $PythonExe (Join-Path $PSScriptRoot "verify_release_tree.py") --release-root $releaseFull --forbidden-string $env:USERNAME
if ($LASTEXITCODE -ne 0) {
    throw "Release-tree verification failed"
}
Write-Host "LAM release staging completed under: $releaseFull"
