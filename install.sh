#!/bin/bash
set -e

WORKDIR="/opt/nsfw"
cd "${WORKDIR}"

# 安装 Python 依赖。
#
# 基础镜像 python:3.13-slim-trixie 已带解释器和 pip，所以这里：
#   - 不需要 apt 装 python3 / python3-pip / python3-venv
#   - 不需要 venv（单用途容器，直接装进系统 site-packages）
#   - 不需要 procps（run.sh 里已经没有 pgrep 了）
# 所有依赖都有 manylinux cp313 wheel，不会触发源码编译。
install_python_deps(){
    pip install --no-cache-dir --upgrade pip
    pip install --no-cache-dir -r app/requirements.txt
}

# 预编译字节码：多 worker 启动时每个进程都要 import 一遍 app，
# 有 .pyc 能少一次解析编译（配合 Dockerfile 里的 PYTHONDONTWRITEBYTECODE=1）
precompile(){
    python -m compileall -q app || true
}

# 清理缓存，缩小镜像体积
clean(){
    rm -rf /tmp/* /root/.cache
}

install_python_deps && precompile && clean
