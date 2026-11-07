# MekiCopy Lite / Full 빌드

[처음으로](README.md) · [개발 구조](docs/architecture.md) · [검증 기록](VALIDATION.md)

Python 3.12~3.14, Windows x64, PowerShell에서 저장소 루트를 작업 디렉터리로 사용합니다.
빌드는 기존 결과를 보존하면서 임시 배포 폴더에 준비하고, 검증이 끝나면 배포 폴더·ZIP·체크섬을 게시합니다.

```powershell
.\build_mekicopy.ps1 -PythonExe .\.build-python\Scripts\python.exe -PackageFlavor Lite -SkipDependencyInstall
.\build_mekicopy.ps1 -PythonExe .\.build-python\Scripts\python.exe -PackageFlavor Full -SkipDependencyInstall -ReuseLiteBuild
```

처음 빌드하여 의존성이 준비되지 않았다면 `-SkipDependencyInstall`을 생략합니다. `-ReuseLiteBuild`는 같은 소스·Python에서 실행 파일 검증까지 마친 Lite의 실행 파일만 재사용합니다. 소스가 바뀌면 Lite를 다시 빌드해야 합니다. Full은 기존 `models`, `runtime_models/meikiocr`, 이전 `MekiCopy-Full`의 정상 모델 자산을 재사용합니다. 별도 위치는 `-FullAssetsRoot`, `-FullMagpieRoot`, `-FfmpegRoot`로 지정합니다.

Lite에는 NVIDIA CUDA/cuDNN DLL과 FFmpeg를 넣지 않습니다. 공통 실행 파일은 GPU 드라이버가 사용 가능할 때 공식 PyPI의 고정 wheel을, 영상 자막 생성 시 공식 FFmpeg ZIP을 내려받아 크기·SHA-256을 검증하고 설치합니다. GPU 다운로드 실패 시 CPU OCR을 사용합니다. Full 빌드는 같은 실행 파일에 검증된 GPU DLL·FFmpeg를 추가하며 오프라인에서 사용합니다. 두 배포본의 공통 파일 내용은 일치해야 합니다.

GPU 패키지의 URL·버전·파일별 해시는 `assets/gpu_runtime_manifest.json`, 미디어 도구는 `media_tools_download.py`에 정의합니다. 다운로드는 임시 폴더에서 검증을 마친 뒤 게시하며 캐시를 재사용합니다. Full의 `-FfmpegRoot`에는 `ffmpeg.exe`, `ffprobe.exe`, `LICENSE`, `README.txt`가 필요합니다.

## 의존성 기준

2026-11-07 공식 PyPI, npm 및 GitHub 릴리스 기준입니다. Python 패키지는 `requirements-build.txt`의 고정 버전을 사용하며, 빌드 검증도 이 파일을 직접 읽습니다.

| 구성 요소 | 버전 / 출처 |
|---|---|
| OCR | [meikiocr 0.3.5](https://pypi.org/project/meikiocr/) |
| 네이티브 추론 | ONNX Runtime / GPU 1.31.1, sherpa-onnx 2.0.1 |
| 웹 추론 | [Transformers.js 4.3.2](https://github.com/huggingface/transformers.js/releases/tag/4.3.2), ONNX Runtime Web 1.31.1 |
| 이미지 | Pillow 13.0.0, NumPy 2.5.4, OpenCV headless 5.0.0.93 |
| HTTP | FastAPI 0.162.1, Pydantic 2.14.3, Uvicorn 0.54.0 |
| 모델 저장소 | huggingface-hub 2.4.0 |
| 화면 확대 | [Magpie 0.13.0](https://github.com/Blinue/Magpie/releases/tag/v0.13.0) x64 |
| 미디어 도구 | [FFmpeg / FFprobe 9.0.2 essentials](https://www.gyan.dev/ffmpeg/builds/) |

Transformers.js는 공식 태그 `4.3.2`의 `packages/transformers/scripts/build.mjs`로 생성합니다. `packages/transformers/package.json`의 `onnxruntime-web`과 `onnxruntime-node`를 안정판 `1.31.1`로 고정하고 설치한 뒤 해당 스크립트를 실행합니다. 번들과 WASM은 동일한 ONNX Runtime 버전을 사용해야 하며, 출처·도구 버전·개별 SHA-256은 `assets/runtime_manifest.json`에 기록합니다. 파일 변경 후 해시도 갱신해야 빌드가 통과합니다.

OCR 모델 세 파일은 meikiocr가 지정한 Hugging Face 저장소의 최신 main LFS SHA-256과 대조합니다. 일본어 Parakeet `ja-35000`, 일본어 ReazonSpeech `2024-08-01`, Silero VAD는 [sherpa-onnx의 공식 asr-models 자산](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models)을 사용합니다. 언어 및 인식 구조가 다른 모델은 별도 모델로 취급합니다.

## hdTrans 고정 배포 구성

| 항목 | 고정 값 |
|---|---|
| 공식 엔진 릴리스 | [v0.9.3](https://github.com/sokcuri/hdTrans/releases/tag/v0.9.3) |
| 소스 커밋 | `dd8375644bb8f6578d3468d7ebc37bc3fb696982` |
| 기본 사전 버전 (`dicts.txt`) | `0.9.3+5fa3c580a6fe` |
| 공식 Windows ZIP | `hdTrans-0.9.3-windows.zip`, 35,724,603 bytes |
| ZIP SHA-256 | `07034dc25c4be3cc35aa26ddd61ae6b6b20547c0837129e50a079a8af362af0a` |
| 기본 사전 SHA-256 | `a58c0e5418b850341738578f09a5436df35713ed49aed56a1bf92192c3a5efb9` |
| x64 C API DLL SHA-256 | `7a7f5fe1a8f252b7995f2b1351b9ce15f41c74e8aa5f790482e51620c6970dfc` |
| x86 C API DLL SHA-256 | `ab87b78f14fcc8cf4d653366c0ab2fd8d33db5469b54aff568a1e8ddd0944c53` |

공식 릴리스의 `SHA256SUMS.txt`와 GitHub 릴리스 자산 digest를 기준으로 ZIP 해시를 고정했습니다. 내부 파일도 해당 ZIP에서 추출한 크기·해시로 확인합니다. [공식 API 문서](https://github.com/sokcuri/hdTrans/blob/v0.9.3/docs/api.md), [빌드 문서](https://github.com/sokcuri/hdTrans/blob/v0.9.3/docs/build.md), [도구 호환성](https://github.com/sokcuri/hdTrans/blob/v0.9.3/docs/tool-compatibility.md), [사전 문서](https://github.com/sokcuri/hdTrans/blob/v0.9.3/docs/dictionary-files.md)를 기준으로 연결했습니다.

`.hdsnap`은 같은 엔진 데이터(`src/core/`)로 만든 엔진에서만 읽을 수 있습니다. 따라서 DLL과 사전은 이 공식 ZIP의 한 세트만 사용하며 다른 버전과 조합하지 않습니다. Windows 배포 DLL은 CRT를 정적으로 링크하므로 hdTrans용 별도 VC 런타임 DLL은 동봉하지 않습니다. Python/EXE의 비트수에 맞는 C API DLL만 `hdTransEngine.dll`로 배치합니다. 이지트랜스 호환 `J2KEngine.dll`, CLI, GUI, 외부 Ehnd 사전은 포함하지 않습니다.

Full에 포함되는 파일은 다음과 같습니다.

```text
HYTrans/hdtrans/active.json
HYTrans/hdtrans/installations/0.9.3-windows-x64/
    hdTransEngine.dll
    hdtrans-data/dicts.hdsnap
    hdtrans-data/dicts.txt
    hdtrans-data/user/*.example.*
    install-manifest.json
    LICENSE
    NOTICE
    THIRD-PARTY-NOTICES.txt
    SOURCE.txt
    hdTrans-v0.9.3-source.zip
```

Lite는 위 엔진·사전 파일을 포함하지 않으며 최초 hdTrans 준비 시 같은 설치기를 사용합니다. 정상 설치본의 무결성을 확인하면 네트워크에 접속하지 않고 재사용합니다. 임시 폴더에서 다운로드·SHA-256·압축 해제·파일 검증을 마친 후 새 세대를 게시합니다. 프로세스 간 잠금으로 중복 설치를 막고, 취소·실패 시 기존 정상 세대와 사용자 사전을 유지합니다. 업데이트는 새 세대를 준비한 후 HYTrans를 종료·재시작하여 사용합니다. 로드된 DLL은 덮어쓰거나 삭제하지 않습니다. 업데이트 확인은 새 공식 릴리스를 표시하지만, 호환성 검증 전의 최신 버전을 자동 설치하지 않습니다. 지원 고정 버전의 교체는 MekiCopy 업데이트로 제공합니다.

실행 파일 옆 `hdtrans`가 우선 저장 경로입니다. 읽기 전용 설치는 기존 `runtime_paths`의 쓰기 가능한 보조 경로에 Full 세대를 오프라인 복사합니다. 사용자 파일은 `hdtrans/user` 또는 설정에서 선택한 외부 파일에 보관하며 `installations` 업데이트와 분리합니다. 설치 세대의 `hdtrans-data/user`에는 실행 시 관리 설정만 생성합니다. 배포 빌드는 개인 설정·사용자 사전을 포함하지 않도록 검사합니다.

기존에 준비한 검증된 번들로 Full을 오프라인 빌드할 수 있습니다.

```powershell
python -m hytrans.hdtrans_download --prepare-build .cache/hdtrans/full
.\build_mekicopy.ps1 -PackageFlavor Full -ReuseLiteBuild -SkipDependencyInstall -FullHdTransRoot .cache/hdtrans/full
```

`-FullHdTransRoot`는 `active.json`과 `installations`가 들어 있는 루트입니다. 원본 공식 ZIP과 대응 소스 ZIP을 `.cache/hdtrans/hdTrans-0.9.3-windows.zip`, `.cache/hdtrans/hdTrans-v0.9.3-source.zip`에 두어도 빌드가 해시를 검사한 뒤 재사용합니다.

## 소스·라이선스

공식 hdTrans 0.9.3은 [AGPL-3.0-only](https://github.com/sokcuri/hdTrans/blob/v0.9.3/LICENSE)를 동봉합니다. 원문 `LICENSE`, `NOTICE`, `THIRD-PARTY-NOTICES.txt`, 정확한 태그의 대응 소스 ZIP과 소스·빌드 안내를 Lite 최초 설치 및 Full에 함께 보존합니다. 대응 소스는 [공식 v0.9.3 소스](https://github.com/sokcuri/hdTrans/tree/v0.9.3), 빌드 방법은 소스 안의 `docs/build.md`, `CMakePresets.json`을 따릅니다. 포함한 소스 ZIP SHA-256은 `4f98538d9bc47cc43afb110f2712e25af13297a54ea9032efe0a517223b940dc`입니다.

공식 제3자 고지는 PCRE2 10.49(`BSD-3-Clause WITH PCRE2-exception`), SLJIT(BSD 2-clause), libyaml 0.2.5(MIT)의 원문 조건을 포함합니다. 별도 상용 사전이나 Ehnd 자료는 추가하지 않았습니다. 배포 사전의 권리 표시는 위 공식 저장소·릴리스의 고지를 그대로 보존하며 별도의 권리를 부여한다고 주장하지 않습니다.

MekiCopy 저장소의 라이선스는 루트 `LICENSE`(GPL-2.0)를 따릅니다. hdTrans 0.9.3은 별도 구성요소로 AGPL-3.0-only가 적용되며, 그 원문·NOTICE와 제3자 고지 및 정확한 대응 소스를 배포본에 함께 보존합니다. MekiCopy 코드와 포함된 hdTrans 엔진의 라이선스는 서로 다르므로, 배포 시 각 구성요소의 고지를 함께 제공해야 합니다. hdTrans 0.9.3의 AGPL-3.0-only 조건과 결합 배포 범위는 배포 전에 별도로 검토해야 합니다.

## 검증

```powershell
python -m unittest tests.test_hdtrans_download tests.test_release_packaging
python -m unittest discover -s tests
```

설치 테스트는 작은 모의 릴리스로 실패·취소·경합·오프라인 재사용·구세대 보존·버전 불일치를 검사합니다. 실제 Windows DLL 번역·문맥·사용자 사전 검증과 frozen EXE 검증은 이 모의 테스트와 구분하여 보고합니다. Full 최종 manifest 작성은 고정 DLL·사전·소스·고지 파일을 다시 확인하고, Lite/Full의 공통 실행 파일 내용과 파일 목록 일치를 검사합니다.
