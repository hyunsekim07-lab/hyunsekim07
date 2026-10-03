#!/usr/bin/env python3
"""
live_sub.py - 컴퓨터에서 재생 중인 영상 소리를 실시간으로 받아
            음성인식(Whisper) -> 번역 -> 화면 위 자막 오버레이로 띄운다.

    python live_sub.py --list-devices
    python live_sub.py --src ja --dst ko
"""

from __future__ import annotations

import argparse
import os
import platform
import queue
import re
import sys
import threading
import time
from collections import deque

import numpy as np

# 모델을 내려받을 때 huggingface_hub 가 윈도우에서 뿜는 symlink 경고를 숨긴다.
# (캐시가 복사본으로 저장될 뿐 동작에는 문제가 없다.)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

TARGET_SR = 16000          # Whisper 입력 샘플레이트
FRAME_MS = 30              # VAD 프레임 길이
RECORD_MS = 100            # soundcard 한 번에 읽는 길이 (너무 짧으면 끊김이 생긴다)


def _stub_av() -> bool:
    """av(PyAV) 를 불러올 수 없는 환경에서도 faster-whisper 를 쓰게 한다.

    av 는 faster_whisper/audio.py 의 decode_audio() 안에서만 쓰이고, 그 함수는
    '오디오 파일' 을 읽을 때만 호출된다. 우리는 항상 numpy 배열을 직접 넘기므로
    실제로 쓰이지 않는데, 모듈 최상단의 `import av` 때문에 전체가 죽는다.
    (Windows 스마트 앱 제어가 av/_core.pyd 를 차단하는 경우가 대표적이다.)
    """
    import importlib
    import types

    try:
        importlib.import_module("av")
        return False
    except Exception:                                        # noqa: BLE001
        pass

    class _Unavailable(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            raise RuntimeError(
                "이 환경에서는 av(PyAV) 를 불러올 수 없어 오디오 '파일' 디코딩은 쓸 수 없습니다.\n"
                "실시간 캡처에는 영향이 없습니다."
            )

    sys.modules["av"] = _Unavailable("av")
    return True


def _import_help(module: str, pip_name: str, exc: Exception) -> str:
    """'설치가 안 된 것' 과 '설치는 됐는데 불러오기가 실패한 것' 을 구분해서 알려준다."""
    import importlib.util

    try:
        installed = importlib.util.find_spec(module) is not None
    except Exception:                                        # noqa: BLE001
        installed = False

    if not installed:
        return f"{pip_name} 가 없습니다:\n    {sys.executable} -m pip install {pip_name}"
    return (
        f"{pip_name} 는 설치돼 있는데 불러오지 못했습니다.\n"
        f"  실제 오류: {type(exc).__name__}: {exc}\n\n"
        f"전체 원인을 보려면:  {sys.executable} diag.py"
    )


# --------------------------------------------------------------------------
# 오디오 입력 장치
# --------------------------------------------------------------------------

def _hostapi_name(sd, dev) -> str:
    try:
        return sd.query_hostapis(dev["hostapi"])["name"]
    except Exception:
        return ""


def list_devices(sd) -> None:
    loopback = {i for i, _ in loopback_inputs(sd)}
    print(f"{'idx':>4}  {'in':>3} {'out':>3}  {'rate':>6}  host / name")
    print("-" * 78)
    for i, dev in enumerate(sd.query_devices()):
        mark = "  <= 스피커 소리" if i in loopback else ""
        print(
            f"{i:>4}  {dev['max_input_channels']:>3} {dev['max_output_channels']:>3}"
            f"  {int(dev['default_samplerate']):>6}"
            f"  [{_hostapi_name(sd, dev)}] {dev['name']}{mark}"
        )
    if loopback:
        print(f"\n'<= 스피커 소리' 표시가 붙은 번호를 --device 로 주면 됩니다.")
        print("지정하지 않으면 지금 소리가 나가는 장치를 자동으로 고릅니다.")
    print(
        "\n스피커로 나가는 소리를 잡으려면:"
        "\n  Windows  : 위에서 '<= 스피커 소리' 로 표시된 입력 장치 (자동 선택됨)"
        "\n  Linux    : 이름에 'monitor' 가 들어간 입력 장치"
        "\n  macOS    : BlackHole / Loopback 같은 가상 출력 장치를 먼저 설치"
    )


def _find_by_name(sd, needle: str) -> int:
    needle = needle.lower()
    for i, dev in enumerate(sd.query_devices()):
        if needle in dev["name"].lower():
            return i
    raise SystemExit(f"'{needle}' 와 일치하는 오디오 장치가 없습니다. --list-devices 로 확인하세요.")


def _norm(name: str) -> str:
    """장치 이름 비교용 정규화 (loopback 장치는 원본 출력 장치와 이름이 같거나 접미사만 다르다)."""
    return re.sub(r"[\s\[\]()]+", "", name.lower().replace("loopback", ""))


def loopback_inputs(sd) -> list[tuple[int, dict]]:
    """WASAPI loopback 입력 장치 [(index, device_info)].

    PortAudio 는 스피커로 나가는 소리를 '입력 장치' 형태로 따로 노출하고,
    그 장치인지는 PaWasapi_IsLoopback 으로만 구분할 수 있다.
    """
    if platform.system() != "Windows":
        return []
    try:
        is_loopback = sd._lib.PaWasapi_IsLoopback
    except Exception:                    # noqa: BLE001  (다른 플랫폼엔 심볼이 없다)
        return []

    found = []
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        if not _hostapi_name(sd, dev).startswith("Windows WASAPI"):
            continue
        try:
            if is_loopback(i) > 0:
                found.append((i, dev))
        except Exception:                # noqa: BLE001
            continue
    return found


def _to_loopback(sd, idx: int) -> int:
    """출력 장치를 지정했으면 같은 이름의 loopback 입력 장치로 바꿔준다."""
    dev = sd.query_devices(idx)
    if dev["max_input_channels"] > 0:
        return idx                       # 이미 입력 장치
    candidates = loopback_inputs(sd)
    if not candidates:
        raise SystemExit(
            f"'{dev['name']}' 는 출력 전용 장치라 소리를 받을 수 없고,\n"
            "WASAPI loopback 입력 장치도 찾지 못했습니다.\n\n"
            "해결 방법:\n"
            "  1) pip install -U sounddevice  (loopback 지원은 최신 PortAudio 가 필요합니다)\n"
            "  2) 또는 Windows 소리 설정에서 '스테레오 믹스' 를 켜고 그 장치를 --device 로 지정\n"
            "  3) 또는 VB-CABLE 같은 가상 오디오 장치 설치\n\n"
            "현재 장치 목록:  python live_sub.py --list-devices"
        )
    target = _norm(dev["name"])
    for i, cand in candidates:           # 같은 이름의 loopback 을 우선
        if _norm(cand["name"]) == target:
            return i
    return candidates[0][0]


def resolve_device(sd, spec):
    """--device 값(또는 None)으로부터 (index, extra_settings, 표시이름)."""
    if spec is not None:
        idx = int(spec) if re.fullmatch(r"-?\d+", str(spec).strip()) else _find_by_name(sd, spec)
        if platform.system() == "Windows":
            idx = _to_loopback(sd, idx)
    else:
        idx = _auto_device(sd)
    dev = sd.query_devices(idx)
    return idx, None, f"[{_hostapi_name(sd, dev)}] {dev['name']}"


STEREO_MIX_NAMES = ("스테레오 믹스", "stereo mix", "what u hear",
                    "wave out mix", "웨이브 출력 믹스", "믹스")


def _stereo_mix_input(sd):
    """'스테레오 믹스' 류의 입력 장치 index (없으면 None).

    PortAudio 에 loopback 이 없을 때 스피커 소리를 받을 수 있는 또 다른 통로.
    """
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        name = dev["name"].lower()
        if any(k in name for k in STEREO_MIX_NAMES):
            return i
    return None


def _auto_device(sd) -> int:
    system = platform.system()
    devices = list(sd.query_devices())

    if system == "Windows":
        mix = _stereo_mix_input(sd)
        candidates = loopback_inputs(sd)
        if not candidates and mix is not None:
            print(f"[audio] loopback 대신 '스테레오 믹스' 를 씁니다: {sd.query_devices(mix)['name']}")
            return mix
        if candidates:
            default_out = None
            for api in sd.query_hostapis():
                if api["name"].startswith("Windows WASAPI") and api["default_output_device"] >= 0:
                    default_out = sd.query_devices(api["default_output_device"])
                    break
            if default_out is None and sd.default.device[1] is not None and sd.default.device[1] >= 0:
                default_out = sd.query_devices(sd.default.device[1])
            if default_out is not None:          # 지금 소리가 나가는 장치와 같은 것을 고른다
                target = _norm(default_out["name"])
                for i, cand in candidates:
                    if _norm(cand["name"]) == target:
                        return i
            return candidates[0][0]

    if system == "Linux":
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0 and "monitor" in dev["name"].lower():
                return i

    if system == "Darwin":
        for key in ("blackhole", "loopback", "soundflower", "aggregate"):
            for i, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and key in dev["name"].lower():
                    return i

    default_in = sd.default.device[0]
    if default_in is not None and default_in >= 0:
        print(
            "\n"
            "!! 스피커 소리를 잡을 통로를 찾지 못해 '마이크' 로 떨어졌습니다.\n"
            "!! 이 상태로는 영상 소리가 아니라 주변 소리를 인식하므로 자막이 제대로 안 나옵니다.\n"
            "   해결: 소리 설정 -> 입력에서 '스테레오 믹스' 켜기, 또는\n"
            "         python live_sub.py --list-devices 로 확인 후 --device 로 지정\n",
            file=sys.stderr,
        )
        return default_in
    raise SystemExit("사용할 수 있는 오디오 장치가 없습니다. --list-devices 로 확인하세요.")


# --------------------------------------------------------------------------
# 캡처 -> 모노 16 kHz
# --------------------------------------------------------------------------

class Capture:
    """오디오 콜백에서 받은 블록을 모노 16 kHz float32 로 바꿔 큐에 넣는다."""

    def __init__(self, sd, device, extra_settings, out_queue: "queue.Queue[np.ndarray]"):
        self.sd = sd
        self.device = device
        self.extra = extra_settings
        self.q = out_queue
        self.stream = None
        self.native_sr = None
        self.channels = None
        self._resampler = _make_resampler()

    def start(self) -> None:
        dev = self.sd.query_devices(self.device)
        native_sr = int(dev["default_samplerate"]) or TARGET_SR

        # loopback 으로 여는 출력 장치는 max_input_channels 가 0 으로 보고된다.
        wanted = [dev["max_input_channels"], dev["max_output_channels"], 2, 1]
        candidates = list(dict.fromkeys(min(c, 2) for c in wanted if c and c > 0))

        errors = []
        for sr in dict.fromkeys([native_sr, TARGET_SR, 48000, 44100]):
            for ch in candidates:
                try:
                    stream = self.sd.InputStream(
                        device=self.device,
                        channels=ch,
                        samplerate=sr,
                        blocksize=int(sr * FRAME_MS / 1000),
                        dtype="float32",
                        latency="low",
                        callback=self._callback,
                        extra_settings=self.extra,
                    )
                    stream.start()
                    self.stream, self.native_sr, self.channels = stream, sr, ch
                    return
                except Exception as exc:                    # noqa: BLE001
                    errors.append(f"  {sr} Hz / {ch}ch -> {exc}")
        raise SystemExit("오디오 스트림을 열지 못했습니다:\n" + "\n".join(errors))

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if status:
            pass                                             # 오버플로는 무시하고 계속
        mono = indata.mean(axis=1) if indata.ndim > 1 and indata.shape[1] > 1 else indata.reshape(-1)
        self.q.put(self._resampler(mono.astype(np.float32, copy=True), self.native_sr))

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:                                # noqa: BLE001
                pass


def _com_init() -> bool:
    """현재 스레드에 Windows COM 을 초기화한다.

    soundcard 는 import 시점에 '그 스레드' 에서만 CoInitializeEx 를 부른다.
    캡처를 별도 스레드에서 돌리면 그 스레드는 초기화되지 않은 상태라
    오디오 클라이언트를 열 때 0x800401f0 (CO_E_NOTINITIALIZED) 로 실패한다.
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes

        ole32 = ctypes.windll.ole32
    except Exception:                                        # noqa: BLE001
        return False

    COINIT_MULTITHREADED, COINIT_APARTMENTTHREADED = 0x0, 0x2
    RPC_E_CHANGED_MODE = -2147417850                         # 0x80010106
    hr = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if hr == RPC_E_CHANGED_MODE:                             # 이미 STA 로 잡혀 있으면 맞춰준다
        hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    return hr in (0, 1)                                      # S_OK / S_FALSE


def _com_uninit() -> None:
    try:
        import ctypes

        ctypes.windll.ole32.CoUninitialize()
    except Exception:                                        # noqa: BLE001
        pass


class SoundcardCapture:
    """soundcard 라이브러리로 WASAPI loopback 을 직접 연다.

    sounddevice 가 쓰는 PortAudio 빌드에 loopback 장치가 없을 때의 대안 경로.
    콜백이 아니라 블로킹 read 라서 전용 스레드에서 돌린다.
    """

    def __init__(self, spec, out_queue: "queue.Queue[np.ndarray]"):
        try:
            import soundcard as sc
        except Exception as exc:                             # noqa: BLE001
            raise SystemExit(_import_help("soundcard", "soundcard", exc)) from exc

        # soundcard 는 끊김 경고를 'always' 로 띄워 화면을 덮는다. 한 번만 보이게 바꾼다.
        try:
            import warnings

            warnings.filterwarnings("once", message=".*discontinuity.*")
        except Exception:                                    # noqa: BLE001
            pass

        self.sc = sc
        self.q = out_queue
        self.mic = _pick_soundcard_mic(sc, spec)
        self.native_sr = TARGET_SR
        self.channels = 2
        self.label = f"[soundcard] {self.mic.name}"
        self.error: Exception | None = None
        self._resampler = _make_resampler()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self._ready.wait(timeout=5.0)                        # 열자마자 나는 오류를 먼저 잡는다
        if self.error is not None:
            raise SystemExit(f"soundcard 로 소리를 열지 못했습니다: {self.error}")

    def _loop(self) -> None:
        last: Exception | None = None
        com_owned = _com_init()                              # 이 스레드에서 COM 을 쓸 수 있게 한다
        try:
            self._record_loop()
        finally:
            if com_owned:
                _com_uninit()

    def _record_loop(self) -> None:
        last: Exception | None = None
        # 16 kHz 로 바로 받으면 리샘플링(scipy)이 아예 필요 없다.
        for rate in (TARGET_SR, 48000, 44100):
            block = int(rate * RECORD_MS / 1000)
            try:
                with self.mic.recorder(samplerate=rate, channels=self.channels,
                                       blocksize=block) as rec:
                    self.native_sr = rate
                    self._ready.set()
                    while not self._stop.is_set():
                        data = rec.record(numframes=block)
                        if data is None or len(data) == 0:
                            continue
                        data = np.asarray(data, dtype=np.float32)
                        mono = (data.mean(axis=1) if data.ndim > 1 and data.shape[1] > 1
                                else data.reshape(-1))
                        self.q.put(self._resampler(mono, rate))
                return
            except Exception as exc:                         # noqa: BLE001
                last = exc
                if self._ready.is_set():                     # 열린 뒤 끊긴 것이면 재시도하지 않는다
                    break
        self.error = last
        self._ready.set()

    def stop(self) -> None:
        self._stop.set()


def _pick_soundcard_mic(sc, spec):
    """soundcard 에서 쓸 입력(가능하면 loopback)을 고른다."""
    mics = sc.all_microphones(include_loopback=True)
    loops = [m for m in mics if getattr(m, "isloopback", False)]

    if spec is not None:
        needle = str(spec).lower()
        for mic in loops + mics:
            if needle in mic.name.lower():
                return mic
        names = "\n".join(f"    {m.name}" for m in mics) or "    (없음)"
        raise SystemExit(f"'{spec}' 와 맞는 장치가 없습니다. 사용 가능한 장치:\n{names}")

    try:                                                     # 지금 소리가 나가는 스피커와 짝을 맞춘다
        speaker = sc.default_speaker().name.lower()
        for mic in loops:
            name = mic.name.lower()
            if name == speaker or speaker in name or name in speaker:
                return mic
    except Exception:                                        # noqa: BLE001
        pass

    if loops:
        return loops[0]
    raise SystemExit(
        "soundcard 에서도 loopback 장치를 찾지 못했습니다.\n"
        "Windows 소리 설정에서 '스테레오 믹스' 를 켜거나 VB-CABLE 설치가 필요합니다."
    )


def make_capture(args, audio_q: "queue.Queue[np.ndarray]"):
    """백엔드를 골라 캡처 객체를 만들고 시작한다. (capture, 표시이름) 반환."""
    backend = args.backend
    sd = None
    if backend in ("auto", "sounddevice"):
        try:
            import sounddevice as sd_mod

            sd = sd_mod
        except Exception as exc:                             # noqa: BLE001
            if backend == "sounddevice":
                raise SystemExit(_import_help("sounddevice", "sounddevice", exc)) from exc

    if backend == "soundcard":
        capture = SoundcardCapture(args.device, audio_q)
        capture.start()
        return capture, capture.label

    # auto: Windows 인데 PortAudio 가 loopback 장치를 못 주면 soundcard 로 돌아간다
    if (backend == "auto" and platform.system() == "Windows"
            and (sd is None or not loopback_inputs(sd))):
        print("[audio] PortAudio 에 loopback 장치가 없어 soundcard 경로를 씁니다.")
        try:
            capture = SoundcardCapture(args.device, audio_q)
            capture.start()
            return capture, capture.label
        except SystemExit as exc:
            if sd is None:
                raise
            print(f"[audio] soundcard 실패 -> sounddevice 로 계속\n  {exc}", file=sys.stderr)

    if sd is None:
        raise SystemExit(_import_help("sounddevice", "sounddevice", RuntimeError("불러오기 실패")))

    device, extra, label = resolve_device(sd, args.device)
    capture = Capture(sd, device, extra, audio_q)
    capture.start()
    return capture, f"{label}  ({capture.native_sr} Hz / {capture.channels}ch)"


def _make_resampler():
    try:
        from math import gcd

        from scipy.signal import resample_poly

        def resample(x, sr):
            if sr == TARGET_SR:
                return x
            g = gcd(int(sr), TARGET_SR)
            return resample_poly(x, TARGET_SR // g, int(sr) // g).astype(np.float32)

        return resample
    except Exception:                                        # noqa: BLE001
        def resample(x, sr):                                 # 선형 보간 폴백
            if sr == TARGET_SR:
                return x
            n = int(round(len(x) * TARGET_SR / sr))
            if n <= 0:
                return np.zeros(0, dtype=np.float32)
            src = np.linspace(0, len(x) - 1, num=len(x), dtype=np.float64)
            dst = np.linspace(0, len(x) - 1, num=n, dtype=np.float64)
            return np.interp(dst, src, x).astype(np.float32)

        return resample


# --------------------------------------------------------------------------
# 에너지 기반 발화 구간 분할 (VAD)
# --------------------------------------------------------------------------

class Segmenter:
    """말이 끊기는 지점에서 오디오를 잘라 Whisper 에 넘길 조각을 만든다."""

    def __init__(self, silence_ms=450, min_speech_ms=350, max_seg_s=6.0,
                 pad_ms=300, threshold=None, abs_floor=0.0035):
        self.frame = int(TARGET_SR * FRAME_MS / 1000)
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.min_speech_frames = max(1, min_speech_ms // FRAME_MS)
        self.max_frames = max(1, int(max_seg_s * 1000) // FRAME_MS)
        self.pad_frames = max(1, pad_ms // FRAME_MS)
        self.fixed_threshold = threshold
        self.abs_floor = abs_floor

        self.tail = np.zeros(0, dtype=np.float32)
        self.noise = deque(maxlen=250)          # 최근 ~7.5초의 프레임 RMS
        self.pre = deque(maxlen=self.pad_frames)
        self.buf: list[np.ndarray] = []
        self.in_speech = False
        self.silence_run = 0
        self.speech_run = 0
        self.level = 0.0
        self.threshold = abs_floor

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        segments: list[np.ndarray] = []
        self.tail = np.concatenate([self.tail, samples])
        while len(self.tail) >= self.frame:
            frame, self.tail = self.tail[:self.frame], self.tail[self.frame:]
            segments.extend(self._push(frame))
        return segments

    def _push(self, frame: np.ndarray) -> list[np.ndarray]:
        rms = float(np.sqrt(np.mean(frame * frame)) + 1e-12)
        self.level = rms

        if self.fixed_threshold is not None:
            self.threshold = self.fixed_threshold
        else:
            self.noise.append(rms)
            floor = float(np.percentile(self.noise, 15)) if len(self.noise) >= 30 else 0.0
            self.threshold = max(floor * 3.0, self.abs_floor)

        voiced = rms > self.threshold

        if not self.in_speech:
            self.pre.append(frame)
            if voiced:
                self.in_speech = True
                self.buf = list(self.pre)
                self.pre.clear()
                self.speech_run = 1
                self.silence_run = 0
            return []

        self.buf.append(frame)
        if voiced:
            self.speech_run += 1
            self.silence_run = 0
        else:
            self.silence_run += 1

        if self.silence_run >= self.silence_frames:
            if self.speech_run >= self.min_speech_frames:
                return [self._close()]
            self._close()                       # 너무 짧은 잡음 -> 버림
            return []

        if len(self.buf) >= self.max_frames:
            return [self._close(continuing=True)]
        return []

    def _close(self, continuing: bool = False) -> np.ndarray:
        audio = np.concatenate(self.buf) if self.buf else np.zeros(0, dtype=np.float32)
        if continuing:
            # 말이 계속 이어지는 중이므로 끝부분을 다음 조각의 앞머리로 남긴다.
            self.buf = self.buf[-self.pad_frames:]
            self.speech_run = 0
            self.silence_run = 0
        else:
            self.buf = []
            self.pre.clear()
            self.in_speech = False
            self.speech_run = 0
            self.silence_run = 0
        return audio


# --------------------------------------------------------------------------
# 음성 인식
# --------------------------------------------------------------------------

HALLUCINATIONS = {
    "ご視聴ありがとうございました", "ご視聴ありがとうございます",
    "チャンネル登録お願いします", "最後までご視聴いただきありがとうございました",
    "おわり", "終わり", "字幕", "字幕視聴者",
    "thanks for watching", "thank you for watching", "subscribe",
    "please subscribe", "you", "bye", "다음 영상에서 만나요",
    "시청해주셔서 감사합니다", "구독과 좋아요",
}


def _is_junk(text: str) -> bool:
    stripped = re.sub(r"[\s。、．，.,!?！？…~ー\-♪♬*]", "", text).lower()
    if not stripped:
        return True
    if stripped in {h.replace(" ", "").lower() for h in HALLUCINATIONS}:
        return True
    # 같은 글자가 계속 반복되는 전형적인 환각
    return len(set(stripped)) <= 2 and len(stripped) >= 6


CUDA_ERROR_HINTS = ("cublas", "cudnn", "cuda", "libcu", "gpu")


def _add_nvidia_dll_dirs() -> list[str]:
    """pip 로 설치한 nvidia-* 패키지의 DLL 폴더를 Windows DLL 검색 경로에 넣는다.

    nvidia-cublas-cu12 / nvidia-cudnn-cu12 는 site-packages 안에 DLL 을 두는데
    그 경로는 기본 검색 대상이 아니라서, 넣어주지 않으면 못 찾는다.
    """
    if platform.system() != "Windows" or not hasattr(os, "add_dll_directory"):
        return []

    try:
        import site

        roots = set(site.getsitepackages())
        try:
            roots.add(site.getusersitepackages())
        except Exception:                                    # noqa: BLE001
            pass
    except Exception:                                        # noqa: BLE001
        roots = set()

    added = []
    for root in roots:
        nvidia = os.path.join(root, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        for pkg in os.listdir(nvidia):
            for leaf in ("bin", "lib"):
                path = os.path.join(nvidia, pkg, leaf)
                if os.path.isdir(path):
                    try:
                        os.add_dll_directory(path)
                        added.append(path)
                    except Exception:                        # noqa: BLE001
                        pass
    return added


def _looks_like_cuda_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(k in text for k in CUDA_ERROR_HINTS)


class Recognizer:
    def __init__(self, model_size: str, device: str, compute_type: str | None,
                 language: str | None, beam_size: int):
        stubbed_av = _stub_av()
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:                             # noqa: BLE001
            raise SystemExit(_import_help("faster_whisper", "faster-whisper", exc)) from exc
        if stubbed_av:
            print("[asr ] av(PyAV) 를 불러올 수 없어 우회했습니다 — 실시간 캡처에는 무관합니다.")

        if device == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        if device == "cuda":
            _add_nvidia_dll_dirs()
        if compute_type is None:
            compute_type = "float16" if device == "cuda" else "int8"

        self._WhisperModel = WhisperModel
        self.model_size = model_size
        self.language = language
        self.beam_size = beam_size
        self.vad_filter = True
        self.device = device
        self.compute_type = compute_type
        self.model = None
        self._load(device, compute_type)

    def _load(self, device: str, compute_type: str) -> None:
        print(f"[asr ] 모델 로드 중: {self.model_size} ({device}/{compute_type}) ...", flush=True)
        try:
            self.model = self._WhisperModel(self.model_size, device=device,
                                            compute_type=compute_type)
            self.device, self.compute_type = device, compute_type
        except Exception as exc:                             # noqa: BLE001
            if device == "cpu":
                raise SystemExit(f"음성 인식 모델을 불러오지 못했습니다: {exc}") from exc
            print(f"[asr ] {device} 실패({exc}) -> cpu 로 전환", flush=True)
            self._load("cpu", "int8")
            return
        print("[asr ] 준비 완료", flush=True)

    def _fallback_to_cpu(self, exc: Exception) -> bool:
        """GPU 추론이 실패하면 CPU 로 갈아탄다. 전환했으면 True."""
        if self.device != "cuda" or not _looks_like_cuda_error(exc):
            return False
        print(
            f"\n[asr ] GPU 추론 실패: {exc}\n"
            "[asr ] CUDA 라이브러리가 없어 CPU 로 전환합니다. 자막은 계속 나옵니다.\n"
            "       GPU 를 쓰려면:  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12\n",
            flush=True,
        )
        self._load("cpu", "int8")
        return True

    def transcribe(self, audio: np.ndarray) -> str:
        try:
            return self._transcribe(audio)
        except Exception as exc:                             # noqa: BLE001
            if self._fallback_to_cpu(exc):
                return self._transcribe(audio)
            raise

    def _transcribe(self, audio: np.ndarray) -> str:
        kwargs = dict(
            language=self.language,
            beam_size=self.beam_size,
            temperature=0.0,
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
            vad_filter=self.vad_filter,
        )
        try:
            segments, _info = self.model.transcribe(audio, **kwargs)
        except Exception:                                    # noqa: BLE001
            if not self.vad_filter:
                raise
            self.vad_filter = False                          # onnxruntime 없음 등
            kwargs["vad_filter"] = False
            segments, _info = self.model.transcribe(audio, **kwargs)

        parts = []
        for seg in segments:
            if getattr(seg, "no_speech_prob", 0.0) > 0.75:
                continue
            if getattr(seg, "avg_logprob", 0.0) < -1.1:
                continue
            text = seg.text.strip()
            if text:
                parts.append(text)
        text = " ".join(parts).strip()
        return "" if _is_junk(text) else text


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:                                        # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 번역 백엔드
# --------------------------------------------------------------------------

LANG_NAMES = {
    "ja": "Japanese", "ko": "Korean", "en": "English", "zh": "Chinese",
    "es": "Spanish", "fr": "French", "de": "German", "ru": "Russian",
}

NLLB_CODES = {
    "ja": "jpn_Jpan", "ko": "kor_Hang", "en": "eng_Latn", "zh": "zho_Hans",
    "es": "spa_Latn", "fr": "fra_Latn", "de": "deu_Latn", "ru": "rus_Cyrl",
}


class NullTranslator:
    name = "none"

    def translate(self, text: str) -> str:
        return text


class GoogleTranslator:
    """deep-translator 경유. 설치가 가볍고 키가 필요 없다 (인식된 '문장'만 전송됨)."""

    name = "google"

    def __init__(self, src: str, dst: str):
        try:
            from deep_translator import GoogleTranslator as _G
        except Exception as exc:                             # noqa: BLE001
            raise SystemExit(_import_help("deep_translator", "deep-translator", exc)) from exc
        self._engine = _G(source=src, target=dst)

    def translate(self, text: str) -> str:
        return (self._engine.translate(text) or "").strip()


class LocalTranslator:
    """NLLB-200 로 기기 안에서만 번역. 첫 실행 때 모델을 내려받는다(약 2.5 GB)."""

    name = "local"

    def __init__(self, src: str, dst: str, model_id: str = "facebook/nllb-200-distilled-600M"):
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:
            raise SystemExit(
                "로컬 번역에는 transformers 가 필요합니다:\n"
                "    pip install transformers sentencepiece torch"
            ) from exc
        if src not in NLLB_CODES or dst not in NLLB_CODES:
            raise SystemExit(f"로컬 번역이 지원하지 않는 언어쌍입니다: {src} -> {dst}")

        print(f"[trans] 로컬 번역 모델 로드 중: {model_id} ...", flush=True)
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, src_lang=NLLB_CODES[src])
        self.model = AutoModelForSeq2SeqLM.from_pretrained(model_id)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device).eval()
        self.dst_code = NLLB_CODES[dst]
        print("[trans] 준비 완료", flush=True)

    def translate(self, text: str) -> str:
        batch = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=256)
        batch = {k: v.to(self.device) for k, v in batch.items()}
        bos = self.tokenizer.convert_tokens_to_ids(self.dst_code)
        with self._torch.inference_mode():
            out = self.model.generate(**batch, forced_bos_token_id=bos,
                                      max_new_tokens=192, num_beams=1)
        return self.tokenizer.batch_decode(out, skip_special_tokens=True)[0].strip()


class ClaudeTranslator:
    """Claude API. 구어체/생략이 많은 대사에서 품질이 가장 좋다. ANTHROPIC_API_KEY 필요."""

    name = "claude"

    def __init__(self, src: str, dst: str, model: str = "claude-opus-5", context: int = 3):
        try:
            import anthropic
        except Exception as exc:                             # noqa: BLE001
            raise SystemExit(_import_help("anthropic", "anthropic", exc)) from exc
        self.client = anthropic.Anthropic(timeout=20.0, max_retries=1)
        self.model = model
        self.history: deque[tuple[str, str]] = deque(maxlen=context)
        self.system = (
            f"You translate live subtitles from {LANG_NAMES.get(src, src)} "
            f"to {LANG_NAMES.get(dst, dst)}.\n"
            "Rules:\n"
            "- Output ONLY the translation. No quotes, no notes, no romanization, "
            "no explanation, no original text.\n"
            "- Keep it natural and colloquial, the way a subtitle would read.\n"
            "- Keep it on one short line.\n"
            "- Earlier lines are given for context; translate ONLY the last line.\n"
            "- If the line is inaudible or meaningless filler, output nothing."
        )

    def translate(self, text: str) -> str:
        messages = []
        for source, target in self.history:
            messages.append({"role": "user", "content": source})
            messages.append({"role": "assistant", "content": target})
        messages.append({"role": "user", "content": text})

        kwargs = dict(model=self.model, max_tokens=300, system=self.system, messages=messages)
        if not self.model.startswith("claude-haiku"):
            kwargs["output_config"] = {"effort": "low"}      # 자막은 속도가 우선

        response = self.client.messages.create(**kwargs)
        if getattr(response, "stop_reason", None) == "refusal":
            return ""
        out = "".join(b.text for b in response.content if b.type == "text").strip()
        if out:
            self.history.append((text, out))
        return out


def build_translator(kind: str, src: str, dst: str, claude_model: str):
    if kind == "none" or src == dst:
        return NullTranslator()
    if kind == "google":
        return GoogleTranslator(src, dst)
    if kind == "local":
        return LocalTranslator(src, dst)
    if kind == "claude":
        return ClaudeTranslator(src, dst, model=claude_model)
    raise SystemExit(f"알 수 없는 번역 백엔드: {kind}")


# --------------------------------------------------------------------------
# 자막 오버레이
# --------------------------------------------------------------------------

def _subtitle_font() -> str:
    system = platform.system()
    if system == "Windows":
        return "Malgun Gothic"
    if system == "Darwin":
        return "Apple SD Gothic Neo"
    return "Noto Sans CJK KR"


class Overlay:
    """항상 위에 떠 있는 반투명 자막 창. tkinter 이므로 메인 스레드에서만 다룬다."""

    def __init__(self, font_size: int, opacity: float, show_source: bool, width_ratio: float):
        import tkinter as tk

        self.tk = tk
        self.show_source = show_source
        self.font_size = font_size

        self.root = tk.Tk()
        self.root.title("live_sub")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", opacity)
        except Exception:                                    # noqa: BLE001
            pass
        self.root.configure(bg="#000000")

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = int(screen_w * width_ratio)

        family = _subtitle_font()
        self.frame = tk.Frame(self.root, bg="#000000", padx=18, pady=12)
        self.frame.pack(fill="both", expand=True)

        self.main = tk.Label(
            self.frame, text="자막 대기 중...  (드래그=이동, 휠=글자크기, 우클릭=메뉴)",
            font=(family, font_size, "bold"), fg="#ffffff", bg="#000000",
            wraplength=width - 40, justify="center",
        )
        self.main.pack(fill="x")

        self.sub = tk.Label(
            self.frame, text="", font=(family, max(10, int(font_size * 0.65))),
            fg="#9fd8ff", bg="#000000", wraplength=width - 40, justify="center",
        )
        if show_source:
            self.sub.pack(fill="x")

        self.root.update_idletasks()
        self.root.geometry(
            f"{width}x{self.root.winfo_reqheight()}"
            f"+{(screen_w - width) // 2}+{int(screen_h * 0.78)}"
        )

        self._drag = (0, 0)
        for widget in (self.root, self.frame, self.main, self.sub):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<Button-3>", self._menu)
            widget.bind("<MouseWheel>", self._wheel)
            widget.bind("<Button-4>", lambda e: self._resize(+2))
            widget.bind("<Button-5>", lambda e: self._resize(-2))
        self.root.bind("<Escape>", lambda e: self.close())

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="원문 표시/숨기기", command=self.toggle_source)
        self.menu.add_command(label="글자 크게", command=lambda: self._resize(+2))
        self.menu.add_command(label="글자 작게", command=lambda: self._resize(-2))
        self.menu.add_separator()
        self.menu.add_command(label="종료 (Esc)", command=self.close)

        self.closed = False
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    # -- 이벤트 -------------------------------------------------------------
    def _drag_start(self, event):
        self._drag = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        self.root.geometry(f"+{event.x_root - self._drag[0]}+{event.y_root - self._drag[1]}")

    def _menu(self, event):
        self.menu.tk_popup(event.x_root, event.y_root)

    def _wheel(self, event):
        self._resize(+2 if event.delta > 0 else -2)

    def _resize(self, delta: int):
        self.font_size = max(10, min(72, self.font_size + delta))
        family = _subtitle_font()
        self.main.configure(font=(family, self.font_size, "bold"))
        self.sub.configure(font=(family, max(10, int(self.font_size * 0.65))))

    def toggle_source(self):
        self.show_source = not self.show_source
        if self.show_source:
            self.sub.pack(fill="x")
        else:
            self.sub.pack_forget()

    # -- 표시 ---------------------------------------------------------------
    def show(self, translated: str, original: str):
        self.main.configure(text=translated or original)
        self.sub.configure(text=original if translated else "")

    def close(self):
        self.closed = True
        try:
            self.root.destroy()
        except Exception:                                    # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 파이프라인
# --------------------------------------------------------------------------

def run(args) -> int:
    try:
        import sounddevice as sd
    except ImportError:
        print("sounddevice 가 없습니다:\n    pip install sounddevice", file=sys.stderr)
        return 1

    if args.list_devices:
        list_devices(sd)
        return 0

    if args.calibrate:
        return _calibrate(args)

    recognizer = Recognizer(args.model, args.device_type, args.compute_type,
                            None if args.src == "auto" else args.src, args.beam_size)
    translator = build_translator(args.translator, args.src, args.dst, args.claude_model)
    print(f"[trans] 백엔드: {translator.name}  ({args.src} -> {args.dst})")

    audio_q: queue.Queue[np.ndarray] = queue.Queue()
    seg_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=args.queue_limit)
    text_q: queue.Queue[str] = queue.Queue()
    out_q: queue.Queue[tuple[str, str]] = queue.Queue()
    stop = threading.Event()

    capture, label = make_capture(args, audio_q)
    print(f"[audio] 입력: {label} -> 16000 Hz mono")

    segmenter = Segmenter(
        silence_ms=args.silence, min_speech_ms=args.min_speech,
        max_seg_s=args.max_segment, pad_ms=args.pad, threshold=args.threshold,
    )

    def segment_worker():
        while not stop.is_set():
            try:
                block = audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            for segment in segmenter.feed(block):
                if segment.size == 0:
                    continue
                try:
                    seg_q.put_nowait(segment)
                except queue.Full:
                    try:                                     # 밀리면 가장 오래된 것부터 버린다
                        seg_q.get_nowait()
                        seg_q.put_nowait(segment)
                    except queue.Empty:
                        pass

    def asr_worker():
        while not stop.is_set():
            try:
                segment = seg_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                text = recognizer.transcribe(segment)
            except Exception as exc:                         # noqa: BLE001
                print(f"[asr ] 오류: {exc}", file=sys.stderr)
                continue
            if text:
                text_q.put(text)

    def translate_worker():
        cache: dict[str, str] = {}
        last = ""
        while not stop.is_set():
            try:
                text = text_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if text == last:                                 # 같은 대사 반복 표시 방지
                continue
            last = text
            if text in cache:
                out_q.put((cache[text], text))
                continue
            try:
                translated = translator.translate(text)
            except Exception as exc:                         # noqa: BLE001
                print(f"[trans] 오류: {exc}", file=sys.stderr)
                translated = ""
            if translated:
                cache[text] = translated
                if len(cache) > 500:
                    cache.pop(next(iter(cache)))
            out_q.put((translated, text))

    threads = [
        threading.Thread(target=segment_worker, daemon=True),
        threading.Thread(target=asr_worker, daemon=True),
        threading.Thread(target=translate_worker, daemon=True),
    ]
    for thread in threads:
        thread.start()

    print("[ok  ] 실행 중. 영상을 재생하세요.  (종료: Esc 또는 Ctrl+C)\n", flush=True)

    try:
        if args.console:
            _console_loop(out_q, stop, args.show_source)
        else:
            _overlay_loop(out_q, stop, args)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        capture.stop()
    return 0


def _console_loop(out_q, stop, show_source):
    while not stop.is_set():
        try:
            translated, original = out_q.get(timeout=0.3)
        except queue.Empty:
            continue
        if show_source and translated:
            print(f"  {original}")
        print(f"> {translated or original}", flush=True)


def _overlay_loop(out_q, stop, args):
    try:
        overlay = Overlay(args.font_size, args.opacity, args.show_source, args.width)
    except Exception as exc:                                 # noqa: BLE001
        print(f"[ui  ] 오버레이를 만들 수 없어 콘솔 모드로 전환합니다: {exc}", file=sys.stderr)
        _console_loop(out_q, stop, args.show_source)
        return

    def pump():
        if overlay.closed:
            stop.set()
            return
        drained = None
        while True:
            try:
                drained = out_q.get_nowait()
            except queue.Empty:
                break
        if drained is not None:
            translated, original = drained
            overlay.show(translated, original)
            if args.show_source and translated:
                print(f"  {original}")
            print(f"> {translated or original}", flush=True)
        overlay.root.after(60, pump)

    overlay.root.after(60, pump)
    overlay.root.mainloop()
    stop.set()


def _calibrate(args) -> int:
    """지금 들어오는 소리의 크기를 보여줘 --threshold 를 정하도록 돕는다."""
    audio_q: queue.Queue[np.ndarray] = queue.Queue()
    capture, label = make_capture(args, audio_q)
    print(f"[audio] 입력: {label}\n")
    segmenter = Segmenter()
    print("10초 동안 레벨을 측정합니다. 영상을 평소 볼륨으로 재생하세요.\n")
    end = time.time() + 10
    try:
        while time.time() < end:
            try:
                segmenter.feed(audio_q.get(timeout=0.3))
            except queue.Empty:
                continue
            bar = "#" * min(50, int(segmenter.level * 500))
            print(f"\r  rms={segmenter.level:.4f}  기준={segmenter.threshold:.4f}  {bar:<50}",
                  end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()
    print("\n\n소리가 날 때 rms 가 기준값을 넘지 않으면 --threshold 로 더 낮은 값을 주세요.")
    return 0


# --------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="재생 중인 영상 소리를 실시간 인식/번역해 자막으로 띄웁니다.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    audio = parser.add_argument_group("오디오")
    audio.add_argument("--list-devices", action="store_true", help="오디오 장치 목록만 출력")
    audio.add_argument("--device", help="장치 번호 또는 이름 일부 (미지정 시 자동 탐지)")
    audio.add_argument("--calibrate", action="store_true", help="입력 레벨 10초 측정")
    audio.add_argument("--backend", default="auto",
                       choices=["auto", "sounddevice", "soundcard"],
                       help="오디오 캡처 방식. auto 면 Windows 에서 필요할 때 soundcard 로 전환")

    asr = parser.add_argument_group("음성 인식")
    asr.add_argument("--model", default="small",
                     help="Whisper 모델: tiny/base/small/medium/large-v3")
    asr.add_argument("--device-type", default="auto", choices=["auto", "cuda", "cpu"])
    asr.add_argument("--compute-type", default=None, help="float16 / int8 / int8_float16 등")
    asr.add_argument("--beam-size", type=int, default=1, help="1이 가장 빠름")
    asr.add_argument("--src", default="ja", help="원본 언어 (auto 면 자동 감지)")

    trans = parser.add_argument_group("번역")
    trans.add_argument("--dst", default="ko", help="번역 대상 언어")
    trans.add_argument("--translator", default="google",
                       choices=["google", "local", "claude", "none"],
                       help="google=설치 간단, local=기기 안에서만, claude=품질 최상")
    trans.add_argument("--claude-model", default="claude-opus-5",
                       help="--translator claude 일 때 쓸 모델")

    seg = parser.add_argument_group("구간 분할 (지연 시간 조절)")
    seg.add_argument("--silence", type=int, default=450, help="이만큼 조용하면 한 문장으로 끊음(ms)")
    seg.add_argument("--min-speech", type=int, default=350, help="이보다 짧은 소리는 무시(ms)")
    seg.add_argument("--max-segment", type=float, default=6.0, help="한 조각 최대 길이(초)")
    seg.add_argument("--pad", type=int, default=300, help="앞머리 여유(ms)")
    seg.add_argument("--threshold", type=float, default=None,
                     help="고정 음량 기준값 (미지정 시 자동)")
    seg.add_argument("--queue-limit", type=int, default=6,
                     help="밀렸을 때 버리기 시작하는 대기 조각 수")

    ui = parser.add_argument_group("자막 표시")
    ui.add_argument("--console", action="store_true", help="오버레이 없이 터미널에만 출력")
    ui.add_argument("--show-source", action="store_true", help="원문도 같이 표시")
    ui.add_argument("--font-size", type=int, default=26)
    ui.add_argument("--opacity", type=float, default=0.85)
    ui.add_argument("--width", type=float, default=0.8, help="화면 가로 대비 자막창 너비 비율")
    return parser.parse_args(argv)


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    sys.exit(main())
