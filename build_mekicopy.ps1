param(
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python 명령을 찾을 수 없습니다."
}

if (-not $SkipDependencyInstall) {
    python -m pip install --upgrade pip
    python -m pip install pyinstaller meikiocr==0.3.2 mss pillow
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
if (Test-Path ".\\MekiCopy.spec") {
    Remove-Item ".\\MekiCopy.spec" -Force
}

python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name MekiCopy `
    --collect-submodules meikiocr `
    --collect-binaries onnxruntime `
    --collect-submodules onnxruntime.capi `
    --collect-data huggingface_hub `
    --hidden-import onnxruntime.capi.onnxruntime_pybind11_state `
    --exclude-module PyQt5 `
    --exclude-module PyQt6 `
    --exclude-module PySide2 `
    --exclude-module PySide6 `
    --exclude-module pyperclip `
    --add-data "runtime_models;runtime_models" `
    .\mekicopy.py

Write-Host ""
Write-Host "Build complete:"
Write-Host (Join-Path $PSScriptRoot "dist\\MekiCopy\\MekiCopy.exe")
