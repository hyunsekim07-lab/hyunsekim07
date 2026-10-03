#!/usr/bin/env python3
"""
selftest.py - 정답을 아는 일본어 문장으로 인식/번역이 맞는지 직접 대조한다.

실제 live_sub.py 의 코드(Segmenter / Recognizer / 번역기)를 그대로 호출하므로,
여기서 통과하면 실제 자막 경로도 같은 결과를 낸다.

    python selftest.py                     # 음성 합성 -> 인식 -> 번역 (전체 경로)
    python selftest.py --text-only         # 번역만 (음성 합성 불필요, 빠름)
    python selftest.py --audio my.mp3      # 직접 만든 음성 파일로
    python selftest.py --save-audio        # 스피커로 틀어볼 음성 파일만 만들기
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import live_sub as L                                         # noqa: E402

# (일본어 원문, 사람이 번역한 참고 한국어)
CASES = [
    ("今日はいい天気ですね。", "오늘은 날씨가 좋네요."),
    ("ちょっと待ってください。", "잠깐만 기다려 주세요."),
    ("これは本当に美味しいです。", "이건 정말 맛있어요."),
    ("すみません、駅はどこですか。", "실례지만 역이 어디예요?"),
    ("昨日の映画はとても面白かった。", "어제 영화는 정말 재미있었어요."),
    ("ありがとう、助かりました。", "고마워요, 덕분에 살았어요."),
    ("もう一度言ってもらえますか。", "한 번 더 말해 주시겠어요?"),
    ("明日の会議は三時からです。", "내일 회의는 3시부터예요."),
]

VOICE = "ja-JP-NanamiNeural"
PAUSE_S = 0.9                                                # 문장 사이 무음 (VAD 가 끊도록)


def _norm(text: str) -> str:
    return re.sub(r"[\s。、．，.,!?！？…「」『』]", "", text)


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# ------------------------------------------------------------------ 음성 만들기
def synthesize(path: str) -> "np.ndarray":                   # noqa: F821
    """edge-tts 로 일본어 음성을 만들고 16 kHz 모노로 돌려준다."""
    import asyncio

    import numpy as np

    try:
        import edge_tts
    except ImportError:
        raise SystemExit(
            "음성 합성에는 edge-tts 가 필요합니다:\n"
            f"    {sys.executable} -m pip install edge-tts\n\n"
            "설치하기 싫으면 번역만 확인하세요:\n"
            f"    {sys.executable} selftest.py --text-only"
        ) from None

    text = f"<_PAUSE_>".join(j for j, _ in CASES)
    print(f"[tts ] 일본어 음성 생성 중 ({VOICE}) ...", flush=True)

    async def run():
        chunks = []
        for japanese, _ in CASES:
            communicate = edge_tts.Communicate(japanese, VOICE)
            buf = bytearray()
            async for item in communicate.stream():
                if item["type"] == "audio":
                    buf.extend(item["data"])
            chunks.append(bytes(buf))
        return chunks

    try:
        parts = asyncio.run(run())
    except Exception as exc:                                 # noqa: BLE001
        raise SystemExit(
            f"음성 합성 실패: {exc}\n"
            "인터넷이 막혀 있으면 번역만 확인하세요:\n"
            f"    {sys.executable} selftest.py --text-only"
        ) from exc

    silence = np.zeros(int(L.TARGET_SR * PAUSE_S), dtype=np.float32)
    per_sentence = [_decode(chunk) for chunk in parts]

    combined = [silence]
    for one in per_sentence:
        combined.append(one)
        combined.append(silence)
    samples = np.concatenate(combined)

    _write_wav(path, samples)
    print(f"[tts ] 저장: {path}  ({len(samples) / L.TARGET_SR:.1f}초, 문장 {len(per_sentence)}개)")
    return per_sentence, samples


def _decode(data: bytes) -> "np.ndarray":                    # noqa: F821
    """mp3 바이트를 16 kHz 모노 float32 로."""
    import io

    import numpy as np

    try:
        import av
    except Exception as exc:                                 # noqa: BLE001
        raise SystemExit(f"오디오 디코딩에 av(PyAV)가 필요합니다: {exc}") from exc

    with av.open(io.BytesIO(data)) as container:
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono",
                                                      rate=L.TARGET_SR)
        out = []
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                out.append(resampled.to_ndarray().reshape(-1))
    if not out:
        return np.zeros(0, dtype=np.float32)
    return (np.concatenate(out).astype(np.float32) / 32768.0)


def _write_wav(path: str, samples: "np.ndarray") -> None:    # noqa: F821
    import wave

    import numpy as np

    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(L.TARGET_SR)
        f.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())


def load_audio(path: str) -> "np.ndarray":                   # noqa: F821
    import numpy as np

    if path.lower().endswith(".wav"):
        import wave

        with wave.open(path, "rb") as f:
            rate = f.getframerate()
            raw = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
            if f.getnchannels() > 1:
                raw = raw.reshape(-1, f.getnchannels()).mean(axis=1)
        samples = raw.astype(np.float32) / 32768.0
        if rate != L.TARGET_SR:
            samples = L._make_resampler()(samples, rate)
        return samples

    with open(path, "rb") as f:
        return _decode(f.read())


# ------------------------------------------------------------------ 본체
def _segment(samples, args):
    """실제 Segmenter 로 구간을 나눈다."""
    segmenter = L.Segmenter(silence_ms=args.silence, min_speech_ms=args.min_speech,
                            max_seg_s=args.max_segment, pad_ms=args.pad)
    segments = []
    block = int(L.TARGET_SR * L.RECORD_MS / 1000)
    for i in range(0, len(samples), block):
        segments.extend(segmenter.feed(samples[i:i + block]))
    if segmenter.buf:
        segments.append(segmenter._close())
    return segments


def run_pipeline(per_sentence, combined, args):
    """문장 하나씩 따로 통과시킨다.

    전부 이어붙여 놓고 VAD 로 다시 쪼개면 구간 개수가 어긋날 수 있고,
    그러면 '몇 번째 문장의 결과인지' 가 밀려서 대조가 무의미해진다.
    """
    import numpy as np

    recognizer = L.Recognizer(args.model, args.device_type, args.compute_type,
                              "ja", args.beam_size)
    translator = L.build_translator(args.translator, "ja", "ko", args.claude_model)
    print(f"[trans] 백엔드: {translator.name}\n")

    if combined is not None:                                 # 참고용: 실제 구간 분할 동작
        count = len(_segment(combined, args))
        status = "일치" if count == len(per_sentence) else "불일치"
        print(f"[seg ] 이어붙인 음성을 VAD 로 나누면 {count}개 "
              f"(문장 {len(per_sentence)}개, {status})\n")

    silence = np.zeros(int(L.TARGET_SR * 0.5), dtype=np.float32)
    rows = []
    for audio in per_sentence:
        padded = np.concatenate([silence, audio, silence])
        pieces = _segment(padded, args) or [padded]
        text_parts, langs = [], []
        for piece in pieces:
            if piece.size < L.TARGET_SR * 0.2:
                continue
            text, lang = recognizer.transcribe(piece)
            if text:
                text_parts.append(text)
                langs.append(lang)
        text = " ".join(text_parts)
        if not text:
            rows.append(("", "<인식 결과 없음>"))
            continue
        try:
            translated = translator.translate(text, src="ja")
        except Exception as exc:                             # noqa: BLE001
            translated = f"<번역 실패: {str(exc)[:60]}>"
        rows.append((text, translated))
    return rows


def run_text_only(args):
    translator = L.build_translator(args.translator, "ja", "ko", args.claude_model)
    print(f"[trans] 백엔드: {translator.name}\n")
    rows = []
    for japanese, _ in CASES:
        try:
            translated = translator.translate(japanese, src="ja")
        except Exception as exc:                             # noqa: BLE001
            translated = f"<번역 실패: {str(exc)[:60]}>"
        rows.append((japanese, translated))
    return rows


def report(rows, matched_by_order: bool) -> int:
    print("=" * 72)
    print("결과 — 번역이 '참고 번역' 과 뜻이 통하면 정상입니다 (표현은 달라도 됩니다)")
    print("=" * 72)

    problems = 0
    for i, (recognized, translated) in enumerate(rows):
        expected_ja, reference_ko = CASES[i] if matched_by_order and i < len(CASES) else ("", "")
        print(f"\n[{i + 1}]")
        if expected_ja:
            score = _similarity(expected_ja, recognized)
            mark = "OK" if score >= 0.8 else ("애매" if score >= 0.5 else "틀림")
            print(f"  들려준 일본어 : {expected_ja}")
            print(f"  인식한 일본어 : {recognized}      [{mark} {score:.0%}]")
            if score < 0.5:
                problems += 1
        else:
            print(f"  인식한 일본어 : {recognized}")
        print(f"  번역 결과     : {translated}")
        if reference_ko:
            print(f"  참고 번역     : {reference_ko}")

        if not recognized:
            print("  !! 아무것도 인식하지 못했습니다")
            problems += 1
        if not translated or translated.startswith("<"):
            print("  !! 번역이 비어 있거나 실패했습니다")
            problems += 1
        elif L._script_of(translated) == "ja":
            print("  !! 번역 결과에 일본어가 그대로 남아 있습니다")
            problems += 1

    print("\n" + "=" * 72)
    if problems:
        print(f"문제 {problems}건. 위에서 '!!' 와 '틀림' 표시를 보세요.")
    else:
        print("문제 없음. 인식과 번역 모두 정상 동작합니다.")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--text-only", action="store_true",
                        help="음성 없이 번역만 확인 (edge-tts 불필요)")
    parser.add_argument("--audio", help="직접 준비한 음성 파일로 테스트")
    parser.add_argument("--save-audio", action="store_true",
                        help="테스트용 음성 파일만 만들고 끝낸다 (스피커로 틀어볼 때)")
    parser.add_argument("--out", default="test_ja.wav", help="만들 음성 파일 이름")
    parser.add_argument("--model", default="small")
    parser.add_argument("--device-type", default="auto", choices=["auto", "cuda", "cpu"])
    parser.add_argument("--compute-type", default=None)
    parser.add_argument("--beam-size", type=int, default=1)
    parser.add_argument("--translator", default="local",
                        choices=["google", "local", "claude", "none"])
    parser.add_argument("--claude-model", default="claude-opus-5")
    parser.add_argument("--silence", type=int, default=450)
    parser.add_argument("--min-speech", type=int, default=350)
    parser.add_argument("--max-segment", type=float, default=6.0)
    parser.add_argument("--pad", type=int, default=300)
    args = parser.parse_args()

    print("=" * 72)
    print("live_sub 자가 테스트 — 정답을 아는 일본어로 인식/번역을 대조합니다")
    print("=" * 72 + "\n")

    if args.save_audio:
        synthesize(args.out)[1]
        print(f"\n이 파일을 스피커로 재생하면서 다른 창에서 live_sub 을 실행하면\n"
              f"소리 캡처까지 포함한 전체 경로를 확인할 수 있습니다:\n"
              f"    python live_sub.py --src ja --dst ko --console")
        return 0

    if args.text_only:
        return report(run_text_only(args), matched_by_order=True)

    if args.audio:
        samples = load_audio(args.audio)
        per_sentence, combined = _segment(samples, args), None
        print(f"[seg ] 파일에서 발화 구간 {len(per_sentence)}개를 찾았습니다\n")
        return report(run_pipeline(per_sentence, combined, args), matched_by_order=False)

    per_sentence, combined = synthesize(args.out)
    return report(run_pipeline(per_sentence, combined, args), matched_by_order=True)


if __name__ == "__main__":
    sys.exit(main())
