# 처음부터 차근차근

순서대로 하면 됩니다. **각 단계 끝에 확인 방법이 있으니, 확인이 통과하면 다음 단계로** 넘어가세요.
막히면 그 단계의 출력을 그대로 복사해서 보여주시면 됩니다.

---

## 0단계 — 준비물 확인

- **컴퓨터** (윈도우 / 맥 / 리눅스). 폰만으로는 안 됩니다 — OS가 다른 앱 소리를 못 가져가게 막아둬서 우회 방법이 없습니다.
- **인터넷** (처음에 모델 파일을 한 번 받습니다)
- 걸리는 시간: 윈도우/리눅스 약 10분, 맥은 BlackHole 설정 때문에 약 20분

---

## 1단계 — 파이썬 설치

터미널(맥: `터미널`, 윈도우: `PowerShell`)을 열고:

```bash
python3 --version
```

**3.9 이상**이 나오면 통과입니다. 윈도우에서 `python3` 가 안 먹으면 `python --version` 으로 해보세요.

> 안 깔려 있으면 <https://www.python.org/downloads/> 에서 설치하세요.
> **윈도우는 설치 첫 화면의 `Add python.exe to PATH` 를 꼭 체크**해야 합니다.

---

## 2단계 — 코드 받기

**방법 A — git 있으면:**

```bash
git clone -b claude/real-time-video-translation-jchd1q https://github.com/hyunsekim07-lab/hyunsekim07.git
cd hyunsekim07/live_translate
```

**방법 B — git 없으면:** 아래 주소에서 ZIP을 받아 풀고, 그 안의 `live_translate` 폴더로 들어가세요.

```
https://github.com/hyunsekim07-lab/hyunsekim07/archive/refs/heads/claude/real-time-video-translation-jchd1q.zip
```

**확인** — 폴더 안에 파일들이 보이면 통과:

```bash
ls        # 윈도우는  dir
```

`check.py`, `live_sub.py`, `README.md` 가 보여야 합니다.

---

## 3단계 — 지금 상태 점검

여기가 핵심입니다. **무엇이 빠졌는지 알아서 알려줍니다.**

```bash
python3 check.py
```

맨 아래에 둘 중 하나가 나옵니다:

- **`아직 안 됩니다`** → 바로 아래에 할 일이 번호로 적혀 있습니다. 그대로 하고 `python3 check.py` 를 다시 돌리세요.
- **`준비 끝`** → 6단계로 건너뛰세요.

---

## 4단계 — 필요한 패키지 설치

3단계가 패키지가 없다고 하면:

```bash
python3 -m pip install numpy sounddevice scipy faster-whisper deep-translator
```

끝나면 `python3 check.py` 를 다시 돌려 확인하세요.

---

## 5단계 — 소리를 잡을 수 있게 만들기

**이 프로그램은 영상 파일을 읽지 않습니다. 스피커로 나가는 소리를 잡습니다.**
그래서 사이트 영상, 스트리밍, 뭐든 소리만 나면 됩니다. 대신 OS별로 설정이 다릅니다.

### 윈도우 — 할 게 없습니다
WASAPI loopback을 자동으로 씁니다. 6단계로 가세요.

### 리눅스 — 할 게 없습니다
PulseAudio/PipeWire의 monitor 소스를 자동으로 찾습니다. 6단계로 가세요.

### 맥 — BlackHole 설정이 필요합니다 (한 번만)

맥은 Apple 정책상 가상 오디오 장치 없이는 시스템 소리를 잡을 수 없습니다.

**5-1) BlackHole 설치**

```bash
brew install blackhole-2ch
```

Homebrew가 없으면 <https://existential.audio/blackhole/> 에서 **2ch** 설치 파일을 받으세요.
설치 후 **재부팅**하세요.

**5-2) 듣기와 잡기를 동시에 — 다중 출력 장치 만들기**

BlackHole로만 출력하면 **귀에 아무 소리도 안 들립니다.** 둘을 묶어야 합니다.

1. Spotlight(`⌘` + `스페이스`)에서 **Audio MIDI 설정** 실행
2. 좌하단 **`+`** → **다중 출력 장치 생성**
3. **내장 출력**(또는 쓰는 헤드폰)과 **BlackHole 2ch** 를 둘 다 체크
4. **내장 출력을 맨 위(마스터)** 로 두고, BlackHole 쪽 **Drift Correction** 체크
5. 시스템 설정 → 사운드 → **출력을 "다중 출력 장치" 로** 변경

> 이걸 쓰면 **키보드 볼륨 키가 안 먹습니다.** 볼륨은 영상 플레이어 안에서 조절하고,
> 다 본 뒤에는 출력을 원래 장치로 되돌리세요.

**5-3) 마이크 권한** — 빼먹으면 계속 무음입니다

시스템 설정 → **개인정보 보호 및 보안 → 마이크** → **터미널**(또는 iTerm) 켜기

**확인** — 영상을 재생한 상태로:

```bash
python3 live_sub.py --device BlackHole --calibrate
```

10초 동안 `rms` 숫자가 **움직이면 통과**입니다. 계속 `0.0000` 이면 5-2나 5-3이 안 된 겁니다.

---

## 6단계 — 실행

```bash
# 윈도우 / 리눅스
python3 live_sub.py --src ja --dst ko

# 맥
python3 live_sub.py --device BlackHole --src ja --dst ko
```

윈도우는 **`run.bat` 더블클릭**으로도 됩니다 (일본어 -> 한국어, 로컬 번역, medium 모델).

> **`--src auto` 는 권장하지 않습니다.** 짧은 대사가 많으면 Whisper 의 언어 감지가 자주 틀리고,
> 틀린 쪽으로 인식해 버립니다. 보려는 영상의 언어를 `--src` 로 직접 지정하는 편이 훨씬 정확합니다.

**첫 실행은 1~3분 걸립니다** — Whisper 모델(약 500MB)을 한 번 받습니다. 다음부터는 바로 뜹니다.

`[ok  ] 실행 중` 이 보이면 영상을 재생하세요. 화면 아래에 자막 창이 뜹니다.

| 조작 | |
|---|---|
| 드래그 | 자막 창 이동 |
| 마우스 휠 | 글자 크기 |
| 우클릭 | 메뉴 (원문 표시, 종료) |
| `Esc` | 종료 |

---

## 안 될 때

| 증상 | 해결 |
|---|---|
| 자막 창은 떴는데 계속 "대기 중" | 소리를 못 잡는 중. `python3 live_sub.py --calibrate` 로 확인 |
| `rms` 가 계속 0 | 맥이면 5-2/5-3 확인. 윈도우면 `--list-devices` 후 `<= 스피커 소리` 표시된 번호를 `--device` 로 지정 |
| `rms` 는 움직이는데 자막이 안 뜸 | 기준값이 높음. `--threshold 0.002` 를 붙여보세요 |
| 자막이 영상보다 많이 느림 | `--model base --silence 300` 으로 가볍게 |
| 번역이 어색함 | `--translator claude` (품질 최상, `ANTHROPIC_API_KEY` 필요) |
| `too many requests` 오류 반복 | Google 무료 번역 제한. `python -m pip install -U deep-translator` 후에도 그러면 `--translator local` |
| `No module named '_tkinter'` | 맥: `brew install python-tk` / 급하면 `--console` 로 터미널 자막 |
| 일본어가 아닌 영상 | `--src auto` (문장마다 감지, 한국어면 번역 안 하고 원문만) |
| 번역이 기계 번역체 | `--translator claude` (앞 대사 3줄을 문맥으로 넘깁니다) |
| 맥락에 안 맞는 말이 나옴 | 잘못 들은 것. `--model medium` 으로 키우거나 `--prompt "자주 나오는 단어"` |

더 자세한 옵션은 [README.md](README.md) 또는 `python3 live_sub.py --help`.
