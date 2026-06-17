param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python 명령을 찾을 수 없습니다."
}

if (-not $SkipDependencyInstall) {
    python -m pip install --upgrade pip
    python -m pip install --upgrade --upgrade-strategy eager pyinstaller meikiocr mss pillow
    # onnxruntime-gpu exposes the same Python package name as onnxruntime.
    # Install it last so PyInstaller bundles the CUDA-capable runtime DLLs.
    python -m pip install --upgrade --upgrade-strategy eager --force-reinstall onnxruntime-gpu
}

$modelDir = Join-Path $PSScriptRoot "runtime_models\\meikiocr"
New-Item -ItemType Directory -Path $modelDir -Force | Out-Null

@'
from pathlib import Path
import shutil
from huggingface_hub import hf_hub_download
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

for repo_id, filename in unique_models:
    src = hf_hub_download(repo_id=repo_id, filename=filename)
    target = dest / filename
    shutil.copy2(src, target)
    print(f"Prepared model: {target}")
'@ | python -

if (Test-Path ".\\build") {
    Remove-Item ".\\build" -Recurse -Force
}
if (Test-Path ".\\dist") {
    Remove-Item ".\\dist" -Recurse -Force
}

python -m PyInstaller `
    --noconfirm `
    --clean `
    .\MekiCopy.spec

Write-Host ""
Write-Host "Build complete:"
Write-Host (Join-Path $PSScriptRoot "dist\\MekiCopy\\MekiCopy.exe")
