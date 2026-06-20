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

Remove-WorkspaceDirectory "build"
Remove-WorkspaceDirectory "dist"

$specs = @(
    ".\MekiCopy.spec",
    ".\HYTrans.spec",
    ".\MekiOverlayer.spec"
)
foreach ($spec in $specs) {
    Invoke-CheckedPython @("-m", "PyInstaller", "--noconfirm", "--clean", $spec)
}

$mekiCopyExe = Join-Path $PSScriptRoot "dist\MekiCopy\MekiCopy.exe"
$hyTransExe = Join-Path $PSScriptRoot "dist\HYTrans\HYTrans.exe"
$overlayerExe = Join-Path $PSScriptRoot "dist\MekiOverlayer\MekiOverlayer.exe"
$expectedExecutables = @($mekiCopyExe, $hyTransExe, $overlayerExe)
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

if (-not $SkipSmokeTests) {
    Write-Host "Running executable smoke tests..."
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-runtime")
    Invoke-ExeSmokeTest $mekiCopyExe @("--self-test-ui")

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
}

Write-Host ""
Write-Host "Build complete and verified:"
Write-Host $mekiCopyExe
Write-Host $hyTransExe
Write-Host $overlayerExe
