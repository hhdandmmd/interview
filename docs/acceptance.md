# 当前验收方案

范围依据[原题对照](requirements_traceability.md)。实际通过记录及未验证项见[交付验收](delivery_validation.md)，本页定义怎样验收，不把计划当作已执行。

## 首次使用

1. 解压整个包，双击根目录START_HERE.html；无需Python或网络，先看财务报告、原题和真实近似回测。
2. 双击最外层run_demo.cmd，确认退出0并找到实际HTML路径及摘要；结果在output/demo。
3. 再运行同一演示，确认复用结果，不新增来源、清洗与特征版本。
4. 按[Windows说明](windows_usage.md)在同一个最外层目录查询单公司单期，解释一个NULL或warning；源码和测试位于source。
5. 人工回测后用小体量真实输入复算；使用新输出目录，旧报告不覆盖。

## 原题核验

| 要求 | 验收方法 |
| --- | --- |
| 三类财报/反爬 | 原文、范围、观察和SHA可核验；串行限频，不绕过403/Retry-After |
| 结构化/查询 | SQLite/CSV/Parquet一致，代码前导零、单位和类型不丢失 |
| 异常/缺失 | 0、NULL、坏字段、冲突及不可比有状态，新坏版不复活旧好值 |
| 最新版本 | 重复、更正、冲突解除、A→B→A和旧重放保留审计并正确选择 |
| 披露对齐 | 公告、观察、处理及发布分离，未来/坏时间不提前消费 |
| 特征 | 15公式独立手算，准确季度、跨年lag1及lag4，不按行位置猜测 |
| 日志 | 字段/记录状态、NULL原因、运行和原始页可定位 |
| 增量 | 与同输入全量等价，未变公司复用，recent不缩小发布全集 |
| 质量 | 请求全集覆盖、空值/非法值、基线及告警恢复，离线不掩盖线上失效 |
| 回测 | 五组手算、次交易日收盘、费用、非重叠持有、缺价不删样本、完整报告 |
| 交付 | EXE/源码一致，锁依赖、许可、当前文档、原题、测试和SHA齐全 |

今日下载的官方PDF不能证明全部历史修订链。未披露记不可比，未覆盖另列，不把匹配率冒充覆盖率。

## 鲁棒性与恢复

- JSON/TOML结构、类型、缺键、重复、非法季度/代码：边界返回可定位错误，不抛堆栈，不先建业务产物。
- 外部信号：报告期不晚于可用时间，盘后不进入当天截面，属性不能覆盖组号/权重/持有日期。
- 行情：复权缺失不以未复权替代，截断、CSV结构、摘要损坏与失败清单须拒绝，保留失败原文。
- 状态：新坏值、冲突、跨范围更新和过期候选须使当前消费失效，旧快照仍可固定审计。
- 故障：锁/busy、采集终止、PREPARED、封存未激活、激活缺回执分别验证，resume遵守原计划。
- 路径：中文/空格、搬迁、错误工作区、无Python PATH、不可用代理及重复运行，不覆盖证据或旧产物。

## 源码检查

在source按README安装锁定开发依赖后运行：

~~~powershell
.\.venv\Scripts\python.exe -B -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts/finpipe_entry.py scripts/package_release.py scripts/verify_backtest_delivery.py
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts/finpipe_entry.py scripts/package_release.py scripts/verify_backtest_delivery.py
~~~

常规测试不依赖公网，使用独立人工工作区，预期不能由待测实现生成。最终ZIP须实际解压、验CRC和逐文件SHA，再复跑文档命令；构建目录测试不能替代包验收。

## 边界

回测为后复权价格研究，公告近似未核验完整历史版本、PIT股票池和可成交性。按用户要求暂不加分红现金流，不称税后总收益。盘中协议/补包、外部通知、生产任务注册和其他资产类型不计入本题交付。

独立干净Windows VM、OS级断网和Python3.11尚未验证；本机无Python PATH及不可用代理只证明对应条件。整机断电、物理磁盘故障和生产全市场规模也不宣称验收。
