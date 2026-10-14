# Windows可执行程序使用说明

先双击交付包根目录`START_HERE.html`查看真实结果和首次步骤。本文命令在`windows/finpipe`目录执行；整个EXE及依赖目录必须保留，不需要Python。双击`run_demo.cmd`离线演示，结果在用户目录的`Finpipe/demo`，窗口显示实际HTML路径和运行摘要位置。

在本目录打开PowerShell，直接复制执行：

```powershell
.\finpipe.exe demo --workspace .\我的演示结果
.\finpipe.exe query --workspace .\我的演示结果 --company 600519.SH --period 2025Q4
.\finpipe.exe explain --workspace .\我的演示结果 --company 600519.SH --period 2025Q4 --feature revenue_growth_yoy
.\finpipe.exe health --workspace .\我的演示结果
.\finpipe.exe doctor --workspace .\我的演示结果
.\finpipe.exe replay --workspace .\我的演示结果 --from-db
```

`demo`完全离线，首次发布180个特征，重复运行复用结果。打开返回路径中的`report.html`查看质量摘要、因子样例和完整产物链接；NULL保留原因。

在线演示及默认50家公司范围：

```powershell
.\finpipe.exe run --config .\config\demo.toml
.\finpipe.exe run --config .\config\default.toml
.\finpipe.exe run --config .\config\default.toml --refresh recent
.\finpipe.exe alerts --workspace .\output\live
.\finpipe.exe resume --workspace .\output\live --run-id <失败运行ID>
.\finpipe.exe backup --workspace .\output\live --output .\output\backups
.\finpipe.exe reconcile --workspace .\我的演示结果 --reference ..\..\source\data\reference\original_reports\reference_contract.json --output .\原报告核对.json
```

配置中的工作区相对于配置文件，显式`--workspace`相对于当前目录。上面默认配置的结果在本目录`output/live`，小样本在线结果在`output/demo_online`。程序返回JSON和退出码，运行成功不等于全部输入有值；告警确认不等于恢复。

交付包根目录的`START_HERE.md`提供3家公司已核验财务示例及10家公司小体量回测输入的实际路径，无需先联网即可查询和复算。源码和依赖文件位于交付包的`source`目录，50家公司公开财务归档可重放生成完整结果，见`docs/backtest.md`。安装与维护请在源码目录阅读`README.md`。

完全离线的人工分组演示：

```powershell
.\finpipe.exe backtest --demo --output .\人工分组回测
```

真实回测默认`observed`时间；显式`publication_proxy`为公告及已知更新日期近似研究，不能宣称严格历史PIT。复算及在线获取方法见包根目录的`source/docs/backtest.md`；重复回测须换新输出目录。

当前财报不构成历史PIT或无前视偏差回测。独立干净Windows机器尚未验收，本机搬迁和无Python PATH验证见包根目录`source/docs/delivery_validation.md`。技术说明和官方PDF集中在source，EXE目录不重复保存。
