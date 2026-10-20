# 개발 구조

[처음으로](../README.md) · [빌드 안내](../BUILDING.md) · [검증 기록](../VALIDATION.md)

MekiCopy는 Tk 본체와 별도 프로세스로 실행하는 번역·표시·음성 도구로 구성됩니다. 각 기능의 로직은 해당 모듈에 두고, 본체는 사용자 입력과 작업 상태를 연결합니다.

## 모듈별 책임

| 영역 | 주요 파일 | 책임 |
|---|---|---|
| 본체 | `mekicopy.py` | 앱 진입점, 메인 창, 기능 간 연결 |
| 설정 | `mekicopy_settings.py`, `mekicopy_settings_window.py` | 설정·북마크 저장, 검증, 페이지별 설정 UI |
| 화면 캡처 | `mekicopy_capture.py`, `mekicopy_region_ui.py` | 모니터·DPI·영역 좌표, MSS 캡처, 영역 선택 UI |
| OCR | `mekicopy_ocr.py` | 모델 준비, GPU/CPU 선택, 인식과 복사 |
| 실시간 OCR | `mekicopy_realtime.py` | 주기·중복 판정·세션 취소 |
| UI 작업 | `mekicopy_tasks.py`, `mekicopy_tooltip.py`, `mekicopy_hotkey.py`, `mekicopy_tray.py` | 작업 스레드 결과 전달, 툴팁, 단축키, 트레이 |
| 외부 도구 | `mekicopy_companions.py`, `companion_http.py`, `companion_watchdog.py`, `companion_liveness.py`, `companion_manual_close.py` | 실행·통신·공통 오버레이 설정·상태 검사·복구·수동 종료 |
| 번역 서버 | `hytrans_main.py`, `hytrans/app.py`, `hytrans/queue.py` | HTTP API, 제한된 순차 작업 큐 |
| 번역 엔진 | `hytrans/hdtrans_backend.py`, `hytrans/browser.py`, `hytrans/api_client.py` | 네이티브·브라우저·클라우드 번역 |
| 번역 설정·자산 | `hytrans/config.py`, `hytrans/*settings.py`, `hytrans/*download.py`, `hytrans/model_*.py` | 서비스 설정, 키 보호, 다운로드·무결성 |
| hdTrans 설정 UI | `hdtrans_settings_window.py` | 설치·사전 편집·문맥 관리 |
| 오버레이 | `meki_overlayer.py`, `win32_overlay.py`, `overlay_receipts.py` | 창 표시, Windows 이벤트, 표시 완료·취소 |
| 음성 | `audio_capture_core.py`, `meki_audio_capture.py`, `meki_script.py` | 녹음·VAD·STT·번역과 대본 표시 |
| 자막 | `meki_subtitle_pipeline.py`, `meki_subtitle_window.py`, `meki_subtitle_paths.py` | 영상 처리, SRT 출력, 작업 UI와 경로 |
| 공통 런타임 | `runtime_paths.py`, `mekicopy_runtime.py`, `meki_bootstrap.py`, `system_logging.py`, `service_ports.py` | 저장·배포 경로, 공통 DLL/Tk 준비, 로그, 포트 |
| 실행 검사 | `mekicopy_selftest.py`, `tests/` | frozen 자기검증, 단위·회귀·통합 검사 |
| 배포 | `build_mekicopy.ps1`, `release_packaging.py`, `*.spec` | 실행 파일 생성·검사, 자산·manifest·ZIP |

## 프로세스와 데이터 흐름

| 도구 | 역할 | 기본 포트 |
|---|---|---|
| HYTrans | 일본어 → 한국어 번역 | 6996 |
| MekiOverlayer | 번역 결과 표시 | 6997 |
| MekiAudioCapture | 시스템 음성 녹음·인식 | 6998 |
| MekiScript | 원문·번역 대본 표시 | 6999 |

- 화면: 확정 영역 캡처 → OCR → 클립보드, 또는 HYTrans → MekiOverlayer.
- 음성: WASAPI loopback → VAD → 일본어 STT → MekiScript 원문 → HYTrans → MekiScript 번역.
- 영상: FFmpeg 오디오 추출 → FAST VAD → 일본어 STT → HYTrans → SRT.

OCR 작업은 겹쳐 실행하지 않습니다. MSS 캡처는 작업 스레드에 맞는 캡처 인스턴스를 사용합니다. Tk 위젯과 Tcl 변수는 UI 스레드에서 다루며 백그라운드 결과는 `TkTaskRunner`로 전달합니다. Windows 이벤트 콜백은 상태만 기록하고 Tk 타이머가 UI 변경을 수행합니다.

상태 조회 응답은 해당 프로세스·포트·모델의 현재 상태에 속하는지 확인해야 합니다. 프로세스 교체나 설정 변경 전 요청의 늦은 응답을 새 상태로 반영하면 안 됩니다. 자동 복구는 본체가 소유한 프로세스에만 적용합니다.

도구 간 HTTP 통신은 `companion_http.urlopen`을 사용합니다. 루프백 요청과 표시·음성 서버는 HTTP/1.1로 길이가 명시된 응답을 끝까지 읽고 연결을 정리하여 Windows의 연결 종료 시 응답 유실을 방지합니다. 번역·표시 POST를 자동 재전송하지 않습니다. 외부 다운로드와 클라우드 API는 기존 전송 경로를 사용합니다.

## 실시간 OCR

사용 옵션과 실제 실행 세션은 별개입니다. 시작과 매 인식 전에 오버레이 모드·확정 영역·HYTrans/오버레이 준비·사용 옵션을 확인합니다. 예약과 결과에는 세션 수명을 적용하여 중지·영역 변경 후 늦은 결과를 버립니다.

다음 시작은 아래 두 시각 중 늦은 시각입니다.

```text
이전 OCR 시작 + 설정한 최소 간격
실제 새 번역 표시 완료 + 0.5초
```

표시 완료는 오버레이의 Tk 그리기 완료 응답으로 확인합니다. 빈 결과·중복·실패에는 표시 대기를 추가하지 않습니다. 주기 대기 중에도 수동 번역 버튼과 단축키 잠금을 유지합니다.

중복 비교 기준은 마지막으로 복사 또는 번역·표시에 성공한 원문입니다.

- 공백과 줄바꿈을 정리한 문자열이 같으면 중복입니다.
- 문자열이 달라도 양쪽에 일본어 문자가 각각 15자 이상이고 레벤슈타인 유사도가 92% 이상이면 중복입니다.
- 숫자·문장부호를 보존하며, 비교용 정규화는 복사·번역 원문을 바꾸지 않습니다.
- 중복은 번역 요청 전에 판단하고 실패한 원문은 다음 주기에 재시도합니다.

네이티브 호출은 시작 후 강제로 중단하지 않습니다. 세션이 취소되면 호출을 안전하게 정리하되 결과를 화면이나 다음 중복 비교 기준에 반영하지 않습니다.

## 번역·음성 처리의 경계

hdTrans C API는 프로세스당 하나의 문맥을 사용합니다. 문맥 초기화·사전 변경·번역은 같은 네이티브 큐에서 처리하며, 작업 소유자가 바뀌면 문맥을 초기화합니다. 엔진·기본 사전은 검증된 동일 릴리스 조합만 사용합니다.

실시간 음성은 인식과 번역 큐를 분리합니다. 번역 버퍼 상한은 2,048개이며 과부하로 건너뛴 항목을 명시합니다. VAD/STT는 각각 1/2개 native thread를 사용합니다. Parakeet은 16 kHz mono, feature dimension 80, greedy CPU 인식 경로를 사용합니다.

사용자 설정·API 키·사용자 사전은 배포 자산과 분리합니다. 자산 준비는 임시 파일의 다운로드·무결성 검증을 마친 뒤 게시하고, 정상 설치를 실패한 다운로드로 교체하지 않습니다.

## 변경과 검증

동작을 바꿀 때는 먼저 책임 모듈을 찾고 관련 테스트를 수정합니다. UI 배치는 설정 UI 테스트, 반복 작업은 세션·취소 테스트, 통신과 네이티브 변경은 해당 통합 테스트로 확인합니다. 주석에는 현재 제약과 설계 이유를 남기고 변경 이력은 Git 기록으로 관리합니다.

```powershell
python -m unittest discover -s tests
node --test tests/test_translator_api_worker.mjs
```

네이티브 통합 시험에는 검증된 설치 경로를 `HYTRANS_NATIVE_TEST_INSTALLATION`으로 지정합니다. 모의 서비스 검사, 실제 DLL 검사, frozen 실행 파일 검사는 서로 다른 범위이며 실행 결과에 구분해 기록합니다.

Lite/Full 빌드 명령과 재사용 조건은 [빌드 안내](../BUILDING.md)에 있습니다. Full은 동일 소스·Python으로 검증한 Lite 실행 파일을 재사용하고 필요한 모델 자산을 추가합니다. 최종 배포에서는 공통 파일·manifest·체크섬과 각 실행 파일의 동작을 검사합니다.
