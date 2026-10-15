# 使用与技术文档

**第一次运行请看[项目README](../README.md)。** 完整Windows ZIP先打开最外层`START_HERE.html`，日常操作都在同一层完成。

本目录交付的内容围绕运行、理解设计和扩展因子，不包含开发约定、历史方案、评审意见或修复过程记录。

| 你要做什么 | 阅读入口 |
| --- | --- |
| 看项目怎样满足题目、查看示例结果 | [题目回应](../题目回应/README.md)、[示例入口](../题目回应/示例结果/index.html) |
| Windows离线演示、自选公司、查询与排错 | [Windows操作说明](windows_usage.md) |
| 选择回测因子、配置参数、理解图表 | [回测说明](backtest.md) |
| 运行测试、了解已验证范围 | [测试与验收](../题目回应/测试与验收.md) |
| 理解模块职责与设计取舍 | [架构说明](architecture.md) |
| 添加因子或修改公式 | [因子扩展](factors.md) |
| 查字段、单位、时间与NULL含义 | [字典使用说明](data_dictionary.md)、[数据口径](data_contract.md) |
| 增量刷新、备份、定时运行与故障恢复 | [运行维护](implementation.md) |

独立原报告的核对结果和参考依据分别在[核对JSON](../题目回应/示例结果/原报告核对.json)与[参考记录](../data/reference/original_reports/reference_records.json)中，可按页码查看随附原PDF。

本地仓库中的其他文档用于开发追溯，不随交付包发送。真实样例、人工反例和时间口径的限制在题目回应中分别说明。
