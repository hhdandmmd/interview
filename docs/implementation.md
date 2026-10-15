# 运行、维护与代码说明

本页供已经跑通项目、需要查询和维护的人使用。第一次运行见[项目README](../README.md)，Windows操作见[Windows说明](windows_usage.md)；不需要先理解下面的版本与发布机制才能使用。

本文命令统一在完整ZIP最外层执行，无需Python。源码开发者须先按[项目README](../README.md)创建`.venv`并安装依赖，再在源码根目录将`.\windows\finpipe\finpipe.exe`替换为`.\.venv\Scripts\python.exe -m finpipe`；完整ZIP中的源码根目录是`source`。实际环境和结果见[测试与验收](../题目回应/测试与验收.md)。

## 运行路径与产物

`demo`自带真实归档；`run`在线读取冻结公司和期间。每个工作区独立拥有`catalog.sqlite`、OS锁、`captures`、不可变`raw`、候选`staging`、发布`snapshots`和`diagnostics`。写操作使用单机单写锁；health仅以只读SQLite连接检查，另写诊断JSON，不等待业务锁。不要把工作区放到网络共享盘或把活动库通过文件同步工具直接复制。

命令退出码：0成功，2参数/输入/环境错误，3门禁阻断或诊断不健康，4工作区写锁冲突。正常CLI不暂停；根目录`run.cmd`和`run_demo.cmd`在结束后保留窗口，分别运行自选公司和固定真实演示。JSON输出UTF-8，CSV含UTF-8 BOM供Excel读取；Parquet明确保存字符串代码、date32、UTC时间及可空float64。

发布固定五个数据集：利润表、资产负债表、主要指标、特征长表和完整面板。每个各一份CSV/Parquet，另有字典JSON/Markdown、质量JSON、无外部资源HTML、封存manifest和派生publication receipt。小样本无需按股票或季度分区；每表一个文件降低碎片，扩大数据量时应依据读取粒度重新测量。

## 配置和维护

日常配置用根config.py的stocks和BacktestConfig，由main.py/run.cmd读取。高级`run --config`读取TOML中的scope.codes，`run --sample-size`从冻结名单选样；这两个高级入口不读取根config.py。配置类型、未知字段和规则标签在写入前校验，错误指出字段位置。

### 高级：最近季度增量刷新

需要调整网络、质量阈值或定期刷新时，在`source/config/default.toml`维护公司和期间，并指定同一个独立工作区。该文件默认50家公司，运行前先确认范围。首次完整运行与后续刷新分别为：

```powershell
.\windows\finpipe\finpipe.exe run --config .\source\config\default.toml --workspace .\output\advanced
.\windows\finpipe\finpipe.exe run --config .\source\config\default.toml --workspace .\output\advanced --refresh recent
```

源码环境除了替换程序前缀，还应使用`--config .\config\default.toml`。后续仍发布完整范围，但只扫描最近四个输入季度。日常run.cmd会重新扫描其配置范围，并复用未变的处理结果；它没有默认为最近季度扫描。

`catalog.py`维护字段名称、来源、单位、期间性质及说明；features/factors各目录的factor.py维护公式、单位、依赖和计算，registry.py登记，specifications.py生成字典，calculation.py负责网格及指纹，见[factors.md](factors.md)。新增因子需同时声明公式、单位、依赖、数学无定义政策和独立手算。`transforms/cleaning.py`与`quality.py`分别负责局部输入处理和整个候选的质量预算，不把网络和数据库塞进计算函数。

`config/default.toml`的网络、质量和job参数可调整。新运行使用mapping/dictionary/time-policy版本1、formula版本2及latest_complete_observation来源政策，未知版本直接拒绝。旧冻结运行仍可按原formula1和legacy_conflict政策恢复，不混入新配置；正式新结果应在默认新profile重放。清洗规则身份包含参数摘要；质量预算、公式等整体纳入profile。修改口径创建新身份并重算，禁止改旧处理结果。

完整且成功扫描确定当前来源快照：同一业务键只有一个版本时选该版，包括真实金额更正和坏新版；同一扫描出现多个版本则冲突，不能按页序选一行。较旧捕获不能覆盖较新扫描或观察事实；后续唯一的新观察可解除冲突。历史来源、观察、旧裁决和旧快照均保留。UPDATE_DATE不是已验证的官方修订序号，当前观察政策不提供历史修订链或PIT证明。明确人工修订序号仅用于隔离夹具。

当前财报新增/更正按改变公司和有限输出期间重算，其他公司复用精确特征版本。来源、清洗、裁决身份分开，正常重复重放不伪造新的HTTP观察。旧捕获不能覆盖较新扫描事实，`replay --run-id`保持原输出范围；要重现旧发布直接固定snapshot。原证据在新工作区重放会产生新的真实处理/裁决时间，不宣称处理历史穿越。

## 告警到恢复

1. `alerts`查看上下文：运行、业务键、字段、规则、原证据、影响下游及重跑命令。确认用`alerts --ack <告警ID>`，不等于恢复。
2. 采集失败检查运行冻结plan指向的`active_capture/checkpoint.json`、请求及失败正文。403需定位来源访问限制；Retry-After未到期时同来源的新会话仍不得请求。
3. `resume --run-id <ID>`：未完整采集使用新完整会话；已完整输入的处理/导出失败重用原证据。`doctor --repair`只补派生回执及已发布运行结果，不改来源值、不自动发布候选。
4. 修复采集错误需在失败之后完成覆盖失败范围的新扫描，并恢复下游。旧归档和覆盖不足的recent扫描不能解除告警。质量问题即使覆盖scope最后原因，原采集恢复义务仍在告警中保留。
5. 复发重新open、持续故障保留确认状态并更新上下文；旧事件仍在quality_events审计。独立health诊断由下一个持锁运行或doctor幂等补入；只有更新且完整的healthy诊断能关闭对应健康告警。

## 发布与备份迁移

候选成员和完整质量报告先事务保存为PREPARED，随后导出回读、seal并将摘要锚定SQL、同盘改名，最后在短事务比较scope父head后激活。PREPARED不可查询。旧候选标SUPERSEDED，不能倒退head或健康状态。恢复验证业务键全集、profile、精确clean/state、输入边界、文件集合和摘要。SQL激活后receipt丢失仅补receipt，不重算或改发布时间。

`backup`使用SQLite backup API，并在同一业务锁下复制固定原文和已发布目录，完成后执行doctor。恢复时复制整个备份目录作为新工作区，再运行：

```powershell
.\windows\finpipe\finpipe.exe doctor --workspace "D:\恢复的财报数据"
.\windows\finpipe\finpipe.exe query --workspace "D:\恢复的财报数据" --company 600519.SH --period 2025Q4
.\windows\finpipe\finpipe.exe replay --workspace "D:\恢复的财报数据" --from-db
```

冻结plan的原逻辑身份不变，工作区内证据路径按新目录重定位；原请求事实中的历史绝对路径保留用于审计。doctor检查SQLite完整性、外键、冻结原文清单/正文和已发布清单，不以目录存在认定健康。采集中断checkpoint用于定位和冷却，不能拿它代替完整manifest发布。

## 定时运行与边界

在线失败的CLI摘要限制长度，完整异常保留在运行记录和原HTTP证据中；返回实际capture/manifest/checkpoint路径，以及可直接执行的PowerShell恢复命令。重试恢复沿用原冻结计划，不用新配置改变旧失败运行的含义。

`scripts/scheduled_job.ps1`提供run或health的等价入口，保存JSON、日志和实际退出码。可由Windows任务计划程序每24小时调用run、每5分钟调用health；配置job规格应与实际周期一致。模板不会自动注册系统任务或发送外部通知。

该脚本使用高级CLI，不读取日常config.py；注册任务前须确认其默认公司范围及参数符合任务。自选公司日常运行直接使用run.cmd，定时任务不属于首次演示步骤。

任务未启动、上次失败和心跳超时独立检查；财报固定历史内容长期不变不等于行情停更。health本身未启动需要OS监督。盘中行情涉及休市、停牌、序号域、乱序、时钟误差和补包，不属于本次财报实现。新增日线分组研究及时间边界见[回测说明](backtest.md)，历史修订版本及交易可实现性尚未核验。
