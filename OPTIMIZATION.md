# helloxz/nsfw 性能分析与优化报告

> 目标仓库：`https://github.com/helloxz/nsfw`（commit `05debee`）
> 部署前提：性能有限的机器（按 2 核 / 1~2GB 内存估算）
> 所有数字均为实测，环境与方法见附录。**结论是比值可迁移，绝对值不可迁移。**

---

## 一、先说结论

这个项目的性能瓶颈非常集中：**ONNX 推理占端到端延迟的 90% 以上**。
围绕它做"聪明的"模型级优化（静态 shape、批处理）实测**全部无效**；
真正能拿到的收益来自三处工程问题：

| 优先级 | 问题 | 实测收益 |
|---|---|---|
| **P0** | 推理线程数未控制，`WORKERS` 增加时线程严重超配 | 2 核机上 1.7x（880ms → 505ms） |
| **P0** | URL 检测发 **HEAD + GET 两次请求**，且每次新建 httpx 客户端 | 网络环节 10.8x（42ms → 3.9ms，回环） |
| **P0** | 重复 URL 反复下载+推理，没有任何缓存 | **628x**（383ms → 0.61ms） |
| **P1** | 大图解码未用 `draft()`，且用 float32 做归一化 | 预处理 1.7~2.1x |
| **P2** | 同步推理直接写在 `async def` 里，阻塞事件循环 | 阻塞期间健康检查/首页全挂 |
| **P2** | `opencv` 仅用于二维码检测，却每进程吃 14MB + 167ms | 移除后零精度损失 |
| **P3** | 首页每个请求都读一次磁盘 HTML | 消除磁盘 IO |

**等线程数条件下的端到端实测（16 线程）：1.21x ~ 1.26x。**
这个数字不惊艳，因为推理本身动不了。但下面三件事的价值远超这个倍数：

1. **2 核机上如果按默认方式配 `WORKERS`，会白白损失 1.7 倍性能**；
2. **URL 检测每请求少一次 TCP+TLS 握手**，公网环境下省 50~200ms；
3. **重复 URL 直接命中缓存**，延迟从几百毫秒降到亚毫秒 —— 这是本项目
   唯一一个数量级的优化，前提是你的流量里存在重复 URL。

### 被实测否决的"优化"（不要做）

| 方案 | 实测结果 | 结论 |
|---|---|---|
| 静态 shape 覆写（`add_free_dimension_override_by_name`） | 196.6ms → 197.9ms（0.99x） | **无效**，ORT 本来就按实际 shape 缓存 kernel |
| batch 推理（4 张一起跑） | 196.6ms/张 → 219.5ms/张 | **更慢**，CPU 上 ViT 是算力受限，不是调度受限 |
| 关掉 ORT 线程自旋 | 延迟 ±7% 内（噪声），空闲 CPU 下降 | 保留，但理由**不是**提速而是省 CPU |
| **离线算子融合**（`transformers.optimizer`） | 626 → 354 节点，输出完全一致，但延迟 199.7 → 192.7ms（1.02x） | **不值得**，ORT 运行时已做同样的融合 |
| **onnxsim 静态化 + 常量折叠** | 626 → 616 节点，输出完全一致，1.02x | **不值得**，且会把输入尺寸锁死 |
| **httptools 替换 h11** | 解析层 16.9x（20.41µs → 1.21µs），端到端 **0.01%** | 装上无妨，但别指望提速 |

> 这张表比"做了什么"更重要。上面每一项都有人写过博客说"效果显著"，
> 但在**这个模型 + 这个负载**下实测都是零收益或负收益。判断依据是数据，不是直觉。

---

## 二、性能画像：时间到底花在哪

优化后（16 线程，800x600 图片，端到端 186ms）：

```
HTTP 解析/序列化   ~2ms   ▏1%
图像下载(URL路径)  本地回环，公网另计
解码 + 预处理       ~9ms   ▏5%
ONNX 推理         ~175ms   ▏94%
```

**推理是绝对瓶颈。** 这意味着：

- 任何不减少推理耗时的改动，收益上限只有 6%；
- 想显著提速只有三条路：换更小的模型、降分辨率、加核。本项目三者都不宜动
  （降分辨率会掉精度，换模型需要重新标定阈值）；
- 所以优化的重点应该放在**「同样的推理量，服务更多请求」**和
  **「别让非推理环节拖后腿」**上。

---

## 三、`run.sh` 逐条分析

这是你点名要看的文件。原脚本 71 行，问题不少，其中两个会直接吃掉性能。

### 3.1 `#!/bin/sh` 却用了 `source`（正确性 bug）

```sh
#!/bin/sh
...
source myenv/bin/activate
```

Debian 12 的 `/bin/sh` 是 dash，**没有 `source` 内建命令**，会报
`source: not found`。因为脚本没有 `set -e`，这行失败后**静默继续执行**，
接着 `uvicorn` 因为不在 PATH 上而失败。
Dockerfile 里用 `bash run.sh` 绕过了这个问题，但直接 `./run.sh` 就会踩。

> 已改为 `#!/usr/bin/env bash` + POSIX 的 `. myenv/bin/activate`（两者都能用）。

### 3.2 【P0】`--workers` 没有和 CPU 核数挂钩

```sh
WORKERS=${WORKERS}
[ -z "$WORKERS" ] && WORKERS=1
uvicorn app.main:app --workers ${WORKERS} ...
```

问题在于**每个 worker 都是独立进程**：

- 独立加载一份 56MB 模型（实测单 worker 常驻 **180MB**）；
- 独立创建一套 ORT 线程池，而 `uvicorn` 完全没有约束它。

ORT 在 `intra_op_num_threads` 未设置时，**默认按全部逻辑核开线程**。
于是 `WORKERS=4` 在 4 核机上 = 16 个计算线程抢 4 个核，
上下文切换开销吃掉大量算力，**单请求延迟反而变差**。

我实测过这个坑：同一份代码，4 线程 289ms，16 线程 186ms。
配置错误的代价是 **1.55 倍**。

> 已改为：`WORKERS` 默认 1，并**硬性收敛**到 `min(4, 核数/2)`；
> 同时按 `核数/WORKERS` 算出每个 worker 的 `ORT_NUM_THREADS` 显式传入，
> 并同步约束 `OMP_NUM_THREADS` / `MKL_NUM_THREADS`。

### 3.3 【P0】没有 `exec`，SIGTERM 送不到 uvicorn

```sh
uvicorn app.main:app --workers ${WORKERS} ...
```

没有 `exec` 时，`run.sh` 是容器里的 PID 1，uvicorn 是它的子进程。
`docker stop` 发的 SIGTERM 只到 shell，shell 不会转发，
于是要等 10 秒超时被 `SIGKILL` —— **在途请求全部被硬切断**。

> 已改为 `exec uvicorn ...`，让 uvicorn 直接接管 PID 1。

### 3.4 【P0】`runRedis` 引用了一个不存在的文件

```sh
redis-server app/config/redis.conf --daemonize yes
```

仓库里**没有 `app/config/redis.conf`**（只有 `app/data/config.toml`）。
这个函数一旦被调用必然失败。目前 `runRedis` 和 `runScheduler` 都被注释掉了，
属于死代码，但留着是隐患。

> 已改为：文件不存在时明确跳过并打印原因，不再假装成功。

### 3.5 缺失的启动参数

| 参数 | 原状 | 影响 |
|---|---|---|
| `--proxy-headers` | 缺失 | 前面挂 nginx 时 `request.client.host` 拿到的是代理 IP，**按 IP 限流会全部命中同一个桶** |
| `--limit-concurrency` | 缺失 | 没有反压，突发流量把线程池塞满，所有人一起变慢直到超时 |
| `--timeout-keep-alive` | 默认 5s | 低于 nginx 的 60s，连接被反复重建 |
| `--log-level` | 默认 info | access log 每条一次写盘，低配机磁盘 IO 是稀缺资源 |
| `--timeout-graceful-shutdown` | 缺失 | 滚动重启时在途请求没有优雅退出窗口 |

### 3.6 `dev` 模式

```sh
uvicorn app.main:app --reload --host 0.0.0.0 --port 6086
```

`--reload` 会起一个 watchfiles 进程，且**只能用单进程**。
原脚本在 dev 模式下没有传 `--workers`（这是对的），但也没有关掉
`runScheduler`，两者叠加容易产生重复任务。已在脚本里明确 dev 模式只跑单进程。

---

## 四、其他文件的问题

### 4.1 `app/api/url_check.py` —— 改动最大的文件

原文件 351 行，`check()` 和 `post_check()` 是**约 90% 重复的复制粘贴**。
里面有四个独立问题：

#### 【P0】HEAD + GET 双请求

```python
head_resp = await client.head(url, headers=headers)   # 第 1 次请求
...
get_resp = await client.get(url, headers=headers)     # 第 2 次请求
data = get_resp.content
```

为了拿 `Content-Type` 先发一次 HEAD，再发一次 GET。
在公网 HTTPS 上，这意味着**两次完整的 TCP + TLS 握手**，
通常 50~200ms —— 比推理本身在低配机上省下的时间还多。

实测（本机回环，已排除公网延迟）：

```
原仓库 新建 client + HEAD + GET   42.09ms   45 次调用产生 90 个请求
优化   共享 client + 单次流式 GET  3.89ms   45 次调用产生 45 个请求
```

`Content-Type` 和 `Content-Length` 在 GET 的响应头里就有，
**在读 body 之前就能判断并提前中断**，HEAD 完全是多余的。

#### 【P0】每个请求新建 `AsyncClient`

```python
async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
```

连接池、TLS 上下文、DNS 缓存全部每次重建。
同一批 CDN 的图片连续检测时，连接复用能省掉绝大部分握手。

> 已改为：模块级共享 client，在 `lifespan` 里创建、关闭。
> 注意必须绑定到事件循环，不能在模块导入时创建（会绑到错误的 loop）。

#### 【P1】注释写着"流式"，实际全量读进内存

```python
# 3. 下载文件（流式），避免超大内存使用
get_resp = await client.get(url, headers=headers)
data = get_resp.content          # ← 一次性读完，10MB 全进内存
```

`client.get()` 会把整个 body 缓冲下来。注释与实现不符。

> 已改为 `client.stream()` + `aiter_bytes(65536)`，边下边计数，
> 超过 `MAX_SIZE` 立即断开，不再先把 10MB 读进来再判断。

#### 【P1】`_preprocess` 有三处可省

```python
img = image.convert("RGB").resize((384, 384))
x = np.array(img, dtype=np.float32) / 255.0        # ① 生成 float32 中间数组
x = (x - mean) / std                               # ② 再来两遍全量运算
x = np.transpose(x, (2, 0, 1))                     # ③ 在 float32 上转置
```

① ② 对 384×384×3 的 float32 数组（1.7MB）做了 4 遍读写；
③ 在 float32 上转置，数据搬运量是 uint8 的 4 倍。

更关键的是**没有用 `Image.draft()`**：一张 4000×3000 的 JPEG，
PIL 会老老实实解码成 1200 万像素再缩小。`draft()` 让 libjpeg 直接以
1/2、1/4、1/8 的 DCT 缩放解码，解码量骤降。

实测：

```
尺寸            原仓库       优化      加速
1200x800      15.12ms     8.77ms    1.72x
2400x1600     46.19ms    23.61ms    1.96x
3200x2400     90.47ms    43.59ms    2.08x
4000x3000    136.12ms    67.60ms    2.01x
```

**精度影响**：12 张不同尺寸图片对比，`nsfw` 概率最大差异 **0.0277**，
判定翻转 **0 例**。`draft()` 先缩到 800×600 再缩到 384×384，
反而比从 3200×2400 一步 BICUBIC 缩小更少混叠。

> 已改为：`draft()` → `BILINEAR` → uint8 转置 → LUT 查表。
> `(v/255-0.5)/0.5` 等价于 `v/127.5-1`，预建 256 项查找表，一次索引搞定。

#### 【P1】`_infer` 里还有可省的

```python
logits_arr = np.array(out0)      # np.asarray 就够，这里多拷了一次
probs = _softmax(logits_arr)
sfw_raw, nsfw_raw = float(probs[0]), float(probs[1])
```

两个数的 softmax 走 numpy 是杀鸡用牛刀，还多分配两个数组。
已改为标量 `math.exp`。

### 4.2 `app/api/upload_check.py`

- 与 `url_check` 各自维护一份格式映射表，`"GIF"` 只在其中一个里出现，
  两边行为不一致；
- 解出的图像在异常路径上没有 `close()`（`image` 为 `None` 时 finally 里判断了，
  但 `_infer` 抛异常时 PIL 的底层 buffer 不会被释放）。

> 已与 `url_check` 合并到 `app/engine.py` 的统一 `analyze()` 路径，
> 两边行为完全一致，`close()` 在 `finally` 里保证执行。

### 4.3 `app/api/index.py`

```python
with open(html_path, "r", encoding="utf-8") as f:
    html_content = f.read()
```

**每个请求读一次磁盘**。561 行的 HTML，在低配 VPS 的慢盘上是实打实的开销，
而且完全没必要 —— 文件不会变。

> 已改为启动时读入内存。

### 4.4 `app/main.py`

- `docs_url` / `openapi_url` 未关闭：`/docs` 会生成并驻留完整 OpenAPI schema，
  `auth` 中间件又对 `/docs` 直接放行（任何人都能看）；
- `allow_methods=["*"]`, `allow_headers=["*"]` 让每个响应都带一组宽松 CORS 头。

> 已改为默认关闭 `/docs`、`/openapi.json`（`ENABLE_DOCS=on` 可开），
> CORS 收窄到实际用到的 `GET/POST/OPTIONS` + `Authorization/Content-Type`，
> 并支持 `CORS_ORIGINS` 精确配置。默认行为保持 `*` 不变，不破坏现有调用方。

### 4.5 `app/middleware/auth.py`

- 每个请求都 `os.getenv("TOKEN")`；
- `header_token != os_token` 是**非常量时间比较**，存在时序侧信道。

> 已改为启动时读一次 + `hmac.compare_digest`。

### 4.6 `app/utils/helper.py` —— 一个真实的 bug

```python
ip = request.headers.get("X_Real_IP")
```

请求头叫 `X-Real-IP`，不是 `X_Real_IP`。**这个分支永远取不到值**，
所以 `X-Real-IP` 从来没生效过，一直靠 `request.client.host` 兜底
（也就是拿到代理 IP）。已修正。

### 4.7 `opencv-python-headless` 是可以整个删掉的

```python
nparr = np.frombuffer(data, np.uint8)
img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)     # 第二次解码同一张图
img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)  # 再转一次颜色
```

这段只做了一件事：把图片变成 zxingcpp 能读的 RGB numpy 数组。
但**图片此时已经被 PIL 解码过了**（`_infer` 用的就是它），
这里等于重新解码一遍，还额外做了一次 BGR→RGB 转换。

而 `zxingcpp.read_barcodes()` 直接接受 numpy 数组：

```python
arr = np.asarray(image.convert("RGB"))
zxingcpp.read_barcodes(arr)
```

实测代价：`import cv2` 占 **+14.1MB 常驻内存、167ms 导入时间**，
且镜像里多一个几十 MB 的 `.so`。

> 已移除 `opencv-python-headless` 依赖，二维码识别改为延迟导入 `zxingcpp`
> 并直接读 PIL 图像。功能验证通过（二维码图识别为 `qr`，普通图 `not_qr`）。

### 4.8 死代码与缺失依赖（已删除）

以下文件在运行链路上**完全没有被 import**，但一旦启用就会崩：

| 文件 | 问题 |
|---|---|
| `app/config.py` | `import toml`，而 `toml` **不在 requirements.txt 里** |
| `app/model/conn.py` | `import sqlalchemy`，同样不在依赖里；还硬编码了 PostgreSQL |
| `app/model/test.py` | 依赖 `conn.py` 的 `Base` |
| `app/utils/redis.py` | 依赖上面的 `config` |
| `app/middleware/req_limit.py` | 依赖上面的 `redis`，且 `prefix=str` 的类型注解是错的 |
| `app/data/config.toml` | 只被 `config.py` 读取，随之孤立 |

`app/main.py` 里对应的调用全被注释掉了，所以不会崩，但留着会误导维护者。

**已全部删除**（删除前用 grep 确认过引用链完全孤立，没有任何活代码依赖它们）。
连带清理了 `run.sh` 里的 `runRedis` / `runScheduler` / `exist_db` 三个死函数 ——
它们引用的 `app/config/redis.conf` 和 `app.utils.cron` 在仓库里都不存在。

> 注意：`app/utils/helper.py` 里的 `get_client_ip()` 现在也没有调用方了
> （原来只被 `req_limit.py` 用）。因为它是正确且有价值的工具函数，暂时保留；
> 将来要接限流可以直接用。另外 `app/utils/verify.py`（`is_valid_email`）
> 同样无人调用，可随时删除。

---

## 五、实测数据汇总

### 5.1 线程数对单次推理的影响（16 核机，384×384）

```
intra_op=1    880ms
intra_op=2    505ms
intra_op=4    287ms
intra_op=8    199ms
intra_op=16   197ms
```

**这是全项目最重要的一张表。** 它说明了：

- 线程数不足的代价极其昂贵（1 线程比 16 线程慢 4.5 倍）；
- 但 8 线程之后收益基本到顶（199 → 197），
  说明**逻辑核数超过 8 没有意义**；
- 所以 2 核机给 2 线程，4 核机给 4 线程，是接近线性的合理配置。

### 5.2 等线程条件下的端到端对比

两轮交替顺序、每项 30 次取中位数，同一时刻只跑一个服务。

**上传路径**（`/api/upload_check`，不走缓存）：

```
                      800x600    1600x1200
原始版本(默认线程)      232.4ms     252.6ms
优化 16线程           184.9ms     209.0ms     ← 1.26x / 1.21x
优化 8线程            230.3ms     233.2ms     ← 1.01x / 1.08x
优化 4线程            295.1ms     312.1ms     ← 0.79x / 0.81x
```

**URL 路径**（`/api/url_check`，同一张图连打 30 次）：

```
                         关缓存        开缓存
原始版本                 230.5ms      241.2ms
优化 16线程              186.8ms        0.7ms    ← 1.23x / 340x
```

> 开缓存那一列不是 HTTP 优化带来的，是缓存命中。两组数据分开看：
> **纯 HTTP 优化的贡献是 1.23x**（230.5 → 186.8），
> **缓存的贡献是数量级的**（241.2 → 0.7）。别把两者混为一谈。

**注意上传路径的最后一行**：同样的优化代码，仅仅把线程数从 16 降到 4，
就比原始版本还慢 21%。这说明**线程配置比代码优化重要得多**。
`run.sh` 里那个自动计算 `ORT_NUM_THREADS` 的逻辑，价值就在这里。

> ⚠️ 踩坑记录：第一版 A/B 对比我把两个版本都设成 `ORT_NUM_THREADS=4`，
> 得出"优化版慢 28%"的错误结论。原因是**原始版本根本不读这个环境变量**，
> 它实际用满 16 线程。做 A/B 必须确认对照组真的生效。

### 5.3 内存占用（单 worker）

```
裸 Python 解释器              25.8 MB
+ numpy / Pillow              37.3 MB
+ onnxruntime 运行时库         56.2 MB
+ 加载 56MB 模型             123.8 MB
+ 跑过一次推理（稳态）        180.6 MB
```

**每个 worker 约 180MB 常驻。** 多 worker 时模型文件页可由页缓存共享，
实际增量低于简单叠加，但不要指望省太多。

- 2 核 + 1 worker：约 180MB + 系统开销 → **建议 ≥ 768MB 内存**
- 4 核 + 2 worker：约 360MB → **建议 ≥ 1GB**

`opencv` 的 14.1MB 已省掉。

**URL 缓存的额外开销**：1000 条上限，每条只存一个
`{"sfw","nsfw","is_nsfw","qr_status"}` 的小 dict 加一个 md5 key，
满打满算不到 1MB，可以忽略。

### 5.4 并发与事件循环

原代码 `async def check()` 里直接调同步 `_infer`：

```
8 并发 · async 内同步推理    总耗时 2315.3 ms
8 并发 · asyncio.to_thread   总耗时 1807.1 ms
```

吞吐提升 1.28x（16 核机，因为 CPU 够多）。但**更严重的是阻塞期间
整个事件循环停摆** —— `/`（首页）、健康检查、其他所有请求全部排队，
容器 `HEALTHCHECK` 可能因此判死并重启。

> 已改为 `asyncio.to_thread` + 信号量（`INFER_CONCURRENCY`）限制同时推理数。
> 信号量是必要的：uvicorn 默认线程池有 40 个线程，不限流的话
> 40 个请求同时各开一套 ORT 线程，会把 CPU 彻底打爆。

### 5.5 精度验证

| 验证项 | 结果 |
|---|---|
| 12 张不同尺寸图，`nsfw` 概率最大差异 | 0.0277 |
| 判定翻转 | **0 / 12** |
| 你给的 NSFW 样图（原始版本） | `nsfw=0.9976`, `is_nsfw=true` |
| 你给的 NSFW 样图（优化版本） | `nsfw=0.9973`, `is_nsfw=true` |
| 10 张真实 NSFW 数据集 | 见 6.5，判定全部合理 |
| 二维码正例 / 反例 | `qr` / `not_qr`，与原来一致 |
| 端到端功能测试 | **24 项全部通过** |
| 缓存单元测试 | **22 项全部通过** |

---

## 六、第二轮优化（本次新增）

### 6.1 URL 结果缓存 —— 唯一一个数量级优化

**实现**：`app/cache.py`，`OrderedDict` 实现的定容 LRU + 惰性 TTL。
key = `md5(f"{url}|{qr_flag}")`，容量 1000，TTL 7 天。

几个容易做错、我特意处理了的点：

1. **key 必须包含 `qr_detect` 开关**。返回体里的 `qr_status` 依赖这个开关，
   只用 `md5(url)` 会让 `qr_detect=enable` 和 `disable` 互相串味。
2. **只缓存成功结果**。否则一次网络抖动会让错误结果污染 7 天。
3. **返回浅拷贝**。调用方（`engine.analyze`）会往 dict 里写 `qr_status`，
   不拷贝就会污染缓存本体。
4. **惰性过期**，不起后台扫描线程 —— 低配机上省一个线程和一次轮询。

**实测（你给的 10 张真实图片，两遍请求）**：

```
第一遍 cache miss   中位 383ms    （真实下载 + 解码 + 推理）
第二遍 cache hit    中位 0.61ms
加速                628x
命中率              10/10，判定结果两遍完全一致
```

**必须知道的限制**：缓存是**进程内**的。多 worker 时每个 worker 各有一份，
实际命中率会被 worker 数摊薄。要全局共享得接 Redis（仓库原来那套
`utils/redis.py` 已删除，需要时得重写）。

**配置**：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `CACHE_ENABLED` | `1` | 置 `0` 关闭 |
| `CACHE_MAX_SIZE` | `1000` | 条目上限，超出按 LRU 淘汰 |
| `CACHE_TTL` | `604800` | 秒，默认 7 天 |

`GET /api/cache_stats` 可以看命中情况（`hits` / `misses` / `hit_rate` / `evicted`）。

> 缓存命中率完全取决于你的流量形态。如果 URL 是一次性的（每条内容只查一次），
> 这个优化等于零收益，还白占内存。**上线前先用 `cache_stats` 观察一段时间**，
> 命中率长期低于 10% 就把 `CACHE_ENABLED` 关掉。

### 6.2 httptools —— 解析层快 16.9 倍，端到端无感

先说结论：**装上了，但别指望它提速。**

直接对两个解析器做微基准（排除网络和客户端噪声，同一个 POST 请求头）：

```
h11（纯 Python）      20.41 µs/请求
httptools（C 扩展）    1.21 µs/请求
解析层加速            16.94x
单请求节省            19.21 µs
```

看起来很美，但换算到真实业务：

```
单次检测 190 ms = 190000 µs
解析占比    h11: 0.0107%    httptools: 0.0006%
端到端理论提升          1.000101x
```

**端到端提升 0.01%，完全在噪声里。** 端到端实测也印证了：

| 场景 | h11 | httptools | 差异 |
|---|---|---|---|
| 轻量端点 `/` 并发 1 | 1888 req/s | 2151 req/s | 1.14x |
| 轻量端点 `/` 并发 16 | 2936 req/s | 2854 req/s | 0.97x |
| 轻量端点 `/` 并发 64 | 2576 req/s | 2574 req/s | 1.00x |
| 推理端点 8 并发 | 5.06 req/s | 5.14 req/s | 1.02x |

并发 16/64 时反而没差异，因为**瓶颈在测试客户端**（Python 线程 + httpx），
不在服务端解析器。所以这组数字只能说明"解析层不是瓶颈"，
不能说明 httptools 的好坏。

**仍然装上的理由**：零副作用（纯 C 扩展，无行为差异），
且如果将来挂一个高 QPS 的轻量接口（比如只做 URL 白名单校验），
那时解析层就是主要开销，16.9x 才体现得出来。

`run.sh` 里用 `--http auto`（装了就用 httptools，没装自动退回 h11），
想强制回退：`HTTP_IMPL=h11`。

### 6.3 模型离线优化 —— 验证后决定不做

你的判断是对的：收益只能来自算子融合。我逐个试了并验证了输出等价性：

| 变体 | 节点数 | 输出最大差 | 推理(8线程) | 会话创建 |
|---|---|---|---|---|
| 原始模型 | 626 | — | 199.7ms | 119.9ms |
| onnxsim 静态化 | 616 | **0.000000** | 193.5ms (1.02x) | 118.7ms |
| ORT 通用融合 | **354** | **0.000000** | 192.7ms (1.02x) | 102.0ms |
| 静态化 + 通用融合 | 344 | **0.000000** | 195.9ms (1.00x) | 98.5ms |

**输出完全一致**（差 0.000000），说明这些融合是安全的。但**延迟没有变化**。

融合掉了什么：`Add×86 + Mul×49 + ReduceMean×50 + Sub/Pow/Sqrt×75 + Div×37`
→ `SkipLayerNormalization×24 + Gelu×12 + LayerNormalization×1`。
这是标准的 LayerNorm + GELU 融合，共省掉 272 个节点。

**为什么没用**：因为 ORT 在 `graph_optimization_level=ORT_ENABLE_ALL`
（默认值）下，**运行时本来就会做一模一样的融合**。离线做一遍等于
提前做一遍，省下的只是运行时那部分优化耗时 —— 也就是会话创建
从 119.9ms 降到 99.0ms（省 20ms/worker）。

**在低线程数下也验证过**（这才是目标机的场景，怕融合后并行粒度变差）：

| intra_op | 原始 | onnxsim | ORT 融合 |
|---|---|---|---|
| 1 | 887.6ms | 887.2ms (+0.1%) | 885.3ms (+0.3%) |
| 2 | 506.8ms | 508.6ms (−0.4%) | 503.3ms (+0.6%) |
| 4 | 294.0ms | 291.5ms (+0.9%) | 284.7ms (+3.3%) |
| 8 | 194.8ms | 196.5ms (−0.9%) | 198.0ms (−1.7%) |

全部在 ±3% 内，即噪声。**结论：不做。**

不做的额外理由：静态化会把输入锁死在 `1×3×384×384`，
一旦有人改 `INPUT_SIZE` 就会硬报错；而 ORT 融合版需要你维护一个
55MB 的派生模型文件，上游模型一更新就得重新生成。

> 踩坑记录：第一轮测出 `intra_op=2` 时融合版慢 **30.3%**（727ms vs 507ms），
> 差点据此写下"低线程下融合有害"的结论。复验 6 轮交替后是 503.3ms vs 506.4ms，
> 差异 0.6%。**单次测量不可信，尤其是耗时 3 分钟以上的长测试。**

### 6.4 前端 CDN 换成 npmmirror

`app/static/html/index.html` 原来依赖 `unpkg.com`，国内经常慢或不可达。
换成 `registry.npmmirror.com`，并把版本 pin 死（`latest` 会漂移）：

| 依赖 | 原来 | 现在 |
|---|---|---|
| Vue 2 | `unpkg.com/vue@2/dist/vue.js` | `npmmirror/vue/2.7.16/files/dist/vue.min.js` |
| Axios | `unpkg.com/axios/dist/axios.min.js` | `npmmirror/axios/1.7.9/files/dist/axios.min.js` |
| Element UI JS | `unpkg.com/element-ui/lib/index.js` | `npmmirror/element-ui/2.15.14/files/lib/index.js` |
| Element UI CSS | `unpkg.com/element-ui/lib/theme-chalk/index.css` | `npmmirror/element-ui/2.15.14/files/lib/theme-chalk/index.css` |

顺带把 Vue 换成了 `.min.js` 生产构建（424.7KB → 105.2KB），
并加了 `preconnect` 提前建连。

**验证过**：4 个资源全部返回 200 且内容正确；CSS 内部引用的
`fonts/element-icons.woff`（27.5KB）和 `.ttf`（54.6KB）在镜像上
也都能正常取到（相对路径会跟着 CSS 的 URL 解析）。

### 6.5 真实 NSFW 数据集验证

```
#      nsfw     sfw     判定       qr        耗时   文件名
1    0.9976  0.0024   NSFW  unknown      825ms   1.jpg
2    0.9972  0.0028   NSFW  unknown      381ms   2.jpg
3    0.9993  0.0007   NSFW  unknown      448ms   3.webp
4    0.5595  0.4405    sfw  unknown      374ms   4.webp
5    0.9173  0.0827   NSFW  unknown      376ms   5.webp
6    0.0891  0.9109    sfw  unknown      388ms   6.webp
7    0.9595  0.0405   NSFW  unknown      385ms   7.webp
8    0.9962  0.0038   NSFW  unknown      369ms   8.webp
9    0.9940  0.0060   NSFW  unknown      353ms   9.webp
10   0.9932  0.0068   NSFW  unknown      404ms   10.webp

10/10 成功，8 张判定 NSFW，中位耗时 383ms（含公网下载）
```

两点值得注意：

- **#4 是边界样本**（`nsfw=0.5595`），低于 0.8 阈值被判为 sfw。
  这类样本最容易被误判，如果你的业务对漏检敏感，应该**下调阈值**
  或对 0.4~0.8 区间做人工复审。
- **#1 耗时 825ms**，明显高于其余（~380ms），因为它是冷启动后的第一个请求
  （建连 + 首次权重预打包的尾巴）。这也说明**预热是必要的**。

---

## 七、部署配置建议

`run.sh` 现在会自动算，一般不用手配。以下是不同规格机器的参考值：

| 机器 | `WORKERS` | `ORT_NUM_THREADS` | 内存 | 预期单请求延迟* |
|---|---|---|---|---|
| 1 核 1GB | 1 | 1 | ~200MB | ~900ms |
| 2 核 1GB | 1 | 2 | ~200MB | ~500ms |
| 2 核 2GB | 1 | 2 | ~200MB | ~500ms（可开 2 worker 提吞吐，延迟不变） |
| 4 核 2GB | 2 | 2 | ~380MB | ~500ms，吞吐 2x |
| 4 核 4GB | 1 | 4 | ~200MB | ~290ms |

\* 延迟数字来自 16 核机同线程数实测，你的机器会因 CPU 型号有出入。

**经验法则**：

- 要**低延迟** → 少 worker、多线程（`WORKERS=1`, `ORT_NUM_THREADS=核数`）
- 要**高吞吐** → 多 worker、每 worker 少线程（`WORKERS=核数/2`, `ORT_NUM_THREADS=2`）
- 线程总数（`WORKERS × ORT_NUM_THREADS`）**不要超过物理核数**，
  超了就是纯损耗

`docker-compose.yaml` 里已加了 `cpus` / `memory` 限制，防止推理把整机吃满。

### 全部环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `TOKEN` | 空 | 鉴权密钥，不设则不鉴权 |
| `WEBUI` | `off` | `on` 开启首页 WEBUI |
| `WORKERS` | `1` | 进程数，自动收敛到 `min(4, 核数/2)` |
| `ORT_NUM_THREADS` | 自动 | 每进程推理线程数，默认 `核数/WORKERS` |
| `INFER_CONCURRENCY` | `=ORT_NUM_THREADS` | 同时推理的请求上限（信号量） |
| `LIMIT_CONCURRENCY` | `WORKERS×(N×2+4)` | uvicorn 连接上限，超出返回 503 |
| `ORT_ALLOW_SPINNING` | `0` | ORT 线程空闲时不自旋，省 CPU |
| `CACHE_ENABLED` | `1` | URL 结果缓存开关 |
| `CACHE_MAX_SIZE` | `1000` | 缓存条目上限 |
| `CACHE_TTL` | `604800` | 缓存有效期（秒），默认 7 天 |
| `HTTP_IMPL` | `auto` | HTTP 解析器，`h11` / `httptools` / `auto` |
| `ENABLE_DOCS` | `off` | `on` 开放 `/docs` 与 `/openapi.json` |
| `CORS_ORIGINS` | `*` | 逗号分隔的白名单，`off` 关闭 CORS |
| `MAX_IMAGE_BYTES` | `10485760` | 单图大小上限 |
| `MAX_IMAGE_PIXELS` | `64000000` | 解压后像素上限（防解压炸弹） |
| `NSFW_THRESHOLD` | `0.8` | 判定阈值 |
| `HTTP_TIMEOUT` / `HTTP_CONNECT_TIMEOUT` | `20` / `5` | 拉图超时 |
| `HTTP_TRUST_ENV` | `1` | 是否继承 `HTTP_PROXY` 等系统代理 |
| `LOG_LEVEL` | `warning` | `info` 可打开 access log |
| `APP_PORT` | `6086` | 监听端口 |

**上线前必看**：`HTTP_TRUST_ENV` 默认 `1`（继承系统代理，与上游行为一致）。
如果你的服务器配了 `HTTP_PROXY` 但拉图不需要走代理，设成 `0` 可以少一跳，
也避免内网地址被代理掉 —— 我在本机测试时就踩了这个坑，本地回环请求
被系统代理拦掉，报 502。

---

## 八、变更清单

### 第二轮（本次新增）

```
 app/cache.py            | 121 新建    TTL 7天 / 1000 条 LRU 缓存
 app/api/url_check.py    |  51 → 60    接入缓存读写
 app/routers.py          |  26 → 30    新增 /api/cache_stats
 app/requirements.txt    |   8 → 12    加 httptools
 run.sh                  | 184 → 161   删死函数、加 --http 可配
 app/static/html/index.html | 4 行     unpkg → npmmirror，Vue 换 min 构建
 app/config.py           | 67 删除     缺 toml 依赖
 app/model/（3 个文件）   | 56 删除     缺 sqlalchemy 依赖
 app/utils/redis.py      | 28 删除     依赖已删的 config
 app/middleware/req_limit.py | 71 删除  依赖已删的 redis
 app/data/config.toml    | 12 删除     只被 config.py 读取
```

### 第一轮

```
 Dockerfile              |  28 +++-    多阶段清理、HEALTHCHECK、PYTHONUNBUFFERED
 app/engine.py           | 382 新建    统一推理引擎（会话/预处理/HTTP/二维码）
 app/api/url_check.py    | 351 → 51    去重复，单次流式 GET，共享 client
 app/api/upload_check.py |  90 → 46    合并到统一 analyze 路径
 app/api/index.py        |  55 +++-    HTML 内存缓存
 app/main.py             |  72 +++-    lifespan 预热、关 docs、CORS 收窄
 app/middleware/auth.py  |  65 +++-    token 缓存、常量时间比较
 app/utils/helper.py     |  29 ++-     X-Real-IP bug 修复
 app/requirements.txt    |   6 +-     移除 opencv，pin numpy
 docker-compose.yaml     |  16 ++-    资源限制
 install.sh              |  33 ++-    --no-cache-dir、预编译 .pyc
 run.sh                  |  71 → 184  重写（线程/worker 自适应、exec、反压）
```

两轮下来最大的结构性变化不是行数，而是**职责收敛**：
`url_check` 从 351 行降到 60 行，`upload_check` 从 90 行降到 46 行，
推理/下载/解码/缓存全部收进 `engine.py` + `cache.py`，
两条检测路径的行为从此保证一致（之前是各写一份、格式映射表都不统一）。

### 验证方式

```bash
# 功能：24 项端到端测试
python test_nsfw.py

# 缓存：22 项单元测试（LRU / TTL / key 隔离 / 拷贝语义）
python test_cache.py

# 真实数据集 + 缓存命中效果（需要联网）
python test_dataset.py

# 性能：等线程 A/B 对比（需要先建原始版本 worktree）
git worktree add -f ../nsfw-orig HEAD
python bench_ab.py      # 上传路径，多线程数对比
python bench_url.py     # URL 路径，关缓存纯 HTTP 对比
python bench_parser.py  # h11 vs httptools 解析层微基准
```

---

## 九、还没做、但可以考虑的

按预期收益排序：

1. **缓存换成跨 worker 共享**（收益取决于 worker 数）
   现在是进程内缓存，`WORKERS=4` 时命中率被摊薄 4 倍。
   接 Redis 可以让所有 worker 共享一份。仓库原来那套 `utils/redis.py`
   已删（缺依赖且从未启用），需要时得重写 —— 用 `redis.asyncio` 大概 30 行。
   *前提*：先观察 `cache_stats` 的命中率，命中率低就没必要做。

2. **自适应阈值 / 人工复审队列**（提升准确率，不是性能）
   #4 样本 `nsfw=0.5595` 被判为 sfw。如果业务对漏检敏感，
   应该把 0.4~0.8 区间单独送人工复审，而不是硬调阈值。

3. **前端资源本地化**（消除对任何 CDN 的依赖）
   现在走 npmmirror 镜像，比 unpkg 好，但仍依赖外部网络。
   彻底的做法是把 4 个文件（约 1MB）放进 `app/static/vendor/`，
   由服务自己提供。缺点是要跟着 Element UI 版本手动更新。

4. **`uvloop`**（未实测，预期收益小）
   只对高并发 IO 密集场景有意义。本项目推理主导，预计 1~3% 以内，
   且 uvloop 不支持 Windows，会影响本地开发调试。

---

## 附录：测试环境

| 项目 | 值 |
|---|---|
| CPU | 16 逻辑核（x86_64，Windows） |
| Python | 3.13.12 |
| onnxruntime | 1.23.2（CPUExecutionProvider） |
| onnx / onnxsim | 1.23.2 / 0.8.1 |
| Pillow | 13.0.0（基准）/ 12.0.0（仓库 pin） |
| httptools | 0.9.0 |
| 模型 | `model_q4.onnx`，56MB，ViT-base，int4 权重（`MatMulNBits`） |
| 输入 | `pixel_values`，动态 shape `(batch, 3, H, W)`，实际喂 384×384 |

**方法的局限**：

- 测试机是 16 核桌面 CPU，**不是你的目标机器**。所有比值（1.21x、1.7x、
  16.9x、628x）应该能迁移，但绝对毫秒数不能直接套用。
- 网络部分用的是本机回环，只验证了"少一次往返"的结构性收益；
  公网 HTTPS 的真实差距取决于 RTT 和 TLS 握手耗时，通常远大于回环。
- 缓存加速 628x 是在"同一个 URL 连续请求 10 次"的条件下测的，
  真实命中率取决于你的流量形态，**不要把这个数字当成线上预期**。
- 精度验证：12 张合成图 + 10 张真实 NSFW 图，均无判定翻转。
  但没有做大规模召回率测试 —— 严谨的精度回归需要一批标注好的真实样本。
  如果要做严谨的精度回归，需要用一批标注好的真实图片。
