"""缓存管理模块。

提供内存缓存和磁盘缓存功能，避免重复网络请求。
"""

from __future__ import annotations

import os
import pickle
import shutil
import threading
from datetime import datetime
from typing import Any

import pandas as pd

# 内存缓存
_DATA_CACHE: dict[str, Any] = {}

# 线程安全的缓存锁
_CACHE_LOCK = threading.Lock()

# 磁盘缓存配置
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "cache")
ENABLE_DISK_CACHE = True


def get_cache_lock() -> threading.Lock:
    """获取缓存锁。"""
    return _CACHE_LOCK


def get_mem_cache(key: str) -> Any | None:
    """从内存缓存获取数据。"""
    return _DATA_CACHE.get(key)


def set_mem_cache(key: str, value: Any) -> None:
    """设置内存缓存。"""
    _DATA_CACHE[key] = value


def get_mem_cache_safe(key: str) -> Any | None:
    """线程安全地从内存缓存获取数据。"""
    with _CACHE_LOCK:
        return _DATA_CACHE.get(key)


def set_mem_cache_safe(key: str, value: Any) -> None:
    """线程安全地设置内存缓存。"""
    with _CACHE_LOCK:
        _DATA_CACHE[key] = value


def clear_cache() -> None:
    """清空内存缓存。"""
    _DATA_CACHE.clear()


def _get_cache_dir() -> str:
    """获取当天的缓存目录。"""
    today = datetime.now().strftime("%Y%m%d")
    cache_dir = os.path.join(CACHE_DIR, today)
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def load_disk_cache(cache_key: str) -> pd.DataFrame | None:
    """从磁盘加载缓存。"""
    if not ENABLE_DISK_CACHE:
        return None
    try:
        cache_file = os.path.join(_get_cache_dir(), f"{cache_key}.pkl")
        if os.path.exists(cache_file):
            with open(cache_file, "rb") as f:
                return pickle.load(f)
    except Exception:
        pass
    return None


def save_disk_cache(cache_key: str, data: Any) -> None:
    """保存数据到磁盘缓存。"""
    if not ENABLE_DISK_CACHE:
        return
    try:
        cache_file = os.path.join(_get_cache_dir(), f"{cache_key}.pkl")
        with open(cache_file, "wb") as f:
            pickle.dump(data, f)
    except Exception:
        pass


def clear_disk_cache() -> None:
    """清空所有磁盘缓存。"""
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
        os.makedirs(CACHE_DIR, exist_ok=True)