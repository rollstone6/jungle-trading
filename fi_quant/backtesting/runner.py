"""统一回测入口，支持多策略。"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

# 支持直接 `python backtesting/runner.py` 运行：
# 脚本方式执行时 sys.path[0] 是 backtesting/ 本身，包内相对导入会失败，这里把 fi_quant
# 根目录补进搜索路径（-m 方式不受影响）。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtesting.engine import BacktestConfig, UnifiedBacktester
from config import STOCK_LIST, StrategyConfig
from core.strategy_base import StrategyBase
from data.fetcher import load_real_data
from strategies.breakout import BreakoutPullbackEngine
from strategies.mean_reversion import MeanReversionEngine
from strategies.accumulation import AccumulationEngine

#: 策略注册表
STRATEGY_REGISTRY: dict[str, type[StrategyBase]] = {
    "breakout": BreakoutPullbackEngine,
    "mean_reversion": MeanReversionEngine,
    "accumulation": AccumulationEngine,
}


def run_backtest(
    strategy_name: str = "breakout",
    start_date: str = "20230101",
    end_date: str | None = None,
    output_dir: str | Path = "backtest_results",
    verbose: bool = True,
    symbols: set[str] | None = None,
) -> dict[str, object]:
    """回测股票池中的指定策略。

    Args:
        strategy_name: 策略名称（breakout/mean_reversion/accumulation）
        start_date: 回测开始日期
        end_date: 回测结束日期
        output_dir: 输出目录
        verbose: 是否输出详细信息
        symbols: 仅回测指定股票代码集合

    Returns:
        回测结果字典
    """
    strategy_cls = STRATEGY_REGISTRY.get(strategy_name)
    if strategy_cls is None:
        raise ValueError(
            f"未知策略: {strategy_name}，"
            f"可选: {list(STRATEGY_REGISTRY.keys())}"
        )

    strategy_config = StrategyConfig(signal_timeframe="1d")
    engine = strategy_cls(strategy_config)
    lookback = getattr(
        strategy_config, "data_lookback_breakout", 365
    )
    fetch_start = (
        pd.Timestamp(start_date) - timedelta(days=lookback)
    ).strftime("%Y%m%d")
    price_data: dict[str, pd.DataFrame] = {}
    signals: dict[str, pd.DataFrame] = {}
    failures: list[dict[str, str]] = []
    selected_stocks = [
        stock for stock in STOCK_LIST
        if symbols is None or stock["symbol"] in symbols
    ]

    for index, stock in enumerate(selected_stocks, start=1):
        symbol = stock["symbol"]
        if symbol in price_data:
            continue
        if verbose:
            print(
                f"[{index}/{len(selected_stocks)}] "
                f"回测数据: {stock['name']} ({symbol})"
            )
        try:
            frame = load_real_data(
                symbol, fetch_start, timeframe="1d", verbose=verbose
            )
            if frame.empty:
                failures.append(
                    {"symbol": symbol, "reason": "行情数据为空"}
                )
                continue
            signal_frame = engine.scan_signals(
                frame.copy(), symbol=symbol, debug=False
            )
        except Exception as error:
            failures.append({"symbol": symbol, "reason": str(error)})
            if verbose:
                print(f"  跳过 {symbol}: {error}")
            continue
        price_data[symbol] = frame
        if not signal_frame.is_empty():
            # 转换为回测引擎所需的 DataFrame 格式
            signals[symbol] = signal_frame.to_backtest_format(symbol)

    if not price_data:
        detail = "; ".join(
            f"{item['symbol']}: {item['reason']}" for item in failures
        )
        raise RuntimeError(f"没有成功获取可回测行情。{detail}")

    result = UnifiedBacktester(BacktestConfig(
        max_holding_bars=strategy_config.hold_bars,
        stop_loss_pct=strategy_config.stop_loss_pct,
    )).run(price_data, signals, start_date=start_date, end_date=end_date)
    result["failures"] = pd.DataFrame(failures)
    _save_result(result, output_dir)
    return result


def _save_result(result: dict[str, object], output_dir: str | Path):
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    trades = result.get("trades")
    if isinstance(trades, pd.DataFrame):
        trades.to_csv(
            directory / "trades.csv", index=False, encoding="utf-8-sig"
        )
    equity = result.get("equity_curve")
    if isinstance(equity, pd.DataFrame):
        equity.to_csv(
            directory / "equity_curve.csv",
            index=False,
            encoding="utf-8-sig",
        )
    metrics = result.get("metrics")
    if metrics is not None:
        pd.DataFrame([metrics]).to_csv(
            directory / "metrics.csv", index=False, encoding="utf-8-sig"
        )
    failures = result.get("failures")
    if isinstance(failures, pd.DataFrame) and not failures.empty:
        failures.to_csv(
            directory / "failures.csv", index=False, encoding="utf-8-sig"
        )


def main():
    parser = argparse.ArgumentParser(description="统一策略回测")
    parser.add_argument(
        "--strategy",
        default="breakout",
        choices=list(STRATEGY_REGISTRY.keys()),
        help="策略名称",
    )
    parser.add_argument("--start-date", default="20230101")
    parser.add_argument("--end-date")
    parser.add_argument("--output-dir", default="backtest_results")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--symbols",
        help="仅回测指定代码，多个代码用逗号分隔",
    )
    args = parser.parse_args()
    symbols = set(args.symbols.split(",")) if args.symbols else None
    result = run_backtest(
        strategy_name=args.strategy,
        start_date=args.start_date,
        end_date=args.end_date,
        output_dir=args.output_dir,
        verbose=not args.quiet,
        symbols=symbols,
    )
    metrics = result.get("metrics")
    if metrics is not None:
        print(pd.Series(metrics).to_string())
    print(f"结果已保存到: {Path(args.output_dir).resolve()}")


if __name__ == "__main__":
    main()