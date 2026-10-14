# 文档导航

**第一次运行请看[项目README](../README.md)。** 完整Windows ZIP先打开最外层`START_HERE.html`，日常操作都在同一层完成。

## 按任务找说明

| 任务 | 入口 |
| --- | --- |
| 看题目是否完成及实际结果 | [题目逐项回应](../题目回应/README.md)、[示例入口](../题目回应/示例结果/index.html) |
| Windows离线演示、自选公司、查询与排错 | [Windows操作说明](windows_usage.md) |
| 开回测、选择因子、理解图表 | [回测使用说明](backtest.md) |
| 跑测试、核验金融正确性 | [测试与验收](../题目回应/测试与验收.md) |
| 查模块职责及设计理由 | [当前架构](architecture.md) |
| 添加因子或修改公式 | [因子维护](factors.md)、[中文注释规范](coding_standard.md) |
| 查字段、单位、时间与NULL口径 | [数据字典](data_dictionary.md)、[数据契约](data_contract.md) |
| 维护、增量刷新、备份与故障恢复 | [运行维护](implementation.md) |
| 查实际测过什么和未验证什么 | [实际验收记录](delivery_validation.md)、[原报告核对](original_report_validation.md) |

## 如何识别历史材料

`reviews/`是带日期与版本的评审档案；`delivery_validation.md`保留各版实际运行记录，请从顶部的当前版本读起。旧测试数量只代表当时的版本。

源码仓库还保留`implementation_plan.md`、`operations.md`、`storage.md`、`review_decisions.md`等早期设计。这些用于追溯取舍，**不作为当前操作手册**；完整ZIP仅选入相关说明和必要证据。日常运行无需遍历这些文件。

代码、数据样例与来源证据的位置见[题目回应](../题目回应/README.md)。人工案例和真实财报分别标识，可直接打开HTML报告或按说明复算。
