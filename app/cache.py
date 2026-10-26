"""URL 检测结果的 SQLite 缓存。

相比原来的进程内 LRU，落盘带来三个实际好处：
1. **多 worker 共享**：原来每个 worker 各存一份，命中率被 worker 数摊薄 N 倍；
   现在共享同一个 .db 文件，WORKERS=4 也不会浪费缓存。
2. **重启不丢**：原来进程一重启缓存全清，容器滚动更新等于冷启动。
3. **可备份/可迁移**：就是一个文件，拷走即可。

实现要点：
- WAL 模式 + busy_timeout，允许多进程同时读、单进程写，写冲突自动重试。
- 所有 DB 操作走**专用单线程池**，不占用推理用的默认线程池
  （推理单次要 200ms，混在一起会让 DB 读排队）。
- 只缓存**模型输出**（sfw / nsfw / qr_status），不缓存 is_nsfw 判定结果。
  这样改 NSFW_THRESHOLD 之后，缓存里的历史条目会立刻按新阈值重新判定，
  不需要清缓存。这是刻意设计。
- DB 出任何问题都只记日志并降级为「不缓存」，绝不让接口报错。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from app.utils.helper import md5

log = logging.getLogger("nsfw.cache")

CACHE_ENABLED = os.getenv("CACHE_ENABLED", "1") == "1"
# 相对路径按进程工作目录解析（run.sh 会 cd 到项目根）
CACHE_DB_PATH = os.getenv("CACHE_DB_PATH", "data/cache.db")
# 条目上限，0 表示不限。超出后按最久未命中（LRU）淘汰。
CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "1000"))
# 有效期，默认 7 天
CACHE_TTL = int(os.getenv("CACHE_TTL", str(7 * 24 * 3600)))
# 命中后刷新 last_hit 的最小间隔，避免每次命中都写盘
_HIT_TOUCH_INTERVAL = 60
# 过期清理的最小间隔
_PURGE_INTERVAL = 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS url_cache (
    key        TEXT    PRIMARY KEY,   -- md5(url|qr开关)
    url        TEXT    NOT NULL,      -- 原始 URL，方便人工排查/导出
    qr_enabled INTEGER NOT NULL,
    sfw        REAL    NOT NULL,
    nsfw       REAL    NOT NULL,
    qr_status  TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_hit   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_url_cache_expires ON url_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_url_cache_lasthit ON url_cache(last_hit);
"""

_conn: Optional[sqlite3.Connection] = None
_conn_lock = threading.Lock()
# 单线程池：SQLite 单连接本来就该串行访问，也避免拖累推理线程池
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite")
_last_purge = 0.0

_stats = {"hits": 0, "misses": 0, "expired": 0, "evicted": 0, "errors": 0, "writes": 0}


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        d = os.path.dirname(os.path.abspath(CACHE_DB_PATH))
        if d:
            os.makedirs(d, exist_ok=True)
        c = sqlite3.connect(CACHE_DB_PATH, timeout=10, check_same_thread=False)
        # WAL：多进程可同时读、一个写；synchronous=NORMAL 在 WAL 下足够安全，
        # 缓存丢了也能重建，不需要 FULL 的 fsync 开销。
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("PRAGMA temp_store=MEMORY")
        c.executescript(_SCHEMA)
        c.commit()
        _conn = c
        log.info("缓存数据库就绪: %s", os.path.abspath(CACHE_DB_PATH))
        return _conn


# --------------------------------------------------------------------------
# 同步实现（只会在 _executor 的线程里跑）
# --------------------------------------------------------------------------


def _get_sync(key: str) -> Optional[dict]:
    now = int(time.time())
    c = _connect()
    row = c.execute(
        "SELECT sfw, nsfw, qr_status, last_hit FROM url_cache "
        "WHERE key = ? AND expires_at > ?",
        (key, now),
    ).fetchone()

    if row is None:
        # 可能只是过期了，顺手删掉
        cur = c.execute(
            "DELETE FROM url_cache WHERE key = ? AND expires_at <= ?", (key, now)
        )
        if cur.rowcount:
            _stats["expired"] += cur.rowcount
            c.commit()
        return None

    sfw, nsfw, qr_status, last_hit = row
    # 60 秒内的重复命中不写盘，避免高频读变成高频写
    if now - last_hit >= _HIT_TOUCH_INTERVAL:
        c.execute("UPDATE url_cache SET last_hit = ? WHERE key = ?", (now, key))
        c.commit()

    return {"sfw": sfw, "nsfw": nsfw, "qr_status": qr_status}


def _put_sync(key: str, url: str, qr_enabled: bool, data: dict) -> None:
    now = int(time.time())
    c = _connect()
    c.execute(
        "INSERT INTO url_cache"
        " (key, url, qr_enabled, sfw, nsfw, qr_status, created_at, expires_at, last_hit)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET"
        "   url = excluded.url,"
        "   sfw = excluded.sfw,"
        "   nsfw = excluded.nsfw,"
        "   qr_status = excluded.qr_status,"
        "   expires_at = excluded.expires_at,"
        "   last_hit = excluded.last_hit",
        (
            key,
            url,
            int(bool(qr_enabled)),
            float(data["sfw"]),
            float(data["nsfw"]),
            str(data.get("qr_status") or "unknown"),
            now,
            now + CACHE_TTL,
            now,
        ),
    )
    c.commit()
    _stats["writes"] += 1
    _maintain(c, now)


def _maintain(c: sqlite3.Connection, now: int) -> None:
    """过期清理 + 超限淘汰。只在写入后调用。"""
    global _last_purge
    if now - _last_purge >= _PURGE_INTERVAL:
        _last_purge = now
        cur = c.execute("DELETE FROM url_cache WHERE expires_at <= ?", (now,))
        if cur.rowcount:
            _stats["expired"] += cur.rowcount
            c.commit()

    if CACHE_MAX_SIZE <= 0:
        return
    total = c.execute("SELECT COUNT(*) FROM url_cache").fetchone()[0]
    if total > CACHE_MAX_SIZE:
        cur = c.execute(
            "DELETE FROM url_cache WHERE key IN ("
            "  SELECT key FROM url_cache ORDER BY last_hit ASC LIMIT ?)",
            (total - CACHE_MAX_SIZE,),
        )
        if cur.rowcount:
            _stats["evicted"] += cur.rowcount
            c.commit()


def _purge_all_expired() -> None:
    global _last_purge
    c = _connect()
    now = int(time.time())
    cur = c.execute("DELETE FROM url_cache WHERE expires_at <= ?", (now,))
    _last_purge = now
    if cur.rowcount:
        _stats["expired"] += cur.rowcount
        c.commit()


def _file_sizes() -> dict:
    """统计主库与 WAL 文件体积。

    WAL 模式下新数据先进 cache.db-wal，checkpoint 之后才并回 cache.db。
    所以只报 cache.db 的大小会严重低估（实测可能只有 4KB），
    备份时也绝不能只拷 cache.db。
    """
    def sz(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0

    main = sz(CACHE_DB_PATH)
    wal = sz(CACHE_DB_PATH + "-wal")
    shm = sz(CACHE_DB_PATH + "-shm")
    return {
        "db_bytes": main,
        "wal_bytes": wal,
        "shm_bytes": shm,
        "total_bytes": main + wal + shm,
    }


def _stats_sync() -> dict:
    c = _connect()
    now = int(time.time())
    total = c.execute("SELECT COUNT(*) FROM url_cache").fetchone()[0]
    expired = c.execute(
        "SELECT COUNT(*) FROM url_cache WHERE expires_at <= ?", (now,)
    ).fetchone()[0]
    return {"rows": total, "expired_rows": expired, **_file_sizes()}


def _clear_sync() -> None:
    c = _connect()
    c.execute("DELETE FROM url_cache")
    c.commit()


# --------------------------------------------------------------------------
# 异步封装
# --------------------------------------------------------------------------


def make_key(url: str, qr_enabled: bool) -> str:
    """md5(url + qr 开关)。qr 开关会影响 qr_status，必须参与 key。"""
    return md5(f"{url}|{1 if qr_enabled else 0}")


async def _run(fn, *args) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, fn, *args)


async def get(url: str, qr_enabled: bool) -> Optional[dict]:
    """命中返回 {sfw, nsfw, qr_status}，未命中返回 None。

    注意：不返回 is_nsfw —— 阈值判定交给调用方用当前配置算，
    这样改阈值不用清缓存。
    """
    if not CACHE_ENABLED:
        return None
    try:
        data = await _run(_get_sync, make_key(url, qr_enabled))
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        log.warning("缓存读取失败，降级为不缓存: %s", e)
        return None
    if data is None:
        _stats["misses"] += 1
    else:
        _stats["hits"] += 1
    return data


async def put(url: str, qr_enabled: bool, data: dict) -> None:
    if not CACHE_ENABLED or not isinstance(data, dict):
        return
    if "nsfw" not in data or "sfw" not in data:
        return
    try:
        await _run(_put_sync, make_key(url, qr_enabled), url, qr_enabled, data)
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        log.warning("缓存写入失败，本次不缓存: %s", e)


async def stats() -> dict:
    base = {
        "enabled": CACHE_ENABLED,
        "backend": "sqlite",
        "db_path": os.path.abspath(CACHE_DB_PATH),
        "max_size": CACHE_MAX_SIZE,
        "ttl_seconds": CACHE_TTL,
        **_stats,
    }
    if not CACHE_ENABLED:
        return base
    try:
        base.update(await _run(_stats_sync))
    except Exception as e:  # noqa: BLE001
        base["error"] = str(e)
    total = base.get("hits", 0) + base.get("misses", 0)
    base["hit_rate"] = round(base["hits"] / total, 4) if total else 0.0
    return base


async def init() -> None:
    """启动时建表并清理过期数据。失败只告警，不影响服务启动。"""
    if not CACHE_ENABLED:
        return
    try:
        await _run(_connect)
        await _run(_purge_all_expired)
    except Exception as e:  # noqa: BLE001
        log.error("缓存数据库初始化失败，将以无缓存模式运行: %s", e)


async def close() -> None:
    global _conn
    _executor.shutdown(wait=True)
    if _conn is not None:
        try:
            _conn.commit()
            # 合并 WAL 到主库，让备份时只需拷一个文件
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


async def clear() -> None:
    """清空缓存（调试用）。"""
    try:
        await _run(_clear_sync)
    except Exception as e:  # noqa: BLE001
        log.warning("清空缓存失败: %s", e)


def _backup_sync(dest: str) -> int:
    """用 SQLite 官方 backup API 在线备份到单个文件。

    比起直接 cp 文件，这个能保证一致性：WAL 模式下运行中直接拷 .db
    会漏掉还留在 -wal 里的数据。备份产物是完整独立的单文件，
    可以直接拿来恢复或拷到别的机器。
    """
    d = os.path.dirname(os.path.abspath(dest))
    if d:
        os.makedirs(d, exist_ok=True)
    src = _connect()
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
    return os.path.getsize(dest)


async def backup(dest: str) -> dict:
    try:
        size = await _run(_backup_sync, dest)
        return {"ok": True, "path": os.path.abspath(dest), "bytes": size}
    except Exception as e:  # noqa: BLE001
        log.error("备份失败: %s", e)
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    # 用法：python -m app.cache [目标路径]
    # 例：docker compose exec nsfw python -m app.cache /opt/nsfw/data/cache-backup.db
    import sys

    _dest = sys.argv[1] if len(sys.argv) > 1 else "cache-backup.db"
    print(asyncio.run(backup(_dest)))
