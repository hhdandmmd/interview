# 金融数据处理与特征工程

选择公司和财报季度，程序会自动从东方财富获取财报，完成清洗、存储和15个财务因子的计算，并输出可查看的报告。

**第一次看项目：先读[题目逐项回应](题目回应/README.md)，再在本地用浏览器打开[示例结果](题目回应/示例结果/index.html)。** 示例已经准备好，不需要先联网运行。

## 1. 怎么运行

### 收到完整 Windows ZIP

1. 解压整个 ZIP，打开根目录 `START_HERE.html`，先看已有结果。
2. 编辑根目录 `config.py`，填写公司和财报季度。
3. 双击根目录 `run.cmd`。完成后，控制台会显示结果目录和报告路径。

此方式不需要安装 Python。保留 `windows/finpipe` 整个目录，其中包含程序依赖；代码和详细说明位于 `source`。

### 从 GitHub 下载源码

在源码根目录打开 PowerShell，首次安装执行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

随后修改 `config.py`，双击 `run.cmd`，或者执行：

```powershell
.\.venv\Scripts\python.exe main.py
```

项目声明 Python 3.11 以上，推荐3.13；已实测3.12与3.13，3.11尚未实测。依赖文件会一起安装本地项目，无需激活虚拟环境。

## 2. 怎么选择公司和时间

日常只编辑根目录的 `config.py`。例如：

```python
from pathlib import Path

from finpipe.config import BacktestConfig, RunConfig

CONFIG = RunConfig(
    stocks=["600519.SH", "000333.SZ", "000651.SZ"],
    source="eastmoney",
    start_period="2025Q1",
    end_period="2025Q4",
    output_dir=Path(__file__).resolve().parent / "output" / "financial",
    backtest=BacktestConfig(enabled=False, factors=["roe_weighted_ytd"]),
)
```

| 配置 | 含义 |
| --- | --- |
| `stocks` | 公司列表，股票代码保留交易所：上海`.SH`、深圳`.SZ`、北京`.BJ` |
| `source` | 财报来源，当前支持`eastmoney`或`东方财富` |
| `start_period / end_period` | 要输出的财报季度，例如`2025Q1—2025Q4`，也接受准确季末日期 |
| `output_dir` | 数据和结果存放位置，相对路径以配置文件目录为基准 |

这些日期表示**财报报告期**。程序会自动补齐上一年比较数据，用于同比和单季计算。当前支持自然年季度、人民币口径的非金融A股；其他网站需要完成接入后才能选择。

## 3. 运行后看什么

控制台会给出 `report.html` 的完整路径。用浏览器打开即可查看公司财务处理结果，并进入完整产物：

| 文件 | 内容 |
| --- | --- |
| `report.html` | 质量摘要、因子样例与结果链接 |
| `features_long.csv / .parquet` | 因子长表：证券代码、报告期、名称、数值、状态和原因 |
| `features_panel.csv / .parquet` | 按公司和季度对齐的因子宽表 |
| `income / balance / indicator` 的CSV与Parquet | 规范化后的三类财报 |
| `dictionary.md / .json` | 字段含义、单位、公式和数据结构 |
| `quality.json` | 数据覆盖、缺失、异常及核对明细 |

原始响应和SQLite数据库也保存在工作区。重复运行会识别未变输入并复用结果；缺值保留原因，不填零。

只想离线体验一次完整流程，可执行：

```powershell
.\.venv\Scripts\python.exe -m finpipe demo --workspace output/demo
```

这个演示使用茅台、美的、格力的真实HTTP归档，包含72份报表，生成180个因子位置；不是人工财报。

## 4. 可选：因子回测

在现有 `config.py` 的 `BacktestConfig` 中设置 `enabled=True`，通过 `factors` 列表选择因子，例如 `roe_weighted_ytd`、`revenue_growth_yoy`。五组回测至少需要五家公司；分组、持有期、费用、交易区间和行情来源均可配置，见[回测配置](docs/backtest.md)。

财报处理成功后，每个选定因子分别生成报告，结果位于工作区 `backtests` 下，打开 `index.html` 查看。未提供本地行情时，程序会获取所选股票的小范围腾讯后复权日线。

**默认按本系统真实已知时间使用财报，历史区间可能没有有效窗口。** 显式选择 `publication_proxy` 才做公告时间近似研究；今天抓到的历史财报可能已更正，不能据此宣称严格历史PIT。

随附[五组回测示例](题目回应/示例结果/分组回测/report.html)使用披露累计加权ROE，图表有日期轴、净值轴、分组图例和等权参考，报告注明实际覆盖范围。

## 5. 面试审核与代码维护

| 要看什么 | 入口 |
| --- | --- |
| 对题目每一项的回答、代码位置与实际结果 | [题目回应](题目回应/README.md) |
| 测试案例、复现命令和验证边界 | [测试与验收方案](题目回应/测试与验收.md) |
| 建议截哪些画面 | [截图清单](题目回应/截图清单.md) |
| 代码模块怎样配合 | [架构说明](docs/architecture.md) |
| 添加新因子 | [因子维护说明](docs/factors.md)、`src/finpipe/features/factors/` |
| 查询、增量刷新、失败恢复、备份和核对 | [运行维护说明](docs/implementation.md) |

`src/` 是业务代码，`tests/` 是自动测试，`scripts/` 是构建和验收工具，`docs/` 保留技术说明。原始[机试题目](机试题目-金融数据处理与特征工程.md)也在根目录。

本项目交付财报处理与简单日线分组研究。盘中行情、实盘撮合、现金分红单独核验及严格历史版本核验未纳入本次实现；独立干净Windows虚拟机尚未验收。
