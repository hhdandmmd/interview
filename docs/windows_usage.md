# Windows操作说明

本文用于**包含Windows程序的完整ZIP**。先解压整个包，之后所有命令都在**同时含有`START_HERE.html`、`config.py`、`run.cmd`的最外层目录**执行。源码安装见[项目README](../README.md)。

## 首次使用只做三步

1. 双击`START_HERE.html`，点击“查看财务结果”，先确认你希望得到的结果长什么样。
2. 双击同目录`run_demo.cmd`，等待“退出码：0”。它离线读取真实三家公司归档，输出180个因子位置。
3. 复制窗口中“报告路径”后面的完整路径，粘贴到浏览器地址栏。

演示结果在`output/demo`，运行摘要在`output/demo_result.json`。重复运行显示`NO_CHANGE`表示复用成功，不是失败。演示固定使用茅台、美的、格力，不读取`config.py`。

## 自己选公司并联网采集

右键最外层`config.py`，用记事本或代码编辑器打开。修改已有的`stocks`、`start_period`、`end_period`三行并保存，再双击`run.cmd`。

```python
    stocks=["600519.SH", "000333.SZ", "000651.SZ"],
    start_period="2025Q1",
    end_period="2025Q4",
```

这里只展示要修改的三行；保留文件其他内容。首次保持来源`eastmoney`及回测`enabled=False`。不要修改`source/config.py`或`windows/finpipe/config/*.toml`来控制此入口。

成功窗口会显示“运行完成”或“已复用结果”、记录数量和“查看报告”的准确路径。默认一家公司有60个因子位置；上面三家公司有180个位置。输出季度为2025年，程序还会自动抓取2024年比较数据。自选结果位于`output/financial`，与固定演示分开。

## 需要查询或检查时

在最外层文件夹的资源管理器地址栏输入`powershell`并回车，再复制命令。以下读取你刚才跑出的离线演示：

```powershell
.\windows\finpipe\finpipe.exe query --workspace .\output\demo --company 600519.SH --period 2025Q4
.\windows\finpipe\finpipe.exe explain --workspace .\output\demo --company 600519.SH --period 2025Q4 --feature revenue_growth_yoy
.\windows\finpipe\finpipe.exe health --workspace .\output\demo
.\windows\finpipe\finpipe.exe doctor --workspace .\output\demo
```

`query`输出数值，`explain`输出公式输入和状态原因，`health`检查当前健康，`doctor`检查数据库及文件完整性。查询自选公司的在线结果时，将`--workspace .\output\demo`改成`.\output\financial`，并修改公司和报告期。JSON用于进一步核验，日常看结果打开HTML和CSV即可。

尚未运行时，也可以直接查随包已有的财务样例：

```powershell
.\windows\finpipe\finpipe.exe query --workspace .\examples\financial --company 600519.SH --period 2025Q4
```

## 看回测或重跑回测

先在`START_HERE.html`点击“查看ROE五组回测”，无需修改配置。要用自己的公司和因子回测，按[完整配置示例](backtest.md)修改最外层`config.py`，仍运行同一个`run.cmd`。

要离线复算随包公开样例，在最外层PowerShell执行：

```powershell
.\windows\finpipe\finpipe.exe backtest --signals .\examples\backtest_real\signals.json --market .\examples\backtest_real\market --time-policy publication_proxy --start 2023-01-01 --end 2025-12-31 --horizon 20 --output .\output\回测复算_01
```

成功后打开`output/回测复算_01/report.html`。第二次将末尾输出目录换成`回测复算_02`；回测不会覆盖已有报告。此样例使用累计加权ROE与公告时间近似，历史财报版本未完整核验。

## 失败后怎么处理

| 窗口现象 | 处理方法 |
| --- | --- |
| 配置第几行语法错误 | 保留原文件结构，修正该行引号、逗号或括号，保存后重跑 |
| 网络错误、TLS错误、403 | 检查网络、代理或来源访问限制；保留窗口中的运行编号和采集证据路径。403不要连续重试 |
| 财报成功，但回测未完成 | 财报结果保留；查看回测总览中的人数、可用时间或行情错误，按[回测排错](backtest.md)调整 |
| 退出码4 | 同一数据目录有另一个写入任务，等它结束后重试 |
| 质量阻断或健康异常 | 先按下面命令看具体问题，再使用窗口实际给出的恢复命令；不要删除数据库来消除告警 |

```powershell
.\windows\finpipe\finpipe.exe alerts --workspace .\output\financial
.\windows\finpipe\finpipe.exe doctor --workspace .\output\financial
```

控制台给出的恢复命令已经带上真实工作区和运行编号，整行复制到PowerShell即可。`resume`恢复的是失败时的原配置；如果你希望换公司或报告期，编辑`config.py`后重新运行`run.cmd`。

首次上手不需要配置定时任务、TOML、备份或数据库。确有维护需求时再读[运行维护](implementation.md)。程序依赖整个`windows/finpipe`目录，请整体保留。已验证中文空格路径和无Python PATH运行，独立干净Windows机器尚未验收。
