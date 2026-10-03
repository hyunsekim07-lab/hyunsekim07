#!/usr/bin/env bash
# 일본어 영상 -> 한국어 자막 (macOS / Linux)
cd "$(dirname "$0")"
exec python3 live_sub.py --src ja --dst ko --translator local --model medium "$@"
