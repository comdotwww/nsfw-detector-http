# Debian 13 (trixie) + Python 3.13
#
# 为什么用 python:3.13-slim-trixie，而不是 debian:13-slim 再 apt 装 Python：
#   debian:13-slim           28.5 MB（amd64 压缩体积）
#   python:3.13-slim-trixie  41.1 MB
# 但 debian 那条路还要 apt 装 python3.13 + pip + venv，安装后约 30MB，
# 再加 venv 里那份 pip 副本约 15MB，合计反而超过 41.1MB。
# 官方 python 镜像已把解释器和 pip 预编译好，还省掉整条 apt 依赖链，
# 构建更快、层更少。
#
# 所有依赖都有 manylinux cp313 wheel；onnxruntime 也不依赖 libgomp
# （已核实 libonnxruntime.so 的 NEEDED 列表，只有 libc/libstdc++/libgcc_s，
# 这三个 Debian 基础镜像里都有），所以构建过程不需要 apt，也不需要 C++ 编译器。
FROM python:3.13-slim-trixie

# PYTHONUNBUFFERED：日志实时输出，docker logs 不会攒着
# PYTHONDONTWRITEBYTECODE：镜像里已用 compileall 预编译，运行时不必再写 .pyc
# PIP_NO_CACHE_DIR：任何 pip 调用都不落缓存
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_PORT=6086 \
    LANG=C.UTF-8

WORKDIR /opt/nsfw

COPY install.sh run.sh ./
COPY app ./app

RUN bash install.sh

EXPOSE 6086

# 用 python 做健康检查，避免额外装 curl / wget
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/' % os.getenv('APP_PORT','6086'),timeout=4).status==200 else 1)"

# run.sh 内部用 exec 启动 uvicorn，SIGTERM 能直达进程
CMD ["bash", "run.sh"]
