# 第三轮交付审核：架构复杂度与交付体积

审核日期：2026-10-14（Asia/Shanghai）。对象为0.1.2源码及封存的`release/finpipe_0.1.2_20261013T164714Z.zip`。先阅读原题及AGENTS，再检查业务模块、实际DDL、命令、文档和ZIP逐项大小。本报告不改业务代码与封存包，不把建议记为完成。

结论：没有发现需要推倒重写的通用框架或平台化设计。主要冗余在历史设计与当前说明共用阅读入口，以及交付包重复携带证据。原题涉及财报更正、披露滞后、增量、质量与回测，用户要求追溯和恢复，不能靠合并状态或删除血缘缩小项目。先精简交付和说明，再按实际改动需要拆长函数。

## 实测规模

ZIP统计为条目压缩字节之和，不包含目录元信息；本地历史产物不属于业务源码。

| 对象 | 数量 | 实际大小与含义 |
| --- | --- | --- |
| 业务Python源码 | 33模块、6644行 | 287810字节，约281 KiB；12个class多数为业务数据类，无公式DSL、插件或任意DAG |
| SQLite | 固定22表、动态3张`clean_*`、5个显式索引 | 表各有实际用途；另有1个成功请求视图，主键与唯一约束还生成内部索引 |
| 0.1.2 ZIP | 1包 | 93054822字节，约88.74 MiB，主要是运行依赖和证据 |
| Windows目录 | 1607条目 | 压缩72383381字节，其中重复原报告证据10793319字节 |
| 源码数据证据 | 239条目 | 压缩16965522字节 |
| 3家公司财务备份 | 55条目 | 压缩605913字节，未包含全50家SQLite |
| 真实回测及复算输入 | 11条目 | 原始665706字节、压缩81233字节；约650 KiB，无E盘大行情 |
| 全部技术文档 | 48份 | 原始669478字节，源码和EXE各复制一次，每树压缩319401字节 |
| 本地release | 12926文件 | 3071998613字节，约2.86 GiB，多版本目录与ZIP重复 |
| 本地output | 3424文件 | 912885259字节，约870.59 MiB，多轮数据库与验收产物，未整树打包 |

Windows中PyArrow压缩24496729字节，NumPy运行库6678966字节，pandas6444362字节，承担Parquet与表格/分组计算。不能只按文件数删除PyInstaller依赖。许可证整树仅压缩189743字节，清理收益小，保留实际分发组件与许可。源码中的资源还包含真实演示归档，不能将整个src树大小误认为代码大小。

## 已证实问题与最小修复

### P1：正式文档入口仍声称主要功能“尚未实现”

位置：`docs/architecture.md:5`、`:21`、`:130`，`docs/storage.md:3`，`docs/operations.md:5`、`:17`、`:67`，`docs/acceptance.md:5`，`docs/implementation_plan.md:5`。

实际业务、EXE和简单日线回测已经存在；旧设计仍说未实现，architecture/operations写recent扫描8季度，当前实现和README为4季度。文件开头加历史提示无法消除正文矛盾，陌生读者仍需跨文档判断哪个命令和状态可信。

最小方案：当前入口只说明实际结构、约束、运行与维护路径，旧设计按字节移入评审历史。README只引向当前architecture、implementation、backtest、delivery_validation及requirements_traceability；验收标准与当前执行结果分开。原架构已按字节保存在本目录`architecture_before.md`，SHA256为`735ca928e40a8e7e1ea9b89bfcde8982953a4a8674e314ca0be86cd5715dc857`，当前architecture已重写。其余入口由主agent统一整理，不据此宣称所有历史说明已处理。

### P2：原报告证据完整重复，占约10.29 MiB压缩空间

位置：`scripts/package_release.py:45`和`:50`。`data/reference/original_reports`在source和windows/finpipe各复制9文件、14407606字节、压缩10793319字节。相同证据复制不增加独立核验覆盖。

最小方案：仅在source/data/reference/original_reports保留完整PDF、合同和核验脚本；同步调整Windows核对命令的--reference路径，验证从交付根目录和Windows目录运行。不要删原PDF或修改摘要。预计省10793319压缩字节，约10.29 MiB。

### P2：打包整树复制data/docs，默认含过多过程材料

位置：`scripts/package_release.py:44`、`:50`、`:69`。源码和测试不消费历史对话；旧基线和历次构建捕获不帮助面试官执行当前流程。未来加入data的研究文件也会默认流入发布包。采用明确白名单即可，无需新的发布插件框架。

下表“排除”只针对新默认包，本地证据、原封存包不删除：

| 精确内容 | 最小处理及压缩收益 |
| --- | --- |
| `data/reference/shared_dialogues/` | 默认排除9个历史对话文件，省512546字节，无代码/测试依赖 |
| `data/evidence/final_exe_after_schedule_fix_capture/` | 默认排除旧构建捕获，省189668字节 |
| `data/evidence/final_exe_source_freshness_capture/` | 同上，省189667字节 |
| `data/evidence/final_exe_online_capture_20261013/` | 同上，省189657字节 |
| `data/evidence/repair_v011_online_capture/` | 默认排除或单独历史附件，省189679字节 |
| `docs/reviews/baseline/` | 不作为当前入口，默认可排除，省24961字节 |
| `docs/reviews/round2/baseline/` | 同上，省78575字节 |
| `docs/implementation_plan.md`、`source_capture_implementation.md`、`review_validation.md`、`review_decisions.md` | 保留本地历史，默认可排除；每份文档树共15116压缩字节 |
| `windows/finpipe/docs/` | 只带当前运行/维护/回测/限制说明，或统一链接源码说明；全树319401压缩字节，主要减少阅读混淆 |

必须保留：完整真实`src/finpipe/resources/demo`；`data/evidence/source_probe.json`（`tests/test_probe_source.py:243`直接读取）；`data/reference/original_reports`（`tests/test_integration.py:303`读取参考合同）；`data/evidence/full_market_capture_20261013`（50家离线重放）；当前验收摘要；小财务备份和小回测输入。不能把历史data统删而破坏源码测试或复算。

完整财务manifest原始22063025字节，保存页面规范行与原始HTTP正文对应关系，压缩后约2.45 MiB。它是封存来源契约，不为减重重写清单、改观察时间或取消正文证据。

## 必要复杂度：应保留

| 结构 | 业务理由与删除风险 |
| --- | --- |
| 源版本与真实观察分表 | 相同内容能重复抓取；混为一体会伪造重放观察或无法解释A→B→A |
| 来源裁决与清洗版本分开 | 最新可能冲突、缺失或坏值；合并后容易跳过新版复活旧值 |
| 因子版本与精确输入 | 更正影响准确期间，NULL也有依赖；删除后无法证明增量等价和时间边界 |
| 快照两类成员 | 固定真实消费的记录及特征；否则因子解释可能连接到另一版本 |
| scope/profile与冻结运行计划 | 样本、期间、金融规则和恢复各有身份；否则改配置会偷改旧恢复口径 |
| PREPARED、SQL条件head与receipt | 文件和SQL不在同一事务；否则目录存在误当发布或旧候选覆盖新结果 |
| 质量事件/指标/告警 | 证据、基线和确认/恢复不同；不能确认后就关闭或覆盖原失败 |
| 回测计算/IO/行情/人工样例分模块 | 数学、时间投影、网络和示例独立维护；一文件混写反而难测 |

25表按职责分为范围规则5、运行请求4、来源清洗裁决7、特征输入2、质量告警3、发布成员head4。没有为每表复制CRUD服务，无动态插件、调度平台、跨资产抽象。用户已明确不加分红，保持当前后复权价格研究边界。

## 风险与可选优化

P2维护风险：`pipeline.py:109`的_process为411行，`quality.py:340`的evaluate_quality为304行，`ingestion/client.py:291`的collect为223行，`backtest/calculation.py:53`的run_groups为205行。长度不是缺陷证据，但修改者需要同时理解多个阶段和状态，审查成本高。

最小可选方案：按已有事务和阶段给_process提取来源裁决清洗、变更目标与版本复用、候选与质量判定等普通局部函数；质量函数按现有指标组拆纯统计。保持执行顺序和事务边界，复用增量、坏版失效及崩溃恢复测试；不增加阶段基类、Repository、任意DAG或万能上下文。本次交付不需要把所有长函数重新拆完。

P3可选：formula1/2及旧来源选择策略用于恢复已有冻结运行，不能顺手删除。依赖裁剪需另行实际构建和运行验证。release/output约3.7 GiB属于历史产物；若要节省磁盘先核验引用和未替代证据，再由用户决定备份及删除，本次不删除也不新增垃圾回收框架。

## 交付判断

架构规模可以交付，优先修正文档相互矛盾和重复打包两个已证实问题，再执行新包解压、离线演示、核对、源码测试和回测复算。当前不需要全局重构。让一次离线运行、一次NULL解释、一次回测复算各有直接命令与明确结果，比压缩业务状态更能降低面试官和维护者成本。
