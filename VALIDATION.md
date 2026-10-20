# hdTrans 및 실시간 OCR 구현·검증

검증일: 2026-10-20 (Windows x64, Python 3.12.14).

## 주요 변경 파일

- `hytrans/hdtrans_backend.py`, `hdtrans_settings.py`, `app.py`, `config.py`, `api_settings.py`: 네이티브 엔진, 제한된 순차 큐, 준비 상태·설정·문맥·사전 API, 명시적 클라우드 공급자 분류.
- `hytrans/hdtrans_download.py`, `HYTrans.spec`, `build_mekicopy.ps1`, `release_packaging.py`: 고정 버전·해시 검증 설치, 세대별 안전 교체, 공유 쓰기 경로, Lite/Full 패키징·검증.
- `hdtrans_settings_window.py`, `mekicopy_settings_window.py`: 다중 공식 사용자 사전 편집·저장·재적용·오류 진단, 문맥 옵션·초기화, 설치·취소·업데이트 확인.
- `mekicopy_realtime.py`, `mekicopy.py`, `mekicopy_settings.py`, `mekicopy_capture.py`: 단일 실시간 OCR 작업, 성공 기준 중복 제거, 단조 시계 일정, 늦은 결과 취소, 버튼·단축키·분리창 잠금, 작업 스레드별 MSS 캡처.
- `overlay_receipts.py`, `meki_overlayer.py`, `mekicopy_companions.py`: 실제 Tk 그리기 완료 시각 확인, 요청 식별·취소, 기존 호출 형식 유지.
- `audio_capture_core.py`, `meki_audio_capture.py`, `meki_subtitle_pipeline.py`, `meki_subtitle_window.py`: 음성 세션 및 새 자막 파일별 작업 경계. 실제 독립 다중 문맥을 제공한다고 가정하지 않음.
- `companion_watchdog.py`: 지속되는 네이티브 호출 타임아웃을 기존 프로세스 복구 경로에 연결.
- `mekicopy_tasks.py`: 종료된 창 참조를 백그라운드 작업에 남기지 않도록 정리. 네이티브 설정 창도 UI 스레드에서 Tcl 변수를 해제한다.
- `README.md`, `BUILDING.md`, `tests/`: 사용·배포 설명과 집중·회귀·실제 DLL 검증.

## 연결·버전 근거

`ctypes.CDLL`로 공식 **v0.2.1**의 `hdTransEngine.dll` Cdecl C API를 직접 호출한다. `J2KEngine.dll`과 요청별 CLI 프로세스를 사용하지 않는다. 공식 태그의 `include/core/hdtrans.h`, `src/cshared/hdtrans_c.cpp`, `docs/api.md`에서 함수 시그니처, UTF-8 바이트 길이, `size_t`, 포인터·메모리 해제 규약을 확인했다. 이 태그의 API 문서는 C 예제를 제공하므로 해당 헤더·구현과 함께 대조했다.

엔진 릴리스 **v0.2.1**, 사전 **0.2.1+c9ab89c732ad**, 소스 커밋 `afc752d4ccf2534b2fe206760435214902ce3c68`을 하나의 검증된 묶음으로 고정했다. 공식 Windows ZIP과 각 DLL·스냅샷·소스 ZIP의 SHA-256을 검증한다. 정확한 해시와 공식 문서 링크는 [BUILDING.md](BUILDING.md)에 기록했다.

## 실행 검증

```powershell
$env:HYTRANS_NATIVE_TEST_INSTALLATION = (Resolve-Path '.cache/hdtrans/full/installations/0.2.1-windows-x64').Path
.\.build-python\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/test_translator_api_worker.mjs
```

- Python 전체 **258개 통과**, 브라우저 worker **11개 통과**.
- 전체 Python 실행에 실제 Windows DLL 테스트 4개와 실시간 통합 테스트 1개를 포함했다. 문맥 학습·OFF·Reset·작업 전환, 다중 사용자 사전 우선순위·잘못된 파일/줄 진단·수정/재로드, 동시 번역, 반복 UTF-8 메모리 복사·해제를 실제 엔진으로 검증했다.
- 실시간 통합은 실제 DLL·네이티브 큐·FastAPI 경로·TkTaskRunner·실시간 스케줄러·오버레이 HTTP 서버·Tk 그리기·표시 확인·취소를 실행했다. OCR 문자열과 HYTrans의 소켓 전송은 모의 처리했다. 중복 원문이 엔진에 한 번만 전달되고, 이미 시작된 번역의 늦은 결과가 중지 후 표시되지 않는 것을 확인했다.
- 다운로드 오류·취소·경합·이전 정상 설치 보존·버전 불일치·오프라인 설치는 모의 릴리스 테스트와 실제 고정 릴리스 파일 검증을 구분했다. 실제 Windows 배포 파일과 대응 소스는 공식 URL에서 받아 해시를 확인했다.
- `sampleimage.jpeg`를 실제 OCR 모델로 인식하여 비어 있지 않은 일본어 199자를 얻었다. 이 환경에서는 CUDA 오류 801 이후 CPU로 정상 전환했다.
- 로컬 TCP 연결 종료 시 간헐적인 Windows `ConnectionResetError`가 최소 표준 라이브러리 서버에서도 재현되었다. 두 소켓 기반 테스트는 같은 제품 경로를 HTTP/1.1 테스트 핸들러로 실행하고, 상태 조회만 재시도한다. 번역·표시 POST는 재시도하지 않는다. 제품 통신을 테스트 성공 목적으로 변경하지 않았다.

## 빌드 검증

Lite/Full 최종 결과와 실행 파일 검사 로그는 `.cache/hdtrans/lite-build.log`, `.cache/hdtrans/full-build.log`에 남긴다. 빌드 스크립트는 전체 소스 검증 결과를 재사용하고 실행 파일·Tk·OCR·음성·서버 검사, Full 네이티브 오프라인 준비·번역·종료, 릴리스 manifest·공통 파일 일치를 별도로 검사한다.

**Lite·Full 모두 빌드 종료 코드 0으로 완료했다.** 실행 파일 검사와 manifest 검증을 통과했고, Full의 공통 파일 일치 검사도 통과했다. 두 manifest의 소스 fingerprint가 현재 소스와 일치하며 모두 `smokeTested=true`다. Lite에 엔진 DLL이 없고 Full에 고정 엔진·사전이 포함되는 것을 최종 확인했다. 두 ZIP과 `.sha256` 파일을 저장소 루트에 생성했다.

```powershell
.\build_mekicopy.ps1 -PythonExe .\.build-python\Scripts\python.exe -PackageFlavor Lite -SkipDependencyInstall -SkipSourceTests
.\build_mekicopy.ps1 -PythonExe .\.build-python\Scripts\python.exe -PackageFlavor Full -SkipDependencyInstall -SkipSourceTests -ReuseLiteBuild -FullHdTransRoot .cache\hdtrans\full -FullMagpieRoot MekiCopy-Full\MagPie
```

실제 frozen Lite의 새 쓰기 경로에서 `HYTrans.exe --self-test-hdtrans`를 실행했다. 공식 파일의 최초 네트워크 다운로드·해시 검증·초기화·번역·종료가 코드 0으로 완료됐다(10.39초). 두 번째 실행도 코드 0으로 완료됐고(2.18초), `오늘은 좋은 날씨입니다.`를 반환했다. 설치 경로·세대 개수·active 포인터 해시/시간·DLL/스냅샷 시간이 그대로여서 정상 설치를 교체하거나 다시 내려받지 않은 것을 확인했다. Lite 릴리스 manifest 해시도 변경되지 않았다. 기록은 `.cache/hdtrans/lite-frozen-first-use`에 보존했다.

자식 프로세스에 실패하는 프록시를 지정하여 네트워크를 차단하는 추가 검사는 자동 승인 검토에서 `blocked by policy`로 거부됐다. 구체적인 이유는 제공되지 않았고 해당 방식의 검사는 실행하지 않았다. 따라서 실제 Lite의 일반 설치 재사용, 모의 네트워크 실패하의 오프라인 재사용, Full의 사전 포함 파일 경로 실행을 네트워크가 물리적으로 차단된 실행과 구분한다.

## 범위와 제한

- 실제 클라우드 API 및 브라우저 모델의 전체 번역 실행은 API 키·사용자 활성화 없이 검증하지 않았다. 해당 엔진은 기존 회귀·프로토콜·worker 테스트와 frozen 서버 검사로 확인한다.
- 실제 게임 화면·마이크/루프백 발화·영상 전체 파일을 연속 재생하는 수동 사용 시험은 수행하지 않았다. 해당 작업의 기존 처리 경로와 새 경계·취소 처리는 자동 테스트로 확인한다.
- GPU OCR은 이 환경에서 사용하지 못했으며 CPU OCR을 실제 검증했다. 빌드 대상은 Windows x64이다.
- hdTrans C API에는 프로세스당 문맥 하나만 있다. 작업 소유자 전환 시 초기화하며 이전 작품의 문맥을 복원하지 않는다. 시작한 DLL 호출은 HTTP 요청 취소와 함께 강제 취소하지 않는다.
- hdTrans의 AGPL-3.0-or-later 원문 고지·제3자 고지·정확한 대응 소스를 배포 묶음에 보존했다. 기존 MekiCopy `LICENSE`는 GPL v2 문서이며 임의로 변경하지 않았다. 공개 재배포 전 결합 배포의 라이선스 조건과 대응 소스 제공 범위를 권리자와 확인해야 한다.
