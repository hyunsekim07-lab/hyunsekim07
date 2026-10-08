# 처음부터 차근차근

영상 소리를 받아 실시간으로 번역 자막을 띄웁니다.
**영상 파일이든 사이트 스트리밍이든 상관없습니다** — 파일을 읽는 게 아니라 스피커로 나가는 소리를 잡습니다.

---

## 0단계 — 준비물

- **컴퓨터** (윈도우 / 맥 / 리눅스). 폰만으로는 안 됩니다 — OS가 다른 앱 소리를 못 가져가게 막아둬서 우회 방법이 없습니다.
- **인터넷** (처음에 모델 파일을 한 번 받습니다)

---

## 1단계 — 파이썬 확인

터미널(윈도우: `PowerShell`, 맥: `터미널`)에서:

```bash
python3 --version
```

**3.9 이상**이면 통과입니다.

> 없으면 <https://www.python.org/downloads/> 에서 설치하세요.
> **윈도우는 설치 첫 화면의 `Add python.exe to PATH` 를 꼭 체크**해야 합니다.

---

## 2단계 — 코드 받기

```bash
git clone -b claude/real-time-video-translation-jchd1q https://github.com/hyunsekim07-lab/hyunsekim07.git
cd hyunsekim07/live_translate
```

git이 없으면 [ZIP으로 받기](https://github.com/hyunsekim07-lab/hyunsekim07/archive/refs/heads/claude/real-time-video-translation-jchd1q.zip) → 압축 풀고 `live_translate` 폴더로 들어가세요.

---

## 3단계 — 설치 (한 번만)

**윈도우: `setup.bat` 더블클릭**

맥 / 리눅스:
```bash
./setup.sh
```

필요한 것을 알아서 다 깔고 마지막에 점검 결과를 보여줍니다.
GPU가 있으면 가속 라이브러리까지 자동으로 깔고, 없으면 건너뜁니다. 5~15분 걸립니다.

맨 아래에 `준비 끝` 이 나오면 4단계로, `아직 안 됩니다` 가 나오면 **거기 적힌 번호대로** 하고 다시 실행하세요.

### 맥은 여기서 한 가지 더

맥은 Apple 정책상 가상 오디오 장치 없이는 시스템 소리를 잡을 수 없습니다.

1. `brew install blackhole-2ch` 후 **재부팅**
2. **Audio MIDI 설정** → 좌하단 `+` → **다중 출력 장치 생성**
   → **내장 출력**과 **BlackHole 2ch** 둘 다 체크, 내장 출력을 맨 위로, Drift Correction 체크
3. 시스템 설정 → 사운드 → **출력을 "다중 출력 장치" 로** 변경
4. 시스템 설정 → 개인정보 보호 및 보안 → **마이크** → **터미널** 허용

> 다중 출력 장치를 쓰면 키보드 볼륨 키가 안 먹습니다. 볼륨은 플레이어 안에서 조절하세요.

---

## 4단계 — 실행

**윈도우: `run.bat` 더블클릭**

맥 / 리눅스:
```bash
./run.sh
```

**첫 실행은 음성 인식 모델(약 1.5GB)을 받느라 몇 분 걸립니다.** 다음부터는 바로 뜹니다.

시작할 때 이 줄들을 확인하세요:

```
[trans] 백엔드: local  (ja -> ko)
[asr ] GPU(CUDA) 로 동작합니다.          <- CPU 라고 나와도 동작은 합니다
[audio] 입력: [soundcard] 스피커(...)    <- 지금 소리 듣는 장치와 같아야 합니다
[ok  ] 실행 중. 영상을 재생하세요.
```

`[ok  ] 실행 중` 이 뜨면 영상을 재생하세요. 화면 아래에 자막 창이 뜹니다.

| 조작 | |
|---|---|
| 드래그 | 자막 창 이동 |
| 마우스 휠 | 글자 크기 |
| 우클릭 | 메뉴 (원문 표시, 종료) |
| `Esc` | 종료 |

---

## 자주 쓰는 변형

`run.bat` 뒤에 옵션을 붙이면 됩니다 (맥/리눅스는 `./run.sh`).

```bash
run.bat --fast              # 자막을 더 빨리 (정확도는 조금 손해)
run.bat --timing            # 자막이 몇 초 늦는지, 어디서 걸리는지 표시
run.bat --device G733       # 특정 출력 장치로 고정
run.bat --show-source       # 일본어 원문도 같이 표시
run.bat --src en            # 영어 영상
run.bat --console           # 자막 창 없이 터미널에만
```

---

## 제대로 번역되는지 확인

정답을 아는 일본어 문장으로 인식·번역을 대조하는 테스트가 들어 있습니다.

```bash
python3 -m pip install edge-tts
python3 selftest.py
```

문장마다 `들려준 일본어 / 인식한 일본어 / 번역 결과 / 참고 번역` 을 나란히 보여줍니다.

---

## 안 될 때

| 증상 | 해결 |
|---|---|
| 자막 창은 떴는데 계속 "대기 중" | 소리를 못 잡는 중. `python3 live_sub.py --calibrate` 로 `rms` 가 움직이는지 확인 |
| `rms` 가 계속 0 | **Windows 출력 장치와 `--device` 가 다른 경우**가 대부분. 맥이면 3단계의 BlackHole 설정 확인 |
| 입력이 `마이크(...)` 로 잡힘 | 스피커 소리가 아니라 주변 소리를 듣는 중. `run.bat --device "스피커"` 처럼 지정 |
| 자막이 말보다 몇 초 늦음 | `--timing` 으로 측정 → 인식이 크면 GPU 문제, `--fast` 로도 줄어듭니다 |
| 번역이 기계 번역체 | 모델 한계입니다. `--translator claude` 가 유일한 해결책 (`ANTHROPIC_API_KEY` 필요) |
| 맥락에 안 맞는 말 | 잘못 들은 것. `--prompt "자주 나오는 단어"` 를 주거나 모델을 키우세요 |
| `No module named '_tkinter'` | 맥: `brew install python-tk` / 급하면 `--console` |

원인을 모르겠으면:

```bash
python3 diag.py
```

환경 전체(패키지, 오디오 장치, GPU)를 진단해 출력합니다. 그 출력을 그대로 보여주시면 됩니다.

더 자세한 옵션은 [README.md](README.md) 또는 `python3 live_sub.py --help`.
