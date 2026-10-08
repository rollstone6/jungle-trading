# Jungle 天才交易员持仓工作台

复刻自 http://121.43.208.121:40326 的股票持仓追踪和AI策略分析系统。

## 功能特性

- 📊 **账户概览** - 本金、总资产、总市值、总盈亏
- 📈 **持仓管理** - 多只股票持仓，实时行情更新
- 🔍 **技术分析** - K线图、均线系统（5/10/20/60日）、量比
- 📰 **消息面** - 东方财富公告自动抓取
- ⚠️ **风控系统** - 自动计算仓位风险、盈亏预警
- 🌤️ **情绪天气** - 基于盈亏的账户情绪指标
- 🤖 **AI策略报告** - 支持Markdown格式的AI分析报告

## 技术栈

- **后端**: FastAPI + SQLite
- **数据源**: 腾讯财经（实时行情）、百度股市通（K线）、东方财富（公告）
- **前端**: Jinja2模板 + 原生JavaScript + 自定义SVG K线图
- **部署**: Uvicorn ASGI服务器

## 快速启动

```bash
cd jungle-trading

# 安装依赖（Web 工作台 + 量化模块已合并为一份清单）
pip install -r requirements.txt

# 启动 Web 工作台
python scripts/run.py

# 量化信号扫描 / 回测（交互式菜单）
cd fi_quant && python main.py
```

访问地址：`http://localhost:8090/?pwd=0mGecaPX3duCfVXhEb`

## 项目结构

```
jungle-trading/
├── app/                       # Web 工作台（FastAPI）
│   ├── main.py                # FastAPI主应用
│   ├── models/
│   │   └── database.py        # SQLite数据库模型
│   └── services/
│       ├── market.py          # 行情数据服务（腾讯/百度/东方财富）
│       ├── portfolio.py       # 持仓和风控计算
│       └── reports.py         # AI报告服务
├── fi_quant/                  # 量化信号扫描与日线回测模块
│   ├── main.py                # 交互式扫描菜单（信号/回测/筹码分析）
│   ├── backtesting/           # 统一回测引擎与入口（engine/runner）
│   ├── core/                  # 信号框架与策略基类
│   ├── data/                  # 多源行情获取（Tushare/东财/新浪/腾讯 + 缓存）
│   ├── strategies/            # 6个策略（突破回踩/均值回归/主力吸筹等）
│   └── utils/                 # 箱体识别/筹码分布/趋势过滤
├── scripts/
│   ├── run.py                 # 启动 Web 工作台
│   └── refresh.py             # 手动刷新行情脚本
├── tests/
│   └── test_backtest.py       # 回测功能测试
├── archive/
│   └── fi_quant_backtest_legacy.py  # 已归档的旧版回测引擎（无引用，仅留档）
├── static/                    # 前端静态资源
├── templates/                 # Jinja2模板
├── reports/                   # AI报告目录（Markdown文件）
├── data/                      # SQLite数据库
└── requirements.txt           # Python依赖（Web + 量化合并清单）
```

## API接口

### 页面访问
- `GET /?pwd=xxx` - 主页面
- `GET /?pwd=xxx&update=1` - 维护模式（显示更新持仓按钮）

### 数据接口
- `POST /api/positions/manual` - 手动更新持仓
- `GET /api/reports/strategies?date=YYYY-MM-DD` - 获取AI策略报告

## 数据源

1. **腾讯财经** - 实时股票报价
   - 接口: `https://qt.gtimg.cn/q=sh603629,sz002281`
   - 更新频率: 交易时间内实时

2. **百度股市通** - 日K线数据
   - 接口: 百度财经API
   - 更新频率: 每日收盘后

3. **东方财富** - 公司公告
   - 接口: 东方财富公告API
   - 更新频率: 每日抓取最新公告

## AI报告格式

在 `reports/` 目录放置Markdown文件，命名格式：`YYYYMMDD_ai_report.md`

示例：
```markdown
# 2026-07-06 持仓策略报告

## 一、账户状态
- 总资产：261,460.99
- 总盈亏：-38,539.01 (-12.85%)

## 二、利通电子策略
结论：继续禁止补仓，观察除权后表现。

- 关键价位：125.06 / 126.46
- 风险：单票仓位过大

## 三、操作计划
1. 开盘观察
2. 10:00前判断强弱
3. 14:30后决定是否减仓
```

## 定时任务（可选）

使用cron定时刷新行情：

```bash
# 交易日 9:30-15:00 每15分钟刷新（路径换成你的仓库实际位置）
*/15 9-14 * * 1-5 cd /path/to/jungle-trading && python scripts/refresh.py
*/15 15 * * 1-5 cd /path/to/jungle-trading && python scripts/refresh.py
```

## 维护说明

### 更新持仓（维护模式）
1. 访问 `http://localhost:8090/?pwd=0mGecaPX3duCfVXhEb&update=1`
2. 点击"更新持仓"按钮
3. 输入股票代码、数量、成本价
4. 提交后自动刷新行情

### 添加AI报告
1. 在 `reports/` 目录创建 `YYYYMMDD_ai_report.md`
2. 写入Markdown格式的分析报告
3. 页面会自动识别并显示

### 数据库备份
```bash
cp data/jungle.db data/jungle.db.backup
```

## 与原版对比

| 功能 | 原版 | 复刻版 |
|------|------|--------|
| 技术栈 | FastAPI + MySQL | FastAPI + SQLite |
| 部署 | Docker + Nginx | 单机Uvicorn |
| 行情源 | 腾讯/百度/东方财富 | 腾讯/百度/东方财富 ✓ |
| K线图 | 自定义SVG | 自定义SVG ✓ |
| AI报告 | GPT-5.5生成 | 手动Markdown |
| OCR识别 | 截图识别持仓 | 手动输入 ✓ |
| 定时任务 | APScheduler | 可选Cron |

## 开发日志

**2026-07-07**
- ✅ 项目结构搭建
- ✅ 数据库模型设计
- ✅ 行情API集成（腾讯/百度/东方财富）
- ✅ 持仓计算和风控逻辑
- ✅ 前端模板渲染
- ✅ K线图交互功能
- ✅ 情绪天气组件
- ✅ API接口（手动更新持仓、AI报告）

## 许可

复刻项目仅供学习参考。
