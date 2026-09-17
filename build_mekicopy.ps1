param(
    [switch]$SkipDependencyInstall,
    [switch]$SkipSmokeTests,
    [string]$PythonExe = "",
    [ValidateSet("Lite", "Full")]
    [string]$PackageFlavor = "Lite",
    [string]$FullAssetsRoot = "",
    [string]$FullMagpieRoot = ""
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$PipVersion = "26.2.1"
$RequirementsFile = Join-Path $PSScriptRoot "requirements-build.txt"
$OnnxRuntimeGpuVersion = "1.30.0"

function Test-BuildPython {
    param([Parameter(Mandatory = $true)][string]$Candidate)

    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return $false
    }

    $probe = @'
import sys
if not ((3, 12) <= sys.version_info[:2] < (3, 15)):
    raise SystemExit("Python 3.12 through 3.14 is required")
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
        Write-Warning "Rejected Python because its version or Tk runtime is unsupported: $expanded"
    }

    if ($RequestedPython) {
        throw "The requested Python cannot create a Tk window: $RequestedPython"
    }
    throw "No usable Python with Tk was found. Install Python 3.12-3.14 with Tcl/Tk, or pass -PythonExe."
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

function Assert-ArtifactFile {
    param(
        [Parameter(Mandatory = $true)][string]$AppRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $path = Join-Path $AppRoot $RelativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required $Description is missing: $path"
    }
}

function Assert-RuntimeAssetManifest {
    param(
        [Parameter(Mandatory = $true)][string]$AssetsRoot,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $manifestPath = Join-Path $AssetsRoot "runtime_manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "$Description runtime manifest is missing: $manifestPath"
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "$Description runtime manifest is invalid JSON: $manifestPath"
    }

    if ($manifest.transformers.version -cne "4.2.0") {
        throw "$Description has an unexpected Transformers.js version"
    }
    if ($manifest.onnxRuntimeWeb.version -cne "1.27.0") {
        throw "$Description has an unexpected ONNX Runtime Web version"
    }

    $requiredPaths = @(
        "transformers.min.js",
        "transformers.LICENSE.txt",
        "onnxruntime-web.LICENSE.txt",
        "onnxruntime-web.ThirdPartyNotices.txt",
        "worker.html",
        "worker.js",
        "wasm/ort-wasm-simd-threaded.asyncify.mjs",
        "wasm/ort-wasm-simd-threaded.asyncify.wasm",
        "wasm/ort-wasm-simd-threaded.jsep.mjs",
        "wasm/ort-wasm-simd-threaded.jsep.wasm",
        "wasm/ort-wasm-simd-threaded.jspi.mjs",
        "wasm/ort-wasm-simd-threaded.jspi.wasm",
        "wasm/ort-wasm-simd-threaded.mjs",
        "wasm/ort-wasm-simd-threaded.wasm"
    )
    $entriesByPath = @{}
    foreach ($entry in @($manifest.files)) {
        $entriesByPath[[string]$entry.path] = $entry
    }
    foreach ($relativePath in $requiredPaths) {
        $entry = $entriesByPath[$relativePath]
        if ($null -eq $entry) {
            throw "$Description manifest is missing '$relativePath'"
        }
        $path = Join-Path $AssetsRoot $relativePath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "$Description asset is missing: $path"
        }
        $item = Get-Item -LiteralPath $path
        if ($item.Length -ne [long]$entry.size) {
            throw (
                "$Description asset '$relativePath' is $($item.Length) bytes; " +
                "expected $($entry.size)"
            )
        }
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        if ($actualHash -cne ([string]$entry.sha256).ToUpperInvariant()) {
            throw "$Description asset hash mismatch: $path"
        }
    }
}

function Assert-ArtifactPattern {
    param(
        [Parameter(Mandatory = $true)][string]$AppRoot,
        [Parameter(Mandatory = $true)][string]$RelativeDirectory,
        [Parameter(Mandatory = $true)][string]$FilePattern,
        [Parameter(Mandatory = $true)][string]$Description
    )

    $directory = Join-Path $AppRoot $RelativeDirectory
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "Required artifact directory for $Description is missing: $directory"
    }

    $matches = @(
        Get-ChildItem `
            -LiteralPath $directory `
            -Filter $FilePattern `
            -File `
            -ErrorAction Stop
    )
    if ($matches.Count -eq 0) {
        throw "Required $Description matching '$FilePattern' is missing from: $directory"
    }
}

function Assert-BundledPythonRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$AppRoot,
        [Parameter(Mandatory = $true)][string]$AppName
    )

    Assert-ArtifactPattern `
        -AppRoot $AppRoot `
        -RelativeDirectory "_internal" `
        -FilePattern "python*.dll" `
        -Description "$AppName bundled Python runtime"
}

function Assert-TkRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$AppRoot,
        [Parameter(Mandatory = $true)][string]$AppName
    )

    Assert-ArtifactPattern `
        -AppRoot $AppRoot `
        -RelativeDirectory "_internal" `
        -FilePattern "_tkinter*.pyd" `
        -Description "$AppName Tk extension"
    Assert-ArtifactFile `
        -AppRoot $AppRoot `
        -RelativePath "_internal\tcl86t.dll" `
        -Description "$AppName Tcl runtime"
    Assert-ArtifactFile `
        -AppRoot $AppRoot `
        -RelativePath "_internal\tk86t.dll" `
        -Description "$AppName Tk runtime"
    Assert-ArtifactFile `
        -AppRoot $AppRoot `
        -RelativePath "_internal\_tcl_data\init.tcl" `
        -Description "$AppName Tcl script library"
    Assert-ArtifactFile `
        -AppRoot $AppRoot `
        -RelativePath "_internal\_tk_data\tk.tcl" `
        -Description "$AppName Tk script library"
}

function New-ReleaseZip {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$ArchivePath
    )

    # Windows 10 includes bsdtar. Use its ZIP writer rather than
    # Compress-Archive so large optional model files remain packageable.
    $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
    if (-not $tar -or -not $tar.Source) {
        throw "tar.exe is required to create the release ZIP. Use Windows 10 or newer."
    }

    # Archives are always created in a unique staging directory.  Refusing to
    # overwrite here ensures a partially failed build can never erase the last
    # verified release archive.
    if (Test-Path -LiteralPath $ArchivePath) {
        throw "Staged release archive path already exists: $ArchivePath"
    }

    $parent = Split-Path -Parent $SourceRoot
    $directoryName = Split-Path -Leaf $SourceRoot
    if (-not $parent -or -not $directoryName) {
        throw "Cannot determine archive root for: $SourceRoot"
    }

    & $tar.Source --format=zip -c -f $ArchivePath -C $parent $directoryName
    if ($LASTEXITCODE -ne 0) {
        throw "tar.exe failed while creating release ZIP: $ArchivePath"
    }
    if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf) -or
        (Get-Item -LiteralPath $ArchivePath).Length -le 0) {
        throw "Release ZIP was not created: $ArchivePath"
    }
}

function Publish-StagedRelease {
    param(
        [Parameter(Mandatory = $true)][string]$StagingRoot,
        [Parameter(Mandatory = $true)][string]$StagedReleaseRoot,
        [Parameter(Mandatory = $true)][string]$ReleaseRoot,
        [Parameter(Mandatory = $true)][string]$StagedArchivePath,
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$StagedChecksumPath,
        [Parameter(Mandatory = $true)][string]$ChecksumPath
    )

    # Build and smoke-test every artifact before touching a previous release.
    # All paths are under the workspace, so same-volume moves are renames.  A
    # backup lets us restore the prior set if publishing any later artifact
    # fails (for example because a user still has the old ZIP open).
    $operations = @(
        [pscustomobject]@{
            Source = $StagedReleaseRoot
            Destination = $ReleaseRoot
            Backup = $null
            Published = $false
            Description = "release directory"
        },
        [pscustomobject]@{
            Source = $StagedArchivePath
            Destination = $ArchivePath
            Backup = $null
            Published = $false
            Description = "release archive"
        },
        [pscustomobject]@{
            Source = $StagedChecksumPath
            Destination = $ChecksumPath
            Backup = $null
            Published = $false
            Description = "release checksum"
        }
    )
    foreach ($operation in $operations) {
        if (-not (Test-Path -LiteralPath $operation.Source)) {
            throw "Staged $($operation.Description) is missing: $($operation.Source)"
        }
    }

    $backupSuffix = "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$([guid]::NewGuid().ToString('N'))"
    try {
        foreach ($operation in $operations) {
            if (Test-Path -LiteralPath $operation.Destination) {
                $operation.Backup = "$($operation.Destination).previous-$backupSuffix"
                Move-Item `
                    -LiteralPath $operation.Destination `
                    -Destination $operation.Backup `
                    -ErrorAction Stop
            }
            Move-Item `
                -LiteralPath $operation.Source `
                -Destination $operation.Destination `
                -ErrorAction Stop
            $operation.Published = $true
        }
    }
    catch {
        $publishError = $_
        for ($index = $operations.Count - 1; $index -ge 0; $index--) {
            $operation = $operations[$index]
            if ($operation.Published -and (Test-Path -LiteralPath $operation.Destination)) {
                $failedPath = Join-Path `
                    $StagingRoot `
                    ("publish-failed-$index-" + (Split-Path -Leaf $operation.Destination))
                try {
                    Move-Item `
                        -LiteralPath $operation.Destination `
                        -Destination $failedPath `
                        -ErrorAction Stop
                }
                catch {
                    Write-Warning "Could not move the new $($operation.Description) aside during rollback: $($operation.Destination)"
                    continue
                }
            }
            if ($operation.Backup -and (Test-Path -LiteralPath $operation.Backup)) {
                try {
                    Move-Item `
                        -LiteralPath $operation.Backup `
                        -Destination $operation.Destination `
                        -ErrorAction Stop
                }
                catch {
                    Write-Warning "Could not restore the previous $($operation.Description): $($operation.Backup)"
                }
            }
        }
        throw $publishError
    }

    # Only remove the replaced artifacts after the complete new set has been
    # published.  A cleanup failure does not invalidate the verified release.
    foreach ($operation in $operations) {
        if ($operation.Backup -and (Test-Path -LiteralPath $operation.Backup)) {
            try {
                Remove-Item `
                    -LiteralPath $operation.Backup `
                    -Recurse `
                    -Force `
                    -ErrorAction Stop
            }
            catch {
                Write-Warning "Previous $($operation.Description) was retained at: $($operation.Backup)"
            }
        }
    }
}

function Copy-ReleaseTree {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$TargetRoot,
        [Parameter(Mandatory = $true)][string]$Description
    )

    if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
        throw "Full package source for $Description is missing: $SourceRoot"
    }

    $source = (Resolve-Path -LiteralPath $SourceRoot).Path.TrimEnd('\', '/')
    New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null
    Get-ChildItem -LiteralPath $source -Recurse -File -Force | ForEach-Object {
        $relativePath = $_.FullName.Substring($source.Length).TrimStart('\', '/')
        $destination = Join-Path $TargetRoot $relativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        # The release ZIP receives ordinary file contents. Hard links merely
        # avoid consuming a second multi-gigabyte working copy while Full is
        # assembled on the same NTFS volume.
        try {
            New-Item -ItemType HardLink -Path $destination -Target $_.FullName -Force | Out-Null
        }
        catch {
            Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
        }
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
        [hashtable]$ExpectedConfig,
        [switch]$GracefulShutdown,
        [int]$TimeoutSeconds = 30
    )

    $process = Start-Process `
        -FilePath $ExePath `
        -ArgumentList $Arguments `
        -PassThru `
        -WindowStyle Hidden
    $controlToken = ""
    try {
        $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
        $healthy = $false
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
                    try {
                        $healthPayload = $response.Content |
                            ConvertFrom-Json -ErrorAction Stop
                        $controlToken = [string]$healthPayload.controlToken
                    }
                    catch {
                        $controlToken = ""
                    }
                    $healthy = $true
                    break
                }
            }
            catch {
                Start-Sleep -Milliseconds 250
            }
        }
        if (-not $healthy) {
            throw "Health check timed out: $ExePath"
        }

        if ($ExpectedConfig) {
            $configResponse = Invoke-WebRequest `
                -UseBasicParsing `
                -Uri "http://127.0.0.1:$Port/config" `
                -TimeoutSec 120
            if ($configResponse.StatusCode -ne 200) {
                throw "Config check failed with HTTP $($configResponse.StatusCode): $ExePath"
            }
            try {
                $config = $configResponse.Content | ConvertFrom-Json -ErrorAction Stop
            }
            catch {
                throw "Config endpoint returned invalid JSON: $ExePath"
            }
            foreach ($entry in $ExpectedConfig.GetEnumerator()) {
                $property = $config.PSObject.Properties[$entry.Key]
                if ($null -eq $property) {
                    throw "Config field '$($entry.Key)' is missing: $ExePath"
                }
                if ([string]$property.Value -cne [string]$entry.Value) {
                    throw (
                        "Config field '$($entry.Key)' was '$($property.Value)'; " +
                        "expected '$($entry.Value)': $ExePath"
                    )
                }
            }
        }
    }
    finally {
        $gracefulFailure = ""
        if ($GracefulShutdown -and $process -and -not $process.HasExited) {
            try {
                if (-not $controlToken) {
                    throw "health endpoint did not provide a control token"
                }
                $shutdownResponse = Invoke-WebRequest `
                    -UseBasicParsing `
                    -Uri "http://127.0.0.1:$Port/shutdown" `
                    -Method Post `
                    -Headers @{ "X-HYTrans-Shutdown-Token" = $controlToken } `
                    -ContentType "application/json" `
                    -Body "{}" `
                    -TimeoutSec 3
                if ($shutdownResponse.StatusCode -ne 200) {
                    throw "shutdown returned HTTP $($shutdownResponse.StatusCode)"
                }
                if (-not $process.WaitForExit(15000)) {
                    throw "process did not exit within 15 seconds"
                }
            }
            catch {
                $gracefulFailure = $_.Exception.Message
            }
        }
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            $process.WaitForExit()
        }
        if ($gracefulFailure) {
            throw "Graceful shutdown smoke failed for $ExePath`: $gracefulFailure"
        }
    }
}

Assert-RuntimeAssetManifest `
    -AssetsRoot (Join-Path $PSScriptRoot "assets") `
    -Description "Source HYTrans"

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
    # CUDA/cuDNN extras were already resolved from requirements-build.txt and
    # are bundled with MekiCopy below; do not depend on a user's CUDA toolkit.
    Invoke-CheckedPython @(
        "-m", "pip", "install", "--force-reinstall", "--no-deps",
        "onnxruntime-gpu[cuda,cudnn]==$OnnxRuntimeGpuVersion"
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
import numpy
import onnxruntime
import PIL
import pydantic
import sherpa_onnx
import soundcard
import typer
import uvicorn
import cv2
from importlib.util import find_spec
if not callable(getattr(sherpa_onnx.OfflineRecognizer, "from_nemo_ctc", None)):
    raise SystemExit("sherpa-onnx does not provide OfflineRecognizer.from_nemo_ctc")
root = tk.Tk()
root.withdraw()
root.update_idletasks()
root.destroy()
expected = {
    "meikiocr": "0.3.4",
    "pyinstaller": "6.22.3",
    "mss": "10.2.0",
    "pillow": "12.3.0",
    "numpy": "2.5.3",
    "opencv-python-headless": "5.0.0.93",
    "fastapi": "0.141.1",
    "uvicorn": "0.53.0",
    "pydantic": "2.13.5",
    "typer": "0.27.2",
    "huggingface-hub": "1.31.0",
    "onnxruntime": "1.30.0",
    "onnxruntime-gpu": "1.30.0",
    "sherpa-onnx": "1.13.8",
    "SoundCard": "0.4.6",
}
for package, wanted in expected.items():
    actual = version(package)
    if actual != wanted:
        raise SystemExit(f"{package} {actual} is installed; expected {wanted}")
    print(f"{package}=={actual}")
if "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
    raise SystemExit(
        "onnxruntime-gpu metadata is installed, but CUDAExecutionProvider is missing; "
        "the CPU wheel likely overwrote the GPU runtime"
    )
# ONNX Runtime 1.30 uses CUDA 13's consolidated package layout. The CUDA
# extras share ``nvidia.cu13`` while cuDNN remains in ``nvidia.cudnn``.
# Checking the importable namespaces catches a partial extras install before
# PyInstaller can silently create a CPU-only release.
gpu_runtime_packages = (
    "nvidia.cu13",
    "nvidia.cudnn",
)
missing_gpu_runtime_packages = [
    package for package in gpu_runtime_packages if find_spec(package) is None
]
if missing_gpu_runtime_packages:
    raise SystemExit(
        "onnxruntime-gpu CUDA/cuDNN runtime packages are missing: "
        + ", ".join(missing_gpu_runtime_packages)
    )
print(f"ONNX Runtime providers: {onnxruntime.get_available_providers()}")
print("Pinned build dependencies and Tk are ready")
'@
Invoke-CheckedPythonScript $dependencyProbe

Write-Host "Running source regression tests..."
Invoke-CheckedPython @("-m", "unittest", "discover", "-s", "tests", "-v")

if ($PackageFlavor -eq "Full") {
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
}

# Lite packages intentionally omit VAD/STT models. The default Parakeet NeMo
# CTC model (or optional ReazonSpeech) is downloaded into the shared
# MekiAudioCapture/models location on first use and reused afterwards.
# MekiSubtitle points at exactly that cache instead of publishing model copies.

# MekiSubtitle needs only video decoding tools from the former standalone
# ReazonSubtitle app. Translation/STT/VAD models remain shared runtime caches.
$subtitleFfmpegRoot = Join-Path (Split-Path $PSScriptRoot -Parent) "ReazonSubtitle\assets\ffmpeg"
foreach ($subtitleTool in @("ffmpeg.exe", "ffprobe.exe")) {
    $subtitleToolPath = Join-Path $subtitleFfmpegRoot $subtitleTool
    if (-not (Test-Path -LiteralPath $subtitleToolPath -PathType Leaf)) {
        throw "MekiSubtitle required FFmpeg tool is missing: $subtitleToolPath"
    }
}

Remove-WorkspaceDirectory "build"
$releaseName = "MekiCopy-$PackageFlavor"
$stagingRelativePath = Join-Path `
    ".release-staging" `
    ("$releaseName-$([guid]::NewGuid().ToString('N'))")
$stagingRoot = Join-Path $PSScriptRoot $stagingRelativePath
New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
$distRelativePath = Join-Path $stagingRelativePath $releaseName
$distRoot = Join-Path $PSScriptRoot $distRelativePath

$specs = @(
    ".\MekiCopy.spec",
    ".\HYTrans.spec",
    ".\MekiDisplay.spec",
    ".\MekiAudioCapture.spec"
)
$previousPackageFlavor = $env:MEKICOPY_PACKAGE_FLAVOR
$env:MEKICOPY_PACKAGE_FLAVOR = $PackageFlavor
try {
    foreach ($spec in $specs) {
        Invoke-CheckedPython @(
            "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", $distRoot,
            $spec
        )
    }
}
finally {
    if ($null -eq $previousPackageFlavor) {
        Remove-Item Env:MEKICOPY_PACKAGE_FLAVOR -ErrorAction SilentlyContinue
    }
    else {
        $env:MEKICOPY_PACKAGE_FLAVOR = $previousPackageFlavor
    }
}

$mekiCopyExe = Join-Path $distRoot "MekiCopy\MekiCopy.exe"
$hyTransExe = Join-Path $distRoot "HYTrans\HYTrans.exe"
$overlayerExe = Join-Path $distRoot "MekiDisplay\MekiOverlayer.exe"
$scriptExe = Join-Path $distRoot "MekiDisplay\MekiScript.exe"
$audioCaptureExe = Join-Path $distRoot "MekiAudioCapture\MekiAudioCapture.exe"
$mekiCopyRoot = Split-Path -Parent $mekiCopyExe
$hyTransRoot = Split-Path -Parent $hyTransExe
$displayRoot = Split-Path -Parent $overlayerExe
$audioCaptureRoot = Split-Path -Parent $audioCaptureExe
$expectedExecutables = @($mekiCopyExe, $hyTransExe, $overlayerExe, $scriptExe, $audioCaptureExe)
foreach ($exe in $expectedExecutables) {
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "Expected executable was not created: $exe"
    }
    Assert-BundledPythonRuntime `
        -AppRoot (Split-Path -Parent $exe) `
        -AppName ([System.IO.Path]::GetFileNameWithoutExtension($exe))
}

# Validate the runtime resources that a successful PyInstaller command alone
# cannot guarantee: Tk for GUI companions, native OCR, native STT, and the
# HYTrans private-worker assets.
Assert-TkRuntime -AppRoot $mekiCopyRoot -AppName "MekiCopy"
Assert-TkRuntime -AppRoot $displayRoot -AppName "MekiDisplay"
Assert-TkRuntime -AppRoot $audioCaptureRoot -AppName "MekiAudioCapture"

Assert-ArtifactPattern `
    -AppRoot $mekiCopyRoot `
    -RelativeDirectory "_internal\cv2" `
    -FilePattern "cv2*.pyd" `
    -Description "MekiCopy OpenCV native extension"
Assert-ArtifactFile `
    -AppRoot $mekiCopyRoot `
    -RelativePath "_internal\onnxruntime\capi\onnxruntime.dll" `
    -Description "MekiCopy ONNX Runtime core"
Assert-ArtifactFile `
    -AppRoot $mekiCopyRoot `
    -RelativePath "_internal\onnxruntime\capi\onnxruntime_providers_cuda.dll" `
    -Description "MekiCopy ONNX Runtime CUDA provider"
Assert-ArtifactPattern `
    -AppRoot $mekiCopyRoot `
    -RelativeDirectory "_internal\onnxruntime\capi" `
    -FilePattern "onnxruntime_pybind11_state*.pyd" `
    -Description "MekiCopy ONNX Runtime Python extension"
$cudaRuntimeArtifacts = @(
    # ONNX Runtime 1.30 is built for CUDA 13. The CUDA extra wheels preserve
    # this single package-relative directory in the frozen one-dir bundle.
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "cublasLt64_13.dll"; Description = "NVIDIA cuBLASLt runtime" },
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "cublas64_13.dll"; Description = "NVIDIA cuBLAS runtime" },
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "cufft64_12.dll"; Description = "NVIDIA cuFFT runtime" },
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "cudart64_13.dll"; Description = "NVIDIA CUDA runtime" },
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "nvrtc64_13*.dll"; Description = "NVIDIA CUDA NVRTC runtime" },
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "nvrtc-builtins64_13*.dll"; Description = "NVIDIA CUDA NVRTC builtins" },
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "curand64_10.dll"; Description = "NVIDIA cuRAND runtime" },
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "cufftw64_12.dll"; Description = "NVIDIA cuFFTW runtime" },
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "nvblas64_13.dll"; Description = "NVIDIA NVBLAS runtime" },
    @{ Directory = "_internal\nvidia\cu13\bin\x86_64"; Pattern = "nvJitLink_13*.dll"; Description = "NVIDIA NVJITLINK runtime" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn_adv64_9.dll"; Description = "NVIDIA cuDNN advanced runtime" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn_cnn64_9.dll"; Description = "NVIDIA cuDNN CNN runtime" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn_engines_precompiled64_9.dll"; Description = "NVIDIA cuDNN precompiled engine" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn_engines_runtime_compiled64_9.dll"; Description = "NVIDIA cuDNN runtime engine" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn_engines_tensor_ir64_9.dll"; Description = "NVIDIA cuDNN tensor IR engine" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn_ext64_9.dll"; Description = "NVIDIA cuDNN extension runtime" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn_graph64_9.dll"; Description = "NVIDIA cuDNN graph runtime" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn_heuristic64_9.dll"; Description = "NVIDIA cuDNN heuristic runtime" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn_ops64_9.dll"; Description = "NVIDIA cuDNN operations runtime" },
    @{ Directory = "_internal\nvidia\cudnn\bin"; Pattern = "cudnn64_9.dll"; Description = "NVIDIA cuDNN runtime" }
)
foreach ($artifact in $cudaRuntimeArtifacts) {
    Assert-ArtifactPattern `
        -AppRoot $mekiCopyRoot `
        -RelativeDirectory $artifact.Directory `
        -FilePattern $artifact.Pattern `
        -Description "MekiCopy $($artifact.Description)"
}
foreach ($subtitleTool in @("ffmpeg.exe", "ffprobe.exe")) {
    Assert-ArtifactFile `
        -AppRoot $mekiCopyRoot `
        -RelativePath (Join-Path "_internal\assets\ffmpeg" $subtitleTool) `
        -Description "MekiSubtitle FFmpeg tool"
}
Assert-ArtifactFile `
    -AppRoot $mekiCopyRoot `
    -RelativePath "_internal\sherpa_onnx\lib\sherpa-onnx-c-api.dll" `
    -Description "MekiSubtitle sherpa-onnx C API"
Assert-ArtifactPattern `
    -AppRoot $mekiCopyRoot `
    -RelativeDirectory "_internal\sherpa_onnx\lib" `
    -FilePattern "_sherpa_onnx*.pyd" `
    -Description "MekiSubtitle sherpa-onnx Python extension"
if ($PackageFlavor -eq "Full") {
    foreach ($ocrModel in @(
        "meiki.text.detect.v0.1.960x544.onnx",
        "meiki.text.rec.v0.960x32.onnx",
        "meiki.text.rec.v0.vertical.32x480.onnx"
    )) {
        Assert-ArtifactFile `
            -AppRoot $mekiCopyRoot `
            -RelativePath (Join-Path "_internal\runtime_models\meikiocr" $ocrModel) `
            -Description "MekiCopy bundled OCR model"
    }
}

Assert-ArtifactFile `
    -AppRoot $audioCaptureRoot `
    -RelativePath "_internal\sherpa_onnx\lib\onnxruntime.dll" `
    -Description "MekiAudioCapture ONNX Runtime"
Assert-ArtifactFile `
    -AppRoot $audioCaptureRoot `
    -RelativePath "_internal\sherpa_onnx\lib\sherpa-onnx-c-api.dll" `
    -Description "MekiAudioCapture sherpa-onnx C API"
Assert-ArtifactPattern `
    -AppRoot $audioCaptureRoot `
    -RelativeDirectory "_internal\sherpa_onnx\lib" `
    -FilePattern "_sherpa_onnx*.pyd" `
    -Description "MekiAudioCapture sherpa-onnx Python extension"

Assert-ArtifactFile `
    -AppRoot $hyTransRoot `
    -RelativePath "_internal\assets\worker.html" `
    -Description "HYTrans worker page"
Assert-ArtifactFile `
    -AppRoot $hyTransRoot `
    -RelativePath "_internal\assets\worker.js" `
    -Description "HYTrans worker script"
Assert-ArtifactFile `
    -AppRoot $hyTransRoot `
    -RelativePath "_internal\assets\transformers.min.js" `
    -Description "HYTrans transformers loader"
Assert-RuntimeAssetManifest `
    -AssetsRoot (Join-Path $hyTransRoot "_internal\assets") `
    -Description "Bundled HYTrans"

if ($PackageFlavor -eq "Lite") {
    # Lite deliberately omits all pre-downloaded model payloads. Check the
    # companion roots directly so a warmed developer cache cannot leak into it.
    foreach ($liteModelRoot in @(
        (Join-Path $mekiCopyRoot "_internal\runtime_models"),
        (Join-Path $hyTransRoot "models"),
        (Join-Path $audioCaptureRoot "models")
    )) {
        if (Test-Path -LiteralPath $liteModelRoot -PathType Container) {
            $modelFile = Get-ChildItem `
                -LiteralPath $liteModelRoot `
                -Recurse `
                -File `
                -ErrorAction Stop |
                Select-Object -First 1
            if ($modelFile) {
                throw "Lite package unexpectedly contains a model file: $($modelFile.FullName)"
            }
        }
    }
}

# Only Full may receive a prepared source-side HYTrans cache. Lite must remain
# download-on-first-use even when a developer's source checkout is warmed.
if ($PackageFlavor -eq "Full") {
$verifyPreparedHyTransModels = @'
import json
from pathlib import Path

from hytrans.model_files import MODEL_PROFILES, is_complete_model

models_root = Path("models")
verified = []
for key, profile in MODEL_PROFILES.items():
    target = models_root.joinpath(*profile.model_id.split("/"))
    if target.is_dir() and is_complete_model(target, profile):
        verified.append(key)
print(json.dumps(verified))
'@
$verifiedModelOutput = $verifyPreparedHyTransModels | & $script:PythonExe -
if ($LASTEXITCODE -ne 0) {
    throw "Failed to validate prepared HYTrans models"
}
$verifiedHyTransModelKeys = @($verifiedModelOutput | ConvertFrom-Json)

$hyTransPreparedModels = @(
    @{
        Key = "mt2"
        RelativePath = "tchinso\Hy-MT2-1.8B-onnx-q4f16"
        RequiredFiles = @(
            "chat_template.jinja",
            "config.json",
            "generation_config.json",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "onnx\model_q4f16.onnx"
        )
    },
    @{
        Key = "mt1.5"
        RelativePath = "onnx-community\HY-MT1.5-1.8B-ONNX"
        RequiredFiles = @(
            "config.json",
            "generation_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "onnx\model_q4.onnx",
            "onnx\model_q4.onnx_data"
        )
    }
)
foreach ($preparedModel in $hyTransPreparedModels) {
    if ($verifiedHyTransModelKeys -notcontains $preparedModel.Key) {
        continue
    }
    $hyTransModelSource = Join-Path `
        (Join-Path $PSScriptRoot "models") `
        $preparedModel.RelativePath
    $hasPreparedHyTransModel = Test-Path `
        -LiteralPath $hyTransModelSource `
        -PathType Container
    if ($hasPreparedHyTransModel) {
        foreach ($relativeFile in $preparedModel.RequiredFiles) {
            if (-not (Test-Path -LiteralPath (Join-Path $hyTransModelSource $relativeFile) -PathType Leaf)) {
                $hasPreparedHyTransModel = $false
                break
            }
        }
    }
    if (-not $hasPreparedHyTransModel) {
        continue
    }

    $hyTransModelTarget = Join-Path `
        (Join-Path $hyTransRoot "models") `
        $preparedModel.RelativePath
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
}

if ($PackageFlavor -eq "Full") {
    $defaultFullAssetsRoot = Join-Path (Split-Path $PSScriptRoot -Parent) "ReazonSubtitle\assets"
    $fullModelAssetsRoot = if ($FullAssetsRoot) { $FullAssetsRoot } else { $defaultFullAssetsRoot }
    if (-not (Test-Path -LiteralPath $fullModelAssetsRoot -PathType Container)) {
        throw "Full package model assets were not found: $fullModelAssetsRoot"
    }
    $fullModelAssetsRoot = (Resolve-Path -LiteralPath $fullModelAssetsRoot).Path

    # MekiSubtitle uses its companion-owned copies below; it never receives
    # a separate STT/VAD/translation cache inside MekiCopy itself.
    $fullAssetCopies = @(
        @{
            Source = Join-Path $fullModelAssetsRoot "sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8"
            Target = Join-Path $audioCaptureRoot "models\sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-int8"
            Description = "Parakeet NeMo CTC STT model"
        },
        @{
            Source = Join-Path $fullModelAssetsRoot "reazonspeech-ja"
            Target = Join-Path $audioCaptureRoot "models\reazonspeech-ja"
            Description = "ReazonSpeech STT model"
        },
        @{
            Source = Join-Path $fullModelAssetsRoot "vad"
            Target = Join-Path $audioCaptureRoot "models\vad"
            Description = "Silero VAD model"
        },
        @{
            Source = Join-Path $fullModelAssetsRoot "onnx-community\HY-MT1.5-1.8B-ONNX"
            Target = Join-Path $hyTransRoot "models\onnx-community\HY-MT1.5-1.8B-ONNX"
            Description = "HY-MT1.5 translation model"
        },
        @{
            Source = Join-Path $fullModelAssetsRoot "tchinso\Hy-MT2-1.8B-onnx-q4f16"
            Target = Join-Path $hyTransRoot "models\tchinso\Hy-MT2-1.8B-onnx-q4f16"
            Description = "HY-MT2 experimental translation model"
        }
    )
    foreach ($asset in $fullAssetCopies) {
        Copy-ReleaseTree `
            -SourceRoot $asset.Source `
            -TargetRoot $asset.Target `
            -Description $asset.Description
    }

    $magpieSearchRoots = @()
    if ($FullMagpieRoot) {
        $magpieSearchRoots += $FullMagpieRoot
    }
    $magpieSearchRoots += @(
        (Join-Path $PSScriptRoot "MagPie"),
        (Join-Path $env:LOCALAPPDATA "MekiCopy\MagPie")
    )
    $magpieExecutable = $null
    foreach ($candidateRoot in $magpieSearchRoots) {
        if (-not $candidateRoot -or -not (Test-Path -LiteralPath $candidateRoot -PathType Container)) {
            continue
        }
        $magpieExecutable = Get-ChildItem `
            -LiteralPath $candidateRoot `
            -Filter "MagPie.exe" `
            -File `
            -Recurse `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($magpieExecutable) {
            break
        }
    }
    if (-not $magpieExecutable) {
        Write-Host "Downloading MagPie for the Full package..."
        $installMagpie = @'
from mekicopy_companions import _install_latest_magpie
print(_install_latest_magpie())
'@
        Invoke-CheckedPythonScript $installMagpie
        $magpieRoot = Join-Path $PSScriptRoot "MagPie"
        $magpieExecutable = Get-ChildItem `
            -LiteralPath $magpieRoot `
            -Filter "MagPie.exe" `
            -File `
            -Recurse `
            -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    if (-not $magpieExecutable) {
        throw "Full package MagPie executable could not be prepared."
    }
    Copy-ReleaseTree `
        -SourceRoot (Split-Path -Parent $magpieExecutable.FullName) `
        -TargetRoot (Join-Path $distRoot "MagPie") `
        -Description "MagPie"
    Assert-ArtifactFile `
        -AppRoot $distRoot `
        -RelativePath "MagPie\MagPie.exe" `
        -Description "Full package MagPie"
}

$smokeStateRoot = Join-Path $distRoot ".smoke-state"
$smokeStateRelativePath = Join-Path $distRelativePath ".smoke-state"
$previousSmokeDataDir = $env:MEKICOPY_DATA_DIR
$previousSmokeForceDataDir = $env:MEKICOPY_FORCE_DATA_DIR
if (-not $SkipSmokeTests) {
    if (Test-Path -LiteralPath $smokeStateRoot) {
        Remove-WorkspaceDirectory $smokeStateRelativePath
    }
    New-Item -ItemType Directory -Path $smokeStateRoot -Force | Out-Null
    # Keep frozen smoke tests from reading or modifying the developer's real
    # executable-adjacent state. All child companions inherit this isolated
    # forced location.
    $env:MEKICOPY_DATA_DIR = $smokeStateRoot
    $env:MEKICOPY_FORCE_DATA_DIR = "1"
    Write-Host "Running executable smoke tests..."
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-runtime")
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-ui")
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-tray-stress")
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-detached-button")
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-detached-survival")

    $expectedHyTransModelMode = if ($PackageFlavor -eq "Full") { "local" } else { "remote" }
    $hyTransPort = Get-FreeTcpPort
    Invoke-HealthSmokeTest `
        -ExePath $hyTransExe `
        -Arguments @("--port", "$hyTransPort", "--no-browser") `
        -Port $hyTransPort `
        -ExpectedConfig @{
            modelId = "onnx-community/HY-MT1.5-1.8B-ONNX"
            dtype = "q4"
            hasLocalWasm = $true
            modelMode = $expectedHyTransModelMode
        } `
        -GracefulShutdown

    $hyTransMt2Port = Get-FreeTcpPort
    Invoke-HealthSmokeTest `
        -ExePath $hyTransExe `
        -Arguments @("--port", "$hyTransMt2Port", "--no-browser", "--model", "mt2") `
        -Port $hyTransMt2Port `
        -ExpectedConfig @{
            modelId = "tchinso/Hy-MT2-1.8B-onnx-q4f16"
            dtype = "q4f16"
            hasLocalWasm = $true
            modelMode = $expectedHyTransModelMode
        } `
        -GracefulShutdown

    $overlayerPort = Get-FreeTcpPort
    Invoke-HealthSmokeTest `
        $overlayerExe `
        @("--port", "$overlayerPort") `
        $overlayerPort

    Invoke-ExeSmokeTest $audioCaptureExe @("--self-test")
    Invoke-ExeSmokeTest $audioCaptureExe @("--self-test-ui")
    if ($PackageFlavor -eq "Full") {
        # Verify the copied, pre-downloaded STT/VAD assets from inside the
        # frozen companion.  Lite intentionally has no model payload and
        # retains its first-use download path instead.
        Invoke-ExeSmokeTest $audioCaptureExe @("--self-test-models")
        Invoke-ExeSmokeTest `
            $audioCaptureExe `
            @("--self-test-models", "--stt-model", "reazonspeech", "--precision", "int8")
        Invoke-ExeSmokeTest `
            $audioCaptureExe `
            @("--self-test-models", "--stt-model", "reazonspeech", "--precision", "fp32")
    }
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

if (-not $SkipSmokeTests) {
    try {
        Remove-WorkspaceDirectory $smokeStateRelativePath
    }
    catch {
        Write-Warning "Could not remove isolated smoke-test state: $smokeStateRoot"
    }
    if ($null -eq $previousSmokeDataDir) {
        Remove-Item Env:MEKICOPY_DATA_DIR -ErrorAction SilentlyContinue
    }
    else {
        $env:MEKICOPY_DATA_DIR = $previousSmokeDataDir
    }
    if ($null -eq $previousSmokeForceDataDir) {
        Remove-Item Env:MEKICOPY_FORCE_DATA_DIR -ErrorAction SilentlyContinue
    }
    else {
        $env:MEKICOPY_FORCE_DATA_DIR = $previousSmokeForceDataDir
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

$releaseRoot = Join-Path $PSScriptRoot $releaseName
$releaseArchivePath = Join-Path $PSScriptRoot "$releaseName-one-dir.zip"
$releaseChecksumPath = "$releaseArchivePath.sha256"
$stagedArchivePath = Join-Path $stagingRoot "$releaseName-one-dir.zip"
$stagedChecksumPath = "$stagedArchivePath.sha256"
New-ReleaseZip -SourceRoot $distRoot -ArchivePath $stagedArchivePath
$releaseHash = (Get-FileHash -LiteralPath $stagedArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
$releaseChecksum = "$releaseHash *$(Split-Path -Leaf $releaseArchivePath)"
Set-Content -LiteralPath $stagedChecksumPath -Value $releaseChecksum -Encoding ASCII

try {
    Publish-StagedRelease `
        -StagingRoot $stagingRoot `
        -StagedReleaseRoot $distRoot `
        -ReleaseRoot $releaseRoot `
        -StagedArchivePath $stagedArchivePath `
        -ArchivePath $releaseArchivePath `
        -StagedChecksumPath $stagedChecksumPath `
        -ChecksumPath $releaseChecksumPath
}
catch {
    Write-Warning "The verified staged release was retained for recovery at: $stagingRoot"
    throw
}
try {
    Remove-WorkspaceDirectory $stagingRelativePath
}
catch {
    Write-Warning "Could not remove the empty release staging directory: $stagingRoot"
}

# The staged tree was moved into its canonical release location above.  Refresh
# these display paths so the completion report never points at a removed stage.
$launcherPath = Join-Path $releaseRoot "Start-MekiCopy.bat"
$mekiCopyExe = Join-Path $releaseRoot "MekiCopy\MekiCopy.exe"
$hyTransExe = Join-Path $releaseRoot "HYTrans\HYTrans.exe"
$overlayerExe = Join-Path $releaseRoot "MekiDisplay\MekiOverlayer.exe"
$scriptExe = Join-Path $releaseRoot "MekiDisplay\MekiScript.exe"
$audioCaptureExe = Join-Path $releaseRoot "MekiAudioCapture\MekiAudioCapture.exe"

Write-Host ""
Write-Host "Build complete and verified:"
Write-Host $launcherPath
Write-Host $mekiCopyExe
Write-Host $hyTransExe
Write-Host $overlayerExe
Write-Host $scriptExe
Write-Host $audioCaptureExe
Write-Host $releaseArchivePath
Write-Host $releaseChecksumPath
