"""Token 管理与鉴权（SQLite 落盘）。

把原来写死在环境变量 TOKEN 里的单一密钥，升级为多 token、可配置的管理系统：

- token 形如 ``nsfw_<64位hex>``，库里**只存 sha256 哈希**，
  数据库泄露也拿不到可用明文（明文只在管理页创建/重置时出现一次）。
- 每个 token 可配置：
  * 名称/备注
  * 有效期（unix 时间戳，0 = 永久）
  * 每日调用上限（按自然日，0 = 不限）
  * 总调用上限（0 = 不限）
  * 启用开关
- 次数记账：total_used 累计；每日次数按 ``(token_id, YYYY-MM-DD)`` 存，
  跨天自动换新行，无需定时清理任务。
- 计数发生在**鉴权通过后**（成功消费一次）；若请求后续失败仍算一次 ——
  语义是「调用次数」而非「成功次数」。
- 所有 DB 操作走专用单线程池；WAL 模式允许多进程并发
  （v1 校验服务与管理版管理端共享同一份 tokens.db）。
- DB 异常一律**拒绝鉴权**（fail-closed）并打日志 —— 鉴权宁可 401，不可放行。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

log = logging.getLogger("nsfw.tokens")

TOKENS_DB_PATH = os.getenv("TOKENS_DB_PATH", "data/tokens.db")
# 管理页登录密钥，通过 docker 环境变量注入（原来 TOKEN 的替代者）
# 注意：这仍是环境变量，但只用于「管理」，不参与业务鉴权。
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    token_prefix TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL DEFAULT 0,   -- 0 = 永不过期
    daily_limit  INTEGER NOT NULL DEFAULT 0,   -- 0 = 不限
    total_limit  INTEGER NOT NULL DEFAULT 0,   -- 0 = 不限
    enabled      INTEGER NOT NULL DEFAULT 1,
    total_used   INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS token_usage_daily (
    token_id INTEGER NOT NULL,
    day      TEXT    NOT NULL,                 -- YYYY-MM-DD
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (token_id, day)
);
"""

_conn: Optional[sqlite3.Connection] = None
_conn_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tokens")

# 运行期计数器
_stats = {"errors": 0, "denied_invalid": 0, "denied_disabled": 0,
          "denied_expired": 0, "denied_daily": 0, "denied_total": 0,
          "auth_ok": 0, "open_mode": 0}


# ---------------------------------------------------------------------------
# 内部：连接与同步实现（只在线程池里跑）
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        d = os.path.dirname(os.path.abspath(TOKENS_DB_PATH))
        if d:
            os.makedirs(d, exist_ok=True)
        c = sqlite3.connect(TOKENS_DB_PATH, timeout=10, check_same_thread=False)
        # 必须设 Row 工厂，否则 fetchone() 返回 tuple，r["field"] 会炸
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.executescript(_SCHEMA)
        c.commit()
        _conn = c
        log.info("token 存储就绪: %s", os.path.abspath(TOKENS_DB_PATH))
        return _conn


def _generate_token() -> str:
    return "nsfw_" + secrets.token_hex(32)


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fmt_time(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "永久"


def _row_to_dict(r: sqlite3.Row) -> dict:
    today = time.strftime("%Y-%m-%d")
    c = _connect()
    drow = c.execute(
        "SELECT count FROM token_usage_daily WHERE token_id = ? AND day = ?",
        (r["id"], today),
    ).fetchone()
    daily_used = int(drow["count"]) if drow else 0
    return {
        "id": r["id"],
        "name": r["name"],
        "token_prefix": r["token_prefix"],
        "created_at": r["created_at"],
        "created_text": _fmt_time(r["created_at"]),
        "expires_at": r["expires_at"],
        "expires_text": _fmt_time(r["expires_at"]),
        "daily_limit": r["daily_limit"],
        "total_limit": r["total_limit"],
        "enabled": bool(r["enabled"]),
        "total_used": r["total_used"],
        "daily_used": daily_used,
        "last_used_at": r["last_used_at"],
        "last_used_text": _fmt_time(r["last_used_at"]),
    }


def _create_sync(name: str, expires_at: int = 0,
                 daily_limit: int = 0, total_limit: int = 0) -> dict:
    raw = _generate_token()
    c = _connect()
    cur = c.execute(
        "INSERT INTO tokens (name, token_hash, token_prefix, created_at, expires_at,"
        " daily_limit, total_limit)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, _hash_token(raw), raw[:8], int(time.time()),
         int(expires_at), int(daily_limit), int(total_limit)),
    )
    c.commit()
    row = c.execute("SELECT * FROM tokens WHERE id = ?", (cur.lastrowid,)).fetchone()
    info = _row_to_dict(row)
    info["raw_token"] = raw  # 仅创建/重置时返回一次明文
    return info


def _update_sync(token_id: int, *, name=None, expires_at=None,
                 daily_limit=None, total_limit=None, enabled=None) -> Optional[dict]:
    sets, args = [], []
    if name is not None:
        sets.append("name = ?")
        args.append(str(name))
    if expires_at is not None:
        sets.append("expires_at = ?")
        args.append(int(expires_at))
    if daily_limit is not None:
        sets.append("daily_limit = ?")
        args.append(int(daily_limit))
    if total_limit is not None:
        sets.append("total_limit = ?")
        args.append(int(total_limit))
    if enabled is not None:
        sets.append("enabled = ?")
        args.append(1 if enabled else 0)
    if not sets:
        return None
    c = _connect()
    args.append(token_id)
    c.execute(f"UPDATE tokens SET {', '.join(sets)} WHERE id = ?", args)
    c.commit()
    row = c.execute("SELECT * FROM tokens WHERE id = ?", (token_id,)).fetchone()
    return _row_to_dict(row) if row else None


def _delete_sync(token_id: int) -> bool:
    c = _connect()
    cur = c.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
    c.execute("DELETE FROM token_usage_daily WHERE token_id = ?", (token_id,))
    c.commit()
    return cur.rowcount > 0


def _reset_sync(token_id: int) -> bool:
    c = _connect()
    cur = c.execute("UPDATE tokens SET total_used = 0 WHERE id = ?", (token_id,))
    c.execute("DELETE FROM token_usage_daily WHERE token_id = ?", (token_id,))
    c.commit()
    return cur.rowcount > 0


def _list_sync() -> list[dict]:
    c = _connect()
    rows = c.execute("SELECT * FROM tokens ORDER BY id").fetchall()
    return [_row_to_dict(r) for r in rows]


def _authenticate_sync(raw: Optional[str]) -> Optional[dict]:
    """校验并记账一次调用。返回 token 信息（含是否放行）。

    返回 None = token 不存在/hash 不匹配。
    否则返回 {"ok": bool, "reason": str, "info": dict|None}
    reason: ok / open_mode / disabled / expired / daily_limit / total_limit

    **公开模式**：如果 tokens 表里一条记录都没有，视为「还没启用鉴权」，
    放行所有请求（与旧版「不设 TOKEN 环境变量就不鉴权」语义一致）。
    只要存在至少一条 token（无论启用与否），就进入强制鉴权模式。
    这样升级旧部署不会突然全部 401，同时一旦建了 token 就自动收紧。
    """
    c = _connect()

    # 空表 → 公开模式（不看请求头）
    if c.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0:
        _stats["open_mode"] += 1
        return {"ok": True, "reason": "open_mode", "info": None}

    if not raw:
        _stats["denied_invalid"] += 1
        return None

    row = c.execute(
        "SELECT * FROM tokens WHERE token_hash = ?", (_hash_token(raw),)
    ).fetchone()
    if row is None:
        _stats["denied_invalid"] += 1
        return None

    now = int(time.time())
    info = _row_to_dict(row)

    if not row["enabled"]:
        _stats["denied_disabled"] += 1
        return {"ok": False, "reason": "disabled", "info": info}
    if row["expires_at"] and now > row["expires_at"]:
        _stats["denied_expired"] += 1
        return {"ok": False, "reason": "expired", "info": info}

    today = time.strftime("%Y-%m-%d")
    drow = c.execute(
        "SELECT count FROM token_usage_daily WHERE token_id = ? AND day = ?",
        (row["id"], today),
    ).fetchone()
    daily_used = int(drow["count"]) if drow else 0
    if row["daily_limit"] and daily_used >= row["daily_limit"]:
        _stats["denied_daily"] += 1
        return {"ok": False, "reason": "daily_limit", "info": info}
    if row["total_limit"] and row["total_used"] >= row["total_limit"]:
        _stats["denied_total"] += 1
        return {"ok": False, "reason": "total_limit", "info": info}

    # 记账：总次数 +1，每日 +1，刷最后使用时间
    c.execute("UPDATE tokens SET total_used = total_used + 1, last_used_at = ?"
              " WHERE id = ?", (now, row["id"]))
    c.execute(
        "INSERT INTO token_usage_daily (token_id, day, count) VALUES (?, ?, 1)"
        " ON CONFLICT(token_id, day) DO UPDATE SET count = count + 1",
        (row["id"], today),
    )
    c.commit()
    _stats["auth_ok"] += 1
    info["total_used"] = row["total_used"] + 1
    info["daily_used"] = daily_used + 1
    return {"ok": True, "reason": "ok", "info": info}


def _stats_sync() -> dict:
    c = _connect()
    total = c.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
    return {"tokens": total}


# ---------------------------------------------------------------------------
# 异步封装
# ---------------------------------------------------------------------------


async def _run(fn, *args, **kwargs) -> Any:
    loop = asyncio.get_running_loop()
    # run_in_executor 的 callable 不能直接带 kwargs，用 partial 打包
    import functools
    return await loop.run_in_executor(
        _executor, functools.partial(fn, *args, **kwargs)
    )


async def init() -> None:
    """启动时建表。失败只告警（鉴权会 fail-closed）。"""
    try:
        await _run(_connect)
    except Exception as e:  # noqa: BLE001
        log.error("token 存储初始化失败，此后所有鉴权将被拒绝: %s", e)


async def create(name: str, expires_at: int = 0,
                daily_limit: int = 0, total_limit: int = 0) -> Optional[dict]:
    try:
        return await _run(_create_sync, name, expires_at, daily_limit, total_limit)
    except Exception as e:  # noqa: BLE001
        log.error("创建 token 失败: %s", e)
        return None


async def update(token_id: int, **kw) -> Optional[dict]:
    try:
        return await _run(_update_sync, token_id, **kw)
    except Exception as e:  # noqa: BLE001
        log.error("更新 token 失败: %s", e)
        return None


async def delete(token_id: int) -> bool:
    try:
        return await _run(_delete_sync, token_id)
    except Exception as e:  # noqa: BLE001
        log.error("删除 token 失败: %s", e)
        return False


async def reset_usage(token_id: int) -> bool:
    try:
        return await _run(_reset_sync, token_id)
    except Exception as e:  # noqa: BLE001
        log.error("重置 token 用量失败: %s", e)
        return False


async def list_tokens() -> list[dict]:
    try:
        return await _run(_list_sync)
    except Exception as e:  # noqa: BLE001
        log.error("读取 token 列表失败: %s", e)
        return []


async def authenticate(raw: Optional[str]) -> Optional[dict]:
    """鉴权并记账。返回 dict 或 None（token 不合法时）。"""
    try:
        return await _run(_authenticate_sync, raw)
    except Exception as e:  # noqa: BLE001
        _stats["errors"] += 1
        log.error("鉴权异常（fail-closed，拒绝）: %s", e)
        # fail-closed：DB 出错时当作 token 无效处理
        return None


async def stats() -> dict:
    base = {"admin_token_set": bool(ADMIN_TOKEN), "db_path": os.path.abspath(TOKENS_DB_PATH), **_stats}
    try:
        base.update(await _run(_stats_sync))
    except Exception:
        pass
    return base
