#!/usr/bin/env bash
# 慢数据情报 Agent —— 一键运行脚本
# 用法: ./run.sh run [--date 2026-09-25] [--max-queries 8]
#       ./run.sh stats
set -euo pipefail
cd "$(dirname "$0")"

# 指向工作区内的模型缓存（避免重新下载嵌入模型）
export HF_HOME="$PWD/.hf-cache"
export FASTEMBED_CACHE_PATH="$PWD/.fastembed-cache"

exec .venv/bin/python -m slowdata "$@"
