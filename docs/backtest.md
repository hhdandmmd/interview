# 财务特征分组回测

本模块落实原题附加项：读取项目生成的财务特征，按值从低到高分五组，计算等权收益并输出HTML、JSON、成员CSV、分组收益CSV及净值CSV。它是价格研究工具，范围小、可离线复算，不引入撮合引擎、任务平台或额外依赖。

## 根配置自动回测

在根目录config.py将backtest.enabled设为True，并在factors填写因子列表。每个因子分别排序、分组和回测；财报主线仍完整生成全部因子。五组至少需要五只股票，名称/人数/日期/费用等在联网前校验，财报成功后固定使用该次返回的快照。

```python
backtest=BacktestConfig(
    enabled=True,
    factors=["roe_weighted_ytd", "revenue_growth_yoy"],
    groups=5,
    horizon=20,
    cost_bps=0.0,
    # 历史公告近似研究；本系统真实已知时间用observed。
    time_policy="publication_proxy",
    start_date="2025-01-01",
    end_date="2025-12-31",
    # 不指定则自动采集腾讯后复权行情；指定本地目录可复用已有行情。
    market_dir=None,
)
```

上述字段放在RunConfig的backtest参数中，完整可运行入口见根config.py。日期是回测交易区间；不填写时取输出报告期首年的1月1日至末个报告期末日。默认时间口径仍为observed，不把本次抓取的数据回填到过去；历史区间可能没有有效窗口。publication_proxy须明确选择，保留历史版本未核验的说明。

多个因子共用一次行情采集与解析；本地行情允许包含额外证券，研究池固定取stocks，缺少已声明股票则报错。自动行情至多50只沪深股票、五个自然年；北京证券或更长范围请提供外部同契约行情。

输出位于财务工作区backtests/时间_编号：index.html是总览，batch.json记录固定快照/配置/阶段状态，各因子目录保留独立报告、SVG、收益/成员CSV和可复算输入。重复运行创建新目录，不覆盖旧报告。行情失败或单因子失败保留上下文；其他因子仍可完成。回测不完整时入口返回非零码，已成功的财报结果保留。因子代码维护见[因子扩展](factors.md)。

## 因子与报告读法

默认因子为`roe_weighted_ytd`（披露累计加权ROE），来自财务特征模块的正式规格。它表示年初至报告期末的累计加权ROE，报告按百分数展示。每个决策日按当时可用的最新报告及版本从低到高排序，G1最低、G5最高，组内等权；每期重新分组，成员不是永久固定。

HTML首屏显示中文因子名称、标识、分组方式、股票池、持有期、费用和实际持有范围。净值图横轴为真实日期，纵轴为初始值1的累计净值；1.20对应累计收益20%。曲线只包含首个入场日与持有窗口端点，不能当作每日净值或每日回撤。图例说明各组与1.0基准线，圆点悬停可查看日期和净值。

虚线“同截面等权参考”以每期全部有效因子成员的个股等权收益复利，并扣除与各组相同的双边费用；不是市场指数，组人数不同时也不是各组收益的简单平均。参考损益未知后同样停止累计净值。前置无有效分组的现金期不画成有效回测，没有形成窗口时显示空结果，不伪造平线绩效。

报告提供分组最终净值、百分比收益、端点回撤和首期成员的报告期/因子值/权重，便于检查因子值与分组对应关系。`nav.svg`可独立保存；`chart_data.csv`包含初始点、各组和参考净值，可与`nav.csv`及分组收益复核。图与CSV都写入摘要清单，原信号与回测计算结果保持原口径。

库内回测可通过`--feature revenue_growth_yoy`等参数切换项目生成的其他因子。外部`signals.json`必须实际包含指定因子；随包真实示例和人工样例只提供ROE，不能只改参数就产生其他因子数据。

## 财报历史与PIT口径

**已有2023—2025年财报，但没有完整核验过这些报告当时的全部历史版本。** 今天下载2023年的报告，报告期仍是2023年，返回值却可能已经更正。按原公告日释放今天的更正值，不自动成为严格历史PIT。

两个入口使用同一分组算法，默认选择真实可用时间：

| 参数 | 信号放出时间 | 能说明什么 |
| --- | --- | --- |
| `observed`（默认） | 本系统实际数据可用时间与首次发布此版本时间的较晚者 | 本系统真实已知信息的回放；2026年首次抓取的数据不会进入2023—2025回测 |
| `publication_proxy`（显式选择） | 固定当前快照中，所有特征依赖的`source_available_at`最大值 | 公告时间近似研究；依赖时间已包含公告及已知更新日期的较晚者，仍不能消除未知历史修订偏差 |

日期精度按上海次日零点处理，不能当作公告当天开盘已知。复合因子的可用时间包括所有滞后报表依赖；源时间缺失时退回真实可用时间，禁止猜测。输出保留`actual_data_available_at`、`actual_published_at`、`timing_status`、精确特征版本与风险标记，财务库和原HTTP证据均不修改。

真实时间模式读取指定快照之前、同一scope/profile的全部已发布版本，重复出现的版本保留首次发布边界。近似模式只投影一个固定快照，报告明确`historical_versions_verified=false`。若以后取得逐版原始公告及更正公告，可整理为带真实可用时间和版本的外部信号文件，再接入同一算法；需要先核验数值版本，不能仅改时间标签。

## 计算契约

1. 使用独立交易日历，不从个股报价并集推断休市。T日上海15:00截面只选当时可用的特征；先选最新报告期，再选该报告期最新版本。最新值失效时排除，不能退回旧好值。
2. 数值有效且状态为`ok/warning`才能分组，warning人数另列。默认至少五只；不足五只的窗口持有现金、不计现金利息，原因与次数保留。股票池由行情清单`codes`事先声明，不按未来报价完整性选样。
3. 按特征值、完整证券代码稳定排序，以`numpy.array_split`近似等分，组内等权。并列值不随机抖动，报告披露不同特征值数量；常数截面的Rank IC为空。
4. 下一交易日收盘进入，持有H个交易日，于第H日收盘退出。决策间隔H个交易日，持有区间不重叠；交界处允许先全部退出再进入。默认H=20，人工演示H=2。数据末尾不足H日的决策明确跳过，不缩短持有期。
5. 个股收益=`exit_close / entry_close - 1`；组收益为全部成员等权平均。缺失任一端点时保留成员并将整组收益置NULL，不前向填价、不填零、不删除。该组累计净值从未知损益开始停止，后续完整窗口仍可单独观察。
6. 默认零费用；`--cost-bps`为统一单边基点，净收益=`(1-c)^2 * (1+gross) - 1`，代表每期全部退出/进入的统一扣费假设，不宣称复现真实佣金和税费。
7. 净值从1开始按窗口收益复利。回撤只观察窗口端点，不作为日内或每日最大回撤。没有形成任何窗口时收益与回撤为NULL。Rank IC按平均秩计算，同池等权报价收益作为研究参照；G5-G1是收益差，不是可交易多空组合净值。

缺少停复牌、涨跌停、成交量限制、历史上市退市与完整复权事件核验，所以本模块不宣称策略可成交或无偏历史绩效。它参考`E:\backtest`中“分层评估、延迟持仓、复权价、缺失生命周期”的处理思路，未复制其代码或任何本地大行情数据。

## 三种运行方式

以下在源码根目录运行；Windows交付程序用`.\finpipe.exe`替换`.\.venv\Scripts\python.exe -m finpipe`。输出使用新目录，已有报告不会覆盖。

完全离线的人工财报演示：

```powershell
.\.venv\Scripts\python.exe -m finpipe backtest --demo --output output/人工回测
```

该样例的人工财报经过主项目`normalize -> compute_features`生成ROE，十只证券分为五组。报价趋势人为设定，用于核验数学和程序，不是因子有效性的证据。人工日期序列不是实际交易日历。

真实财报和小范围在线日线：

```powershell
.\.venv\Scripts\python.exe -m finpipe run --config config/default.toml
.\.venv\Scripts\python.exe -m finpipe market --codes 000002.SZ,000006.SZ,000007.SZ,000008.SZ,000009.SZ,000010.SZ,000011.SZ,000012.SZ,000014.SZ,000016.SZ --start 2023-01-01 --end 2025-12-31 --output output/market
.\.venv\Scripts\python.exe -m finpipe backtest --workspace output/live --market output/market --time-policy publication_proxy --start 2023-01-01 --end 2025-12-31 --horizon 20 --output output/真实近似回测
```

历史财报也可直接重放随源码的50家真实HTTP证据，保留原观察时间并免去在线财报请求：

```powershell
.\.venv\Scripts\python.exe -m finpipe replay --manifest data/evidence/full_market_capture_20261013/manifest.json --workspace output/full
```

在交付包根目录复算`examples/backtest_real`中的小体量公开输入：

```powershell
.\windows\finpipe\finpipe.exe backtest --signals .\examples\backtest_real\signals.json --market .\examples\backtest_real\market --time-policy publication_proxy --start 2023-01-01 --end 2025-12-31 --horizon 20 --output .\我的复算结果
```

输出的`signals.json`与`market`目录可单独复算；它们只包含本次研究的小池日线和所选因子，不要求交付SQLite库或`E:\backtest`的私有大行情文件。请求原字节留在采集工作区，派生输入保存SHA和原请求摘要。交付包附真实小报告，人工报告可用离线命令自动生成；财务演示库采用3家公司小样本，50家完整财务结果可由公开归档重新生成。

## 数据来源与外部输入

公共日线入口参考[AKShare维护者的腾讯日线适配代码](https://github.com/akfamily/akshare/blob/main/akshare/stock_feature/stock_hist_tx.py)，直接使用公开HTTPS接口，不额外安装AKShare。支持沪深股票、最多50只和五个自然年，逐证券逐年请求。使用`hfqday`后复权收盘价，缺该字段时拒绝用未复权`day`代替；上海指数`sh000001`的未复权报价日期仅作为共同交易日代理，不是官方完整交易日历。北京证券请导入外部文件。

复用现有串行限频、UA、有限重试和HTTP原文归档。返回范围外记录会过滤，达到条数上限且起始日被截断则失败；重叠响应异价不能拼接。失败保留`complete=false`清单与请求证据，回测拒绝消费。后复权报价不保证含税分红总收益，也不保证历史复权版本不变。

外部信号JSON格式：`schema_version=1`、`metadata.kind`、`metadata.time_policy`、`rows`。每行必需：`security_code`、`report_end`、`feature_name`、`feature_value`、`feature_status`、`feature_version_id`、`available_at`（带时区）。允许额外记录实际观察/发布时间和来源证据，建议直接以已有报告的`signals.json`为模板。同一特征同一可用时间多个版本须先消除冲突。

行情目录必需：`prices.csv`（`security_code,trade_date,close`）、`calendar.csv`（`trade_date`）、`manifest.json`。清单声明`schema_version=1`、`complete=true`、`adjustment=hfq`、`calendar_kind`、固定`codes`、建议的请求范围`start/end`、两个CSV的SHA256。人工行情明确使用`synthetic`。日期键不能重复，收盘价必须有限且大于零；缺报价省略行，由回测计算覆盖与NULL。外部来源声明由提交者负责，文件格式校验不等于独立来源核验。

## 维护与验证

`backtest/calculation.py`只做输入校验和计算；`io.py`固定版本、时间投影及文件读写；`report.py`将计算结果呈现为中文HTML、真实日期SVG和参考图表数据；`market.py`处理公共行情契约及采集；`sample.py`生成明确人工的教学输入。新增财务因子仍在原`features/`维护，不在回测复制公式。

自动测试使用独立分数手算、假日间隔、盘后信号、坏新版失效、空价格、未知净值、并列分组、实际版本发布时间、公告/更新边界、外部文件摘要损坏和失败行情清单。真实数据运行及两环境回归结果见[本轮验收](reviews/backtest_delivery_20261014.md)。
