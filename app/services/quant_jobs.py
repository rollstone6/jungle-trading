"""信号中心扫描任务管理器（2026-11-10 性能改造）。

四大扫描（signals/trend/box/chip）首跑是分钟级网络取数，原先 API 同步
await 把请求挂住最长 10 分钟：前端 fetch 超时、用户刷新还会并发重复扫描。
本模块提供：

- ``run(key, compute, force)``：singleflight —— 同一 key 同时只跑一个后台
  任务（asyncio.to_thread），其余请求立即拿当前状态（scanning/ready/error）。
  任务 running 期间一律返回 scanning（不返回上一代结果，避免前端把轮询
  链断在半路）。
- 进程内当日 ready 缓存：前端每 3s 轮询同一 URL，完成后直接拿到 payload。
- ``prime(key, payload)``：定时预热写结果（15:45 收盘后 warm job 用）。
- 全局并发闸 asyncio.Semaphore(2)：最多两个扫描任务同时在跑，防止
  多线程×网络并发打爆 3.4G 小机或触发行情源限流。

缓存 key 与 ``quant_scan`` 落盘文件名一致（如 ``signals_breakout_pool``），
ready 路径先查进程内再查当日磁盘缓存，重启后不重复冷扫。

前端契约：带 refresh=1 的提交请求拿到 scanning 后，后续轮询必须剥掉
refresh=1（否则任务完成后的下一次 force 轮询会误触发重扫）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from app.services import quant_scan as qs

logger = logging.getLogger(__name__)

#: key -> (asOf, payload)  进程内当日 ready 结果
_RESULTS: dict[str, tuple[str, dict]] = {}
#: key -> {"status": running|done|error, "startedAt", "ts", "error"}
_JOBS: dict[str, dict] = {}
#: 运行中的 asyncio.Task 引用，防 GC
_TASKS: set[asyncio.Task] = set()

#: 扫描任务并发闸（每个任务内部已有 8 worker 线程池）
_MAX_CONCURRENT_SCANS = 2
_GATE: asyncio.Semaphore | None = None

#: 失败后多久允许自动重新提交（防重试风暴）
ERROR_COOLDOWN = 30.0

#: running 超时兜底（审计 P2-2）：正常情况下四大赛道扫描首跑为分钟级
#: 网络取数，串行队列下最坏 ≈ 闸排队(前一任务最长耗时) + 自身耗时。
#: 取 30 分钟（= 10 分钟级首跑的 3 倍余量，见模块 docstring 历史超时上限）：
#: 超过即视为 compute 线程挂死（网络 hang / 死锁），置 error 并放行重算，
#: 避免该 key 永卡 running 的 singleflight 死锁态。残留旧任务若后来完成，
#: prime/error 覆盖写 _JOBS 无害（payload 落盘是幂等结果）。
RUNNING_TIMEOUT_SECONDS = 30 * 60


def _gate() -> asyncio.Semaphore:
    global _GATE
    if _GATE is None:
        _GATE = asyncio.Semaphore(_MAX_CONCURRENT_SCANS)
    return _GATE


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def prime(key: str, payload: dict) -> None:
    """写入当日 ready 结果（定时预热 / 任务完成共用）。"""
    _RESULTS[key] = (_today(), payload)
    _JOBS[key] = {"status": "done", "ts": time.time()}


def status_of(key: str) -> dict | None:
    job = _JOBS.get(key)
    if not job:
        return None
    return {"key": key, **{k: v for k, v in job.items() if k != "startedAt"}}


async def run(key: str, compute, *, force: bool = False) -> dict:
    """返回 ready payload（含 status=ready）或 scanning / error 状态。

    - 未 force 且有当日缓存（进程内或磁盘）→ 直接 ready（不占并发闸）。
    - 有任务 running → scanning（singleflight：绝不重复提交，force 也复用）。
    - error 冷却期内（非 force）→ error；force 可立即重试。
    - 其余情况 → 提交后台任务并立即返回 scanning。
    """
    if not force:
        got = _RESULTS.get(key)
        if got and got[0] == _today():
            return {"status": "ready", **got[1]}
        cached = qs.today_cache(key)
        if cached:
            _RESULTS[key] = (_today(), cached)
            return {"status": "ready", **cached}

    job = _JOBS.get(key)
    if job and job.get("status") == "running":
        # 超时兜底（P2-2）：running 超 RUNNING_TIMEOUT_SECONDS 视为 compute
        # 挂死，就地置 error，不再返回 scanning —— 让本次调用走下面的
        # error/重提交路径，key 可自愈重算，不再永卡。
        started = job.get("startedAt") or job.get("ts", 0)
        if time.time() - started > RUNNING_TIMEOUT_SECONDS:
            logger.warning(
                "quant job %s running 超过 %ss（startedAt=%s），视为挂死置 error",
                key, RUNNING_TIMEOUT_SECONDS, started)
            _JOBS[key] = {"status": "error",
                          "error": f"timeout: running > {RUNNING_TIMEOUT_SECONDS}s",
                          "ts": time.time()}
            job = _JOBS[key]
        else:
            return _scanning(key, job)
    if (job and job.get("status") == "error" and not force
            and time.time() - job.get("ts", 0) < ERROR_COOLDOWN):
        return {"status": "error", "key": key, "error": job.get("error", "")}

    _JOBS[key] = {"status": "running", "startedAt": time.time(),
                  "ts": time.time()}
    task = asyncio.create_task(_execute(key, compute))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return _scanning(key, _JOBS[key])


def _scanning(key: str, job: dict) -> dict:
    started = job.get("startedAt")
    running = sum(1 for j in _JOBS.values() if j.get("status") == "running")
    return {
        "status": "scanning",
        "key": key,
        "startedAt": datetime.fromtimestamp(started).strftime("%H:%M:%S")
        if started else None,
        "queued": running > _MAX_CONCURRENT_SCANS,
    }


async def _execute(key: str, compute) -> None:
    started = _JOBS.get(key, {}).get("startedAt")
    async with _gate():
        try:
            payload = await asyncio.to_thread(compute)
        except Exception as exc:
            logger.warning("quant job %s compute 异常: %s", key, exc)
            _JOBS[key] = {"status": "error",
                          "error": f"{type(exc).__name__}: {exc}",
                          "startedAt": started, "ts": time.time()}
            return
    if isinstance(payload, dict):
        prime(key, payload)
    else:
        # compute 返回非 dict（如 None）：不能静默让 job 永挂 running
        # （singleflight 死锁态，审计 P2-2）——置 error 并 log，
        # 冷却期后或 force 请求可重新提交计算。
        logger.warning(
            "quant job %s compute 返回非 dict（%r），置 error",
            key, type(payload).__name__)
        _JOBS[key] = {"status": "error",
                      "error": f"compute returned non-dict: {type(payload).__name__}",
                      "startedAt": started, "ts": time.time()}
