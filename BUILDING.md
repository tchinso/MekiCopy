# MekiCopy Lite / Full 빌드

[처음으로](README.md) · [개발 구조](docs/architecture.md) · [검증 기록](VALIDATION.md)

Python 3.12~3.14, Windows x64, PowerShell에서 저장소 루트를 작업 디렉터리로 사용합니다.
빌드는 기존 결과를 보존하면서 임시 배포 폴더에 준비하고, 검증이 끝나면 배포 폴더·ZIP·체크섬을 게시합니다.

```powershell
.\build_mekicopy.ps1 -PythonExe .\.build-python\Scripts\python.exe -PackageFlavor Lite -SkipDependencyInstall
.\build_mekicopy.ps1 -PythonExe .\.build-python\Scripts\python.exe -PackageFlavor Full -SkipDependencyInstall -ReuseLiteBuild
```

처음 빌드하여 의존성이 준비되지 않았다면 `-SkipDependencyInstall`을 생략합니다. `-ReuseLiteBuild`는 같은 소스·Python에서 실행 파일 검증까지 마친 Lite의 실행 파일만 재사용합니다. 소스가 바뀌면 Lite를 다시 빌드해야 합니다. Full은 기존 `models`, `runtime_models/meikiocr`, 이전 `MekiCopy-Full`의 정상 모델 자산을 재사용합니다. 별도 위치는 `-FullAssetsRoot`, `-FullMagpieRoot`, `-FfmpegRoot`로 지정합니다.

## hdTrans 고정 배포 구성

| 항목 | 고정 값 |
|---|---|
| 공식 엔진 릴리스 | [v0.2.1](https://github.com/sokcuri/hdTrans/releases/tag/v0.2.1) |
| 소스 커밋 | `afc752d4ccf2534b2fe206760435214902ce3c68` |
| 기본 사전 버전 (`dicts.txt`) | `0.2.1+c9ab89c732ad` |
| 공식 Windows ZIP | `hdTrans-0.2.1-windows.zip`, 27,582,747 bytes |
| ZIP SHA-256 | `5825082c11fb96aab7492f1f11dac3119e440d1132f85605b95e838c2cc54fc3` |
| 기본 사전 SHA-256 | `d282a66d43bde0f9508596e25b7fb83dd5232c4720908dba0f816e03ceba1235` |
| x64 C API DLL SHA-256 | `f8fcca95c38cc5e538c1a6e6d4c6ec3fce1d04d44e3a5c2c970b245cb6117a15` |
| x86 C API DLL SHA-256 | `c1d56ecb5b0c92750568321e0535e02b49db4f9a7220945b6c7f66cd2c3444de` |

공식 릴리스의 `SHA256SUMS.txt`와 GitHub 릴리스 자산 digest를 기준으로 ZIP 해시를 고정했습니다. 내부 파일도 해당 ZIP에서 추출한 크기·해시로 확인합니다. [공식 API 문서](https://github.com/sokcuri/hdTrans/blob/v0.2.1/docs/api.md), [빌드 문서](https://github.com/sokcuri/hdTrans/blob/v0.2.1/docs/build.md), [도구 호환성](https://github.com/sokcuri/hdTrans/blob/v0.2.1/docs/tool-compatibility.md), [사전 문서](https://github.com/sokcuri/hdTrans/blob/v0.2.1/docs/dictionary-files.md)를 기준으로 연결했습니다.

`.hdsnap`은 같은 엔진 데이터(`src/core/`)로 만든 엔진에서만 읽을 수 있습니다. 따라서 DLL과 사전은 이 공식 ZIP의 한 세트만 사용하며 다른 버전과 조합하지 않습니다. Windows 배포 DLL은 CRT를 정적으로 링크하므로 hdTrans용 별도 VC 런타임 DLL은 동봉하지 않습니다. Python/EXE의 비트수에 맞는 C API DLL만 `hdTransEngine.dll`로 배치합니다. 이지트랜스 호환 `J2KEngine.dll`, CLI, GUI, 외부 Ehnd 사전은 포함하지 않습니다.

Full에 포함되는 파일은 다음과 같습니다.

```text
HYTrans/hdtrans/active.json
HYTrans/hdtrans/installations/0.2.1-windows-x64/
    hdTransEngine.dll
    hdTrans/dicts.hdsnap
    hdTrans/dicts.txt
    hdTrans/user/*.example.*
    install-manifest.json
    LICENSE
    THIRD-PARTY-NOTICES.txt
    SOURCE.txt
    hdTrans-v0.2.1-source.zip
```

Lite는 위 엔진·사전 파일을 포함하지 않으며 최초 hdTrans 준비 시 같은 설치기를 사용합니다. 정상 설치본의 무결성을 확인하면 네트워크에 접속하지 않고 재사용합니다. 임시 폴더에서 다운로드·SHA-256·압축 해제·파일 검증을 마친 후 새 세대를 게시합니다. 프로세스 간 잠금으로 중복 설치를 막고, 취소·실패 시 기존 정상 세대와 사용자 사전을 유지합니다. 업데이트는 새 세대를 준비한 후 HYTrans를 종료·재시작하여 사용합니다. 로드된 DLL은 덮어쓰거나 삭제하지 않습니다. 업데이트 확인은 새 공식 릴리스를 표시하지만, 호환성 검증 전의 최신 버전을 자동 설치하지 않습니다. 지원 고정 버전의 교체는 MekiCopy 업데이트로 제공합니다.

실행 파일 옆 `hdtrans`가 우선 저장 경로입니다. 읽기 전용 설치는 기존 `runtime_paths`의 쓰기 가능한 보조 경로에 Full 세대를 오프라인 복사합니다. 사용자 파일은 `hdtrans/user` 또는 설정에서 선택한 외부 파일에 보관하며 `installations` 업데이트와 분리합니다. 설치 세대의 `hdTrans/user`에는 실행 시 관리 설정만 생성합니다. 배포 빌드는 개인 설정·사용자 사전을 포함하지 않도록 검사합니다.

기존에 준비한 검증된 번들로 Full을 오프라인 빌드할 수 있습니다.

```powershell
python -m hytrans.hdtrans_download --prepare-build .cache/hdtrans/full
.\build_mekicopy.ps1 -PackageFlavor Full -ReuseLiteBuild -SkipDependencyInstall -FullHdTransRoot .cache/hdtrans/full
```

`-FullHdTransRoot`는 `active.json`과 `installations`가 들어 있는 루트입니다. 원본 공식 ZIP과 대응 소스 ZIP을 `.cache/hdtrans/hdTrans-0.2.1-windows.zip`, `.cache/hdtrans/hdTrans-v0.2.1-source.zip`에 두어도 빌드가 해시를 검사한 뒤 재사용합니다.

## 소스·라이선스

공식 hdTrans 저장소는 엔진과 기본 사전에 대해 [AGPL-3.0-or-later](https://github.com/sokcuri/hdTrans/blob/v0.2.1/LICENSE)를 명시합니다. 원문 `LICENSE`, `THIRD-PARTY-NOTICES.txt`, 정확한 태그의 대응 소스 ZIP과 소스·빌드 안내를 Lite 최초 설치 및 Full에 함께 보존합니다. 대응 소스는 [공식 v0.2.1 소스](https://github.com/sokcuri/hdTrans/tree/v0.2.1), 빌드 방법은 소스 안의 `docs/build.md`, `CMakePresets.json`을 따릅니다. 포함한 소스 ZIP SHA-256은 `273e6f7d77fa3df75b5b0d9172b1ac0d5698e649645574f40103487c6052e7cd`입니다.

공식 제3자 고지는 PCRE2 10.49(`BSD-3-Clause WITH PCRE2-exception`), SLJIT(BSD 2-clause), libyaml 0.2.5(MIT)의 원문 조건을 포함합니다. 별도 상용 사전이나 Ehnd 자료는 추가하지 않았습니다. 배포 사전의 권리 표시는 위 공식 저장소·릴리스의 고지를 그대로 보존하며 별도의 권리를 부여한다고 주장하지 않습니다.

MekiCopy 저장소의 `LICENSE`는 GPL v2 문서입니다. hdTrans와 직접 연결한 결합 배포본의 공개 재배포 전에는 원 프로젝트의 GPL 적용 범위·후속 버전 허용 근거와 AGPL 의무, 결합 프로그램의 대응 소스 제공 조건을 권리자와 확인해야 합니다. 이 문서는 해당 라이선스 호환성에 대한 법적 허가를 대신하지 않습니다.

## 검증

```powershell
python -m unittest tests.test_hdtrans_download tests.test_release_packaging
python -m unittest discover -s tests
```

설치 테스트는 작은 모의 릴리스로 실패·취소·경합·오프라인 재사용·구세대 보존·버전 불일치를 검사합니다. 실제 Windows DLL 번역·문맥·사용자 사전 검증과 frozen EXE 검증은 이 모의 테스트와 구분하여 보고합니다. Full 최종 manifest 작성은 고정 DLL·사전·소스·고지 파일을 다시 확인하고, Lite/Full의 공통 실행 파일 내용과 파일 목록 일치를 검사합니다.
