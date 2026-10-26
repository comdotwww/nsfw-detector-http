#!/usr/bin/env bash
#
# 本地构建镜像。
#
# 用法:
#   ./build.sh              构建 nsfw:local
#   ./build.sh --no-cache   不用构建缓存，从零重建
#   IMAGE=myrepo/nsfw:1.0 ./build.sh   自定义镜像名
#
# 构建完成后可直接 docker compose up -d（compose 里配的是 build: .）。
set -euo pipefail

cd "$(dirname "$0")"

IMAGE="${IMAGE:-nsfw:local}"
EXTRA_ARGS=()

for arg in "$@"; do
    case "$arg" in
        --no-cache) EXTRA_ARGS+=(--no-cache) ;;
        -h|--help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "未知参数: $arg（可用: --no-cache）" >&2
            exit 1
            ;;
    esac
done

echo "==> 构建镜像 ${IMAGE}"
docker build ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} -t "$IMAGE" .

echo
echo "==> 完成：${IMAGE}"
docker images "$IMAGE" --format '    {{.Repository}}:{{.Tag}}  {{.Size}}'
echo
echo "启动方式一：docker compose up -d        （compose 已配置 build: .）"
echo "启动方式二：docker run -d --name nsfw \\"
echo "              -p 6086:6086 -p 6087:6087 \\"
echo "              -e ADMIN_TOKEN=your_admin_token \\"
echo "              -v \"\$(pwd)/data:/opt/nsfw/data\" \\"
echo "              $IMAGE"
