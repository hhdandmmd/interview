# 0.1.1交付审核整改记录

本轮针对[0.1.0独立交付审核](delivery_audit_20261013.md)整改。旧ZIP、旧快照和原始HTTP/PDF证据保持原封存；新代码、新profile、新工作区与新交付包分别记录。

| 问题 | 实际整改 | 验证 |
| --- | --- | --- |
| F01 真实金额更正不能选新版 | 最新完整观察作为当前来源快照；同一扫描异值仍冲突；新唯一观察可恢复，坏新版不回退，旧捕获不覆盖新事实 | 真实来源分支的人工更正、重复、新坏值、旧重放及A→B→A回归 |
| F02 Q1/ROE同比绕过不可比 | 公式2检查同比两端和跨期差；已知不可比或未解决重述风险输出NULL；单期披露及同期比率仍可用 | 本期/基期×两种风险的Q1/ROE反例，旧公式冻结复现，新profile强制重算 |
| F03 健康故障复发仍resolved | 每次诊断独立留事实，同一全局告警按最新事实更新；复发open，持续故障保留acknowledged，旧诊断不复活已解决问题 | 实际损坏临时发布HTML，故障→确认→恢复→复发→恢复完整回归 |
| F04 超时任务被离线成功掩盖 | 每scope的在线任务按自身冻结job与心跳检查超时 | 3小时前RUNNING在线任务+新离线SUCCEEDED，health正确unhealthy并给run_timeout |
| F05 文档命令不可直接执行 | 源码使用显式venv Python入口；Windows单独使用说明，包内README不引用缺失依赖文件 | 新包按Windows说明运行与源码明确入口验证 |
| F06 HTML首屏巨量JSON | 首屏质量摘要和5列因子样例，完整CSV/Parquet/JSON/字典提供本地链接 | 完整真实示例HTML从约13.4MB降至6362字节、256行，数据明细保留 |
| F07 当前文档与历史状态混杂 | JD对应改为当前状态；保留历史审核但明确对象和整改跳转；增量对照进入仓库 | 两独立人工数据库成本缺值和分母参数升级，比较全部FeatureResult字段 |

新运行采用formula_version=2和latest_complete_observation政策；mapping、dictionary、time-policy仍为1。旧冻结计划保留formula1和旧来源政策，已发布旧快照不回算。新profile与旧head不等价，即使来源没变也会求值，避免旧缓存绕过修复。

“最新”是本项目选择的数据源的当前完整观察，并非已验证的官方更正链。UPDATE_DATE、HTTP行序都没有被包装为权威修订序号。官方历史版本和历史PIT仍未实现；季度差分source_assumed警告保留。

实际回归：Python3.12.7为166 passed、289 subtests、172.60秒；独立Python3.13.1为166 passed、289 subtests、153.63秒。Ruff检查及45文件format通过，pip check无冲突。新增11个有效回归包含参数化金融反例和全量/增量对照；没有用测试数量替代交付判定。

同一真实全范围归档在`output/acceptance_full_release_v011`生成新快照`snapshot_a0885a44460f4231b7370353644d36ec`，profile为`profile_7a5c338c347275e20b02fc599dd560c0`。2353报表、9000位置、8482数值、518 NULL；全部值、状态、单位、原因和风险标记与旧真实样本一致，处理时间和版本身份属于新运行。doctor通过。源码与新版EXE对共同固定快照的9000条查询字段逐项一致。

证据入口为`data/evidence/repair_v011_full_validation.json`、`repair_v011_source_exe_equivalence.json`、`repair_v011_exe_smoke.json`及对应的新在线捕获。中文注释检查为194个docstring、18处注释，非中文说明0。

兼容性与选做边界保持明确：独立干净Windows机器、Python3.11、实际系统任务注册/外部通知、盘中行情和真实回测未执行。新的包搬迁验收回执随ZIP另存，包含ZIP SHA、文件摘要和实际命令结果。
