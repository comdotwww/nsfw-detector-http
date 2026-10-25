"""URL 检测结果的进程内缓存。

设计要点：
1. key 用 md5(url)，并把 qr_detect 开关一起纳入 —— 因为返回体里的
   qr_status 字段依赖这个开关，只用 url 做 key 会串味。
2. 容量上限 + LRU 淘汰，防止被随机 URL 打爆内存。
3. TTL 7 天，读时惰性过期（不做后台扫描线程，省资源）。
4. 只缓存成功结果，失败/异常一律不缓存 —— 否则一次网络抖动会污染 7 天。
5. 返回浅拷贝，避免调用方修改返回体污染缓存。
6. 进程内缓存，多 worker 之间不共享。每个 worker 各有一份，
   所以实际命中率会被 worker 数摊薄；要全局共享得接 Redis。

实测参考：命中时 URL 检测从 ~190ms 降到 ~1ms（省掉下载 + 推理）。
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

from app.utils.helper import md5

CACHE_ENABLED = os.getenv("CACHE_ENABLED", "1") == "1"
CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", "1000"))
CACHE_TTL = int(os.getenv("CACHE_TTL", str(7 * 24 * 3600)))  # 7 天


class TTLCache:
    """定容 LRU + TTL 的极简缓存。

    OrderedDict 头部是最近最少使用的，尾部是最近刚用过的。
    读命中时把 key 移到尾部，写满时从头部弹出。
    """

    def __init__(self, maxsize: int = 1000, ttl: int = 7 * 24 * 3600):
        self.maxsize = max(1, maxsize)
        self.ttl = ttl
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.expired = 0
        self.evicted = 0

    def get(self, key: str) -> Optional[Any]:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            expire_at, value = item
            if expire_at <= now:
                # 惰性过期：读到了才发现过期，顺手删掉
                del self._data[key]
                self.expired += 1
                self.misses += 1
                return None
            # 命中，移到尾部表示"最近用过"
            self._data.move_to_end(key)
            self.hits += 1
            # 浅拷贝：调用方可能往 dict 里塞字段
            return dict(value) if isinstance(value, dict) else value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
                self.evicted += 1

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "enabled": CACHE_ENABLED,
                "size": len(self._data),
                "max_size": self.maxsize,
                "ttl_seconds": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
                "expired": self.expired,
                "evicted": self.evicted,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }


_cache = TTLCache(CACHE_MAX_SIZE, CACHE_TTL)


def make_key(url: str, qr_enabled: bool) -> str:
    """md5(url + qr 开关)。qr 开关会影响 qr_status，必须参与 key。"""
    return md5(f"{url}|{1 if qr_enabled else 0}")


def get(url: str, qr_enabled: bool) -> Optional[dict]:
    if not CACHE_ENABLED:
        return None
    return _cache.get(make_key(url, qr_enabled))


def put(url: str, qr_enabled: bool, data: dict) -> None:
    if not CACHE_ENABLED or not isinstance(data, dict):
        return
    _cache.set(make_key(url, qr_enabled), data)


def stats() -> dict:
    return _cache.stats()


def clear() -> None:
    _cache.clear()
