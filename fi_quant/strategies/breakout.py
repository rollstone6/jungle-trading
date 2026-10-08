"""
核心策略引擎模块
- 技术指标计算（TA-Lib）
- 突破回踩信号扫描
"""

import pandas as pd
import talib

from core.signal import Signal, SignalFrame
from core.strategy_base import StrategyBase


class BreakoutPullbackEngine(StrategyBase):
    """突破回踩策略引擎"""

    name = "突破回踩"

    def prepare_indicators(self, df):
        """批量计算技术指标"""
        close = df['close'].values.astype(float)
        volume = df['volume'].values.astype(float)

        # 1. OBV 及 OBV 均线
        df['obv'] = talib.OBV(close, volume)
        df['obv_ma10'] = talib.SMA(df['obv'].values, timeperiod=self.cfg.obv_ma_short)
        df['obv_ma30'] = talib.SMA(df['obv'].values, timeperiod=self.cfg.obv_ma_long)

        # 2. MACD
        macd, macdsignal, _ = talib.MACD(
            close, fastperiod=12, slowperiod=26, signalperiod=9
        )
        df['macd'] = macd
        df['macd_signal'] = macdsignal

        # 3. 布林带宽度
        upper, middle, lower = talib.BBANDS(
            close, timeperiod=20, nbdevup=2, nbdevdn=2
        )
        df['boll_width'] = (upper - lower) / middle

        # 4. RSI
        df['rsi'] = talib.RSI(close, timeperiod=self.cfg.rsi_period)

        # 5. 30日成交量均线
        df['vol_ma30'] = talib.SMA(volume, timeperiod=self.cfg.vol_ma_window)

        return df

    def scan_signals(self, df, symbol: str = "", debug=False) -> SignalFrame:
        """扫描满足条件的交易信号。

        Args:
            df: 行情数据。
            symbol: 股票代码。
            debug: 是否输出调试信息。

        Returns:
            SignalFrame: 统一信号集合。
        """
        df = self.prepare_indicators(df)
        signals: list[Signal] = []

        # 调试计数器
        stats = {
            'total_candidates': 0,
            'pass_box_width': 0,
            'pass_price': 0,
            'pass_volume': 0,
            'pass_obv': 0,
            'pass_momentum': 0,
            'pass_pullback': 0,
        }

        start_idx = self.cfg.box_bars + 10
        for i in range(start_idx, len(df)):
            today_data = df.iloc[i]

            for t_back in range(self.cfg.stand_bars, 9):
                breakout_idx = i - t_back
                if breakout_idx < self.cfg.box_bars:
                    continue

                stats['total_candidates'] += 1

                # 箱体切片
                box_data = df.iloc[
                    breakout_idx - self.cfg.box_bars: breakout_idx
                ]
                box_high = box_data['high'].max()
                box_low = box_data['low'].min()
                box_width = (box_high - box_low) / box_low * 100

                if box_width > self.cfg.max_box_width:
                    continue
                stats['pass_box_width'] += 1

                breakout_row = df.iloc[breakout_idx]

                # --- 条件一：价格突破 ---
                if self.cfg.use_price_filter:
                    price_ok = (
                        breakout_row['close']
                        >= box_high * (1 + self.cfg.breakout_pct)
                    )
                    stand_slice = df.iloc[
                        breakout_idx + 1:
                        breakout_idx + 1 + self.cfg.stand_bars
                    ]['close']
                    if len(stand_slice) > 0:
                        above_count = (stand_slice > box_high).sum()
                        stand_ok = above_count >= len(stand_slice) // 2
                    else:
                        stand_ok = False
                    if not (price_ok and stand_ok):
                        continue
                stats['pass_price'] += 1

                # --- 条件二：量能放量 ---
                if self.cfg.use_volume_filter:
                    vol_ok = (
                        breakout_row['volume']
                        >= breakout_row['vol_ma30'] * self.cfg.vol_ratio
                    )
                    if not vol_ok:
                        continue
                stats['pass_volume'] += 1

                # --- 条件三：OBV 量能潮 ---
                obv_alert = ""  # OBV破位警报
                if self.cfg.use_obv_filter:
                    # 1. 突破日OBV创箱体期新高
                    obv_new_high = (
                        breakout_row['obv'] >= box_data['obv'].max()
                    )
                    # 2. OBV回撤检查
                    obv_pullback = (
                        (breakout_row['obv'] - today_data['obv'])
                        / abs(breakout_row['obv'] + 1e-5)
                    )
                    obv_pullback_ok = (
                        obv_pullback <= self.cfg.obv_pullback_limit
                    )
                    # 3. OBV多头锁仓：OBV > MA10 且 OBV > MA30
                    obv_above_ma = True
                    if self.cfg.use_obv_trend_filter:
                        obv_above_ma10 = (
                            breakout_row['obv'] > breakout_row['obv_ma10']
                        )
                        obv_above_ma30 = (
                            breakout_row['obv'] > breakout_row['obv_ma30']
                        )
                        obv_above_ma = obv_above_ma10 and obv_above_ma30
                    obv_ok = (
                        obv_new_high and obv_pullback_ok and obv_above_ma
                    )
                    if not obv_ok:
                        continue
                # 4. OBV破位警报（卖出警告）
                if self.cfg.use_obv_breakdown_alert:
                    alerts = []
                    if today_data['obv'] < today_data['obv_ma10']:
                        alerts.append("⚠️OBV破位MA10")
                    if today_data['obv'] < today_data['obv_ma30']:
                        alerts.append("⚠️OBV破位MA30")
                    obv_alert = " | ".join(alerts) if alerts else ""
                stats['pass_obv'] += 1

                # --- 条件四：动能辅助 ---
                if self.cfg.use_momentum_filter:
                    macd_ok = (
                        today_data['macd'] > 0
                        and today_data['macd_signal'] > 0
                    )
                    boll_ma = df['boll_width'].iloc[i - 10:i].mean()
                    boll_ok = today_data['boll_width'] > boll_ma
                    rsi_ok = today_data['rsi'] < self.cfg.rsi_overbought
                    if not (macd_ok and boll_ok and rsi_ok):
                        continue
                stats['pass_momentum'] += 1

                # --- 回踩支撑判定 ---
                pullback_ratio = (
                    (today_data['close'] - box_high) / box_high
                )
                max_pullback = (
                    self.cfg.breakout_pct * self.cfg.pullback_limit
                )
                # 放宽：允许回踩到箱顶下方5%
                min_pullback = -0.05
                if min_pullback <= pullback_ratio <= max_pullback:
                    stats['pass_pullback'] += 1
                    metadata = {
                        'box_high': round(box_high, 2),
                        'box_low': round(box_low, 2),
                        'box_width': f"{round(box_width, 2)}%",
                        'pullback': "完美回踩",
                    }
                    if obv_alert:
                        metadata['obv_alert'] = obv_alert
                    signals.append(Signal(
                        date=pd.Timestamp(today_data['date']),
                        symbol=symbol,
                        close=round(float(today_data['close']), 2),
                        strategy_name=self.name,
                        stop_loss=round(float(today_data['close']) * (1 - self.cfg.stop_loss_pct), 2),
                        metadata=metadata,
                    ))
                    break

        if debug:
            print(f"     [调试] 候选:{stats['total_candidates']}"
                  f"  箱宽:{stats['pass_box_width']}"
                  f"  价格:{stats['pass_price']}"
                  f"  量能:{stats['pass_volume']}"
                  f"  OBV:{stats['pass_obv']}"
                  f"  动能:{stats['pass_momentum']}"
                  f"  回踩:{stats['pass_pullback']}")

        return SignalFrame(signals)