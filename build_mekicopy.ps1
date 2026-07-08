param(
    [switch]$SkipDependencyInstall,
    [switch]$SkipSmokeTests,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$PipVersion = "26.1.2"
$RequirementsFile = Join-Path $PSScriptRoot "requirements-build.txt"
$OnnxRuntimeGpuVersion = "1.27.0"

function Test-BuildPython {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }

    $probe = @'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required")
import tkinter as tk
root = tk.Tk()
root.withdraw()
root.update_idletasks()
root.destroy()
'@

    try {
        $probe | & $Candidate - *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Get-RegistryPythonCandidates {
    $registryRoots = @(
        "HKCU:\Software\Python\PythonCore",
        "HKLM:\Software\Python\PythonCore",
        "HKLM:\Software\WOW6432Node\Python\PythonCore"
    )

    foreach ($registryRoot in $registryRoots) {
        Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue |
            Sort-Object PSChildName -Descending |
            ForEach-Object {
                $installPathKey = Join-Path $_.PSPath "InstallPath"
                $properties = Get-ItemProperty -LiteralPath $installPathKey -ErrorAction SilentlyContinue
                if (-not $properties) {
                    return
                }

                if ($properties.ExecutablePath) {
                    $properties.ExecutablePath
                }

                $installPath = $properties.'(default)'
                if ($installPath) {
                    foreach ($name in @("python.exe", "python3.exe", "python$($_.PSChildName).exe")) {
                        Join-Path $installPath $name
                    }
                }
            }
    }
}

function Resolve-BuildPython {
    param([string]$RequestedPython)

    $candidates = [System.Collections.Generic.List[string]]::new()
    if ($RequestedPython) {
        $candidates.Add($RequestedPython)
    }
    else {
        $localBuildPython = Join-Path $PSScriptRoot ".build-python\python.exe"
        $candidates.Add($localBuildPython)

        foreach ($commandName in @("python", "python3")) {
            $command = Get-Command $commandName -ErrorAction SilentlyContinue
            if ($command -and $command.Source) {
                $candidates.Add($command.Source)
            }
        }

        foreach ($candidate in Get-RegistryPythonCandidates) {
            if ($candidate) {
                $candidates.Add([string]$candidate)
            }
        }
    }

    $seen = @{}
    foreach ($candidate in $candidates) {
        $expanded = [Environment]::ExpandEnvironmentVariables($candidate)
        $key = $expanded.ToLowerInvariant()
        if ($seen.ContainsKey($key)) {
            continue
        }
        $seen[$key] = $true

        Write-Host "Checking build Python: $expanded"
        if (Test-BuildPython -Candidate $expanded) {
            return (Resolve-Path -LiteralPath $expanded).Path
        }
        Write-Warning "Rejected Python because Tk initialization failed: $expanded"
    }

    if ($RequestedPython) {
        throw "The requested Python cannot create a Tk window: $RequestedPython"
    }
    throw "No usable Python with Tk was found. Install Python 3.11+ with Tcl/Tk, or pass -PythonExe."
}

function Invoke-CheckedPython {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & $script:PythonExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE`: $($Arguments -join ' ')"
    }
}

function Invoke-CheckedPythonScript {
    param([Parameter(Mandatory = $true)][string]$Script)

    $Script | & $script:PythonExe -
    if ($LASTEXITCODE -ne 0) {
        throw "Python script failed with exit code $LASTEXITCODE"
    }
}

function Remove-WorkspaceDirectory {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
    $target = [System.IO.Path]::GetFullPath((Join-Path $root $RelativePath))
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the workspace: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

function Invoke-ExeSmokeTest {
    param(
        [Parameter(Mandatory = $true)][string]$ExePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [int]$TimeoutSeconds = 300
    )

    $process = Start-Process `
        -FilePath $ExePath `
        -ArgumentList $Arguments `
        -PassThru `
        -WindowStyle Hidden
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "Smoke test timed out: $ExePath $($Arguments -join ' ')"
    }
    if ($process.ExitCode -ne 0) {
        throw "Smoke test failed with exit code $($process.ExitCode): $ExePath $($Arguments -join ' ')"
    }
}

function Get-FreeTcpPort {
    $listener = [System.Net.Sockets.TcpListener]::new(
        [System.Net.IPAddress]::Loopback,
        0
    )
    $listener.Start()
    try {
        return ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    }
    finally {
        $listener.Stop()
    }
}

function Invoke-HealthSmokeTest {
    param(
        [Parameter(Mandatory = $true)][string]$ExePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutSeconds = 30
    )

    $process = Start-Process `
        -FilePath $ExePath `
        -ArgumentList $Arguments `
        -PassThru `
        -WindowStyle Hidden
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($process.HasExited) {
                throw "Process exited before its health endpoint became ready: $ExePath"
            }
            try {
                $response = Invoke-WebRequest `
                    -UseBasicParsing `
                    -Uri "http://127.0.0.1:$Port/health" `
                    -TimeoutSec 1
                if ($response.StatusCode -eq 200) {
                    return
                }
            }
            catch {
                Start-Sleep -Milliseconds 250
            }
        }
        throw "Health check timed out: $ExePath"
    }
    finally {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $process.WaitForExit()
        }
    }
}

$script:PythonExe = Resolve-BuildPython -RequestedPython $PythonExe
Write-Host "Using build Python: $script:PythonExe"
$versionProbe = @'
import sys
import tkinter
print(sys.version)
print(f"Tk {tkinter.TkVersion}")
'@
Invoke-CheckedPythonScript $versionProbe

if (-not $SkipDependencyInstall) {
    Invoke-CheckedPython @("-m", "pip", "install", "--upgrade", "pip==$PipVersion")
    Invoke-CheckedPython @(
        "-m", "pip", "install", "--upgrade", "--upgrade-strategy", "eager",
        "--requirement", $RequirementsFile
    )
    # meikiocr depends on the CPU distribution, while onnxruntime-gpu exposes
    # the same import package. Reinstall the GPU wheel last so its binaries win.
    Invoke-CheckedPython @(
        "-m", "pip", "install", "--force-reinstall", "--no-deps",
        "onnxruntime-gpu==$OnnxRuntimeGpuVersion"
    )
}

$dependencyProbe = @'
import tkinter as tk
from importlib.metadata import version
import PyInstaller
import fastapi
import huggingface_hub
import meikiocr
import mss
import onnxruntime
import PIL
import sherpa_onnx
import soundcard
import uvicorn
root = tk.Tk()
root.withdraw()
root.update_idletasks()
root.destroy()
expected = {
    "meikiocr": "0.3.4",
    "pyinstaller": "6.21.0",
    "mss": "10.2.0",
    "pillow": "12.2.0",
    "fastapi": "0.138.0",
    "uvicorn": "0.49.0",
    "huggingface-hub": "1.20.1",
    "onnxruntime-gpu": "1.27.0",
    "sherpa-onnx": "1.13.3",
    "SoundCard": "0.4.6",
}
for package, wanted in expected.items():
    actual = version(package)
    if actual != wanted:
        raise SystemExit(f"{package} {actual} is installed; expected {wanted}")
    print(f"{package}=={actual}")
print("Pinned build dependencies and Tk are ready")
'@
Invoke-CheckedPythonScript $dependencyProbe

$modelDir = Join-Path $PSScriptRoot "runtime_models\meikiocr"
New-Item -ItemType Directory -Path $modelDir -Force | Out-Null

$prepareModels = @'
from pathlib import Path
import shutil
import meikiocr.ocr as o

models = [
    (o.DET_MODEL_REPO, o.DET_MODEL_NAME),
    (o.REC_MODEL_REPO, o.REC_MODEL_NAME),
]
if hasattr(o, "VREC_MODEL_NAME"):
    models.append((o.REC_MODEL_REPO, o.VREC_MODEL_NAME))

seen = set()
unique_models = []
for model in models:
    if model in seen:
        continue
    seen.add(model)
    unique_models.append(model)

dest = Path("runtime_models") / "meikiocr"
dest.mkdir(parents=True, exist_ok=True)

missing_models = []
for repo_id, filename in unique_models:
    target = dest / filename
    if target.exists() and target.stat().st_size > 0:
        print(f"Using prepared model: {target}")
    else:
        missing_models.append((repo_id, filename))

if missing_models:
    from huggingface_hub import hf_hub_download

for repo_id, filename in missing_models:
    src = hf_hub_download(repo_id=repo_id, filename=filename)
    target = dest / filename
    shutil.copy2(src, target)
    print(f"Prepared model: {target}")
'@
Invoke-CheckedPythonScript $prepareModels

# VAD/STT models are intentionally not prepared during packaging. The
# MekiAudioCapture executable downloads them into MekiAudioCapture/models on
# first use and reuses existing files on subsequent runs.

Remove-WorkspaceDirectory "build"
$distRelativePath = "dist"
try {
    Remove-WorkspaceDirectory $distRelativePath
}
catch {
    # A running copy of an older build can keep a DLL locked on Windows.
    # Preserve that process and publish the new verified build separately.
    Write-Warning "The existing dist folder is in use; publishing to dist-new instead."
    $distRelativePath = "dist-new"
    Remove-WorkspaceDirectory $distRelativePath
}
$distRoot = Join-Path $PSScriptRoot $distRelativePath

$specs = @(
    ".\MekiCopy.spec",
    ".\HYTrans.spec",
    ".\MekiDisplay.spec",
    ".\MekiAudioCapture.spec"
)
foreach ($spec in $specs) {
    Invoke-CheckedPython @(
        "-m", "PyInstaller", "--noconfirm", "--clean",
        "--distpath", $distRoot,
        $spec
    )
}

$mekiCopyExe = Join-Path $distRoot "MekiCopy\MekiCopy.exe"
$hyTransExe = Join-Path $distRoot "HYTrans\HYTrans.exe"
$overlayerExe = Join-Path $distRoot "MekiDisplay\MekiOverlayer.exe"
$scriptExe = Join-Path $distRoot "MekiDisplay\MekiScript.exe"
$audioCaptureExe = Join-Path $distRoot "MekiAudioCapture\MekiAudioCapture.exe"
$expectedExecutables = @($mekiCopyExe, $hyTransExe, $overlayerExe, $scriptExe, $audioCaptureExe)
foreach ($exe in $expectedExecutables) {
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "Expected executable was not created: $exe"
    }
    $pythonDll = Get-ChildItem `
        -LiteralPath (Split-Path -Parent $exe) `
        -Recurse `
        -Filter "python*.dll" `
        -File |
        Select-Object -First 1
    if (-not $pythonDll) {
        throw "Bundled Python DLL was not found beside: $exe"
    }
}

# When a verified source-side HYTrans model already exists, publish it beside
# HYTrans.exe without duplicating the 1.4 GB external-data file on this volume.
# A release built without these files still downloads them directly into the
# same HYTrans\models path on first launch.
$hyTransModelSource = Join-Path $PSScriptRoot "models\onnx-community\HY-MT1.5-1.8B-ONNX"
$hyTransRequiredFiles = @(
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "onnx\model_q4.onnx",
    "onnx\model_q4.onnx_data"
)
$hasPreparedHyTransModel = $true
foreach ($relativeFile in $hyTransRequiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $hyTransModelSource $relativeFile) -PathType Leaf)) {
        $hasPreparedHyTransModel = $false
        break
    }
}
if ($hasPreparedHyTransModel) {
    $hyTransModelTarget = Join-Path $distRoot "HYTrans\models\onnx-community\HY-MT1.5-1.8B-ONNX"
    Get-ChildItem -LiteralPath $hyTransModelSource -Recurse -File | ForEach-Object {
        $relativeFile = $_.FullName.Substring($hyTransModelSource.Length).TrimStart("\")
        $targetFile = Join-Path $hyTransModelTarget $relativeFile
        New-Item -ItemType Directory -Path (Split-Path -Parent $targetFile) -Force | Out-Null
        if ($_.Name -eq ".hytrans-model-manifest.json") {
            Copy-Item -LiteralPath $_.FullName -Destination $targetFile -Force
        }
        else {
            try {
                New-Item -ItemType HardLink -Path $targetFile -Target $_.FullName -Force | Out-Null
            }
            catch {
                Copy-Item -LiteralPath $_.FullName -Destination $targetFile -Force
            }
        }
    }
    Write-Host "Prepared local HYTrans model: $hyTransModelTarget"
}

if (-not $SkipSmokeTests) {
    Write-Host "Running executable smoke tests..."
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-runtime")
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-ui")
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-tray-stress")
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-detached-button")
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-detached-survival")

    $hyTransPort = Get-FreeTcpPort
    Invoke-HealthSmokeTest `
        $hyTransExe `
        @("--port", "$hyTransPort", "--no-browser") `
        $hyTransPort

    $overlayerPort = Get-FreeTcpPort
    Invoke-HealthSmokeTest `
        $overlayerExe `
        @("--port", "$overlayerPort") `
        $overlayerPort

    Invoke-ExeSmokeTest $audioCaptureExe @("--self-test")
    Invoke-ExeSmokeTest $scriptExe @("--self-test")

    $scriptPort = Get-FreeTcpPort
    Invoke-HealthSmokeTest `
        $scriptExe `
        @("--port", "$scriptPort") `
        $scriptPort

    $audioPort = Get-FreeTcpPort
    Invoke-HealthSmokeTest `
        $audioCaptureExe `
        @("--port", "$audioPort", "--self-test-server") `
        $audioPort
}

# UI smoke tests intentionally publish a synthetic OCR region and window
# geometry. Do not ship that test state to users.
foreach ($runtimeStateName in @(
    "detached_button_region.json",
    "detached_button_geometry.json"
)) {
    $runtimeStatePath = Join-Path (Split-Path -Parent $mekiCopyExe) $runtimeStateName
    if (Test-Path -LiteralPath $runtimeStatePath -PathType Leaf) {
        Remove-Item -LiteralPath $runtimeStatePath -Force
    }
}

$launcherPath = Join-Path $distRoot "Start-MekiCopy.bat"
$launcherContent = @'
@echo off
setlocal
cd /d "%~dp0MekiCopy"
start "" "MekiCopy.exe"
'@
Set-Content -LiteralPath $launcherPath -Value $launcherContent -Encoding ASCII

Write-Host ""
Write-Host "Build complete and verified:"
Write-Host $launcherPath
Write-Host $mekiCopyExe
Write-Host $hyTransExe
Write-Host $overlayerExe
Write-Host $scriptExe
Write-Host $audioCaptureExe
