param(
    [switch]$SkipDependencyInstall,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not $PythonExe) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $PythonExe = $pythonCommand.Source
    }
}
if (-not $PythonExe) {
    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCommand) {
        $PythonExe = $pyCommand.Source
    }
}
if (-not $PythonExe) {
    throw "Python 실행 파일을 찾을 수 없습니다. -PythonExe 경로를 지정하세요."
}

if (-not $SkipDependencyInstall) {
    & $PythonExe -m pip install --upgrade pip
    & $PythonExe -m pip install --upgrade --upgrade-strategy eager pyinstaller meikiocr mss pillow fastapi "uvicorn[standard]"
    # onnxruntime-gpu exposes the same Python package name as onnxruntime.
    # Install it last so PyInstaller bundles the CUDA-capable runtime DLLs.
    & $PythonExe -m pip install --upgrade --upgrade-strategy eager --force-reinstall onnxruntime-gpu
}

$modelDir = Join-Path $PSScriptRoot "runtime_models\\meikiocr"
New-Item -ItemType Directory -Path $modelDir -Force | Out-Null

@'
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
'@ | & $PythonExe -

if (Test-Path ".\\build") {
    Remove-Item ".\\build" -Recurse -Force
}
if (Test-Path ".\\dist") {
    Remove-Item ".\\dist" -Recurse -Force
}

$specs = @(
    ".\MekiCopy.spec",
    ".\HYTrans.spec",
    ".\MekiOverlayer.spec"
)

foreach ($spec in $specs) {
    & $PythonExe -m PyInstaller `
        --noconfirm `
        --clean `
        $spec
}

Write-Host ""
Write-Host "Build complete:"
Write-Host (Join-Path $PSScriptRoot "dist\\MekiCopy\\MekiCopy.exe")
Write-Host (Join-Path $PSScriptRoot "dist\\HYTrans\\HYTrans.exe")
Write-Host (Join-Path $PSScriptRoot "dist\\MekiOverlayer\\MekiOverlayer.exe")
