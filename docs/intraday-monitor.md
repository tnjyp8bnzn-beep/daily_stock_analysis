# 持续盘中监控、持仓证据与回放

English summary: an opt-in, always-on, session-aware quote monitor reuses the
existing providers, AlphaSift screening, portfolio ledger and notification
channels. Signals are paper observations, never orders or broker fills. The
optional standalone Backtrader research program has a separate GPL license.
The explicit intraday Tencent watchlist fetch uses one request while retaining
the existing shares/yuan normalization and quote freshness gate.

## 核实后的现状和边界

以 main 的 `8582fe1` 为基线：已有多源行情、AlphaSift 选股、账户/成交台账、
事件提醒、PushPlus 业务失败检测和每日分析。Draft PR #5 仅添加 10:00/14:30
两次 Actions 分析，不能持续盯盘。本功能不修改该 PR，也不修改现有 18:00 任务。

| 缺口 | 原因及对应修复 |
| --- | --- |
| 固定时点分析不能及时捕获条件变化 | 常驻进程交易时段每 60 秒轮询，不通过 Actions cron 模拟实时 |
| 日报允许缓存，不适合交易预警 | 小于 5 只也可显式刷新 TickFlow；旧行情或未确认报价时间继续尝试配置中的其他源 |
| 自选股不等于持仓 | 读取既有成交台账；截图作为独立证据，禁止虚构期初买入或重复回放截图历史成交 |
| 长报告和重复提醒 | 不调用 AI，每个条件从未触发到触发产生一条短提醒，数据库持久化去重 |
| 推送失败被误解为成功 | 按各渠道真实结果记录 pending/failed/accepted；最多 3 次、至少间隔 120 秒；accepted 只代表服务接受 |
| 缺少可复查样本 | 保存通过质量检查的真实行情样本、来源、报价/抓取/规则检查时间，独立于真实成交 |
| 日报评估不等于股票交易回测 | 可选 Backtrader 回放，明确费用、滑点、T+1、停牌与历史涨跌停约束 |

开源核查：已有 [AlphaSift](https://github.com/ZhuLinsen/alphasift) 的筛选因子
优先复用；[Qlib](https://github.com/microsoft/qlib) 的训练和在线模型栈不进入
一分钟报价路径；[vn.py](https://github.com/vnpy/vnpy) 的券商交易接入不属于本功能；
[Backtrader](https://github.com/mementum/backtrader) 仅用于独立离线研究。
[GitHub schedule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
可能排队，不是常驻服务。

实测对比（2026-10-16 盘后，同一测试环境，只测六只股票行情获取）：
[PanWatch 腾讯批量实现](https://github.com/TNT-Likely/PanWatch/blob/b7afef533830f295efaba5678927f54eb482af68/packages/marketdata/src/marketdata/vendors/tencent.py)
为 0.8/1.1/1.6 秒；本项目旧逐股腾讯路径为 32.6/33.0 秒；
本项目新增 HTTPS 批量路径含原有接口限速约 6.2 秒。PanWatch 原始亨通光电成交量
1,023,074 与本项目按成交额/价格校验的 102,307,400 股相差 100 倍；
因此仅借鉴其批量请求方式，不接入未标准化的原始量能。盘后报价时间约 16:14，
这些数字只是抓取耗时，不是盘中行情源延迟或手机提醒到达时间。

免费常驻方案核查：[Oracle Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
提供免费额度内的虚拟机，但有资源容量限制与闲置实例回收；[Google Cloud 免费层级](https://docs.cloud.google.com/free/docs/free-cloud-features?hl=zh-cn)
仅在指定美国区域提供 e2-micro，向中国的流量不在其列出的免费出站额度内；
[Render 免费 Web 服务](https://render.com/docs/free) 在无入站访问 15 分钟后休眠，且本地
SQLite 文件不持久，不适合持续盯盘。若有现成常开电脑，运行本项目服务最省去新增云费用。
尚未验证任何服务器到国内行情和 PushPlus 的盘中端到端延迟，不能承诺长期零费用或秒级送达。

## 启动和真实时效

需要一台交易时段保持运行、能够访问行情和推送源的现有电脑/服务器。已有 Web/API
服务可直接启用，或使用项目现有 `python main.py --serve-only`。
在该服务的**私有环境配置**中设置：

```dotenv
INTRADAY_MONITOR_ENABLED=true
INTRADAY_MONITOR_INTERVAL_SECONDS=60
INTRADAY_MONITOR_NOTIFY=false
INTRADAY_PORTFOLIO_SNAPSHOT_PATH=
```

先观察 paper 信号和来源质量，审核后将 `INTRADAY_MONITOR_NOTIFY=true`。
不增加收费接口，不在轮询中调用 LLM。通知复用原有 PushPlus 等配置，无需新密钥。
监控独立启用时不会顺带启动日分析；已有 `--schedule` 也会注册该后台任务。

60 秒为检查间隔，不是送达保证：调度器还有最多约 30 秒检查粒度，接口延迟、
限流、串行行情/历史请求和推送服务会增加延迟。全市场发现单独线程每 15 分钟扫描，
最多 10 只候选，不阻塞持仓检查；新发现股票仍须通过单股实时质量门槛。
休市、午休、日历不可用、未知阶段不取行情、不产生交易信号；收盘集合竞价不提示新建仓。
进程、网络或数据源不可用时无法承诺提醒，应结合状态接口检查。

## 行情与信号规则

- 只接受同交易日、带时区且与检查时刻相差不超过 90 秒的报价和获取时间，拒绝未来时间。
- 价格、OHLC、昨收、涨跌幅、成交量（股）、成交额（元）均须完整一致；异常量能不能进入分析。
- 日线只取今天之前最近 20 根完整记录；不接受不足、重复日期、非数值价格/量或旧历史。
  历史末收盘与报价昨收不一致时，提示价格口径问题，不跨复权口径生成信号。
- 历史请求结束后再次检查行情时效和市场阶段，避免慢请求把有效报价拖成过期信号。
- 买入候选：主板普通股票，非 ST，突破前 20 根日线高点、MA5 > MA20、真实量比 ≥ 2、
  成交额 ≥ 1 亿元、涨幅 2% 至不足 9.5%。ETF 继续监控风险，但不套用该主板入场策略。
- 跌破上一日已完成 MA20 为技术风险预警。MA20 明确保存为参考支撑/预警价；
  **不是**已确认的个性化止损价。实际止损、目标价保持 null，未获得预算/计划时不编造。
- 已持仓提示核对可卖数量和风险，避免重复建仓；无已核实持仓不下达卖出数量。
  不自动下单，不把信号当成成交，也不保证收益。
- 无效报价不会解除去重；有效恢复到 watch 才重新布防。条件已恢复则过期未成功的旧提醒。
  notify=false 阶段只记 paper，后来启用通知也不补发已过时的 paper 提醒。

## 真实持仓、成交和对账

既有 `/api/v1/portfolio` 和 Web 持仓模块继续管理账户、真实买卖成交、费用、
FIFO/平均成本、导入去重和超卖检查。新监控仅以 read_only=True 读取，不改持仓。
台账尚无完整历史时，私有截图证据可通过 `INTRADAY_PORTFOLIO_SNAPSHOT_PATH` 读取：

```json
{
  "schema_version": 1,
  "snapshot_date": null,
  "snapshot_date_confirmed": false,
  "reconciled": false,
  "positions": [
    {"account": "账户别名", "code": "600000", "quantity": 100,
     "available_quantity_at_snapshot": 100, "avg_cost": 10.0, "asset_type": "stock"}
  ],
  "executions": [],
  "cancelled_orders": []
}
```

此例仅说明格式，不是真实持仓。截图与真实台账同时存在时不叠加，返回
`reconciliation_required`；仅有截图返回 `snapshot_unreconciled`。
截图日期、清算状态或成交与余仓冲突时保留证据和差异，不推断缺失成交。
截图“可用”数量仅代表截图时点，当前可卖量保持未知，不能绕过 T+1。
同一证券不同账户分开保留成本；撤单和零成交委托不计入买入。
场外基金（asset_type=off_exchange_fund）留在证据里，份额可空，不进入交易所盘中轮询。
所有真实截图、账户数据与该 JSON 都应保存在仓库之外，不提交 PR。

## 可观察性与通知样例

既有 API 认证和访问保护继续适用：

- `GET /api/v1/monitor/status`：区分配置 enabled 与常驻任务实际 active。
- `GET /api/v1/monitor/events?limit=50`：信号、价格、来源、账户证据与通知状态。
- `GET /api/v1/monitor/observations?limit=200`：最多 5000 条真实报价样本，方便回放。
- `GET /api/v1/monitor/portfolio`：当前台账、最近真实成交、私有截图证据及未对账项。

短提醒模板（数字来自实际信号，不补写虚假新闻或资金叙述）：

```text
持仓/卖出风险预警 <证券代码>
现价 <真实报价>；跌破上一交易日完整日线 MA20
参考支撑/预警价 <MA20>；实际止损触发价：未配置；目标价：数据不足
持仓证据 <数量>；状态 <台账/未对账>；当前可卖数量待券商确认
来源 <接口>；行情源标注报价时间 <带时区时间>
交易所报价时间：未提供可独立核验的交易所原始时间，不能当作官方成交时间
抓取时间 <时间>；规则检查时间 <时间>
规则信号，未下单、未成交；推送服务接受消息不代表手机已读。
```

## 离线回测与验收限制

```bash
python -m pip install -r requirements-backtest.txt
python scripts/research/intraday_backtest.py \
  --quotes private-quotes.csv --daily private-daily.csv \
  --evaluate-from 2026-10-01 --output private-backtest.json
```

`daily.csv` 必须包含 date/close/high/volume，且是相应历史时点可得、与报价一致的
价格口径。`quotes.csv` 单证券、时间严格递增且带时区，必须包含：
timestamp/code/name/price/open_price/high/low/pre_close/change_pct/volume/amount/
volume_ratio/upper_limit/lower_limit/suspended。历史量比、停牌和涨跌停价必须有真实来源；
仅有日线不能声称完成盘中策略回测。当前统一行情样本未必包含后三项，需补充真实历史证据。

复用同一入场/风险规则；信号后的下一个样本价模拟执行，不使用全天高低价提前成交。
缺失/无效证据不产生买入。T+1 不允许当日买入当日卖出；停牌、涨停买入、跌停卖出延后。
100 股整手、单票可用资金 10%、佣金万三/每笔最低 5 元、卖出印花税万五、滑点千一是
**显式研究假设**，可经 Python API 调整，不等于用户券商实际费用或推荐仓位。
输出成交、费用、期末权益、最大回撤和阻断原因。未模拟盘口排队和容量，样本价不保证成交。
`--evaluate-from` 保留样本外边界，但不自动消除策略选择偏差；单票回放不验证全市场选股收益。
目前没有足够的真实分钟历史及全市场时点股票池，不报告虚构收益率。

新测试覆盖六股轮询、旧缓存故障切换、未知时间/异常量能、交易时段、无真实量比不买入、
跨历史请求的时效变化、重启去重、推送失败/有限重试、截图/台账不重算、T+1、费用、
涨跌停/停牌和未来数据隔离。测试合成行情仅用于验证程序，不能作为实盘收益证据。

关闭：设置 INTRADAY_MONITOR_ENABLED=false 并重启常驻服务。所有新增开关默认关闭，
关闭后不影响既有日报、行情单位、收盘有效性判断、PushPlus、持仓台账或每日定时规则。
