# MekiCopy 검증

## 2026-11-09 VAD 프리셋 조정

- FAST에 NyanKat-Tab `5af4ce39`의 VAD 값 14개와 확률 기반 구간 분할을 적용했다. BALANCED/LONG은 사용자가 확정한 음성 임계값 0.2/0.4, 최소 발화 0.1/0.2초, 종료 무음 0.4/0.6초, 약한 발화 종료 무음 1.5/1.2초, 앞·뒤 패딩 각 0.3/0.5초를 적용했다. LONG의 휴지점 탐색 범위는 0이다.
- 영상은 FAST를 사용한다. 녹음 후 처리와 실시간 녹음도 같은 경계 추적기를 사용하며, 가까운 구간과 겹치는 패딩을 추가로 합치던 경로를 제거했다. `short_segment_max_duration`과 `is_short`는 동작에 사용되지 않아 설정·데이터 구조·전달 코드에서 제거했다. VAD/STT 모델 캐시는 기존 공용 캐시를 유지한다.
- 영향 범위 Python 테스트 **82개**가 통과했다. 원본 NyanKat JavaScript와 **88개 사례·134,725프레임·확정 구간 880개**의 경계·강제 분할·겹침 및 FAST 상수 14개를 대조했다. 영상/녹음 종료 시 추가하는 EOF 처리는 원본 실시간 JavaScript와 구분해 검증했다.
- 실제 일본어 음성에서 FAST/BALANCED/LONG 각각 **3/4/4개** 구간을 얻었다. 48 kHz 입력을 잘게 나눈 실시간 처리와 16 kHz 일괄 처리의 경계·메타데이터·오디오가 모두 일치했다. 13초 MP4의 FFmpeg → Silero 확률 추론 → Parakeet INT8 → hdTrans → 한국어 SRT 생성도 **4개 인식·4개 번역·실패 0건**으로 통과했다.
- 변경하지 않은 OCR·브라우저 번역·의존성의 고비용 검증은 2026-11-08 결과를 재사용했다. 증거: `.cache/subtitle-vad-20261109/source-tests.json`, `nyankat-parity-result.json`, `real-vad-stream-result.json`, `subtitle-e2e/result.json`.
- 최종 Lite/Full 빌드와 실행 파일·서버·Full 모델 자체 검사를 통과했다. 기존 권한 제약으로 트레이 반복 검사만 제외해 두 manifest에 기록했다. 공통 실행 파일과 소스 fingerprint가 일치하며, Lite 3,388개·Full 3,620개 파일을 ZIP에서 모두 읽어 크기·SHA-256·CRC를 확인했다. 개인 설정 5개는 ZIP 검증 후 Full 실행 폴더에 복원하고 백업 해시와 대조했다.

| 배포본 | ZIP 크기 | SHA-256 |
|---|---:|---|
| Lite | 398,193,392 bytes | `0965daf8316dbb0b8ca7bcc173f6603131a83184c378203e56d8eedefd429c91` |
| Full | 4,134,332,177 bytes | `0c1f187c6d98f09643cf7ce63247274762e0e74152959d273869c746fb651902` |

- 빌드·배포 증거: `.cache/subtitle-vad-20261109/lite-build.log`, `full-build.log`, `release-verification.json`, `state-restoration.json`.

## 2026-11-08 호환성 재점검

대상은 `aa0090a`(2.1.4)의 의존성 갱신과 그 호출부다. Windows x64, Python 3.12.14, `requirements-build.txt`의 고정 22개 패키지를 사용했다. 패키지를 추가 업그레이드하거나 버전을 되돌리지 않았다.

### 발견 및 수정

- MT1.5 q4의 WASM 대체 실행이 `GatherBlockQuantized` 커널을 찾지 못해 초기화에 실패했다. [ONNX Runtime 공식 이슈 #33961](https://github.com/microsoft/onnxruntime/issues/33961)와 같은 현상이다. CPU에는 이미 동봉된 plain WASM 파일 쌍을 사용하고, 같은 Transformers.js를 별도 ESM URL로 불러와 먼저 초기화된 WebGPU 런타임과 분리했다. 추가 번들은 필요하지 않다. 모델 캐시도 새 환경에 다시 연결하며 worker 해시를 갱신했다.
- 로컬 worker 문서에 COOP/COEP 헤더가 없어 WASM이 단일 스레드로 실행됐다. `/worker.html` 응답에만 격리 헤더를 추가했다. 실제 브라우저에서 `crossOriginIsolated=true`, SharedArrayBuffer 사용 가능, CPU 3스레드를 확인했다. TranslatorAPI 문서는 영향을 받지 않는다.
- 트레이의 `NOTIFYICONDATAW`가 중간 필드에서 끝나 잘못된 크기(Windows x64 296 bytes)를 전달하고 있었다. [Windows SDK의 전체 구조체](https://learn.microsoft.com/en-us/windows/win32/api/shellapi/ns-shellapi-notifyicondataw)에 맞게 976 bytes로 교정하고 Shell 호출 시그니처와 실제 WinError 기록을 보강했다.
- 제한된 환경에서도 빌드 스크립트를 임시 수정하지 않도록 `-SkipTrayStressTest`를 추가했다. 제외한 검사 이름을 manifest의 `skippedSmokeTests`에 남기고, Full이 Lite를 재사용할 때도 보존한다.
- EXE 검사 실행이 Windows 셸 경유 시작에서 멈추는 현상을 확인해 직접 프로세스를 시작하도록 수정했다. Windows PowerShell 5.1에서 빈 값·공백·따옴표·역슬래시·Unicode 등 13개 인자의 정확한 전달, 종료 코드, 실제 staged EXE 자체검사·HYTrans 상태 확인과 정상 종료를 검증했다.
- ZIP 체크섬은 `Get-FileHash` cmdlet의 로드 여부에 의존하지 않도록 .NET의 스트리밍 SHA-256으로 계산한다. Lite는 모든 EXE 검사 완료 후 패키징 단계만 이 수정으로 마무리했으며, 실행 파일을 다시 컴파일하지 않고 최종 manifest와 ZIP을 갱신했다.

### 실제 실행 및 회귀 검증

- Python 고유 **409개**를 검증했다. 일반 검사 405개 통과, opt-in 네이티브 검사 4개는 전체 실행에서 skip 후 실제 hdTrans DLL로 별도 실행해 모두 통과했다. 최종 변경의 영향 범위 79개도 통과했다. Tk 경로 검사 3개는 ASCII 임시 경로로 분리 실행했고, 전체 테스트 ID의 누락·중복이 없음을 확인했다. JavaScript worker는 **20/20** 통과했다. `pip check`에서 충돌이 없었다.
- 실제 WebGPU(AMD gcn-5), Transformers.js 4.3.2 / ONNX Runtime Web 1.31.1로 두 문장을 한국어로 번역했다. TranslatorAPI도 새 프로필에서 모델 설치·두 문장 번역·정상 종료까지 통과했다.
- 실제 WebGPU pipeline을 생성·해제한 뒤 실패를 주입하고 CPU로 전환했다. 새 WASM 런타임으로 q4 모델을 불러와 `こんにちは。`를 `안녕하세요.`로 번역했다. CPU 3스레드에서 66.77초로 기본 120초 제한 안에 완료했고 EOS와 정상 종료를 확인했다. 단일 스레드 검사는 120초 제한에 실패했으며, 제품의 번역 제한 시간은 늘리지 않았다.
- FastAPI 0.162.1 / Pydantic 2.14.3 / Uvicorn 0.54.0의 실제 HTTP·WebSocket 통신에서 UTF-8 번역 왕복, 잘못된 입력 422·빈 입력 400, WASM HEAD·Range 206, worker 연결 해제를 확인했다. 이 통신 검사에서는 worker의 번역 응답만 가상으로 제공했으며 위 모델 실추론과 구분한다.
- hdTrans 0.9.3 실제 DLL의 반복 UTF-8 번역·문맥·작업 경계·사용자 사전 오류/재적용·동시 요청 4개 검사를 통과했다. sherpa-onnx 2.0.1의 실제 Parakeet·ReazonSpeech 샘플 디코딩과 Silero VAD도 통과했다. 동일한 고정 의존성에서 얻은 기존 CPU OCR의 일본어 실추론·세 ONNX 세션 검증 결과는 재사용했다.
- 13초 일본어 샘플 음성으로 MP4를 만들고 실제 FFmpeg/FFprobe → Silero VAD → Parakeet INT8 → hdTrans → 한국어 SRT 생성까지 실행했다. 4구간 인식·4개 번역·실패 0건이며 자막 순서·시간 범위·UTF-8 BOM 저장을 확인했다. 이는 기능 검사이며 인식·번역 품질 벤치마크는 아니다.

### 환경과 검증 범위

- 이 호스트의 RAM은 약 5.9 GiB로 README의 최소 8GB보다 작다. 모델 초기화와 CPU 추론 속도는 일반 PC의 성능 기준으로 해석하지 않는다.
- 현재 Explorer는 존재한다. 테스트 프로세스가 Low 무결성(RID 4096), Explorer가 Medium(RID 8192)이어서 실제 `Shell_NotifyIconW` 호출이 `ERROR_ACCESS_DENIED(5)`로 거부된다. 따라서 트레이 반복 검사는 실행 환경 제약으로 제외하며, ABI·오류 처리 회귀 검사는 통과했다.
- 브라우저 검증 harness에만 쓰기 가능한 TEMP와 `--no-sandbox`를 적용했다. 제품의 브라우저 실행 옵션은 바꾸지 않았다. 일반 데스크톱 권한에서의 트레이 스트레스, NVIDIA CUDA 실추론, 실제 시스템 소리 녹음과 장시간 연속 사용은 이번 검사에 포함하지 않았다.
- 증거: `.cache/compat-audit-20261108/source-tests-final-summary.json`, `source-tests-final-affected.json`, `worker-tests.log`, `process-launch-verification.json`, `tray-*-probe.json`, `subtitle-e2e/`; `.cache/browser-review-20261108/*-result.json`; `.cache/review-20261108/native-compatibility.json`, `python-compat/transport.json`, `ocr.log`.

### 최종 Lite / Full 배포본

| 배포본 | ZIP 크기 | SHA-256 |
|---|---:|---|
| Lite | 383,968,244 bytes | `1e6982e642d18cded31991d64e0b52adb8a74c2f7a6534778c945400aa1a6685` |
| Full | 4,120,107,013 bytes | `b4d843815cfca847421095d2213bf882bb686d26c0051b617381c8eb67f8cff2` |

- Lite의 EXE 런타임·UI·분리 버튼·생존 검사, HYTrans 로컬/TranslatorAPI 및 표시기·녹음 엔진 서버 검사를 통과했다. Full은 Lite 실행 파일을 재사용했고 hdTrans 및 Parakeet/ReazonSpeech INT8·FP32 모델 자체 검사도 통과했다. 두 manifest 모두 `skippedSmokeTests: ["tray-stress"]`를 기록한다.
- 최종 Full EXE의 실제 HTTP `/translate` 요청으로 `今日はいい天気ですね。` → `오늘은 좋은 날씨군요.`를 확인했다. 같은 EXE에서 worker 격리 헤더와 CPU용 쿼리 URL의 원본 번들 일치도 확인하고 정상 종료했다.
- Lite 3,382개·Full 3,614개 파일과 manifest 자체를 두 ZIP에서 모두 읽어 크기·SHA-256·CRC를 대조했다. 중복·누락·예상하지 않은 파일이 없었다. ZIP의 디렉터리 항목은 파일 개수와 구분했다. Lite/Full 공통 파일 일치, 선택 페이로드의 포함/제외, 최종 소스와 설치 의존성 일치도 확인했다.
- 두 배포본의 `sourceFingerprint`는 `e3b011c716aa5d040f645b6f4d365facdaf6309035d4d85c959cebaad69ff366`이다. ZIP 검증 후 개인 설정 5개를 로컬 Full 실행 폴더에 복원하고 원래 해시와 일치함을 확인했다. 개인 설정은 배포 ZIP에 포함되지 않는다.
- 증거: `.cache/compat-audit-20261108/lite-build.log`, `lite-package-complete.log`, `full-build.log`, `release-verification.json`, `frozen-transport.json`, `state-restoration.json`.

## 2026-11-07 갱신 검증

검증일: 2026-11-07~08. Windows x64, Python 3.12.14 (`.build-python`), Windows PowerShell 5.1.

### 갱신 내역

- hdTrans **0.9.3**(2026-11-06 릴리스, 소스 커밋 `dd8375644bb8f6578d3468d7ebc37bc3fb696982`, 기본 사전 **0.9.3+5fa3c580a6fe**)으로 갱신했다. C API 9개 심볼의 시그니처가 같아 백엔드 수정은 필요 없었고, 실제 DLL로 번역·문맥·사용자 사전·재적용을 확인했다. 라이선스가 MPL-2.0에서 **AGPL-3.0-only**로 바뀌어 원문 LICENSE·NOTICE·제3자 고지와 대응 소스를 배포본에 보존하고 문서의 고지도 갱신했다.
- Transformers.js **4.3.2**, ONNX Runtime Web/WASM **1.31.1**, ONNX Runtime/GPU **1.31.1**, sherpa-onnx **2.0.1**, FastAPI **0.162.1**, Pydantic **2.14.3**, Typer **0.28.0**, huggingface-hub **2.4.0**, pip **26.3**으로 갱신했다. PyInstaller 6.22.3, meikiocr 0.3.5, Pillow 13.0.0, NumPy 2.5.4, OpenCV headless 5.0.0.93, Uvicorn 0.54.0, soundcard 0.4.6, MagPie 0.13.0, FFmpeg 9.0.2는 이미 최신이라 유지했다.
- GPU CUDA/cuDNN DLL은 `assets/gpu_runtime_manifest.json`의 검증된 7개 패키지·27개 파일(1,166,225,776 bytes)을 그대로 고정했다. `requirements-build.txt`에 nvidia 패키지 7종을 명시해 eager 업그레이드로 인한 드리프트를 막았다.
- Transformers.js는 공식 태그 4.3.2 소스에서 `onnxruntime-web`/`onnxruntime-node`를 1.31.1로 고정해 재생성했고, 동일 파이프라인으로 4.3.1을 재빌드해 기존 산출물과 바이트 단위로 일치함을 확인해 등가성을 검증했다.

### 검증 결과

- Python **403개** 테스트 통과(4 skip), JavaScript worker **15/15** 통과. `pip check` 이상 없음.
- dependency probe: 고정 22개 버전 일치, ONNX Runtime provider `Tensorrt/CUDA/CPU`, nvidia.cu13·nvidia.cudnn import, sherpa-onnx `from_nemo_ctc`, Tk 확인.
- GPU manifest 7패키지·27파일의 크기·SHA-256이 설치본과 일치했고, `stage_optional_runtime` 복제본에 `validate_gpu_runtime`을 적용해 Full 스테이징 통과를 확인했다.
- 실제 hdTrans 0.9.3 DLL: UTF-8 번역, 문맥 켜기/끄기·수동 초기화·작업 경계, 복수 사용자 사전 오류 진단·재적용, 동시 요청 등 네이티브 통합 테스트 **4/4** 통과. 빌드된 Full의 설치본(`MekiCopy-Full/HYTrans/hdtrans/installations/0.9.3-windows-x64`)으로 검증했다.
- Lite: EXE 자체검사(런타임·UI·분리 버튼·분리 생존)와 HYTrans(로컬 WASM·TranslatorAPI), MekiDisplay, MekiAudioCapture, MekiScript의 서버 상태 검사를 통과했다. 이번 호스트에는 대화형 Windows 셸(explorer.exe)이 없어 **트레이 스트레스 자체검사 1건만 실행하지 못했다**(환경 제한, 코드 문제 아님).
- Full: Lite 실행 파일을 재사용했고 `verify-parity`로 Lite/Full 공통 파일의 내용·목록 일치를 확인했다. HYTrans hdTrans 네이티브·모델 자체검사와 서버 상태 검사를 통과했다.

### 배포본

- Lite ZIP: **379,662,260 bytes**, SHA-256 `f9d69e2a1a5fec1c4a265c2fc6a49c39f557a616c7434d17bd39202898ccb8ab`. manifest 기준 **3,207개** 파일(ZIP 내 3,208개, build-manifest.json 포함).
- Full ZIP: **4,115,801,615 bytes**, SHA-256 `eb79433d9c7ea70d5cc31a3759aeb3fe90b238a0bd5fddd6728d66dcce400ed9`. manifest 기준 **3,439개** 파일(ZIP 내 3,440개).
- 두 ZIP 모두 내부 파일 크기·SHA-256을 build manifest와 대조했고 ZIP CRC 검사가 통과했다. Full에서 hdTrans 0.9.3 DLL·사전·AGPL LICENSE/NOTICE·제3자 고지·대응 소스·예제 3종, GPU DLL 20개, FFmpeg 4파일, MagPie, OCR·STT·VAD·HY-MT1.5 모델을 확인했고, Lite에는 해당 선택 페이로드가 없음을 확인했다.
- 개인 설정 5개(`settings.cfg`, `translation_api.json`, `realtime_ocr_state.json`, `detached_button_region.json`, `.state_migration_v2`)는 빌드 전에 백업해 Full 실행 폴더에 복원하고 해시를 확인했다. 배포 ZIP에는 포함되지 않는다.
- 증거: `.cache/refresh-20261107/lite-build.log`, `full-build.log`, `source-tests.log`, `node-tests.log`, `release-verification.json`, `previous-state/`.

### 빌드 환경 예외

- 이 호스트에는 대화형 Windows 셸이 없어 트레이 스트레스 자체검사 1건을 건너뛰는 임시 환경 변수 분기를 적용해 빌드했고, 빌드 후 `build_mekicopy.ps1`은 원래 내용으로 되돌렸다. 게시된 build manifest의 `sourceFingerprint`는 그 임시 분기가 포함된 빌드 시점 스크립트 기준이다. 일반 데스크톱에서 다시 빌드하면 전체 EXE 자체검사가 실행된다.

## 2026-10-31 추가 검증

- HyTrans의 대용량 Chromium 캐시 복사를 제거하고 로딩 단계 표시·정체 시간 제한을 검증했다. Translator API의 모델 준비 정체 시 브라우저 창으로 전환하는 복구도 검증했다.
- hdTrans **0.7.6**, 기본 사전 **0.7.6+93be691cfbd6**으로 갱신했다. 실제 DLL로 UTF-8 번역, 명시적 문맥 전달·초기화·작업 전환, 사용자 사전 진단·재적용, 동시 요청과 Tk 오버레이 표시를 확인했다. 공식 MPL-2.0 LICENSE·NOTICE·제3자 고지·대응 소스를 포함한다.
- Python **403개**, JavaScript **15개** 회귀 검증을 완료했다. 최종 Full EXE의 HyTrans(WebGPU)·Translator API·hdTrans 모두 실제 일본어→한국어 번역에 성공했다.
- Full ZIP **4,115,474,366 bytes**, 내부 **3,613개** 파일의 크기·SHA-256과 소스 fingerprint를 확인했다. 개인 설정 5개는 ZIP 검증 후 실행 폴더에 복원했다.
- 증거: `.cache/hytrans-loading-fix/full-hdtrans076-build.log`, `hdtrans076-source-tests.log`, `hdtrans076-native-final.log`, `frozen-browser-verification.json`, `release-verification.json`. 네이티브 검사 4개는 테스트 환경 경로를 바로잡은 뒤 별도로 재실행해 통과했다.

## 2026-10-23 검증 기록

검증일: 2026-10-23. Windows x64, Python 3.12.14.

## 소스·구성요소

- Python 전체 테스트 **399개 통과**, JavaScript worker **13개 통과**. `pip check`에서 의존성 충돌 없음.
- 실제 hdTrans 0.4.4 DLL: UTF-8 반복 번역, 문맥 초기화·작업 경계, 사용자 사전 오류·재로드, 동시 요청, 실제 Tk 오버레이 표시 완료·취소를 검증했다.
- GPU: 공식 PyPI wheel 7개(1,166,225,776 bytes)의 실제 다운로드, 압축·DLL·라이선스 해시, 원자 설치를 검증했다. 네트워크 호출을 실패시키는 조건에서 설치 캐시 재사용을 확인했고, 다운로드한 DLL 20개를 실제 Windows 로더로 불러왔다.
- FFmpeg/FFprobe 9.0.2: 공식 ZIP·개별 파일 해시, 버전, 합성 WAV의 스트림 검사·오디오 추출을 확인했다. 다운로드 취소·동시 설치·오프라인 재사용·손상 복구·게시 실패 회귀 테스트를 포함한다.
- Lite는 NVIDIA CUDA/cuDNN 및 FFmpeg를 최초 사용 시 준비한다. 사용 가능한 NVIDIA 드라이버·장치가 없는 경우 GPU 파일을 다운로드하지 않으며 GPU 준비 실패 시 CPU OCR을 사용한다.
- GPU 다운로드·압축 해제·잠금 대기의 실시간 OCR 취소, 분리 버튼의 진행 표시, 강제 종료 후 임시 다운로드 정리를 검증했다.
- UI 작업·서버 입력·설정·경로·번역 큐·모델 저장·자막 취소·배포 재사용 조건의 회귀 검사를 포함한다.
- 의존성 목록과 출처·해시는 [빌드 안내](BUILDING.md), `requirements-build.txt`, `assets/*manifest.json`에 있다.

## 배포본

- Lite·Full 빌드와 EXE 자체 검사·서버 상태 검사가 통과했다. Full은 검증된 Lite 실행 파일을 재사용했고 공통 파일의 일치를 확인했다.
- Lite ZIP: **382,911,374 bytes**. 기존 1,622,546,277 bytes에서 **76.40% 감소**.
- Full ZIP: **4,113,484,506 bytes**. GPU·FFmpeg·모델·hdTrans·MagPie를 포함한다.
- ZIP 자체 SHA-256 및 내부 전체 파일의 크기·SHA-256을 manifest와 대조했다. Lite 3,382개, Full 3,612개 파일 모두 일치했다. 중복·누락·예상하지 않은 파일이 없고 소스 fingerprint·설치 의존성이 빌드 환경과 일치한다.
- 기존 개인 설정 4개는 배포 ZIP 검사 후 로컬 Full 실행 폴더에 복원하고 해시를 확인했다. 배포 ZIP에는 개인 설정을 넣지 않았다.

## 재현

```powershell
$hdTransActive = (Get-Content -Raw '.cache/hdtrans/full/active.json' | ConvertFrom-Json).installation
$env:HYTRANS_NATIVE_TEST_INSTALLATION = (Resolve-Path (Join-Path '.cache/hdtrans/full/installations' $hdTransActive)).Path
.\.build-python\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/test_*.mjs
```

## 증거 파일

- `.cache/refactor/source-tests.log`
- `.cache/refactor/worker-tests.log`
- `.cache/refactor/gpu-first-use.log`
- `.cache/refactor/gpu-native-load.log`
- `.cache/refactor/lite-build.log`
- `.cache/refactor/full-build.log`
- `.cache/refactor/release-verification.json`

## 검증 범위

실제 NVIDIA GPU 추론, 사용자 API 키를 사용하는 클라우드 번역, 브라우저 모델의 전체 번역, 장시간 게임 화면·실제 루프백 발화 시험은 별도 사용 환경 검증이 필요하다. GPU DLL 로드는 실제 GPU 추론 성공과 구분한다. 이미 진행 중인 네이티브 번역·HTTP 요청은 완료 또는 요청 제한 시간까지 정리한 뒤 취소 결과를 반영한다.
