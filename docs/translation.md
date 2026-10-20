# 번역 안내

[처음으로](../README.md) · [사용 안내](usage.md) · [설정과 문제 해결](settings.md)

## 번역 서비스 선택

`도구/설정 → 설정 → 번역`에서 서비스를 선택합니다. OCR 오버레이·음성·자막 생성은 모두 같은 HYTrans 서비스와 설정을 사용합니다. 기본 번역 방향은 일본어 → 한국어입니다.

| 서비스 | 준비 사항 | 모델·데이터 위치 |
|---|---|---|
| hdTrans (기본) | 네이티브 엔진과 기본 사전 | `HYTrans/hdtrans` |
| HY-MT1.5 | Chrome 또는 Edge, q4 번역 모델 | `HYTrans/models/onnx-community/HY-MT1.5-1.8B-ONNX` |
| TranslatorAPI | 해당 언어 쌍을 지원하는 Chrome 또는 Edge | 브라우저에서 언어 모델 관리 |
| Cloudflare Workers AI | API 토큰, Account ID, 모델 ID | 원문을 해당 서비스로 전송 |
| DeepInfra / Groq | API 키, 모델 ID | 원문을 해당 서비스로 전송 |

실행 중 서비스·API 키·모델·프롬프트·HYTrans 포트를 바꿔 저장하면 HYTrans를 새 설정으로 재시작합니다. 번역 입력은 요청당 최대 8,000자입니다.

## 번역 오버레이

1. `설정 → 번역`에서 서비스를 선택합니다.
2. `번역 오버레이`에서 **오버레이어 번역 모드 사용**을 켜고 저장합니다.
3. `도구/설정`에서 **HYTrans 서버 실행**, **MekiOverlayer 실행**을 누릅니다.
4. 번역 준비가 완료되면 확정 영역을 설정하고 **번역 후 표시**를 누릅니다.

MekiCopy가 OCR을 수행하고, HYTrans가 번역한 결과를 MekiOverlayer가 표시합니다. 창의 위치·크기는 직접 조절하고 외형은 [번역 오버레이 설정](settings.md#번역-오버레이)에서 변경합니다.

준비 상태는 **모든 도구 연결 상태확인**으로 확인합니다. API 인증과 실제 서비스 가용성은 번역 요청 결과로 확인합니다. MekiCopy가 시작한 HYTrans와 MekiOverlayer는 본체 종료 시 함께 종료됩니다.

## 실시간 OCR

**번역 설정 탭 아래쪽**에서 **실시간 처리 사용**과 **최소 인식 간격(초)**을 확인해 저장합니다. 기본 사용 상태는 ON이며 최소 간격은 **1초**입니다. 간격은 **0.5 / 1 / 1.5 / 2 / 2.5초** 중 선택합니다. 옆의 **?**에 마우스를 올리면 시작 방법과 조건을 확인할 수 있습니다.

본체의 **영역** 탭에서 **실시간 OCR 시작**을 누릅니다. 다음 조건을 모두 확인합니다.

1. **오버레이어 번역 모드 사용**이 켜져 있습니다.
2. **확정 영역**이 있습니다.
3. **HYTrans와 MekiOverlayer**가 실행되어 연결·번역 준비가 완료되었습니다.
4. **실시간 처리 사용**이 켜져 있습니다.

시작·중지 버튼은 저장된 사용 옵션을 변경하지 않습니다. 앱을 다시 열거나 연결이 복구되어도 자동 시작하지 않습니다. 필수 조건이 해제되면 중지하며 다시 시작 버튼을 눌러 재개합니다.

OCR → 번역 → MekiOverlayer의 실제 화면 표시가 성공한 뒤 다음 인식을 예약합니다. 최소 인식 간격과 새 번역의 표시 후 0.5초를 모두 지키므로, 처리 시간이 길면 실제 주기도 길어집니다. 빈 결과·중복·실패에는 표시 대기를 추가하지 않습니다.

이미 성공한 원문과 같거나 매우 비슷한 결과는 다시 번역하지 않습니다. 실패한 원문은 다음 주기에 재시도합니다. 실행 중에는 본체·분리창의 수동 번역 버튼과 전역 단축키를 잠급니다. 중지하거나 모드·영역·북마크·OCR 설정을 바꾸면 예약과 이전 작업의 늦은 결과를 무효화합니다. 세부 기준은 [개발 구조](architecture.md#실시간-ocr)를 참고하세요.

## hdTrans

Lite는 처음 준비할 때 공식 **v0.2.1** 엔진과 기본 사전 **0.2.1+c9ab89c732ad**를 내려받아 체크섬을 검증합니다. Full에는 같은 엔진·사전과 대응 소스·라이선스를 포함합니다. 브라우저·ONNX 번역 모델·API 키는 필요하지 않습니다.

`도구/설정 → hdTrans 설정` 또는 번역 설정의 hdTrans 전용 버튼에서 설치 진행·취소·재시도·업데이트 확인·지원 버전 재설치를 수행합니다. 새 공식 릴리스는 알림으로 제공하며, 설치에는 검증된 버전 조합을 사용합니다. 설치 실패 시 정상 설치와 사용자 사전을 유지합니다. 고정 파일과 무결성 정보는 [빌드 안내](../BUILDING.md)에 있습니다.

### 사용자 사전

UTF-8의 `일본어<TAB>한국어` 형식과 공식 선택 필드를 사용합니다. 여러 `.txt`/`.tsv` 파일을 추가하거나 새 파일을 만들고 편집한 뒤 **설정 저장 및 사전 재적용**을 누릅니다. **제거**는 적용 목록에서만 제외합니다.

잘못된 파일은 경로·줄 번호로 진단합니다. Ehnd는 기본으로 사용하지 않습니다. 사전 재적용과 외부 편집에 따른 자동 재로드는 문맥을 초기화할 수 있습니다.

### 대화 문맥

**이전 대화 문맥 보존**은 기본 ON이며 OFF에서는 번역 직전에 문맥을 초기화합니다. **문맥 초기화** 버튼, 옵션 변경, 새 자막 파일과 OCR·음성·자막 작업 간 전환도 문맥을 초기화합니다. 다른 게임으로 바꿀 때는 직접 초기화하세요.

문맥은 디스크에 저장하지 않습니다. 엔진은 프로세스당 하나의 문맥을 사용하므로 여러 작품을 번갈아 처리하면 작업이 바뀔 때마다 초기화하며 이전 작품의 문맥을 복원하지 않습니다.

## HY-MT1.5

`onnx-community/HY-MT1.5-1.8B-ONNX`의 q4 모델을 사용합니다. HYTrans가 모델을 다운로드·검증한 뒤 Chrome 또는 Edge의 보이지 않는 전용 worker에서 로드합니다. 유효한 모델이 있으면 다운로드 없이 재사용합니다.

번역 런타임은 포함된 `Transformers.js 4.3.0`과 `ONNX Runtime Web/WASM 1.30.0`을 로컬에서 읽습니다. WebGPU 하드웨어 어댑터를 우선 사용하며 이용할 수 없으면 CPU/WASM으로 동작합니다. 초기 모델 준비에는 시간이 걸릴 수 있습니다.

## TranslatorAPI

Chrome 또는 Edge의 내장 번역 기능입니다. API 키나 MT1.5 모델 파일은 필요하지 않으며, 해당 브라우저가 일본어 → 한국어 언어 쌍을 지원해야 합니다.

언어 모델을 처음 준비할 때 네트워크 연결과 대기가 필요할 수 있습니다. 자동 시작이 실패해 브라우저와 안내 팝업이 열리면 **모델 시작**을 직접 누르고 번역하는 동안 창을 열어 두세요. 언어 모델의 다운로드·저장은 브라우저에서 관리합니다.

공식 설명: [Chrome Translator API](https://developer.chrome.com/docs/ai/translator-api), [Edge Translator API](https://learn.microsoft.com/en-us/microsoft-edge/web-platform/translator-api).

## 클라우드 API

`설정 → 번역`에서 서비스를 선택하고 API 설정을 엽니다. 서비스별로 키·선택 모델·모델 목록·프롬프트를 저장합니다. 목록에 없는 모델 ID도 입력할 수 있습니다.

프롬프트의 `{source}`, `{target}`, `{text}`는 원문 언어·번역 언어·원문으로 치환됩니다. `{text}`는 반드시 포함해야 합니다.

| 서비스 | 인증·필수 입력 | Chat Completions 주소 |
|---|---|---|
| Cloudflare Workers AI | API 토큰 + Account ID | `https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions` |
| DeepInfra | API 키 | `https://api.deepinfra.com/v1/openai/chat/completions` |
| Groq | API 키 | `https://api.groq.com/openai/v1/chat/completions` |

세 서비스에는 Bearer 인증과 `model`·`messages` JSON을 사용합니다. Cloudflare 토큰에는 Workers AI 읽기/편집 권한이 필요합니다. 사용 가능한 모델은 API 설정에서 변경할 수 있습니다.

공식 문서: [Cloudflare](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/), [DeepInfra](https://docs.deepinfra.com/chat/overview), [Groq](https://console.groq.com/docs/api-reference).

API 키는 Windows 사용자 계정에 묶인 DPAPI로 암호화하여 `settings.cfg` 옆 `translation_api.json`에 저장합니다. 다른 Windows 계정이나 PC에서는 다시 입력해야 합니다. 키를 실행 인자·로그·서버 상태 응답에 넣지 않습니다. API 번역에는 원문을 선택한 서비스로 전송합니다.
