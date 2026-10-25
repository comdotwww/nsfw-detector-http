#!/usr/bin/env bash
#
# NSFW 服务启动脚本
#
# 用法:
#   ./run.sh          # 生产模式
#   ./run.sh dev      # 开发模式（热重载，单进程）
#
# 可调环境变量:
#   WORKERS            进程数，默认 1；会被收敛到 min(4, 核数/2)
#   ORT_NUM_THREADS    每个进程的推理线程数，默认 核数/WORKERS
#   INFER_CONCURRENCY  同时推理的请求上限，默认 = ORT_NUM_THREADS
#   LIMIT_CONCURRENCY  uvicorn 连接数上限，默认 WORKERS*8，超出返回 503
#   APP_PORT           监听端口，默认 6086
#   LOG_LEVEL          日志级别，默认 warning（关掉 access log 省磁盘 IO）
#   TOKEN / WEBUI      见 README
#
# 注意：原来这里写的是 #!/bin/sh，但脚本里用了 source，在 Debian 的 dash 下
# 会报 "source: not found"。现在改成 bash 并使用 POSIX 的 "." 。
#
set -euo pipefail

# 保证从脚本所在目录启动，避免相对路径（app/onnx/...）失效
cd "$(dirname "$0")"

APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-6086}"
LOG_LEVEL="${LOG_LEVEL:-warning}"

# ---------------------------------------------------------------------------
# CPU 核数
# ---------------------------------------------------------------------------
detect_cores() {
    if command -v nproc >/dev/null 2>&1; then
        nproc
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import os; print(os.cpu_count() or 1)'
    else
        echo 1
    fi
}

CPU_CORES="$(detect_cores)"
case "$CPU_CORES" in
    ''|*[!0-9]*) CPU_CORES=1 ;;
esac
[ "$CPU_CORES" -lt 1 ] && CPU_CORES=1

# ---------------------------------------------------------------------------
# worker 数：多进程不是越多越好
#
# 每个 worker 独立加载一份 56MB 模型（实测常驻约 180MB）且各自开一套
# ORT 线程池。进程数超过物理核数的一半时，线程会互相抢核，
# 单请求延迟反而变差。所以这里做一次硬性收敛。
# ---------------------------------------------------------------------------
WORKERS="${WORKERS:-1}"
case "$WORKERS" in
    ''|*[!0-9]*)
        echo "[run.sh] WORKERS 必须是正整数，收到 '$WORKERS'" >&2
        exit 1
        ;;
esac
[ "$WORKERS" -lt 1 ] && WORKERS=1

MAX_WORKERS=$(( CPU_CORES / 2 ))
[ "$MAX_WORKERS" -lt 1 ] && MAX_WORKERS=1
[ "$MAX_WORKERS" -gt 4 ] && MAX_WORKERS=4
if [ "$WORKERS" -gt "$MAX_WORKERS" ]; then
    echo "[run.sh] WORKERS=$WORKERS 超出建议上限，已收敛为 $MAX_WORKERS（共 $CPU_CORES 核）" >&2
    WORKERS="$MAX_WORKERS"
fi

# ---------------------------------------------------------------------------
# 推理线程数：必须显式设置
#
# 实测（16 核，单张 384x384）：
#   intra_op=1 -> 880ms   intra_op=2 -> 505ms
#   intra_op=4 -> 287ms   intra_op=8 -> 199ms
# 不显式设置时 ORT 默认按全部逻辑核开线程，多 worker 场景下会严重超配。
# ---------------------------------------------------------------------------
THREADS="${ORT_NUM_THREADS:-}"
if [ -z "$THREADS" ]; then
    THREADS=$(( CPU_CORES / WORKERS ))
    [ "$THREADS" -lt 1 ] && THREADS=1
fi
export ORT_NUM_THREADS="$THREADS"
# 同步约束 OpenMP / MKL，避免第三方库再开一套线程
export OMP_NUM_THREADS="$THREADS"
export MKL_NUM_THREADS="$THREADS"
export INFER_CONCURRENCY="${INFER_CONCURRENCY:-$THREADS}"
export ORT_ALLOW_SPINNING="${ORT_ALLOW_SPINNING:-0}"

# 并发上限：CPU 密集型服务，超过这个数的请求排队也只会让所有人变慢，
# 不如快速返回 503 让上游重试/降级。
# 必须明显大于 INFER_CONCURRENCY，否则正常突发流量会被直接拒掉。
LIMIT_CONCURRENCY="${LIMIT_CONCURRENCY:-$(( WORKERS * (INFER_CONCURRENCY * 2 + 4) ))}"

# HTTP 解析器：auto 会在 httptools 已安装时自动选它，否则退回 h11。
# 实测解析层 httptools 比 h11 快 16.9 倍（1.21µs vs 20.41µs），
# 但单次检测 ~190ms，解析只占 0.01%，端到端无差异。
# 想强制回退：HTTP_IMPL=h11
HTTP_IMPL="${HTTP_IMPL:-auto}"

# ---------------------------------------------------------------------------
# 启动前准备
# ---------------------------------------------------------------------------
activate_env() {
    # shellcheck disable=SC1091
    . myenv/bin/activate
}

# ---------------------------------------------------------------------------
# 生产模式
# ---------------------------------------------------------------------------
runMain() {
    echo "[run.sh] cores=$CPU_CORES workers=$WORKERS ort_threads=$THREADS" \
         "infer_concurrency=$INFER_CONCURRENCY limit_concurrency=$LIMIT_CONCURRENCY" \
         "log_level=$LOG_LEVEL"

    activate_env

    # exec 让 uvicorn 接管 PID 1，docker stop 的 SIGTERM 才能正确送达，
    # 否则要等 10s 超时才被强杀，重启期间请求直接断掉。
    exec uvicorn app.main:app \
        --host "$APP_HOST" \
        --port "$APP_PORT" \
        --workers "$WORKERS" \
        --http "$HTTP_IMPL" \
        --proxy-headers \
        --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
        --limit-concurrency "$LIMIT_CONCURRENCY" \
        --timeout-keep-alive 65 \
        --timeout-graceful-shutdown 10 \
        --no-server-header \
        --log-level "$LOG_LEVEL"
}

# ---------------------------------------------------------------------------
# 开发模式
# ---------------------------------------------------------------------------
runDev() {
    echo "[run.sh] development mode"
    activate_env
    exec uvicorn app.main:app \
        --reload \
        --host "$APP_HOST" \
        --port "$APP_PORT" \
        --log-level info
}

ARG1="${1:-}"

case "$ARG1" in
    "")      runMain ;;
    dev)     runDev ;;
    *)
        echo "Unknown argument: $ARG1" >&2
        echo "Usage: $0 [dev]" >&2
        exit 1
        ;;
esac
