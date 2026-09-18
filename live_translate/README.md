# live_sub — 재생 중인 영상 소리를 실시간 번역 자막으로

컴퓨터 스피커로 나가는 소리를 그대로 받아서
**음성인식(Whisper) → 번역 → 화면 위에 항상 떠 있는 자막**으로 띄웁니다.
영상 파일이든 스트리밍이든, 소리만 나면 됩니다.

---

## 1. 가장 빠른 시작 (3분)

```bash
pip install numpy sounddevice scipy faster-whisper deep-translator
python live_sub.py --src ja --dst ko
```

Windows면 `run.bat` 더블클릭으로도 됩니다.

첫 실행 때 Whisper 모델(`small`, 약 500 MB)을 한 번 내려받습니다. 그 뒤로는 바로 뜹니다.
자막 창은 **드래그로 이동, 휠로 글자 크기, 우클릭으로 메뉴, Esc로 종료**입니다.

---

## 2. 소리를 못 잡을 때 (제일 흔한 문제)

마이크가 아니라 **스피커로 나가는 소리**를 잡아야 합니다.

```bash
python live_sub.py --list-devices
```

목록을 보고 `--device` 로 직접 지정하세요.

| OS | 무엇을 고르나 | 추가 설치 |
|---|---|---|
| **Windows** | 목록의 **출력(out)** 장치 번호. WASAPI loopback으로 자동 전환됩니다 | 없음 |
| **Linux** | 이름에 `monitor` 가 들어간 입력 장치 (예: `pulse` / `...Monitor of...`) | 없음 (PulseAudio/PipeWire) |
| **macOS** | [BlackHole](https://existential.audio/blackhole/) 같은 가상 출력 장치 | BlackHole 설치 후, 소리 출력을 멀티출력 장치로 |

```bash
python live_sub.py --device 7
python live_sub.py --device "monitor"     # 이름 일부만 써도 됩니다
```

장치는 맞는데 자막이 안 나오면 음량 기준값 문제입니다:

```bash
python live_sub.py --calibrate     # 10초간 입력 레벨 측정
python live_sub.py --threshold 0.002
```

---

## 3. 번역 백엔드 고르기

```bash
python live_sub.py --translator google   # 기본값. 설치 간단, 키 불필요
python live_sub.py --translator local    # 기기 밖으로 아무것도 안 나감
python live_sub.py --translator claude   # 구어체 품질 최상
python live_sub.py --translator none     # 번역 없이 원문 자막만
```

| 백엔드 | 품질 | 추가 설치 | 밖으로 나가는 것 |
|---|---|---|---|
| `google` | 보통 | `deep-translator` | 인식된 **문장 텍스트** (오디오는 아님) |
| `local` | 준수 | `transformers sentencepiece torch` + 모델 약 2.5 GB | **없음 — 전부 내 컴퓨터 안** |
| `claude` | 최상 | `anthropic`, `ANTHROPIC_API_KEY` | 인식된 문장 텍스트 |
| `none` | — | 없음 | 없음 |

> 오디오 자체는 어떤 설정에서도 밖으로 나가지 않습니다. 음성인식은 항상 로컬(faster-whisper)에서 돌아갑니다.
> 그래도 인식된 대사 텍스트조차 내보내고 싶지 않으면 `--translator local` 을 쓰세요.

`claude` 백엔드는 앞 대사 3줄을 문맥으로 같이 넘기기 때문에 주어가 생략된 구어체에 강합니다.
기본 모델은 `claude-opus-5` 인데 자막용으로는 지연이 있을 수 있습니다.
더 빠른 응답을 원하면:

```bash
python live_sub.py --translator claude --claude-model claude-haiku-4-5
```

---

## 4. 지연 시간 줄이기

자막 지연 ≈ **발화 길이 + 인식 시간**. 둘 다 줄일 수 있습니다.

```bash
# 더 잘게 끊어서 빨리 띄우기 (정확도는 조금 손해)
python live_sub.py --silence 300 --max-segment 4

# NVIDIA GPU가 있으면 인식이 몇 배 빨라집니다
python live_sub.py --device-type cuda --model medium

# CPU뿐이고 너무 느리면 모델을 낮춥니다
python live_sub.py --model base
```

| 모델 | 속도 | 정확도 | 권장 |
|---|---|---|---|
| `tiny` / `base` | 매우 빠름 | 낮음 | CPU만 있고 지연이 심할 때 |
| `small` | 빠름 | 보통 | **기본값** |
| `medium` | 느림 | 좋음 | GPU 있을 때 |
| `large-v3` | 매우 느림 | 최상 | GPU 8 GB 이상 |

인식이 밀리면 오래된 조각부터 자동으로 버려서 자막이 영상보다 뒤처지지 않게 합니다
(`--queue-limit` 로 조절).

---

## 5. 자주 쓰는 옵션

```bash
python live_sub.py \
  --src ja --dst ko \        # 일본어 -> 한국어 (--src auto 면 자동 감지)
  --show-source \            # 원문도 같이 표시
  --font-size 32 \           # 글자 크기
  --opacity 0.9 \            # 자막창 불투명도
  --width 0.7 \              # 화면 가로 대비 자막창 너비
  --console                  # 오버레이 없이 터미널에만 출력
```

전체 옵션은 `python live_sub.py --help`.

---

## 6. 구조

```
오디오 콜백 ─▶ 모노 16 kHz 변환 ─▶ 에너지 VAD로 문장 단위 분할
                                        │
                                   faster-whisper (로컬 인식)
                                        │
                                   번역 백엔드
                                        │
                                   tkinter 오버레이 자막
```

각 단계가 별도 스레드라 인식이 느려도 오디오 캡처는 끊기지 않습니다.
`ご視聴ありがとうございました` 같은 Whisper의 전형적인 무음 구간 환각과,
같은 글자만 반복되는 출력은 자동으로 걸러집니다.

---

## 7. 알려진 한계

- 배경음악이 크면 에너지 기반 VAD가 문장 경계를 잘 못 잡습니다. `--threshold` 를 올려보세요.
- 여러 사람이 동시에 말하면 인식률이 떨어집니다. Whisper 공통 한계입니다.
- 실시간이라 한 문장이 끝나야 자막이 뜹니다. 문장 중간부터 미리 띄우지는 않습니다.
- macOS는 OS 정책상 가상 오디오 장치(BlackHole 등) 없이는 시스템 소리를 잡을 수 없습니다.
