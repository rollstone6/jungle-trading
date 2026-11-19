# Jungle Trading —— 个人量化交易工作台

一个 all in one 的 A 股量化交易仓库，Web 工作台与量化研究模块统一在 `app/` 包下：

| 子系统 | 位置 | 形态 | 用途 |
|---|---|---|---|
| **持仓工作台** | `app/main.py` + `app/services/` | FastAPI Web 应用（端口 8090） | 持仓追踪、实时行情、K线分析、AI 策略报告、回测 API |
| **量化研究模块** | `app/quant/` | Python 包 + 交互式 CLI | 多策略信号扫描、统一日线回测引擎、筹码/箱体分析 |

两者共享同一份 `requirements.txt`、同一个仓库根目录 `.env` 和同一份 `.gitignore`。

---

## 功能特性

### 持仓工作台（`app/main.py`）

- 📊 **账户概览** —— 本金、总资产、总市值、总盈亏、情绪天气指标
- 📈 **持仓管理** —— 多只股票持仓，腾讯财经实时行情每 15 分钟自动刷新
- 🔍 **技术分析** —— 自绘 SVG K线图、MA5/10/20/60 均线系统、量比
- 📰 **消息面** —— 东方财富公告自动抓取
- ⚠️ **风控系统** —— 自动计算仓位风险、盈亏预警、单票风险等级
- 🤖 **AI 策略报告** —— 读取 `reports/` 下的 Markdown 报告并在页面展示
- 🔬 **三套回测引擎** —— Backtrader（`app/services/backtest.py`）、Zipline（`zipline_backtest.py`）、vnpy（`vnpy_service.py`），经 `/backtest` 页面和 API 调用（懒加载，缺装返回 503）
- 📊 **走势分类扫描**（`/regime`）—— 布林带带宽量化分类（挤压/趋势/区间/中性），日线+30分钟双周期共振
- 📡 **量化信号中心**（`/quant`）—— app/quant 全能力 Web 化：策略信号扫描（突破回踩/均值回归/主力吸筹/双均线交叉）、趋势过滤（强趋势/超卖）、箱体检测、筹码分布（独立 tab，池/全市场集中度扫描+单股分布图）、大盘点金、量化实验室
- 🔬 **研究中心**（`/research`）—— 基金经理跟踪 / 东财模拟组合排行 / 资金流向背离 / 央行流动性仪表板

### 量化研究模块（`app/quant/`）

- 入口：`python -m app.cli`（交互式菜单：1-8 扫描，9 回测，10 筹码分析）
- 扫描模式：股票池模式（默认 8 只，见 `app/quant/config.py` 的 `STOCK_LIST`）/ 全量 A 股模式（菜单按 `S` 切换）
- 8 个策略引擎 + 1 个评分 Mixin（`app/quant/strategies/`）：

| 策略 | 引擎类 | 思路（一句话） |
|---|---|---|
| 突破回踩 | `BreakoutPullbackEngine` | 箱体突破后回踩确认、放量启动（OBV 等指标过滤） |
| 均值回归+周期共振 | `MeanReversionEngine` | 触及半年/年线支撑 + RSI<25 超卖 + MACD 底背离 |
| 主力吸筹 | `AccumulationEngine` | 日线趋势过滤 + 60分钟吸筹评分（0-100）+ 突破确认 + 筹码集中度过滤 |
| 双均线交叉 | `MaCrossEngine` | 金叉买/死叉卖，均线参数按 vol60/ER120 自适应分档（与量化实验室同源口径） |
| 筹码单峰密集 | `ChipSinglePeakEngine` | 博主5条件（90%集中度<12%、主峰占比>18%、次峰/主峰<0.5、60日换手>80%、60日振幅<30%）首次满足即入场 |
| 产业链动量滞后 | `LeadLagEngine` | 下游 PCB 龙头带量启动后，上游覆铜板存在 2-5 日动量滞后，左侧潜伏 |
| 铜价配对交易 | `PairTradingEngine` | 沪铜跌破 20 日线（成本释放）+ 生益处箱体下沿 + 铜/股价 Ratio 偏离 -2σ |
| 聪明资金追踪 | `SmartMoneyEngine` | 主力连续 3 日净流入 + 融资余额断崖 >15%（杠杆清洗）+ 跌破布林下轨 |

> `accumulation_scoring.py` 不是独立策略，是从 `accumulation.py` 拆出的五大吸筹评分因子 Mixin（低位承接30/主力吸货25/OBV资金流20/量能吸筹15/RSI修复10），供主力吸筹引擎复用。
>
> **注册表差异（以代码为准）**：Web 信号下拉只列 `app/quant/strategies/__init__.py` 的 `STRATEGY_REGISTRY` 4 项（breakout/mean_reversion/accumulation/ma_cross）；`chip_single_peak` 不在信号下拉，它注册在回测入口 `app/quant/backtesting/runner.py`（`--strategy chip_single_peak`），筹码方向在 `/quant` 页有独立「筹码分布」tab（`ChipDistributionAnalyzer` 集中度评分扫描 + 单股分布图，与引擎的 5 条件信号口径不同）；`lead_lag`/`pair`/`smart` 为独立引擎类，经 CLI 菜单（`python -m app.cli` 选项 2/3/4）调用，未进上述两个注册表。

- 统一回测：收盘出信号 → 次日开盘成交，含手续费、印花税、滑点、止损/移动止损/止盈/跳空止损/最长持仓期
- 数据源级联：磁盘缓存（`cache/`）→ Tushare → 东方财富 → 新浪财经 → 腾讯财经（自动降级重试）

---

## 快速启动

```bash
cd jungle-trading

# 安装全部依赖（Web + 量化一份清单）
pip install -r requirements.txt

# 1) 启动 Web 工作台
python scripts/run.py
# 访问 http://localhost:8090/?pwd=<你的站点口令>

# 2) 量化信号扫描 / 回测（另一个终端，仓库根目录执行）
python -m app.cli        # 交互式菜单：1-8 扫描，9 回测，10 筹码分析
```

> 站点口令统一配置在服务器仓库根目录 `.env` 的 `SITE_PASSWORD`（不入库、不写入文档），访问时把 `<你的站点口令>` 替换为该值。

单独跑回测（两种方式都可以，均在仓库根目录执行）：

```bash
python -m app.quant.backtesting.runner --strategy breakout --start-date 20240101
python app/quant/backtesting/runner.py --strategy mean_reversion --symbols 601366,300823
```

### Tushare Token（可选）

不配也能跑（自动走东方财富等免费源）。要启用 Tushare，在仓库根目录建 `.env` 填入 token，程序启动时自动加载：

```bash
cp .env.example .env   # 然后把 your_token_here 换成真实 token
```

---

## 项目结构

```
jungle-trading/
├── app/                            # ===== 统一应用包 =====
│   ├── main.py                     # FastAPI 主应用：页面路由 + 回测 API + 定时刷新
│   ├── cli.py                      # 量化交互式扫描菜单（原 fi_quant/main.py）
│   ├── models/database.py          # SQLite：account / positions / reports 三张表
│   ├── services/                   # Web 服务层
│   │   ├── market.py               # 行情：腾讯实时 / 百度K线 / 东财公告
│   │   ├── portfolio.py            # 持仓计算与风控
│   │   ├── reports.py              # Markdown 报告读取
│   │   ├── backtest.py             # Backtrader 回测
│   │   ├── zipline_backtest.py     # Zipline 回测
│   │   └── vnpy_service.py         # vnpy 实盘/回测
│   └── quant/                      # ===== 量化研究模块（原 fi_quant） =====
│       ├── config.py               # StrategyConfig：策略参数与默认股票池
│       ├── core/                   # Signal / SignalFrame / StrategyBase 抽象基类
│       ├── data/                   # fetcher（级联取数+股票池）/ providers / cache
│       ├── strategies/             # 8 个策略引擎 + accumulation_scoring（吸筹评分 Mixin）
│       ├── backtesting/            # engine（统一回测引擎）+ runner（CLI 入口）
│       └── utils/                  # 箱体识别 / 筹码分布 / 趋势过滤 + reporting（共享报告输出）
├── scripts/                        # 15 个脚本，完整清单见下方「scripts/ 脚本清单」
│   ├── run.py                      # 启动 uvicorn（端口 8090，--reload）
│   └── refresh.py                  # 手动刷新全部持仓行情（crontab 用）
├── tests/
│   └── test_backtest.py            # 回测服务冒烟测试（走网络，见「测试与自检」）
├── archive/
│   └── fi_quant_backtest_legacy.py # 旧版引擎留档（无引用，勿改）
├── static/  templates/             # 前端资源（Jinja2 + 原生 JS + SVG K线）
├── reports/                        # AI 策略报告（YYYYMMDD_ai_report.md）
├── data/                           # SQLite 运行时库（gitignored）+ 4 个入库 json/jsonl 快照（见「data/ 目录说明」）
├── cache/                          # 行情磁盘缓存（gitignored，自动重建）
├── backtest_results/               # 回测结果输出（gitignored）
├── .env.example                    # Tushare token 模板
└── requirements.txt                # 统一依赖清单
```

### scripts/ 脚本清单（15 个）

以当前代码为准盘点；除标注外均在仓库根目录执行，`python` 指 `/opt/hermes-agent/.venv/bin/python`。

| 脚本 | 用途 | 走网络 | 运行命令 |
|---|---|---|---|
| `run.py` | 启动 uvicorn Web 服务（端口 8090，--reload） | 否（启动本身；服务运行时拉行情） | `python scripts/run.py` |
| `refresh.py` | 定时刷新全部持仓行情+公告，写 SQLite（首次自动建库建表） | 是（腾讯实时/百度K线+腾讯降级/东财公告） | `python scripts/refresh.py` |
| `refresh_etf.py` | 定时刷新 ETF 申购净流入（份额法，沪深交易所官方数据），写 `data/etf_flow.json`、`data/etf_share_snap.json` | 是（akshare/httpx 交易所接口） | `python scripts/refresh_etf.py` |
| `test_ma_cross.py` | ma_cross 离线单元自检（合成数据，断言齐全，ALL OK 退出码 0） | 否 | `python scripts/test_ma_cross.py` |
| `multi_backtest.py` | 多策略同股票池同口径横向对比回测（T+1、无未来函数），输出 `reports/multi_backtest_<date>.json` | 是（腾讯日K） | `python scripts/multi_backtest.py [策略名…]`（BT_DAYS 可调） |
| `martin_pullback_backtest.py` | Martin Luk 回调买法日线近似版回测复刻 | 是（腾讯日K） | `python scripts/martin_pullback_backtest.py` |
| `martin_pullback_15m.py` | 同策略 15 分钟版（日线定背景+分钟择时，无未来函数；新浪 15m 上限约 3 个月） | 是（新浪分钟+腾讯日线） | `python scripts/martin_pullback_15m.py` |
| `martin_fit_analysis.py` | Martin 策略适配性分析：24 只代表股按 regime/波动/价格打标签分组 | 是（腾讯日K） | `python scripts/martin_fit_analysis.py` |
| `sweep_compare.py` | 股票池多策略对比扫描，输出 HTML+CSV 到仓库上级目录 | 是（新浪分钟/腾讯日线） | `python scripts/sweep_compare.py 60m\|30m\|15m\|1d` |
| `sweep_regime.py` | 带走势分类的全池扫描（分类匹配策略 vs 统一策略），输出 CSV+HTML | 是（新浪分钟/腾讯日线） | `python scripts/sweep_regime.py 1d\|60m` |
| `sweep_boll_60m.py` | 布林带 60 分钟全池扫描（结果写 `backtest_results/`，自动建目录） | 是（新浪分钟） | `python scripts/sweep_boll_60m.py` |
| `chip_peak_sanity.py` | 单股筹码峰 sanity check（新浪日K+流通股本+峰切割） | 是（新浪） | `python scripts/chip_peak_sanity.py` |
| `chip_scan_market.py` | 全A股筹码 5 条件流式扫描（低内存版，断点续跑；股票池文件经 CHIP_UNIV 指定，默认 /tmp） | 是（新浪） | `python scripts/chip_scan_market.py [workers]` |
| `chip_download_klines.py` | 全量池日K下载器（旧版，已被 chip_scan_market 替代；全量留内存曾致 OOM，勿用于大池） | 是（新浪） | `python scripts/chip_download_klines.py` |
| `quant_pipeline.py` | 量化管线 CLI：ingest 只更新行情库 / run 完整闭环（DuckDB+VectorBT） | 是（级联取数） | `python scripts/quant_pipeline.py [ingest\|run] [fast slow]` |

### data/ 目录说明

并非纯 SQLite：运行时数据库与入库快照并存。

- **运行时 SQLite**：`data/jungle.db`（account/positions/reports 三表，gitignored，`scripts/refresh.py` 首次运行自动创建）；量化实验室另有 `data/jungle.duckdb*`（gitignored）。
- **入库 json/jsonl 快照（4 个，git 跟踪）**：
  - `astock_universe.json` —— 全A股票池快照（代码/名称/现价/流通市值/换手率），筹码扫描脚本的股票池输入（脚本默认读 /tmp 副本，经 CHIP_UNIV 环境变量指向）
  - `etf_flow.json` —— ETF 申购净流入最新结果（研究中心资金流页读取）
  - `etf_share_snap.json` —— 深交所 ETF 份额每日快照（官方无历史接口，滚动存盘对比算净流入）
  - `chip_scan_20261030.jsonl` —— 2026-10-30 全A筹码 5 条件扫描结果快照（每行一股：集中度/主峰/换手/振幅/信号统计，历史留档）
- **运行时扫描缓存（gitignored）**：`regime_scan.json`、`research_*.json`、`quant_scan_*.json`、`pipeline_result.json` 等，当日有效、可再生。

## 数据流

```
行情源              Web 工作台                        量化模块
──────             ──────────                        ────────
腾讯实时 ─┐        ┌─ services/market.py ──► positions 表   ┌─ quant/data/providers.py（Tushare/东财/新浪/腾讯）
百度K线 ──┼──────► │                    └─► index 页面    │      └─► cache/ 磁盘缓存 + 内存缓存
东财公告 ─┘        ├─ portfolio.py ──► 风控/盈亏          ├─ quant/strategies/*.py ─► SignalFrame
                   ├─ reports.py ──► reports 表           ├─ quant/backtesting/engine.py ─► 成交/资金曲线
Tushare ──┐        └─ backtest*.py ◄─ /backtest API       └─ quant/backtesting/runner.py ─► backtest_results/
akshare ──┴──────► quant/data/fetcher.py（级联降级，缓存命中即跳过网络）
```

---

## 指标口径（KDJ / MACD）

技术指标口径说明（`app/services/market.py`）：

- **KDJ**（`calc_kdj`）：全历史滑动窗口递推 RSV→K→D，国内通达信口径
  `K=2/3·K+1/3·RSV`、`D=2/3·D+1/3·K`（默认 9,3,3），初值 K=D=50。
  旧实现仅从倒数第 9 根开始迭代、之前全部历史完全不参与，已修复。
  验证基准为**全历史递推参考实现**（repro §P1-3 `kdj_full`，误差 0）；
  注意 talib.STOCH 默认对 RSV 做 SMA(3) 平滑（美式 KDJ），与国内递推口径
  设计上不同源，末端数值不可直接互相对照。
- **MACD**（`calc_macd`）：EMA 平滑系数采用标准 `alpha = 2/(N+1)`（12/26/9），
  以 **talib 为基准**，长历史（≥250 根）末端 DIF/DEA 与 `talib.MACD` 对照
  误差 <1e-6（本实现首值播种、talib 前 N 均值播种，差值随历史长度指数衰减）。
  MACD 柱为国内行情软件惯例 `(DIF-DEA)×2`（talib 返回的 hist 不×2，对照时仅比 DIF/DEA）。

---

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/?pwd=<你的站点口令>` | 主页面 |
| GET | `/?pwd=<你的站点口令>&update=1` | 维护模式（更新持仓） |
| POST | `/api/positions/manual` | 手动更新持仓 |
| GET | `/api/reports/strategies?date=YYYY-MM-DD` | 获取 AI 策略报告 |
| GET | `/backtest` | 回测页面 |
| GET | `/api/backtest/strategies` | 列出可用回测策略 |
| POST | `/api/backtest/run` | 运行回测 |
| GET | `/regime` | 走势分类扫描页 |
| GET | `/api/regime/latest?refresh=1` | 全池走势分类（当日缓存落盘 `data/regime_scan.json`） |
| GET | `/research` | 研究中心入口（基金/排行/资金流/央行 4 子页） |
| GET | `/api/research/{funds\|rankings\|moneyflow\|pboc}?refresh=1` | 研究数据接口（当日缓存） |
| GET | `/quant` | 量化信号中心页 |
| GET | `/api/quant/strategies` | 可扫描策略列表 |
| GET | `/api/quant/signals?strategy=breakout&refresh=1` | 策略信号扫描（近30天，当日缓存） |
| GET | `/api/quant/trend?mode=strong&refresh=1` | 全池趋势过滤（strong/oversold；宽松模式已从扫描入口移除） |
| GET | `/api/quant/box?refresh=1` | 全池箱体检测 |
| GET | `/api/quant/chip?refresh=1` | 全池筹码集中度扫描 |
| GET | `/api/quant/chip/detail?symbol=600183` | 单股筹码分布明细（画图数据） |

## 定时任务（可选）

```bash
# 交易日 9:30-15:00 每15分钟刷新行情（路径换成你的仓库实际位置）
*/15 9-14 * * 1-5 cd /path/to/jungle-trading && python scripts/refresh.py
*/15 15 * * 1-5 cd /path/to/jungle-trading && python scripts/refresh.py
```

## 测试与自检

**无统一 pytest 入口**：仓库没有 pytest.ini / pyproject.toml / conftest.py，运行环境未安装 pytest，测试按脚本各自运行。

- **离线自检（推荐回归用）**：`python scripts/test_ma_cross.py` —— ma_cross 策略单元自检，合成数据（300 根三段趋势+噪声）、无网络；stub 掉 vectorbt/duckdb 冷导入，仅需 numpy/pandas。断言买卖信号齐全、相邻信号交替、金叉数与 pandas rolling 状态机复核一致等，全部通过打印 `ALL OK` 且退出码 0。
- **走网络冒烟**：`python tests/test_backtest.py` —— 拉 603629 日K（百度股市通，403 时自动降级腾讯）→ 跑 ma/macd 两个 Backtrader 回测并打印指标。⚠️ 它是打印式脚本、无断言，**失败时退出码仍为 0，不能以退出码判成败**，须看输出中的 ✓/❌。上游数据源失败 2 次即停，勿反复重试。
- **单策略回测冒烟（走网络）**：`python -m app.quant.backtesting.runner --strategy <名> --start-date 20240101`，走 quant/data 级联取数，结果写 `backtest_results/`。

## 维护说明

- **更新持仓**：访问维护模式 → 点「更新持仓」→ 输入代码/数量/成本价
- **添加 AI 报告**：在 `reports/` 下创建 `YYYYMMDD_ai_report.md`，页面自动识别
- **初始化持仓**：首次启动若持仓表为空，会自动写入 4 只示例持仓（利通电子、工商银行、光迅科技、券商ETF）

## 已知事项

- 三个回测引擎为可选依赖、路由内懒加载：未安装时对应引擎的 API 返回 503 并提示安装命令，不影响其余页面（zipline-reloaded / vnpy 尚不支持 Python 3.14，backtrader2 已可用）
- 百度股市通 K 线接口已开启风控（403），K 线自动降级到腾讯财经前复权数据，行为透明无需配置
- 腾讯 fqkline 分钟参数已失效（param error），分钟数据（60m/30m/15m）走新浪 `CN_MarketDataService.getKLineData` 源，单次约 1023 根（60 分钟约 13 个月）
- 东方财富分钟数据在新版 akshare 中迁移到 `stock_zh_a_hist_min_em`，providers 已适配；东财对单 IP 有频率限制，触发后自动降级新浪源
- akshare 依赖 py_mini_racer（V8），其引擎池不支持并发初始化，多线程首次调用会直接 FATAL 崩溃进程——providers 中所有 akshare 调用统一经 `_AK_LOCK` 串行化
- `app/quant/backtesting/` 目录名与 PyPI 上的 `backtesting` 包同名，避免 `pip install backtesting` 到同一环境
- 数据库为单文件 SQLite（`data/`），适合个人单机使用，勿多进程并发写；`scripts/refresh.py` 首次运行会自动建库建表
