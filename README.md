# 金融数据处理与特征工程

面向[机试题目](机试题目-金融数据处理与特征工程.md)的可执行项目：公开财报采集 → 原文归档 → 规范化与清洗 → SQLite版本存储 → 15个财务特征 → 质量门禁 → 固定快照、CSV、Parquet和HTML报告。

已运行真实数据：冻结沪深京50家非金融公司，2022—2025年16个输入季度，2023—2025年12个输出季度。取得2353/2400条报表记录；输出9000个特征位置，其中8482个有数值、518个保留NULL及原因。47条缺报表未伪造为“不适用”。详见[实际交付记录](docs/delivery_validation.md)与[原题要求对应](docs/requirements_traceability.md)。

## 一分钟运行

日常只修改根目录`config.py`，然后运行根目录`main.py`或双击`run.cmd`。以下五项配置控制自选股票、来源、输出报告期和数据目录；上一年比较历史由程序自动补齐，采集后同步生成全部15个因子。

```python
from pathlib import Path

from finpipe.config import RunConfig

CONFIG = RunConfig(
    stocks=["600519.SH"],
    source="eastmoney",
    start_period="2025Q1",
    end_period="2025Q4",
    output_dir=Path(__file__).parent / "output" / "financial",
)
```

股票须带`.SH/.SZ/.BJ`，适用自然年季度、人民币口径的非金融A股。报告期可写季度简写或准确季末日期，表示财报报告期而非公告日期。来源默认东方财富，也接受`东方财富`名称；未接入的来源在联网和创建工作区之前报错。

源码完成下方首次安装后直接执行：

```powershell
.\.venv\Scripts\python.exe main.py
```

在Python中也可以直接调用，返回原流水线结果及报告路径；导入模块不会联网：

```python
from main import run

result = run()
```

Windows完整交付包包含根目录`config.py`和`run.cmd`以及自带程序，修改配置后双击即可运行。控制台显示实际财报数量、因子位置和报告路径，CSV、Parquet、字典及质量结果一起生成；缺值仍保留原因。

从GitHub获取源码时，按下方“源码运行”安装并执行。仓库根目录包含`src`、`tests`、`config`和`docs`；虚拟环境、运行输出与EXE构建产物不进入Git。

拿到Windows完整交付包后，先双击解压根目录`START_HERE.html`，无需Python和网络即可看真实财务与回测结果。交付包中的原题、源码、测试和当前技术说明在`source`；小财务库在`examples/financial`，真实回测输入在`examples/backtest_real`。

回测默认使用披露累计加权ROE，报告首屏说明因子与分组；图表带真实日期轴、净值刻度、图例和等权参考，提供独立SVG及图表CSV。实际持有范围与请求范围分别列出，见[回测读法](docs/backtest.md)。

原有维护、离线演示和回测入口继续保留。以下命令在交付包的`windows/finpipe`目录执行，整个EXE及依赖目录必须保留。双击`run_demo.cmd`，或在PowerShell执行：

```powershell
.\finpipe.exe demo --workspace .\我的演示结果
.\finpipe.exe query --workspace .\我的演示结果 --company 600519.SH --period 2025Q4
.\finpipe.exe explain --workspace .\我的演示结果 --company 600519.SH --period 2025Q4 --feature revenue_growth_yoy
.\finpipe.exe health --workspace .\我的演示结果
```

开发目录的EXE位于`dist\finpipe\finpipe.exe`。`demo`自带3家×8季度×3表真实HTTP归档，完全离线；重复运行复用180个结果，不新增来源、清洗及裁决版本。CLI不等键盘输入，双击脚本显示实际HTML路径并保留窗口。

## 源码运行

需要Python 3.11以上；已实际验证3.12.7和3.13.1，3.11尚未实测。在源码根目录执行；如果解压了交付包，先进入其中的`source`目录。Windows推荐：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe -m finpipe demo --workspace .\output\demo
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

只运行时安装`requirements-runtime.txt`和项目即可。所有人工代码注释及docstring使用中文。配置中的相对工作区以配置文件目录为基准，显式`--workspace`以当前目录为基准。

简单入口读取的相对`output_dir`以`config.py`所在目录为基准，换目录启动不改变结果位置。库调用`finpipe.runner.run(RunConfig(...))`中的相对路径以调用目录为基准。原TOML及高级命令保持原语义；`finpipe.exe`无参数也读取根目录配置，或用`--settings`指定本地配置文件。早期[v0.1.4下载页](https://github.com/hhdandmmd/interview/releases/tag/v0.1.4)保留旧入口，简单入口从0.1.5开始提供。

## 在线、增量、恢复与核对

以下命令在源码根目录执行，不需要激活虚拟环境。Windows程序的等价命令见[可执行程序说明](docs/windows_usage.md)。

```powershell
.\.venv\Scripts\python.exe -m finpipe run --config config/demo.toml
.\.venv\Scripts\python.exe -m finpipe run --config config/default.toml
.\.venv\Scripts\python.exe -m finpipe run --config config/default.toml --refresh recent
.\.venv\Scripts\python.exe -m finpipe alerts --workspace output/live
.\.venv\Scripts\python.exe -m finpipe resume --workspace output/live --run-id <失败运行ID>
.\.venv\Scripts\python.exe -m finpipe replay --workspace output/live --from-db
.\.venv\Scripts\python.exe -m finpipe doctor --workspace output/live
.\.venv\Scripts\python.exe -m finpipe backup --workspace output/live --output output/backups
.\.venv\Scripts\python.exe -m finpipe reconcile --workspace output/demo --reference data/reference/original_reports/reference_contract.json --output output/原报告核对.json
```

默认配置读取随包冻结的50家公司名单；小样本为茅台、美的、格力。首次采完整范围，`recent`随后只扫描最新4季度，仍发布原范围完整结果。改变公司在有限季度范围内重算，其他公司复用精确版本。请求串行、间隔至少1秒，有限重试遵守同来源Retry-After；403不自动反复请求。

失败运行保存冻结计划、实际捕获目录和阶段。采集中断使用新完整会话，不拼跨会话分页；计算/导出失败复用原完整输入。旧运行和旧候选不能覆盖新head。原文、已发布快照及业务版本不原地修改。

真实财报采用最新完整观察作为当前来源快照：同一扫描只有一个版本即可选择新值；多个不同版本仍冲突，旧归档不能覆盖较新观察。所有旧值保留用于审计。该政策不把观察顺序或UPDATE_DATE冒充官方修订序列。0.1.1的新运行使用公式2和新来源选择政策，profile改变会重算，避免复用旧公式结果；旧冻结计划保留原口径。

当前范围受阻时普通查询拒绝旧结果，审计可显式`--snapshot`或`--allow-stale`。`--as-of`选择当时实际已发布的快照。确认告警不代表恢复；旧离线归档不能证明上游恢复。详细重跑、备份迁移和任务模板见[运行与维护说明](docs/implementation.md)。

独立核对针对演示样本：茅台、美的原报告34项参考中33项匹配、1项不可比；另有4个单季收入核对。左侧384字段中350项无独立参考，报告分别披露覆盖率和匹配率。参考公司不在默认50家公司名单中，不能据此声称全市场原报告核验。

## 代码维护与业务口径

| 位置 | 职责 |
| --- | --- |
| `catalog.py`、`config.py` | 字段、单位、期间、规则和冻结profile |
| `ingestion/` | HTTP、分页、限流、字节归档及中断检查点 |
| `transforms/`、`features/` | 无IO的清洗和金融计算，15公式及字典 |
| `source_records.py`、`storage/` | 来源裁决、观察事实、SQL约束和索引 |
| `quality.py`、`reconciliation.py` | 覆盖/空值/异常/基线、同源及独立核对 |
| `pipeline.py`、`publication.py` | 阶段顺序、冻结计划、血缘核验和条件发布 |
| `monitoring.py`、`ops.py` | 告警复发与恢复、只读健康诊断、备份 |
| `exports.py`、`cli.py` | 固定产物和统一命令入口 |
| `backtest/` | 纯分组计算、时间口径、公开小范围行情与回测报告 |

累计金额只在同年准确相邻季度做差，Q1直接取本期；同比严格滞后4季度。合并和归母利润分别使用。百分数转小数、流动比率保留倍数；增长率要求正基期，亏损不填零。坏时间、非法字段、缺历史和版本冲突有不同NULL原因。

公告日、实际观察、处理和真实发布时间分别保存。公开接口不能证明所有历史重述版本可比；累计差分带`source_assumed`警告，已知不可比输入停止差分及跨期比较，包括Q1同比和ROE变化；单期值及同期比率仍可展示。当前结果不宣称历史PIT或无前视偏差回测。

每个发布包提供5组CSV/Parquet、字典、质量JSON、HTML、封存manifest及SQL派生receipt。小数据每数据集一个Parquet文件，避免公司×季度小文件。SQLite是唯一权威，head按scope条件激活，封存目录本身不代表已发布。

进一步阅读：[维护说明](docs/implementation.md)、[当前架构](docs/architecture.md)、[数据口径](docs/data_contract.md)、[原报告证据](docs/original_report_validation.md)、[验收方案](docs/acceptance.md)、[本轮独立审核](docs/delivery_review_20261014.md)。当前说明在源码仓库的docs或Windows交付包的source/docs；历史对话与旧设计留档不进入Git。

## 特征分组回测

```powershell
.\.venv\Scripts\python.exe -m finpipe backtest --demo --output output/人工回测
```

支持五组等权收益、持有窗口净值、端点回撤、Rank IC、成员与排除原因，以及HTML/JSON/CSV报告。真实信号默认按本系统实际可用时间放出；显式`--time-policy publication_proxy`可将历史财报按公告及已知更新日期近似回放，报告保留真实抓取/发布时间并说明历史版本未核验。已实际执行10家公司2023—2025公开日线与财务特征的近似研究；另有人工手算样例。输入小体量且可独立复算，未打包`E:\backtest`的任何私有行情数据。具体来源、命令和口径见[回测说明](docs/backtest.md)。

盘中行情尚未接入真实协议和补包源；本次实现财报监控与核对、日线研究回测。独立无Python、断网VM验收仍需执行；清空PATH的测试不等同干净机器验证。
