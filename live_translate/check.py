#!/usr/bin/env python3
"""
check.py - live_sub 를 돌릴 준비가 됐는지 하나씩 점검한다.

표준 라이브러리만 쓰므로 아무것도 설치하지 않은 상태에서도 그냥 돌아간다.

    python3 check.py
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

OK, WARN, BAD = "[ OK ]", "[ ? ]", "[ X ]"

PACKAGES = [                        # (import 이름, pip 이름, 필수 여부, 설명)
    ("numpy", "numpy", True, "숫자 계산"),
    ("sounddevice", "sounddevice", True, "오디오 캡처"),
    ("faster_whisper", "faster-whisper", True, "음성 인식"),
    ("soundcard", "soundcard", False, "Windows 스피커 소리 캡처"),
    ("scipy", "scipy", False, "리샘플링 (없어도 동작)"),
    ("deep_translator", "deep-translator", False, "번역 (--translator google)"),
    ("anthropic", "anthropic", False, "번역 (--translator claude)"),
    ("transformers", "transformers", False, "번역 (--translator local)"),
]

BLOCKED_HINTS = ("애플리케이션 제어 정책", "application control policy",
                 "DLL load failed", "WinError 1260")

todo: list[str] = []
blockers: list[str] = []


def head(title: str) -> None:
    print(f"\n{title}\n" + "-" * 62)


def pip_cmd() -> str:
    return f"{sys.executable} -m pip install"


# ---------------------------------------------------------------- 1. 파이썬
def check_python() -> None:
    head("1. 파이썬")
    version = ".".join(str(n) for n in sys.version_info[:3])
    if sys.version_info < (3, 9):
        print(f"{BAD} 파이썬 {version} — 3.9 이상이 필요합니다.")
        blockers.append("파이썬 3.9 이상을 설치하세요 (https://www.python.org/downloads/).")
    else:
        print(f"{OK} 파이썬 {version}")
    print(f"       실행 파일: {sys.executable}")
    print(f"       OS: {platform.system()} {platform.release()} ({platform.machine()})")


# ---------------------------------------------------------------- 2. 패키지
def check_packages() -> None:
    """설치 여부가 아니라 '실제로 불러와지는지' 를 본다.

    설치는 됐는데 확장 모듈(.pyd/.dll)이 차단되어 못 쓰는 경우가 있어서,
    find_spec 만 보면 멀쩡해 보이는데 실행은 실패한다.
    """
    import importlib
    import importlib.util

    head("2. 파이썬 패키지 (실제로 불러와지는지까지 확인)")
    missing_required, missing_optional, broken = [], [], []

    for module, pkg, required, note in PACKAGES:
        try:
            installed = importlib.util.find_spec(module) is not None
        except Exception:                                    # noqa: BLE001
            installed = False

        if not installed:
            mark = BAD if required else WARN
            tail = "" if required else " (선택)"
            print(f"{mark} {pkg:<18} {note}  <- 없음{tail}")
            (missing_required if required else missing_optional).append(pkg)
            continue

        try:
            importlib.import_module(module)
            print(f"{OK} {pkg:<18} {note}")
        except Exception as exc:                             # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"
            print(f"{BAD} {pkg:<18} {note}  <- 설치는 됐는데 불러오기 실패")
            print(f"       {reason[:120]}")
            broken.append((pkg, reason, required))

    if missing_required:
        blockers.append(
            f"필수 패키지를 설치하세요:\n    {pip_cmd()} {' '.join(missing_required)}"
        )

    if broken:
        _report_broken(broken)

    if "deep-translator" in missing_optional and "anthropic" in missing_optional:
        todo.append(f"번역기가 없습니다:\n    {pip_cmd()} deep-translator")


def _report_broken(broken) -> None:
    """불러오기 실패한 패키지를 '차단된 것' 과 '그 외' 로 나눠 안내한다."""
    blocked = [(p, r) for p, r, _ in broken if any(h in r for h in BLOCKED_HINTS)]
    names = {p for p, _, _ in broken}

    if blocked:
        print(f"\n{WARN} 확장 모듈(.pyd)이 Windows 에 의해 차단된 것으로 보입니다.")
        print("       '스마트 앱 제어(Smart App Control)' 가 서명 없는 파일을 막을 때 나는 증상입니다.")

    # scipy 는 없어도 되고, av 는 실시간 캡처에 안 쓰이므로 자동 우회된다.
    fatal = names - {"scipy"}
    if "faster-whisper" in fatal:
        blockers.append(
            "faster-whisper 를 불러오지 못합니다.\n"
            "    대부분 av(PyAV) 의 _core.pyd 가 차단된 경우이고, live_sub.py 가 자동 우회하므로\n"
            "    일단 그냥 실행해보세요:\n"
            f"        {sys.executable} live_sub.py --src ja --dst ko\n"
            "    그래도 안 되면 Windows 보안 -> 앱 및 브라우저 제어 -> 스마트 앱 제어 -> 끄기\n"
            f"    전체 원인:  {sys.executable} diag.py"
        )
    if "scipy" in names:
        todo.append("scipy 가 차단됐지만 없어도 동작합니다 (16 kHz 로 직접 받습니다). 무시하세요.")


# ---------------------------------------------------------------- 3. tkinter
def check_tkinter() -> None:
    head("3. 자막 창 (tkinter)")
    try:
        import tkinter                                       # noqa: F401
    except Exception as exc:                                 # noqa: BLE001
        print(f"{WARN} tkinter 를 쓸 수 없습니다: {exc}")
        if platform.system() == "Darwin":
            fix = "brew install python-tk"
        elif platform.system() == "Linux":
            fix = "sudo apt install python3-tk   (또는 배포판에 맞는 패키지)"
        else:
            fix = "python.org 설치 파일로 파이썬을 다시 설치하면 함께 들어옵니다"
        print(f"       화면 자막을 쓰려면: {fix}")
        print("       지금 당장은 --console 을 붙이면 터미널에 자막이 나옵니다.")
        todo.append(f"화면 자막을 쓰려면 tkinter 설치: {fix}")
    else:
        print(f"{OK} tkinter 사용 가능 (화면 위 자막 창을 띄울 수 있습니다)")


# ---------------------------------------------------------------- 4. 오디오
def check_audio() -> None:
    head("4. 오디오 입력 — 스피커로 나가는 소리를 잡을 수 있나")
    try:
        import sounddevice as sd
    except Exception as exc:                                 # noqa: BLE001
        print(f"{BAD} sounddevice 를 불러올 수 없습니다: {exc}")
        if platform.system() == "Linux":
            print("       리눅스는 portaudio 가 필요합니다: sudo apt install libportaudio2")
        return

    try:
        devices = list(sd.query_devices())
        hostapis = list(sd.query_hostapis())
    except Exception as exc:                                 # noqa: BLE001
        print(f"{BAD} 오디오 장치를 읽을 수 없습니다: {exc}")
        blockers.append("오디오 시스템을 인식하지 못했습니다. 소리가 나는 상태인지 확인하세요.")
        return

    system = platform.system()
    print(f"       장치 {len(devices)}개 발견\n")

    if system == "Windows":
        _check_windows(sd, devices, hostapis)
    elif system == "Darwin":
        _check_macos(devices)
    else:
        _check_linux(devices)

    print("\n       전체 목록:  python3 live_sub.py --list-devices")


def _check_windows(sd, devices, hostapis) -> None:
    """스피커 소리는 WASAPI 'loopback' 입력 장치로만 잡을 수 있다.

    그런 장치인지는 PortAudio 의 PaWasapi_IsLoopback 으로만 구분된다
    (이름만 봐서는 일반 마이크 입력과 구별되지 않는다).
    """
    try:
        is_loopback = sd._lib.PaWasapi_IsLoopback
    except Exception:                                        # noqa: BLE001
        print(f"{BAD} 이 sounddevice 빌드에는 WASAPI loopback 기능이 없습니다.")
        blockers.append(f"sounddevice 를 올리세요:\n    {pip_cmd()} -U sounddevice")
        return

    found = []
    for i, d in enumerate(devices):
        if d["max_input_channels"] <= 0:
            continue
        if not hostapis[d["hostapi"]]["name"].startswith("Windows WASAPI"):
            continue
        try:
            if is_loopback(i) > 0:
                found.append((i, d))
        except Exception:                                    # noqa: BLE001
            continue

    if found:
        print(f"{OK} 스피커 소리를 잡을 수 있는 loopback 입력 장치가 있습니다.")
        for i, d in found[:6]:
            print(f"       {i:>3}  {d['name']}")
        print("\n       그냥 실행하면 지금 소리가 나가는 장치를 자동으로 고릅니다.")
    else:
        print(f"{BAD} WASAPI loopback 입력 장치를 찾지 못했습니다.")
        print("       스피커로 나가는 소리를 가져올 통로가 없는 상태입니다.")
        blockers.append(
            "다음 중 하나를 하세요:\n"
            f"    a) {pip_cmd()} -U sounddevice   (최신 PortAudio 에 loopback 이 들어있습니다)\n"
            "    b) 소리 설정 -> 입력에서 '스테레오 믹스' 를 켜고 그 장치를 --device 로 지정\n"
            "    c) VB-CABLE 같은 가상 오디오 장치 설치"
        )


def _check_macos(devices) -> None:
    virtual = [
        (i, d) for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
        and any(k in d["name"].lower() for k in ("blackhole", "loopback", "soundflower"))
    ]
    if virtual:
        print(f"{OK} 가상 오디오 장치가 설치되어 있습니다.")
        for i, d in virtual:
            print(f"       {i:>3}  {d['name']}")
        print("\n       아직 남은 것 (한 번만 하면 됩니다):")
        print("       a) Audio MIDI 설정 -> '+' -> 다중 출력 장치 생성")
        print("          -> 내장 출력 + BlackHole 2ch 둘 다 체크, Drift Correction 체크")
        print("       b) 시스템 설정 -> 사운드 -> 출력을 '다중 출력 장치' 로 변경")
        print("       c) 시스템 설정 -> 개인정보 보호 및 보안 -> 마이크 -> 터미널 허용")
        print("\n       다 됐는지 확인:  python3 live_sub.py --device BlackHole --calibrate")
        todo.append(
            "BlackHole 은 깔렸습니다. 다중 출력 장치 생성 + 출력 전환 + 마이크 권한을 마치고\n"
            "    python3 live_sub.py --device BlackHole --calibrate 로 소리가 들어오는지 확인하세요."
        )
    else:
        print(f"{BAD} 가상 오디오 장치가 없습니다.")
        print("       macOS 는 Apple 정책상 가상 장치 없이는 시스템 소리를 잡을 수 없습니다.")
        brew = "설치됨" if shutil.which("brew") else "없음 — https://brew.sh 참고"
        print(f"       Homebrew: {brew}")
        blockers.append(
            "BlackHole 을 설치하세요:\n"
            "    brew install blackhole-2ch\n"
            "    (Homebrew 가 없으면 https://existential.audio/blackhole/ 에서 2ch 설치 파일)\n"
            "    설치 후 재부팅하고 이 점검을 다시 돌리세요."
        )


def _check_linux(devices) -> None:
    monitors = [
        (i, d) for i, d in enumerate(devices)
        if d["max_input_channels"] > 0 and "monitor" in d["name"].lower()
    ]
    if monitors:
        print(f"{OK} monitor 입력 장치가 있어 추가 설치 없이 바로 됩니다.")
        for i, d in monitors[:4]:
            print(f"       {i:>3}  {d['name']}")
    else:
        pulse = [i for i, d in enumerate(devices)
                 if d["max_input_channels"] > 0 and "pulse" in d["name"].lower()]
        if pulse:
            print(f"{WARN} monitor 장치는 없지만 pulse 입력이 있습니다.")
            print("       pavucontrol 의 '녹음' 탭에서 입력을 Monitor 로 바꿔주세요.")
            print(f"       실행:  python3 live_sub.py --device pulse")
        else:
            print(f"{BAD} 시스템 출력을 잡을 입력 장치가 없습니다.")
            print("       PulseAudio/PipeWire 가 돌고 있는지 확인하세요.")
            blockers.append("PulseAudio/PipeWire 의 monitor 소스를 찾지 못했습니다.")


# ---------------------------------------------------------------- 5. GPU
def _cuda_libs_present() -> tuple[bool, list[str]]:
    """pip 로 설치한 nvidia-* 의 DLL 폴더가 있는지. (있음, 경로들)"""
    import os

    try:
        import site

        roots = set(site.getsitepackages())
        try:
            roots.add(site.getusersitepackages())
        except Exception:                                    # noqa: BLE001
            pass
    except Exception:                                        # noqa: BLE001
        return False, []

    found = []
    for root in roots:
        nvidia = os.path.join(root, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        for pkg in os.listdir(nvidia):
            for leaf in ("bin", "lib"):
                path = os.path.join(nvidia, pkg, leaf)
                if os.path.isdir(path) and os.listdir(path):
                    found.append(f"{pkg}/{leaf}")
    return bool(found), found


def check_gpu() -> None:
    head("5. 속도 — GPU 를 쓰고 있나")
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
    except Exception:                                        # noqa: BLE001
        count = 0

    if count > 0:
        has_libs, found = _cuda_libs_present()
        print(f"{OK} CUDA GPU {count}개 발견")
        if has_libs:
            print(f"{OK} CUDA 라이브러리 설치됨: {', '.join(sorted(set(found))[:4])}")
            print("       GPU 로 동작합니다. --model medium 까지 쓸 만합니다.")
        else:
            print(f"{BAD} GPU 는 있는데 CUDA 라이브러리가 없어 CPU 로 돌게 됩니다.")
            print("       자막이 말보다 몇 초씩 늦는 가장 큰 원인입니다.")
            todo.append(
                "GPU 를 켜면 인식이 몇 배 빨라집니다:\n"
                f"    {pip_cmd()} nvidia-cublas-cu12 nvidia-cudnn-cu12"
            )
    elif platform.system() == "Darwin" and platform.machine() == "arm64":
        print(f"{WARN} Apple Silicon 입니다. faster-whisper 는 CPU 로만 돕니다.")
        print("       --model small 로 시작하고, 버벅이면 --model base 로 내리세요.")
    else:
        print(f"{WARN} CUDA GPU 없음 — CPU 로 돕니다.")
        print("       --model small 로 시작하고, 버벅이면 --model base 로 내리세요.")


# ---------------------------------------------------------------- 결론
def verdict() -> int:
    print("\n" + "=" * 62)
    if blockers:
        print("아직 안 됩니다. 다음을 먼저 해결하세요:\n")
        for n, item in enumerate(blockers, 1):
            print(f"  {n}. {item}\n")
        print("해결한 뒤 이 점검을 다시 돌리세요:  python3 check.py")
        return 1

    print("준비 끝. 바로 실행할 수 있습니다:\n")
    if platform.system() == "Darwin":
        print("    python3 live_sub.py --device BlackHole --src ja --dst ko")
    else:
        print("    python3 live_sub.py --src ja --dst ko")

    if todo:
        print("\n먼저 확인해두면 좋은 것:\n")
        for n, item in enumerate(todo, 1):
            print(f"  {n}. {item}\n")
    print("자막이 안 뜨면:  python3 live_sub.py --calibrate")
    return 0


def main() -> int:
    print("=" * 62)
    print("live_sub 준비 상태 점검")
    print("=" * 62)
    check_python()
    if not blockers:
        check_packages()
    check_tkinter()
    check_audio()
    check_gpu()
    return verdict()


if __name__ == "__main__":
    sys.exit(main())
