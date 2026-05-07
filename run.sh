#!/bin/bash
# Use conda's Python — has the fast onnxruntime-gpu 1.25.1.
# (The previous `source ../.venv/bin/activate` activated a Python 3.10
# venv pinned to onnxruntime-gpu 1.23.2, which silently fell back to CPU
# at runtime despite reporting CUDAExecutionProvider — see commit log.)
cd "$(dirname "$0")"
exec /home/islabac/miniconda3/bin/python main.py "$@"
