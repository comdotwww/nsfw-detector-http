中文 | [English](README_en.md)

# NSFW 图片检测服务

基于 ViT 架构的 NSFW（Not Safe For Work）图片检测 HTTP 服务。纯 CPU 推理，适合部署在普通 VPS 或云服务器上，为论坛、图床、IM、内容平台等提供图片鉴黄能力。

模型用的是 [Marqo/nsfw-image-detection-384](https://huggingface.co/Marqo/nsfw-image-detection-384)（ViT-tiny，5.6M 参数），int8 量化后只有 6.4MB，16 线程下单张推理约 18ms。

提供两种调用方式：传图片 URL 或直接上传文件。返回 `0~1` 的风险分值和布尔判定，调用方可自行决定后续处置。

---

## 目录

- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [配置项](#配置项)
- [Token 管理与鉴权](#token-管理与鉴权)
- [API 文档](#api-文档)
- [结果缓存](#结果缓存)
- [数据与备份](#数据与备份)
- [性能与调优](#性能与调优)
- [项目结构](#项目结构)
- [常见问题](#常见问题)
- [许可与致谢](#许可与致谢)

---

## 功能特性

**检测能力**

- 纯 CPU 推理，无需 GPU；单 worker 常驻内存约 135MB，模型仅 6.4MB
- 支持 jpg / png / bmp / webp / gif（动图取首帧）
- 可选检测图片中是否含二维码（`qr_detect=enable`）
- 返回原始概率值，判定阈值可配置

**工程能力**

- **URL 检测只接受 POST**，URL 放请求体，避免特殊字符转义问题，也避免 URL 被访问日志完整记录
- **多 Token 管理**：有效期、每日上限、总量上限，独立管理端口可视化增删改查
- **结果缓存**：SQLite 落地，多 worker 共享，重启不丢，命中延迟 <1ms
- **全本地构建**：镜像由本仓库 Dockerfile 构建，不依赖任何第三方预构建镜像
- 内置并发保护：推理信号量 + 连接数上限，过载时快速失败而不是拖垮整机
- 首页内置 Web 界面（`WEBUI=on` 开启），可手动传图测试

---

## 快速开始

### 环境要求

- Docker 20.10+（或 Docker Compose v2）
- 至少 1GB 内存、1 核 CPU；推荐 2 核以上
- 磁盘约 300MB（镜像 + 模型 + 运行时数据）

### 方式一：Docker Compose（推荐）

```bash
git clone <本仓库地址> nsfw
cd nsfw

# 1. 设置管理密钥（务必改掉默认值）
sed -i 's/your_admin_token_here/你的管理密钥/' docker-compose.yaml

# 2. 构建并启动（首次会自动构建镜像）
docker compose up -d --build
```

启动后：

- 业务接口：`http://服务器IP:6086`
- 管理页面：`http://服务器IP:6087`（输入 `ADMIN_TOKEN` 登录）

### 方式二：手动构建镜像

```bash
cd nsfw

# 构建本地镜像（默认标签 nsfw:local）
./build.sh

# 启动
docker run -d \
  --name nsfw \
  -p 6086:6086 \
  -p 6087:6087 \
  -e ADMIN_TOKEN=你的管理密钥 \
  -v "$(pwd)/data:/opt/nsfw/data" \
  --restart always \
  nsfw:local
```

`build.sh` 支持：

```bash
./build.sh --no-cache                    # 不用缓存，从零重建
IMAGE=myrepo/nsfw:1.0 ./build.sh         # 自定义镜像名
```

### 方式三：不使用 Docker

需要 Python 3.10+（推荐 3.13）。

```bash
cd nsfw
python3 -m venv myenv
. myenv/bin/activate
pip install -r app/requirements.txt

ADMIN_TOKEN=你的管理密钥 ./run.sh
```

`run.sh` 会自动探测 CPU 核数并计算 worker 数与推理线程数。开发时用 `./run.sh dev` 开启热重载。

### 第一次使用

1. 打开 `http://服务器IP:6087`，用 `ADMIN_TOKEN` 登录
2. 点「新建 Token」，填名称（如「生产环境」），有效期和限次按需填写
3. **复制弹出的 Token 明文**（只显示这一次）
4. 用它调用业务接口

```bash
curl -X POST 'http://服务器IP:6086/api/url_check' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer nsfw_xxxxxxxx' \
  -d '{"url":"https://example.com/test.jpg"}'
```

> ⚠️ **在创建第一个 Token 之前，业务接口是不鉴权的**（兼容旧版行为）。
> 如果要对公网开放，请先创建 Token。

---

## 配置项

全部通过环境变量配置。Docker 部署时写在 `docker-compose.yaml` 的 `environment` 里。

### 业务与推理

| 变量 | 默认 | 说明 |
|---|---|---|
| `NSFW_THRESHOLD` | `0.8` | 判定阈值。`nsfw` 分值**大于**该值时 `is_nsfw=true`。调高更宽松（漏检变多），调低更严格（误判变多）。只影响布尔值，`nsfw` 始终返回 0~1 原始概率。当前模型分值整体偏低，默认从 0.9 调到 0.8 |
| `WORKERS` | `1` | 进程数，自动收敛到 `min(4, CPU核数/2)` |
| `ORT_NUM_THREADS` | 自动 | 每进程推理线程数，默认 `CPU核数 / WORKERS`。**对延迟影响最大**，建议实测确认 |
| `INFER_CONCURRENCY` | `=ORT_NUM_THREADS` | 同时推理的请求上限，超出排队 |
| `LIMIT_CONCURRENCY` | `WORKERS×(N×2+4)` | 连接数上限，超出直接返回 503 |
| `ORT_ALLOW_SPINNING` | `0` | ORT 线程空闲时不自旋。**不要改成 1**，多线程下会明显变慢（见[性能](#性能与调优)） |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `6086` | 业务监听地址与端口 |
| `WEBUI` | `off` | `on` 开启首页 Web 界面 |
| `ENABLE_DOCS` | `off` | `on` 开放 `/docs` 与 `/openapi.json` |
| `CORS_ORIGINS` | `*` | 逗号分隔白名单；`off` 关闭 CORS |
| `LOG_LEVEL` | `warning` | `info` 会输出 access log |

### 图片获取

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_IMAGE_BYTES` | `10485760` | 单图大小上限（10MB） |
| `MAX_IMAGE_PIXELS` | `64000000` | 解码后像素上限，防解压炸弹 |
| `HTTP_TIMEOUT` | `30` | 拉图总超时（秒） |
| `HTTP_CONNECT_TIMEOUT` | `10` | 拉图建连超时（秒）。境外 CDN 建连可能很慢，不要设太小 |
| `HTTP_TRUST_ENV` | `1` | 是否继承 `HTTP_PROXY` 等系统代理。服务器有代理但拉图不该走代理时设 `0` |

### Token 管理

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_TOKEN` | 空 | **管理页面登录密钥**。不设置则无法登录管理页 |
| `ADMIN_ENABLED` | `1` | 是否启动管理端口，`0` 关闭 |
| `ADMIN_HOST` | `127.0.0.1` | 管理端口监听地址。默认只监听本机 |
| `ADMIN_PORT` | `6087` | 管理端口 |
| `TOKENS_DB_PATH` | `data/tokens.db` | Token 数据库路径 |
| `TOKEN` | 空 | **已废弃**，仅兼容保留：设置后仍接受这个单一密钥 |

### 缓存

| 变量 | 默认 | 说明 |
|---|---|---|
| `CACHE_ENABLED` | `1` | 缓存开关，`0` 关闭且不创建数据库文件 |
| `CACHE_DB_PATH` | `data/cache.db` | 缓存数据库路径 |
| `CACHE_MAX_SIZE` | `1000` | 条目上限，`0` 不限；超出按最久未命中淘汰 |
| `CACHE_TTL` | `604800` | 有效期（秒），默认 7 天 |

---

## Token 管理与鉴权

### 为什么用 Token 表而不是单一密钥

单个写在环境变量里的密钥有两个问题：改一次要重启容器；没法给不同调用方分配不同权限。
现在改成 SQLite 存储 + 独立管理页面：

| | 单一密钥 | 现在 |
|---|---|---|
| 数量 | 1 个 | 任意多个 |
| 修改方式 | 改环境变量 + 重启容器 | 页面点几下，**即时生效** |
| 有效期 | 无 | 可设到期时间 |
| 次数限制 | 无 | 每日上限 + 总量上限 |
| 用量统计 | 无 | 总量 / 今日 / 最后使用时间 |
| 泄露处置 | 全局换密钥 | 单独停用那一个 |

### 安全设计

- Token 形如 `nsfw_` + 64 位十六进制，数据库里**只存 sha256 哈希**，泄露也拿不到明文
- 明文只在创建成功时显示一次，请立即保存
- 管理端口默认只监听 `127.0.0.1`
- 鉴权失败区分状态码，便于排查

### 打开管理页面

```
http://服务器IP:6087
```

输入 `ADMIN_TOKEN` 登录。

**外网访问建议用 SSH 隧道**，而不是把 `ADMIN_HOST` 改成 `0.0.0.0`：

```bash
ssh -L 6087:127.0.0.1:6087 user@服务器IP
# 然后本地浏览器打开 http://127.0.0.1:6087
```

确实需要直接暴露时，请配合防火墙白名单或反向代理 + HTTPS。

### 页面功能

| 操作 | 说明 |
|---|---|
| 新建 | 填名称、有效期（留空 = 永久）、每日上限（0 = 不限）、总量上限（0 = 不限） |
| 列表 | 名称、Token 前缀、有效期、限次配置、总用量、今日用量、启用状态 |
| 停用 / 启用 | 停用后该 Token 立即被拒 |
| 重置用量 | 总量与今日计数清零（适合按月给客户计量） |
| 删除 | 彻底移除 |

### 公开模式

- 数据库里**一条 Token 都没有**时，业务接口**不鉴权**
- 只要存在**至少一条** Token（不论启用与否），业务接口立即强制鉴权

这样旧部署升级不会突然全部 401，而一旦开始用 Token 就自动收紧。

### 调用示例

```bash
curl -X POST 'http://localhost:6086/api/url_check' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer nsfw_xxxxxxxxxxxxxxxx' \
  -d '{"url":"https://example.com/a.jpg"}'
```

### 鉴权错误码

| HTTP 状态 | `msg` | 含义 |
|---|---|---|
| `401` | `Token invalid` | 未携带 Token、Token 错误、已停用、已过期 |
| `403` | `Token limit exceeded` | 超过每日上限或总量上限 |

### 管理 API

页面背后的 JSON API，可用脚本批量管理。鉴权方式二选一：登录后的 Cookie，或 `X-Admin-Token` 请求头。

```bash
ADMIN=http://127.0.0.1:6087
KEY='X-Admin-Token: 你的管理密钥'

# 列表
curl "$ADMIN/api/admin/tokens" -H "$KEY"

# 创建：30 天有效期、每日 1000 次、总量 100000 次
curl -X POST "$ADMIN/api/admin/tokens" -H "$KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"客户A","expires_at":'$(( $(date +%s) + 30*86400 ))',"daily_limit":1000,"total_limit":100000}'

# 停用
curl -X PUT "$ADMIN/api/admin/tokens/1" -H "$KEY" \
  -H 'Content-Type: application/json' -d '{"enabled":false}'

# 重置用量
curl -X POST "$ADMIN/api/admin/tokens/1/reset" -H "$KEY"

# 删除
curl -X DELETE "$ADMIN/api/admin/tokens/1" -H "$KEY"
```

---

## API 文档

### POST /api/url_check —— 通过 URL 检测

> **只接受 POST。** 用 GET 访问返回 `405`。

```bash
curl -X POST 'http://localhost:6086/api/url_check' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer 你的Token' \
  -d '{
    "url": "https://example.com/a.jpg",
    "qr_detect": "disable"
  }'
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | 图片地址，仅支持 http/https |
| `qr_detect` | 否 | `enable` / `disable`，默认 `disable`。开启后额外检测二维码 |

`qr_detect` 也可放在 query string（`POST /api/url_check?qr_detect=enable`），请求体优先。

### POST /api/upload_check —— 上传文件检测

```bash
curl -X POST 'http://localhost:6086/api/upload_check' \
  -H 'Authorization: Bearer 你的Token' \
  -F 'file=@/path/to/image.jpg'
```

### GET /api/cache_stats —— 缓存统计

```bash
curl 'http://localhost:6086/api/cache_stats'
```

返回命中率、条目数、数据库体积等。此接口**不需要鉴权**。

### 返回格式

```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "sfw": 0.0014,
    "nsfw": 0.9986,
    "is_nsfw": true,
    "qr_status": "unknown"
  }
}
```

| 字段 | 说明 |
|---|---|
| `code` | `200` 成功，其余为失败 |
| `msg` | 提示信息，失败时为具体原因 |
| `data.sfw` | 安全分值 `[0,1]`，越高越安全 |
| `data.nsfw` | 风险分值 `[0,1]`，越高风险越高 |
| `data.is_nsfw` | `nsfw > NSFW_THRESHOLD`（默认 0.8）时为 `true` |
| `data.qr_status` | `unknown` 未检测 / `qr` 含二维码 / `not_qr` 不含 |

> 如果业务对漏检敏感，建议忽略 `is_nsfw`，自己对 `nsfw` 分值做分级处理，
> 比如 `>0.8` 直接拦截、`0.4~0.8` 转人工复审。

### 业务错误

| `code` | `msg` | 说明 |
|---|---|---|
| `-1000` | `Invalid URL, only http/https are supported.` | URL 格式不合法 |
| `-1000` | `Unsupported file type: ..., only jpg/png/bmp/webp/gif allowed.` | 图片类型不支持 |
| `-1000` | `Download failed, status code: 404` | 图片拉取失败 |
| `-1000` | `Image parsing failed.` | 内容不是有效图片 |
| `-1000` | `File too large (>10MB), refusing to process.` | 超过大小上限 |
| `-1000` | `Image too large to decode.` | 超过像素上限 |
| `-1000` | `Invalid qr_detect value, ...` | `qr_detect` 只接受 enable / disable |

---

## 结果缓存

URL 检测结果会缓存在 SQLite（默认 `data/cache.db`）。同一个 URL 第二次请求直接命中，
端到端延迟从几十毫秒降到 1ms 以内（省掉下载、解码和推理）。

- **多 worker 共享**同一份缓存（不是每进程一份）
- 容器重启不丢
- 缓存 key 包含 `qr_detect` 开关，两种模式互不干扰
- **只缓存模型输出**（分值 + 二维码状态），不缓存判定结果 —— 改 `NSFW_THRESHOLD` 后历史缓存立刻按新阈值生效，不用清库
- 只缓存成功结果，失败不缓存

命中率取决于你的流量形态。如果 URL 基本不重复，这个优化收益接近零，
可以用 `CACHE_ENABLED=0` 关掉。

```bash
curl 'http://localhost:6086/api/cache_stats'
```

```json
{
  "code": 200,
  "data": {
    "backend": "sqlite",
    "hits": 120, "misses": 30, "hit_rate": 0.8,
    "rows": 30, "expired_rows": 0,
    "db_bytes": 40960, "wal_bytes": 45352, "total_bytes": 86312
  }
}
```

---

## 数据与备份

`docker-compose.yaml` 已把宿主机 `./data` 挂载到容器 `/opt/nsfw/data`：

```
data/
├── cache.db       # 结果缓存（可重建，丢了只是缓存失效）
└── tokens.db      # Token 与用量记录（重要，建议定期备份）
```

### 备份缓存（在线，不用停容器）

```bash
docker compose exec nsfw python -m app.cache /opt/nsfw/data/cache-backup.db
```

使用 SQLite 官方 backup API，运行中也能保证一致性。

### 备份 Token 库（需先停容器）

```bash
docker compose stop
cp data/tokens.db "data/tokens-$(date +%F).db"
docker compose start
```

> ⚠️ 两个库都用 WAL 模式，新写入先落在 `-wal` 文件里。
> **不要在运行中直接 `cp cache.db` 或 `cp tokens.db`**，会漏掉最近的数据。
> 实测跑完 10 条记录后主库只有 4KB，数据几乎全在 `-wal` 里。

### 恢复

把备份文件覆盖回 `data/cache.db` 或 `data/tokens.db`，
同时删掉同名的 `-wal`、`-shm` 文件，再启动容器。

---

## 性能与调优

### 基准数据

实测环境：16 逻辑核 x86_64，单张 384×384 输入，纯推理（不含下载和解码）。

| `ORT_NUM_THREADS` | 1 | 2 | 4 | 8 | 16 |
|---|---|---|---|---|---|
| 单次推理耗时 | 58ms | 36ms | 23ms | 19ms | 18ms |

4 线程之后收益递减，8 线程和 16 线程只差 1ms。端到端还要加上图片下载和解码，通常几十毫秒。

> ⚠️ **不要删掉 `ORT_ALLOW_SPINNING=0`。** 这是本服务的默认设置（`run.sh` 里 export，
> `engine.py` 里也设了 session option）。实测多线程下开自旋会让推理明显退化：
> 16 线程时本模型从 18ms 掉到 32ms，旧的大模型更夸张（178ms → 381ms）——
> 十几个线程空转互相抢 CPU，线程越多越糟。

### 线程数是最重要的参数

`WORKERS × ORT_NUM_THREADS` 超过物理核数就是纯损耗。两种取向：

- **低延迟** → `WORKERS=1`，`ORT_NUM_THREADS=核数`
- **高吞吐** → `WORKERS=核数/2`，`ORT_NUM_THREADS=2`

### 参考配置

下表延迟是**纯推理**耗时，端到端请再加上下载和解码：

| 机器 | `WORKERS` | `ORT_NUM_THREADS` | 内存 | 单次推理 |
|---|---|---|---|---|
| 1 核 1GB | 1 | 1 | ~140MB | ~58ms |
| 2 核 1GB | 1 | 2 | ~140MB | ~36ms |
| 2 核 2GB | 1 | 2 | ~140MB | ~36ms |
| 4 核 2GB | 2 | 2 | ~270MB | ~36ms，吞吐 2x |
| 4 核 4GB | 1 | 4 | ~140MB | ~23ms |

### 资源占用

- 单 worker 常驻约 **135MB**（含 6.4MB 模型）
- 启动约 **2.2 秒**（含模型预热，避免首个请求变慢）
- 镜像约 **160MB**

### 过载保护

推理是 CPU 密集型的，请求堆积只会让所有人一起变慢。所以设了两道闸：

- `INFER_CONCURRENCY`：同时推理的请求数，超出排队
- `LIMIT_CONCURRENCY`：连接数上限，超出直接返回 503

上游拿到 503 应该退避重试，而不是继续压。

---

## 项目结构

```
nsfw/
├── app/
│   ├── main.py               # FastAPI 应用、生命周期、CORS
│   ├── routers.py            # 路由注册
│   ├── engine.py             # 推理引擎：模型会话/预处理/HTTP 拉图/二维码
│   ├── cache.py              # 结果缓存（SQLite）
│   ├── tokens.py             # Token 存储与鉴权（SQLite）
│   ├── admin.py              # Token 管理端口（页面 + API）
│   ├── api/
│   │   ├── url_check.py      # URL 检测
│   │   ├── upload_check.py   # 上传检测
│   │   └── index.py          # 首页
│   ├── middleware/auth.py    # 鉴权中间件
│   ├── onnx/model_nsfw_int8.onnx  # 模型（int8 量化，6.4MB）
│   ├── static/html/          # 内置 Web 界面
│   └── requirements.txt
├── Dockerfile
├── docker-compose.yaml
├── build.sh                  # 本地构建镜像
├── install.sh                # 镜像内依赖安装
├── run.sh                    # 启动脚本（线程/worker 自适应）
└── data/                     # 运行时数据（缓存 + Token，不入库）
```

---

## 常见问题

**Q：第一次调用很慢？**

启动时会预热一次推理，但如果容器刚起来就发请求，仍可能有几百毫秒的额外开销（首次权重预打包）。生产环境建议用健康检查等待就绪。

**Q：改了 `NSFW_THRESHOLD` 需要清缓存吗？**

不需要。缓存只存模型输出的原始分值，判定是在返回时按当前阈值现算的。

**Q：改了 Token 需要重启容器吗？**

不需要。鉴权是每个请求查一次库，页面上改完立即生效。

**Q：多 worker 时 Token 用量统计会不准吗？**

不会。计数在 SQLite 里用 `UPDATE ... SET total_used = total_used + 1` 原子完成，多进程共享同一份数据。

**Q：为什么 URL 检测不支持 GET？**

图片 URL 常带 `?`、`&`、`#`，放 query string 要额外转义；而且 GET 的完整 URL 会被浏览器、CDN、访问日志原样记录，检测内容跟着泄漏。所以只保留 POST。

**Q：管理页面打不开？**

默认只监听 `127.0.0.1`（容器内）。用 SSH 隧道访问，或确认 `ADMIN_HOST`、端口映射是否正确。

**Q：日志里没有访问记录？**

默认 `LOG_LEVEL=warning` 关掉了 access log 以减少磁盘写入。需要时设 `LOG_LEVEL=info`。

**Q：能识别 AI 生成的图片吗？**

准确率明显低于真实照片。模型的训练数据以真实图片和传统绘画为主。

---

## 许可与致谢

本项目采用 AGPL-3.0 许可，详见 [LICENSE](LICENSE)。

模型来自 [Marqo/nsfw-image-detection-384](https://huggingface.co/Marqo/nsfw-image-detection-384)，
在此致谢。使用本服务时请遵守模型自身的许可条款。

> 检测结果不能保证 100% 准确。任何内容审核方案都不存在绝对精准的实现，
> 建议采用「机器初审 + 人工复审」的组合，不要把自动判定作为唯一依据。
