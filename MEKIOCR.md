### meikiOCR

https://github.com/rtr46/meikiocr 에서 자세한 정보를 찾을 수 있습니다.

아래와 같이 하면 meikiOCR이 설치됩니다.

```
python -m pip install meikiocr
```

아래와 같이 호출하면 해당 이미지에 있는 일본어를 인식하여 응답합니다.

```
python -m meikiocr.cli "D:\meikiocr.jpg"
```

### NVIDIA 그래픽카드를 사용하는 경우 meikiOCR의 인식속도 높이기

NVIDIA 그래픽카드를 사용하는 경우 다음과 같은 명령어를 사용하여 OCR인식 속도를 높일 수 있습니다.

NVIDIA 그래픽카드를 사용하는 경우에만 권장됩니다.

그 외 그래픽카드에서도 작동은 할 거 같긴 한데, 정상작동은 보장하지 않습니다.

```
python -m pip uninstall onnxruntime
python -m pip install onnxruntime-gpu
```

### 후처리 작업
MekiOCR의 작업 결과는 줄을 과하게 나누는 경우가 있으므로, 인식된 결과를 한 줄로 합치는 과정이 필요하다.
