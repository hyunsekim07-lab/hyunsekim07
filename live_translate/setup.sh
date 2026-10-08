#!/usr/bin/env bash
# live_sub 설치 (macOS / Linux) — 한 번만 하면 됩니다.
set -u
cd "$(dirname "$0")"

PY=python3
command -v $PY >/dev/null || { echo "파이썬 3.9 이상이 필요합니다."; exit 1; }

echo "[1/4] 소리 캡처와 음성 인식 ..."
$PY -m pip install --quiet --upgrade pip
$PY -m pip install --quiet numpy sounddevice scipy faster-whisper || exit 1

echo "[2/4] 번역기 (로컬 - 제한 없음) ..."
$PY -m pip install --quiet "deep-translator>=1.11.4" transformers sentencepiece torch || exit 1

echo "[3/4] GPU 가속 ..."
if $PY -c "import ctranslate2,sys; sys.exit(0 if ctranslate2.get_cuda_device_count()>0 else 1)" 2>/dev/null; then
  $PY -m pip install --quiet nvidia-cublas-cu12 nvidia-cudnn-cu12
else
  echo "      CUDA GPU 가 없어 건너뜁니다."
fi

echo
echo "[4/4] 점검"
echo
$PY check.py

echo
echo "설치 끝. ./run.sh 로 실행하세요."
