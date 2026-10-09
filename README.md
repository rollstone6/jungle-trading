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
- 📡 **量化信号中心**（`/quant`）—— app/quant 全能力 Web 化：策略信号扫描（突破回踩/均值回归/主力吸筹）、趋势过滤（强趋势/超卖/宽松）、箱体检测、筹码分布（含单股分布图）
- 🔬 **研究中心**（`/research`）—— 基金经理跟踪 / 东财模拟组合排行 / 资金流向背离 / 央行流动性仪表板

### 量化研究模块（`app/quant/`）

- 入口：`python -m app.cli`（交互式菜单：1-8 扫描，9 回测，10 筹码分析）
- 扫描模式：股票池模式（默认 13 只）/ 全量 A 股模式（菜单按 `S` 切换）
- 6 个内置策略：

| 策略 | 引擎类 | 思路 |
|---|---|---|
| 突破回踩 | `BreakoutPullbackEngine` | 箱体突破后回踩确认，放量启动 |
| 产业链动量滞后 | `LeadLagEngine` | 上游（铜/覆铜板）异动后找下游补涨 |
| 铜价配对交易 | `PairTradingEngine` | 沪铜期货与铜相关股票的 lead-lag |
| 聪明资金追踪 | `SmartMoneyEngine` | 主力资金流/北向/龙虎榜行为 |
| 均值回归+周期共振 | `MeanReversionEngine` | 超跌反弹叠加行业周期位置过滤 |
| 主力吸筹 | `AccumulationEngine` | 筹码分布识别底部吸筹形态 |

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
# 访问 http://localhost:8090/?pwd=0mGecaPX3duCfVXhEb

# 2) 量化信号扫描 / 回测（另一个终端，仓库根目录执行）
python -m app.cli        # 交互式菜单：1-8 扫描，9 回测，10 筹码分析
```

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
│       ├── strategies/             # 6 个策略引擎 + accumulation_scoring（吸筹评分 Mixin）
│       ├── backtesting/            # engine（统一回测引擎）+ runner（CLI 入口）
│       └── utils/                  # 箱体识别 / 筹码分布 / 趋势过滤 + reporting（共享报告输出）
├── scripts/
│   ├── run.py                      # 启动 uvicorn（端口 8090，--reload）
│   └── refresh.py                  # 手动刷新全部持仓行情（crontab 用）
├── tests/
│   └── test_backtest.py            # 回测服务冒烟测试
├── archive/
│   └── fi_quant_backtest_legacy.py # 旧版引擎留档（无引用，勿改）
├── static/  templates/             # 前端资源（Jinja2 + 原生 JS + SVG K线）
├── reports/                        # AI 策略报告（YYYYMMDD_ai_report.md）
├── data/                           # SQLite 数据库文件
├── cache/                          # 行情磁盘缓存（gitignored，自动重建）
├── backtest_results/               # 回测结果输出（gitignored）
├── .env.example                    # Tushare token 模板
└── requirements.txt                # 统一依赖清单
```

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

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/?pwd=xxx` | 主页面 |
| GET | `/?pwd=xxx&update=1` | 维护模式（更新持仓） |
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
| GET | `/api/quant/trend?mode=strong&refresh=1` | 全池趋势过滤（strong/loose/oversold） |
| GET | `/api/quant/box?refresh=1` | 全池箱体检测 |
| GET | `/api/quant/chip?refresh=1` | 全池筹码集中度扫描 |
| GET | `/api/quant/chip/detail?symbol=600183` | 单股筹码分布明细（画图数据） |

## 定时任务（可选）

```bash
# 交易日 9:30-15:00 每15分钟刷新行情（路径换成你的仓库实际位置）
*/15 9-14 * * 1-5 cd /path/to/jungle-trading && python scripts/refresh.py
*/15 15 * * 1-5 cd /path/to/jungle-trading && python scripts/refresh.py
```

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
