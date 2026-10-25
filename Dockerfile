FROM debian:12-slim

# PYTHONUNBUFFERED：日志实时输出，docker logs 不会攒着
# PYTHONDONTWRITEBYTECODE：镜像里已用 compileall 预编译，运行时不必再写 .pyc
# PIP_NO_CACHE_DIR：任何 pip 调用都不落缓存
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_PORT=6086 \
    LANG=C.UTF-8

WORKDIR /opt/nsfw

COPY install.sh run.sh ./
COPY app ./app

RUN bash install.sh

EXPOSE 6086

# 用 python 做健康检查，避免额外装 curl / wget
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python3 -c "import os,sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/' % os.getenv('APP_PORT','6086'),timeout=4).status==200 else 1)"

# run.sh 内部用 exec 启动 uvicorn，SIGTERM 能直达进程
CMD ["bash", "run.sh"]
