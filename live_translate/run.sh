#!/usr/bin/env bash
# macOS / Linux 실행 스크립트
cd "$(dirname "$0")"
exec python3 live_sub.py --src ja --dst ko "$@"
