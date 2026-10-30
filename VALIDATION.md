# MekiCopy 검증

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
