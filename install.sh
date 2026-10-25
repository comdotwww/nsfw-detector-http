#!/bin/bash
set -e

# 工作目录
WORKDIR="/opt/nsfw"

# 安装系统依赖
install_deps(){
    apt-get update
    apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv procps
    mkdir -p "${WORKDIR}" && cd "${WORKDIR}"
}

# 安装 Python 依赖
install_python_deps(){
    cd "${WORKDIR}"
    python3 -m venv myenv
    # shellcheck disable=SC1091
    . myenv/bin/activate
    # --no-cache-dir：不落 pip 缓存，直接省掉一层镜像体积
    pip3 install --no-cache-dir --upgrade pip
    pip3 install --no-cache-dir -r app/requirements.txt
}

# 预编译字节码：多 worker 启动时每个进程都要 import 一遍 app，
# 有 .pyc 能少一次解析编译（配合 Dockerfile 里的 PYTHONDONTWRITEBYTECODE=1）
precompile(){
    cd "${WORKDIR}"
    # shellcheck disable=SC1091
    . myenv/bin/activate
    python3 -m compileall -q app || true
}

# 清理缓存，缩小镜像体积
clean(){
    apt-get clean
    rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache
}

install_deps && install_python_deps && precompile && clean
