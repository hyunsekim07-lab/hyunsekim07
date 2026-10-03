#!/usr/bin/env python3
"""
diag.py - 무엇이 왜 안 되는지 원인을 그대로 보여준다. 에러를 숨기지 않는다.

    python3 diag.py
"""

from __future__ import annotations

import platform
import sys
import traceback

LINE = "=" * 70


def section(title: str) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}")


# ------------------------------------------------------------------ 환경
section("1. 환경")
print(f"파이썬  : {sys.version}")
print(f"실행파일: {sys.executable}")
print(f"OS      : {platform.system()} {platform.release()} ({platform.machine()})")


# ------------------------------------------------------------------ 패키지
section("2. 패키지 — 설치 여부와 '실제로 불러와지는지'는 다르다")
for name in ("numpy", "scipy", "sounddevice", "soundcard", "ctranslate2",
             "faster_whisper", "deep_translator", "av", "tokenizers", "onnxruntime"):
    try:
        import importlib
        import importlib.util

        spec = importlib.util.find_spec(name)
    except Exception as exc:                                 # noqa: BLE001
        print(f"[X ] {name:<16} find_spec 실패: {exc}")
        continue

    if spec is None:
        print(f"[- ] {name:<16} 설치 안 됨")
        continue

    try:
        mod = importlib.import_module(name)
        version = getattr(mod, "__version__", "?")
        print(f"[OK] {name:<16} {version}")
    except Exception as exc:                                 # noqa: BLE001
        print(f"[X ] {name:<16} 설치는 됐는데 불러오기 실패 -> {type(exc).__name__}: {exc}")
        print("     --- 전체 오류 ---")
        traceback.print_exc()
        print("     ----------------")


# ------------------------------------------------------------------ 음성인식
section("3. faster-whisper 실제로 쓸 수 있나")
try:
    from faster_whisper import WhisperModel                  # noqa: F401

    print("[OK] from faster_whisper import WhisperModel  성공")
except Exception:                                            # noqa: BLE001
    print("[X ] 실패. 전체 오류:")
    traceback.print_exc()


# ------------------------------------------------------------------ 오디오
section("4. 오디오 장치 (sounddevice / PortAudio)")
try:
    import sounddevice as sd

    print(f"sounddevice {sd.__version__}")
    try:
        print(f"PortAudio   {sd.get_portaudio_version()}")
    except Exception as exc:                                 # noqa: BLE001
        print(f"PortAudio   확인 실패: {exc}")

    try:
        is_loopback = sd._lib.PaWasapi_IsLoopback
        print("PaWasapi_IsLoopback: 사용 가능")
    except Exception as exc:                                 # noqa: BLE001
        is_loopback = None
        print(f"PaWasapi_IsLoopback: 없음 ({exc})")

    hostapis = list(sd.query_hostapis())
    print("\nhost API:")
    for i, api in enumerate(hostapis):
        print(f"  {i}  {api['name']}  (기본출력={api.get('default_output_device')})")

    print(f"\n기본 장치: 입력={sd.default.device[0]}  출력={sd.default.device[1]}")

    print(f"\n{'idx':>4} {'in':>3} {'out':>3} {'loopback':>9}  host / name")
    print("-" * 70)
    loopback_found = []
    for i, dev in enumerate(sd.query_devices()):
        flag = ""
        if is_loopback is not None and dev["max_input_channels"] > 0:
            try:
                raw = is_loopback(i)
                flag = str(raw)
                if raw > 0:
                    loopback_found.append(i)
            except Exception as exc:                         # noqa: BLE001
                flag = f"err:{exc}"
        api = hostapis[dev["hostapi"]]["name"] if dev["hostapi"] < len(hostapis) else "?"
        print(f"{i:>4} {dev['max_input_channels']:>3} {dev['max_output_channels']:>3}"
              f" {flag:>9}  [{api}] {dev['name']}")

    print(f"\nloopback 으로 판정된 장치: {loopback_found if loopback_found else '없음'}")
except Exception:                                            # noqa: BLE001
    print("[X ] sounddevice 사용 불가. 전체 오류:")
    traceback.print_exc()


# ------------------------------------------------------------------ soundcard
section("5. soundcard (Windows loopback 대안 경로)")
try:
    import soundcard as sc

    print(f"soundcard {getattr(sc, '__version__', '?')}")
    try:
        speaker = sc.default_speaker()
        print(f"기본 스피커: {speaker.name}")
    except Exception as exc:                                 # noqa: BLE001
        print(f"기본 스피커 확인 실패: {exc}")

    mics = sc.all_microphones(include_loopback=True)
    print(f"\n입력 장치 {len(mics)}개 (loopback 포함):")
    for m in mics:
        kind = "loopback" if getattr(m, "isloopback", False) else "mic"
        print(f"  [{kind:>8}] {m.name}")
except ImportError:
    print("[- ] soundcard 설치 안 됨.  pip install soundcard")
except Exception:                                            # noqa: BLE001
    print("[X ] soundcard 오류:")
    traceback.print_exc()


section("6. GPU / CUDA 라이브러리")
try:
    import ctranslate2

    count = ctranslate2.get_cuda_device_count()
    print(f"ctranslate2 {getattr(ctranslate2, '__version__', '?')} — CUDA 장치 {count}개")
except Exception as exc:                                     # noqa: BLE001
    count = 0
    print(f"ctranslate2 확인 실패: {exc}")

try:
    import os
    import site

    roots = set(site.getsitepackages())
    try:
        roots.add(site.getusersitepackages())
    except Exception:                                        # noqa: BLE001
        pass

    any_found = False
    for root in sorted(roots):
        nvidia = os.path.join(root, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        any_found = True
        print(f"\n{nvidia}")
        for pkg in sorted(os.listdir(nvidia)):
            for leaf in ("bin", "lib"):
                path = os.path.join(nvidia, pkg, leaf)
                if not os.path.isdir(path):
                    continue
                files = [f for f in os.listdir(path) if f.lower().endswith((".dll", ".so"))]
                shown = ", ".join(sorted(files)[:6]) or "(비어 있음)"
                print(f"  {pkg}/{leaf}: {len(files)}개 — {shown}")
    if not any_found:
        print("\nnvidia-* 패키지가 설치되어 있지 않습니다:")
        print(f"    {sys.executable} -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")

    target = "cublas64_12.dll" if platform.system() == "Windows" else "libcublas.so.12"
    hits = []
    for root in roots:
        for base, _dirs, files in os.walk(os.path.join(root, "nvidia")):
            for name in files:
                if name.lower().startswith(target.split(".")[0].lower()):
                    hits.append(os.path.join(base, name))
    print(f"\n{target} 검색 결과: {hits if hits else '찾지 못함'}")
except Exception:                                            # noqa: BLE001
    traceback.print_exc()


print(f"\n{LINE}\n이 출력 전체를 그대로 복사해서 보내주세요.\n{LINE}")
