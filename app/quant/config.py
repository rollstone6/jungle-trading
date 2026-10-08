"""
策略参数配置模块
==================
策略开关和参数统一管理中心

策略列表：
1. 突破回踩策略（原有）
2. 产业链动量滞后策略（Lead-Lag）
3. 铜价配对交易策略（Pair Trading）
4. 聪明资金追踪策略（Smart Money）
5. 均值回归+周期共振策略（Mean Reversion）
"""


class StrategyConfig:
    """策略参数配置类（带条件控制开关）"""

    def __init__(self, signal_timeframe="60m"):
        # ================================================================
        #                    策略总开关（在这里启用/禁用策略）
        # ================================================================
        self.enable_breakout_pullback = True   # 原有：突破回踩策略
        self.enable_lead_lag = False           # 策略一：产业链动量滞后
        self.enable_pair_trading = False       # 策略二：铜价配对交易
        self.enable_smart_money = False        # 策略三：聪明资金追踪
        self.enable_mean_reversion = False     # 策略四：均值回归+周期共振
        self.enable_accumulation = False       # 策略五：60分钟主力吸筹

        # ================================================================
        #                    时间框架配置
        # ================================================================
        self.signal_timeframe = signal_timeframe

        # ================================================================
        # 原有策略参数：突破回踩策略
        # 逻辑位置：strategy.py -> BreakoutPullbackEngine.scan_signals()
        # ================================================================
        # --- 核心开关：控制是否启用某些条件筛选 ---
        self.use_price_filter = True       # 价格突破 >= 1% 且站稳
        self.use_volume_filter = True      # 突破日放量 >= 30日均量 x 1.2
        self.use_obv_filter = True         # OBV创新高且回踩回撤 <= 50%
        self.use_momentum_filter = False   # MACD零轴上 + 布林带扩张 + RSI非超买

        # --- 根据 signal_timeframe 加载不同参数集 ---
        if self.signal_timeframe == "60m":
            # 60分钟K线参数
            self.box_bars = 48             # 箱体周期（48根60分钟≈12个交易日）
            self.breakout_pct = 0.005      # 突破幅度 0.5%
            self.stand_bars = 2            # 突破后需站稳的K线数
            self.vol_ma_window = 20        # 成交量均线周期
            self.vol_ratio = 1.5           # 突破日放量倍数
            self.hold_bars = 20            # 最大持仓K线数
            self.obv_ma_short = 10         # OBV短期均线
            self.obv_ma_long = 30          # OBV长期均线

        elif self.signal_timeframe == "1d":
            # 日线参数
            self.box_bars = 60             # 箱体周期（60个交易日）
            self.breakout_pct = 0.01       # 突破幅度 1%
            self.stand_bars = 1            # 突破后需站稳的天数
            self.vol_ma_window = 30        # 成交量均线周期
            self.vol_ratio = 1.2           # 突破日放量倍数
            self.hold_bars = 20            # 最大持仓天数
            self.obv_ma_short = 10         # OBV短期均线
            self.obv_ma_long = 30          # OBV长期均线

        else:
            raise ValueError(
                f"不支持的 signal_timeframe: {self.signal_timeframe}，"
                f"可选值: '1d', '60m'"
            )

        # --- 不受时间框架影响的参数（百分比/比率类）---
        self.max_box_width = 35.0          # 允许的最大箱体震荡幅度（%）
        self.pullback_limit = 0.50         # 回踩深度不超过突破波幅的 50%
        self.obv_pullback_limit = 0.50     # OBV最大允许回撤
        self.use_obv_trend_filter = True   # OBV多头锁仓过滤（OBV > MA10 且 OBV > MA30）
        self.use_obv_breakdown_alert = True  # OBV破位警报（卖出警告）

        # --- 动能参数 ---
        self.rsi_period = 14               # RSI周期
        self.rsi_overbought = 75           # RSI超买界限

        # --- 风控参数 ---
        self.stop_loss_pct = 0.03          # 硬止损线 (3%)
        self.trailing_stop_pct = 0.05      # 移动止盈触发线 (5%)

        # ================================================================
        # 日线趋势预筛选参数（策略运行前自动过滤股票池）
        # 逻辑位置：trend_filter.py -> TrendFilter
        # ================================================================
        self.enable_trend_filter = True    # 预筛选总开关

        # 各策略过滤模式：
        #   "strong"   = 强趋势（均线多头+20日新高），适合趋势策略
        #   "loose"    = 宽松（只检查数据量充足），适合非趋势策略
        #   "oversold" = 反向（股价低于MA20），适合均值回归类策略
        self.filter_mode_breakout = "strong"
        self.filter_mode_lead_lag = "strong"
        self.filter_mode_pair = "loose"
        self.filter_mode_smart = "loose"
        self.filter_mode_mean_rev = "oversold"
        self.filter_mode_accumulation = "loose"

        # 各策略数据回溯天数（自动计算 start_date）
        # 确保数据量足够策略计算所需的最长 lookback
        self.data_lookback_breakout = 180       # ~6个月
        self.data_lookback_lead_lag = 180       # ~6个月
        self.data_lookback_pair = 300           # ~10个月（ratio_ma=120）
        self.data_lookback_smart = 180          # ~6个月
        self.data_lookback_mean_rev = 480       # ~16个月（ma250+余量）
        self.data_lookback_accumulation = 180   # ~6个月

        # ① 均线多头（权重最高）: MA5 > MA10 > MA20 且 Close > MA5
        self.trend_ma_short = 5             # 短期均线
        self.trend_ma_mid = 10             # 中期均线
        self.trend_ma_long = 20            # 长期均线

        # ② MA20向上: MA20(today) > MA20(5日前)
        self.trend_ma20_lookback = 5       # MA20对比天数

        # ③ 创20日新高附近: Close >= Highest20 * 0.97
        self.trend_high_days = 20          # 新高观察周期
        self.trend_high_threshold = 0.97   # 新高阈值

        # ④ 成交量不过度萎缩: Vol > MA5Vol * 0.8
        self.trend_vol_ma_days = 5         # 成交量均线周期
        self.trend_vol_min_ratio = 0.8     # 最小量能比例

        # ================================================================
        # 策略一参数：产业链动量滞后（Lead-Lag）
        # 逻辑位置：strategy_lead_lag.py -> LeadLagEngine.scan_signals()
        # 核心逻辑：下游PCB龙头启动 -> 上游覆铜板滞后 -> 左侧潜伏
        # ================================================================
        self.downstream_symbols = [
            "002463",  # 沪电股份（PCB）
            "002916",  # 深南电路（PCB）
            "002384",  # 东山精密（PCB）
        ]
        self.downstream_days = 3           # 下游观察天数
        self.downstream_threshold = 0.08   # 下游涨幅阈值 8%
        self.upstream_lag_threshold = 0.05 # 上游滞后偏差 5%
        self.obv_turn_days = 3             # OBV勾头观察天数

        # ================================================================
        # 策略二参数：铜价配对交易（Pair Trading）
        # 逻辑位置：strategy_pair.py -> PairTradingEngine.scan_signals()
        # 核心逻辑：铜价高位破位 + 生益被错杀 -> 成本压力减轻 -> 买入
        # ================================================================
        self.copper_symbol = "CU.SHF"      # 沪铜期货代码
        self.copper_ma_days = 20           # 铜价均线天数
        self.ratio_ma_days = 120           # Ratio均值天数
        self.ratio_std_threshold = -2.0    # Ratio偏离标准差（负值表示低估）
        self.box_lower_pct = 0.20          # 箱体下沿20%范围

        # ================================================================
        # 策略三参数：聪明资金追踪（Smart Money）
        # 逻辑位置：strategy_smart.py -> SmartMoneyEngine.scan_signals()
        # 核心逻辑：外资逆势抄底 + 融资盘清洗 + 跌破下轨 -> 黄金坑
        # ================================================================
        self.north_flow_days = 3           # 北向资金连续流入天数
        self.margin_drop_threshold = 0.15  # 融资余额骤降阈值 15%
        self.margin_drop_days = 5          # 融资余额观察天数
        self.boll_break_threshold = -0.02  # 跌破布林下轨阈值

        # ================================================================
        # 策略四参数：均值回归+周期共振（Mean Reversion）
        # 逻辑位置：strategy_mean_rev.py -> MeanReversionEngine.scan_signals()
        # 核心逻辑：大周期支撑 + RSI超卖 + 底背离 -> 均值回归
        # ================================================================
        self.ma_long_days = 120            # 长期均线（半年线）
        self.ma_year_days = 250            # 年线
        self.ma_deviation = 0.02           # 均线偏离度 ±2%
        self.rsi_oversold = 25             # RSI极度超卖
        self.macd_divergence_days = 10     # MACD底背离观察天数

        # ================================================================
        # 策略五参数：60分钟主力吸筹模型（Accumulation）
        # 逻辑位置：strategy_accumulation.py -> AccumulationEngine.scan_signals()
        # 核心逻辑：日线趋势过滤 + 60分钟吸筹评分 + 突破确认
        # ================================================================
        # 周期参数
        self.acc_low_period = 40           # 低位计算周期（40根60分钟K线）
        self.acc_obv_period = 40           # OBV观察周期

        # 分数阈值
        self.acc_min_score = 70            # 最低吸筹分数（0-100分）

        # 低位承接参数（30分）
        self.acc_position_threshold = 0.35  # 低位阈值（position < 0.35 得分）

        # OBV参数（20分）
        self.acc_obv_ma = 10               # OBV均线周期

        # 成交量参数（15分）
        self.acc_volume_ratio = 1.3        # 放量倍数（突破确认用）

        # RSI参数（10分）
        self.acc_rsi_min = 30              # RSI下限
        self.acc_rsi_max = 55              # RSI上限

        # 风控参数
        self.acc_stop_loss = 0.03          # 止损 3%
        self.acc_take_profit = 0.08        # 止盈 8%

        # ================================================================
        # 筹码分布分析参数（Chip Distribution）
        # 逻辑位置：utils/chip_distribution.py -> ChipDistributionAnalyzer
        # 核心逻辑：三角分布+换手衰减 -> 集中度指标 -> 筛选/评分
        # ================================================================
        # 筹码计算参数
        self.chip_lookback_days = 120      # 筹码计算回溯天数
        self.chip_price_step = 0.01        # 价格网格步长（1%）
        self.chip_decay_factor = 0.03      # 默认换手衰减系数（无换手率时用）

        # 集中度筛选阈值
        self.chip_conc90_max = 12.0        # 90%筹码集中度 < 12%
        self.chip_conc70_max = 8.0         # 70%筹码集中度 < 8%
        self.chip_peak_ratio_min = 0.65    # 主峰占比 > 65%
        self.chip_kurtosis_min = 2.5       # 峰度 > 2.5（分布越尖锐分数越高）
        self.chip_skewness_range = (-0.6, 0.6)  # 偏度范围（接近0为对称正态）
        self.chip_min_score = 65           # 最低综合评分（0-100）

        # 中心重合度（90%区间中心与70%区间中心的偏离度）
        # 偏离越小，筹码峰越对称、越集中于同一中心
        self.chip_center_offset_max = 1.0   # 偏离 ≤ 1% 得满分（10分）
        self.chip_center_offset_zero = 3.0  # 偏离 > 3% 得0分

        # 吸筹策略筹码过滤开关
        self.enable_chip_filter = True     # 吸筹策略中启用筹码过滤
        self.chip_score_bonus = 10         # 筹码集中度在吸筹评分中的加分权重

    def __repr__(self):
        return (
            f"Config(\n"
            f"  突破回踩={self.enable_breakout_pullback}, "
            f"动量滞后={self.enable_lead_lag}, "
            f"配对交易={self.enable_pair_trading}, "
            f"聪明资金={self.enable_smart_money}, "
            f"均值回归={self.enable_mean_reversion}, "
            f"主力吸筹={self.enable_accumulation}\n"
            f"  价格={self.use_price_filter}, "
            f"量能={self.use_volume_filter}, "
            f"OBV={self.use_obv_filter}, "
            f"动能={self.use_momentum_filter})"
        )


# ================================================================
#                    股票池配置
# ================================================================

# 策略一：产业链动量滞后（区分上下游）
STOCK_LIST_LEAD_LAG = [
    {"symbol": "600183", "name": "生益科技", "role": "upstream"},
    {"symbol": "002463", "name": "沪电股份", "role": "downstream"},
    {"symbol": "002916", "name": "深南电路", "role": "downstream"},
    {"symbol": "002384", "name": "东山精密", "role": "downstream"},
]

# 统一股票池（所有策略共用，去重合并）
STOCK_LIST = [
    {"symbol": "600183", "name": "生益科技"},
    {"symbol": "688981", "name": "中芯国际"},
    {"symbol": "600206", "name": "有研新材"},
    {"symbol": "300394", "name": "天孚通信"},
    {"symbol": "601366", "name": "利群股份"},
    {"symbol": "301658", "name": "首航新能"},
    {"symbol": "601615", "name": "明阳智能"},
    {"symbol": "301666", "name": "大普微-UW"},
    {"symbol": "300823", "name": "建科智能"},
    {"symbol": "300674", "name": "宇信科技"},
    {"symbol": "002796", "name": "世嘉科技"},
    {"symbol": "002290", "name": "禾盛新材"},
    {"symbol": "603228", "name": "景旺电子"},
    {"symbol": "600643", "name": "爱建集团"},
    {"symbol": "300390", "name": "天华新能"},
    {"symbol": "688017", "name": "绿的谐波"},
    {"symbol": "301632", "name": "广东建科"},
    {"symbol": "300098", "name": "高新兴"},
    {"symbol": "603007", "name": "顺景科技"},
    {"symbol": "300505", "name": "川金诺"},
    {"symbol": "001248", "name": "华润新能"},
    {"symbol": "600353", "name": "旭光电子"},
    {"symbol": "688323", "name": "瑞华泰"},
    {"symbol": "603629", "name": "利通电子"},
    {"symbol": "600118", "name": "中国卫星"},
    {"symbol": "600370", "name": "*ST三房"},
    {"symbol": "300759", "name": "康龙化成"},
    {"symbol": "688017", "name": "绿的谐波"},
    {"symbol": "688655", "name": "迅捷兴"},
    {"symbol": "688772", "name": "珠海冠宇"},
]
