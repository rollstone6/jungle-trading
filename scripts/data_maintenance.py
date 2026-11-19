#!/usr/bin/env python3
"""data/ 目录统一维护入口：快照体检 + 缓存清理 + 快照提交辅助。

用法（默认 --dry-run，只报告不动盘）:
    python3 scripts/data_maintenance.py                 # 体检报告
    python3 scripts/data_maintenance.py --apply         # 清理过期缓存（备份先行）
    python3 scripts/data_maintenance.py --commit-snapshots --apply
                                                        # 白名单快照有 diff 时 chore(data) 提交（不 push）
    python3 scripts/data_maintenance.py --max-age-days 7 --stale-days 3
    python3 scripts/data_maintenance.py --data-dir /tmp/x/data --backup-dir /tmp/x/bk --repo /tmp/x
                                                        # 隔离实测用

数据分类（逐个核实生成代码，2026-11-19 t_f912f559 勘察）:

  SNAPSHOT（git 跟踪、服务定时刷新、--commit-snapshots 白名单）:
    data/etf_flow.json        <- app/services/etf_flow.py RESULT_FILE (L24)
                                  由 scripts/refresh_etf.py crontab 19:30 刷新
    data/etf_share_snap.json  <- app/services/etf_flow.py SNAP_FILE (L23)
                                  深市份额滚动快照，同上 producer

  CACHE（gitignored、可安全再生、--apply 过期才清）:
    data/regime_scan.json     <- app/main.py REGIME_CACHE_FILE (L642)，miss 时现场重扫落盘
    data/research_*.json      <- app/main.py _research_produce cache_file (L893)，6h TTL 自动重拉
    data/quant_scan_*.json    <- app/services/quant_scan.py (L153/L197)，当日有效重扫重建
    data/pipeline_result.json <- app/services/quant_pipeline.py RESULT_PATH (L29)，lab 管线重跑重建
    data/valuation_*.pkl      <- app/services/valuation.py _cache_path (L153-159)，6h TTL 重算重建

  PROTECTED（任何模式绝不删除/覆盖）:
    data/jungle.db            持仓 SQLite 主库（本脚本只读 stat，不 open 不 copy 不 touch）
    data/jungle.duckdb*       量化底座数据库（重灌代价高，不列清理白名单）
    data/astock_universe.json git 跟踪的手动全A清单（代码只读不写）
    data/chip_scan_*.jsonl    git 跟踪历史留档（无自动 writer）

  UNKNOWN（不在任何名单）: 只报告，永不触碰 —— 新增数据文件须先在此登记分类。

安全约定: 清理 = 白名单 glob 命中 + mtime 超期 双条件；删除前先 copy2 到
--backup-dir（默认 /root/backups/data-maint-<UTC+8 ts>/），备份失败即中止。
提交 = 仅 git add 白名单快照路径，message 走 chore(data)，绝不 push。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parents[1]

# ---- 分类白名单（glob，相对 data/） -------------------------------------
SNAPSHOT_GLOBS = [
    "etf_flow.json",
    "etf_share_snap.json",
]
CACHE_GLOBS = [
    "regime_scan.json",
    "research_*.json",
    "quant_scan_*.json",
    "pipeline_result.json",
    "valuation_*.pkl",
]
PROTECTED_GLOBS = [
    "jungle.db",
    "jungle.db-*",
    "jungle.duckdb",
    "jungle.duckdb*",
    "astock_universe.json",
    "chip_scan_*.jsonl",
]

# 快照内可读到的新鲜度字段（体检展示用，取第一个命中的）
FRESHNESS_KEYS = ("updated_at", "_updated_at", "data_date", "generated_at", "date")


def classify(name: str) -> str:
    for label, globs in (
        ("snapshot", SNAPSHOT_GLOBS),
        ("protected", PROTECTED_GLOBS),
        ("cache", CACHE_GLOBS),
    ):
        if any(fnmatch.fnmatch(name, g) for g in globs):
            return label
    return "unknown"


def git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def git_tracked(repo: Path, rel: str) -> bool:
    return bool(git(repo, "ls-files", "--error-unmatch", rel).strip())


def git_ignored(repo: Path, rel: str) -> bool:
    return bool(git(repo, "check-ignore", rel).strip())


def read_freshness(path: Path) -> str:
    """尽量从 JSON 里抠一个时间戳字段；pkl/二进制返回 '-'。"""
    if path.suffix != ".json":
        return "-"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "parse_err"
    if isinstance(data, dict):
        for k in FRESHNESS_KEYS:
            if k in data:
                return f"{k}={data[k]}"
    return "-"


def human_size(nbytes: int) -> str:
    n = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{nbytes}B"


def fmt_age(seconds: float) -> str:
    d = seconds / 86400
    return f"{d:.1f}天" if d >= 1 else f"{seconds/3600:.1f}小时"


def audit(data_dir: Path, repo: Path, now: float, stale_days: float) -> list[dict]:
    rows = []
    for p in sorted(data_dir.iterdir()):
        if not p.is_file():
            continue
        rel = f"data/{p.name}"
        cls = classify(p.name)
        age = now - p.stat().st_mtime
        tracked = git_tracked(repo, rel)
        ignored = git_ignored(repo, rel) if not tracked else False
        if cls == "cache":
            expect = "gitignored" if ignored else "⚠ 应被 ignore 但未被"
        elif cls == "snapshot":
            expect = "tracked" if tracked else "⚠ 应被 git 跟踪但未跟踪"
        else:
            expect = "tracked" if tracked else ("ignored" if ignored else "untracked")
        expired = age > stale_days * 86400
        rows.append({
            "name": p.name, "cls": cls, "size": p.stat().st_size,
            "age_s": age, "fresh": read_freshness(p), "git": expect,
            "action": action_for(cls, expired, tracked),
            "expired": expired,
        })
    return rows


def action_for(cls: str, expired: bool, tracked: bool) -> str:
    if cls == "cache":
        return "过期→可清理" if expired else "新鲜→保留"
    if cls == "snapshot":
        return "保留（git 数据资产）"
    if cls == "protected":
        return "绝不触碰"
    return "未登记→只报告不触碰"


def print_report(rows: list[dict], stale_days: float, apply_mode: bool) -> None:
    print(f"\n=== data/ 快照体检（{len(rows)} 个文件，过期阈值 {stale_days} 天，模式 {'APPLY' if apply_mode else 'DRY-RUN'}）===\n")
    order = {"snapshot": 0, "cache": 1, "protected": 2, "unknown": 3}
    for r in sorted(rows, key=lambda x: (order[x["cls"]], x["name"])):
        print(f"  [{r['cls']:>9}] {r['name']:<45} {human_size(r['size']):>8}  "
              f"mtime {fmt_age(r['age_s']):>7}前  git:{r['git']:<10} {r['fresh']}")
        print(f"              → {r['action']}")
    n_clean = sum(1 for r in rows if r["cls"] == "cache" and r["expired"])
    n_cache = sum(1 for r in rows if r["cls"] == "cache")
    n_unknown = sum(1 for r in rows if r["cls"] == "unknown")
    print(f"\n小结: 缓存类 {n_cache} 个（其中过期 {n_clean} 个），"
          f"快照 {sum(1 for r in rows if r['cls']=='snapshot')} 个，"
          f"保护 {sum(1 for r in rows if r['cls']=='protected')} 个，"
          f"未登记 {n_unknown} 个。")
    if n_unknown:
        print("⚠ 未登记文件需人工核实生成方后补进本脚本白名单，维护工具永不自动触碰它们。")


def clean_expired(rows: list[dict], data_dir: Path, backup_dir: Path,
                  apply_mode: bool) -> int:
    targets = [r for r in rows if r["cls"] == "cache" and r["expired"]]
    if not targets:
        print("\n[清理] 无过期缓存，跳过。")
        return 0
    if not apply_mode:
        print(f"\n[清理] DRY-RUN：{len(targets)} 个过期缓存将被清理（--apply 执行，清理前整文件备份到 {backup_dir}）：")
        for r in targets:
            print(f"  - data/{r['name']} ({human_size(r['size'])}, {fmt_age(r['age_s'])}前)")
        return 0
    backup_dir.mkdir(parents=True, exist_ok=True)
    import shutil
    removed = 0
    print(f"\n[清理] APPLY：备份 → {backup_dir}")
    for r in targets:
        src = data_dir / r["name"]
        dst = backup_dir / r["name"]
        shutil.copy2(src, dst)               # 备份先行
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            print(f"  ✗ 备份校验失败，中止且不删除: {r['name']}")
            return removed
        src.unlink()
        removed += 1
        print(f"  ✓ 已备份并清理 data/{r['name']}")
    print(f"[清理] 完成：{removed} 个文件。回滚 = 从备份目录 copy 回 data/ 即可。")
    return removed


def commit_snapshots(rows: list[dict], repo: Path, data_dir: Path,
                     apply_mode: bool) -> int:
    """对白名单快照做 chore(data) 提交。仅 git add 明确路径，绝不 push。"""
    snap_names = [r["name"] for r in rows if r["cls"] == "snapshot"]
    rels = [f"data/{n}" for n in snap_names]
    status = git(repo, "status", "--porcelain", "--", *rels)
    changed = [line[3:] for line in status.splitlines() if line[3:].strip()]
    if not changed:
        print("\n[提交] 白名单快照无 diff，跳过。")
        return 0
    ts = datetime.now().strftime("%Y%m%d")
    msg = (f"chore(data): ETF快照定时刷新 {ts}\n\n"
           f"Agent-Task: scripts/data_maintenance.py --commit-snapshots\n"
           f"Agent-Model: data-maintenance\n"
           f"Agent-Decision: 仅提交 SNAPSHOT_GLOBS 白名单文件，运行时缓存已 gitignore\n")
    if not apply_mode:
        print(f"\n[提交] DRY-RUN：以下快照有 diff，--apply 时会 chore(data) 提交（不 push）：")
        for c in changed:
            print(f"  - {c}")
        return 0
    # 双保险：add 路径逐一复核在白名单内
    safe = [c for c in changed if classify(Path(c).name) == "snapshot"
            and git_tracked(repo, c)]
    if not safe:
        print("\n[提交] diff 文件均不在快照白名单，拒绝提交。")
        return 1
    subprocess.run(["git", "-C", str(repo), "add", "--", *safe], check=True)
    res = subprocess.run(["git", "-C", str(repo), "commit", "-m", msg],
                         capture_output=True, text=True)
    print(f"\n[提交] git commit rc={res.returncode}: {res.stdout.strip().splitlines()[0] if res.stdout.strip() else res.stderr.strip()}")
    print("[提交] 未执行 push（按约定由人工/终验卡决定）。")
    return res.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="jungle-trading data/ 维护工具（体检/清理/快照提交）")
    ap.add_argument("--repo", default=str(REPO_DEFAULT), help="仓库根（默认脚本所在仓库）")
    ap.add_argument("--data-dir", default=None, help="数据目录（默认 <repo>/data，实测可指临时目录）")
    ap.add_argument("--backup-dir", default=None, help="清理备份目录（默认 /root/backups/data-maint-<ts>）")
    ap.add_argument("--apply", action="store_true", help="真正执行删除/提交（默认 dry-run）")
    ap.add_argument("--dry-run", action="store_true", help="显式 dry-run（默认行为，仅为可读性）")
    ap.add_argument("--max-age-days", type=float, default=7.0,
                    help="缓存过期阈值（天，默认7；mtime 超期才算清理候选）")
    ap.add_argument("--stale-days", type=float, default=None,
                    help="体检过期阈值（天，默认与 --max-age-days 相同）")
    ap.add_argument("--clean", action="store_true", help="执行缓存清理段（--apply 时默认包含）")
    ap.add_argument("--commit-snapshots", action="store_true", help="执行快照提交辅助段")
    args = ap.parse_args()

    apply_mode = args.apply and not args.dry_run  # 同给时 dry-run 优先，安全侧
    repo = Path(args.repo).resolve()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else repo / "data"
    stale_days = args.stale_days if args.stale_days is not None else args.max_age_days
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(args.backup_dir) if args.backup_dir else Path(f"/root/backups/data-maint-{ts}")

    if not data_dir.is_dir():
        print(f"✗ data 目录不存在: {data_dir}", file=sys.stderr)
        return 2
    print(f"repo={repo}\ndata={data_dir}\nmode={'APPLY' if apply_mode else 'DRY-RUN'}")

    now = time.time()
    rows = audit(data_dir, repo, now, stale_days)
    print_report(rows, stale_days, apply_mode)

    rc = 0
    if args.clean or apply_mode:
        clean_expired(rows, data_dir, backup_dir, apply_mode)
    if args.commit_snapshots:
        rc = commit_snapshots(rows, repo, data_dir, apply_mode)
    return rc


if __name__ == "__main__":
    sys.exit(main())
