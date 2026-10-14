# 存储、版本与发布契约 v3

修订日期：2026-10-13。本文是待实现契约，不能作为真实 DDL、故障恢复或性能已通过的证据。金融语义见[数据契约](data_contract.md)，字段见[字典](data_dictionary.md)，维护操作见[运维契约](operations.md)。

## 1. 单一权威与资源边界

SQLite 保存版本、裁决、输入、运行、告警和发布事实；原文文件保存不可变证据；CSV/Parquet/报告是固定快照产物。修改产物不能修复权威库。所有业务写入及诊断补入持同一个工作区 OS 锁；health 只读数据库并写独立诊断，不直接修改数据库。

默认 journal_mode=DELETE、synchronous=FULL、foreign_keys=ON，有限 busy timeout。网络及大文件导出在写事务外；写事务短，一次消费使用一致读视图。活跃库、锁、暂存目录在同一本机文件系统。WAL 仅在实际运行版本、平台、备份及崩溃检查通过后开启，不能仅按 Python 版本判断；当前环境 SQLite 3.45.3。[SQLite WAL 官方约束](https://sqlite.org/wal.html)

## 2. 对象、身份和约束

| 对象 | 业务职责与最小身份 |
| --- | --- |
| runs | run_id；冻结 RunPlan、profile、scope/universe、代码版本、父 run；终态与进度分别记录 |
| batches | run+source+statement+report_end+代码分片；预期键、页数、分页会话和完整性 |
| http_attempts | task/batch+page_session+page_number+attempt；每次请求的参数、状态、错误、冷却、时间与正文摘要 |
| source_pages | 成功归档尝试的视图或一对一定位映射；不重复维护 HTTP 事实 |
| universe_snapshots / expected_keys | 不可变名单及请求全集；期望键资格结论引用证据，运行引用其版本 |
| source_versions | source_version_id 与声明的业务内容摘要；原金融字段及原日期、精度、时区事实 |
| source_observations | 每次实际成功捕获的观察、尝试及行位置；同内容重复观察仍记录 |
| processing_profiles | 映射、清洗及参数、公式、字典与时间政策的不可变内容指纹 |
| clean_versions | 唯一(source_version_id,mapping_version,cleaning_rule_version)，处理状态及字段原因 |
| clean_income / clean_balance / clean_indicator | clean_version_id 主键/外键；类型化财务宽表，不用万能 value 列 |
| record_states | 完整源业务键的追加裁决；selected/conflict/missing、来源引用、真实 decision_observed_at 与证据 |
| feature_versions / feature_inputs | scope/universe、公司/期/因子、profile、公式及输入指纹；精确 clean/state/字段/期间及未满足期望键 |
| quality_events / quality_metrics / alerts | 规则事件、分子分母、同范围基线、影响面、恢复和去重身份 |
| dataset_snapshots | snapshot_id、scope/universe/profile、expected_parent_head、输入截止、manifest_hash、状态及实际发布序号/时间 |
| snapshot_records | 每快照每完整源业务键唯一；精确状态、可空 selected_clean_version_id、冻结 data_available_at/source_available_at |
| snapshot_features | 每快照每公司/报告期/因子唯一；精确 feature_version_id，含 NULL 结果 |
| workspace_head | scope_id 主键；仅指向该 scope 的 PUBLISHED 快照 |

source、security_code、report_end、statement_type、consolidation_scope 构成源完整业务键。scope_id 是冻结样本/范围的身份，与财报合并口径不同。首版只有一条真实观察时间链，不保留 mode 或 purpose 分支。profile 是域内发布口径，新 profile 可以发布新快照，但不创建第二套 head。

snapshot_records 包含输入上下文期和输出期的状态。selected 必须有精确 clean 引用，匹配其来源和快照 profile 的映射/清洗版本；该 clean 即便被隔离也保留引用及原因。conflict/missing 引用为 NULL。不另建第三套 clean 成员集合。

snapshot_features 的引用必须与目标键、scope、universe、profile 一致。每条 feature_input 必须匹配 snapshot_records 的精确状态、选定 clean、准确期间和冻结边界；未满足依赖核验期望键及裁决/扫描证据。外键、NOT NULL、CHECK、唯一约束和发布门禁各自承担可表达的约束，不用对象 ID 唯一代替业务键唯一。

## 3. 不可变身份与有限增量

响应字节 SHA-256 证明归档完整性；source_record_hash 识别来源业务内容。稳定序列化字段白名单、有限数、日期语法及 NULL，不包含抓取时间、页码、JSON 键顺序或时间政策投影。声明的金融字段/修订事实变化可以产生新源版本，未使用新增字段不会。

源内容、真实观察、裁决、清洗和公式结果分别追加。A→B→A 只有完整内容相同才能复用原 A 源版本；第三次真实观察与裁决仍是新事件。晚到旧源不覆盖已证实新源，无可靠先后关系时冲突。裁决矩阵见数据契约，不引入通用优先级打分器。

清洗和特征不得覆盖原版本。时间政策只生成 profile 投影，冻结在输入与 snapshot_records；source/state 的原时间事实不改写。最新无效也有非空实际状态边界，先选最新状态再判质量，禁止过滤坏状态后找旧好值。

首版以改变公司及有限期间筛选候选，保留精确审计引用；同数值但血缘不同允许新增版本。真实重复输入复用，数值、状态、血缘和实际求值次数分别统计。不建设字段版本库或第二套缓存身份。相同输入清单/profile 的增量与全量结果必须等价。

## 4. 文件与数据库的恢复协议

候选状态为 PREPARED / PUBLISHED / SUPERSEDED / ABORTED。run 的阶段只是进度；是否已发布以关联 snapshot 的数据库事实判断。

1. 持工作区锁，校验 profile/范围，读取该 scope 的当前 head 并冻结 expected_parent_head；完整处理后在短事务建立 PREPARED、两类成员及血缘。
2. 在 staging/snapshot_id 从固定候选读取，导出财务表、特征长表、面板、字典和质量报告。回读核验类型、行数、唯一键、NULL 及与 SQL 的一致性。
3. 写 manifest 并 seal，固定范围、输入、规则、文件摘要和 exported_at；不预填实际 published_at。
4. 同文件系统原子重命名到 snapshots/snapshot_id；既有目标只校验，不能覆盖。移动失败保留候选，数据库尚不可消费。
5. 在一个短 SQL 写事务重新读取 head，比较预期父；同时核验候选仍 PREPARED、完整文件摘要及门禁。只有匹配时分配该 scope 的单调发布序号、记录实际 published_at、设 PUBLISHED 并更新 head。
6. 提交成功后，由数据库确定性生成独立 publication_receipt.json，记录 snapshot_id、scope_id、publication_sequence、published_at、manifest_hash。这是可修复的派生凭据；manifest 不回写。

**父 head 比较与激活必须在同一事务。** 首次发布也验证当前无 head。scope_id 的唯一约束防止首次并发插入；OS 锁不能代替串行旧计划校验。实现可在 BEGIN IMMEDIATE 中比较并条件更新，匹配行数/首次插入结果必须断言；不把事务外 Python 检查作为最终依据。

若父 head 不匹配，候选变为 SUPERSEDED，保持原文件和运行证据，不取得生产发布时间，不更新 head 或当前成功健康基线。resume 只能解释和结束旧恢复；重新处理须开关联新 run，冻结当前 head 和输入选择。显式回滚也是新发布操作，不能借旧 resume 暗中回退。

| 中断位置 | 消费与恢复行为 |
| --- | --- |
| seal 前 | 候选不可见；按冻结计划完成或重建不完整导出 |
| seal 后、rename 前 | 校验暂存摘要后移动，不改成员 |
| rename 后、SQL 激活前 | 文件存在仍不可消费；校验父 head 再激活，过期则 SUPERSEDED |
| SQL 激活后、receipt 前 | 已发布；resume/doctor 只补回执与 run 结果，不重算、不重新激活、不改 published_at |
| 旧候选与新 head 不一致 | 保留审计；不得覆盖新快照或当前健康状态 |

首版删除 latest.json；数据库 head 是当前入口。文件消费者固定 snapshot ID 并核验 manifest+receipt，只能确认其曾发布；当前是否 blocked/stale 必须查询数据库或实际健康入口。离线包不能证明供应源现在健康。全局 gate 阻断保留上次成功快照，但默认当前查询非成功，显式 allow-stale 才能消费并标明滞后；固定历史快照仍可审计。

## 5. 查询边界与示意 SQL

默认当前查询在一致读事务中检查 scope 健康，再读取 workspace_head。query --snapshot 固定历史版本；query --as-of 只选该时点已经实际发布的一个完整快照，使用当时 profile、成员及输入边界，不运行今日算法还原未知过去。

下面只展示“先固定快照、再精确引用”的查询形状；不是已验收的业务 DDL或最终完整字段清单。调用者负责当前 blocked/stale、快照归属和日期参数规范化。

~~~sql
WITH picked AS (
    SELECT snapshot_id
    FROM dataset_snapshots
    WHERE scope_id = :scope_id
      AND status = 'PUBLISHED'
      AND published_at <= :as_of
    ORDER BY publication_sequence DESC
    LIMIT 1
)
SELECT f.security_code, f.report_end, f.feature_name,
       f.feature_value, f.feature_status, f.data_available_at
FROM picked AS p
JOIN snapshot_features AS m ON m.snapshot_id = p.snapshot_id
JOIN feature_versions AS f ON f.feature_version_id = m.feature_version_id
WHERE f.data_available_at <= :as_of
ORDER BY f.security_code, f.report_end, f.feature_name;
~~~

~~~sql
SELECT r.security_code, r.report_end, r.statement_type,
       s.state_status, r.selected_clean_version_id,
       c.quality_status, r.data_available_at, r.source_available_at
FROM snapshot_records AS r
JOIN record_states AS s ON s.record_state_id = r.record_state_id
LEFT JOIN clean_versions AS c
       ON c.clean_version_id = r.selected_clean_version_id
WHERE r.snapshot_id = :snapshot_id
  AND r.data_available_at <= :as_of
ORDER BY r.security_code, r.report_end, r.statement_type;
~~~

第二段不能按 source_version 连接所有 clean 版本，不能过滤 invalid/conflict 后复活旧值，也不能用 source_available_at 非空作为保留坏状态的条件。非空失效时间使新 NULL 在实际边界后可见。没有已发布历史快照返回无结果，不自动切换到源公告日推算的历史值。

索引围绕业务键裁决、scope 发布序号、feature 输入反查、run/批次及告警查询设计；实际 DDL、约束、EXPLAIN QUERY PLAN、规模与耗时是实现验收，不能靠示意 SQL 宣称数据库性能已验证。

## 6. Parquet、面板与字典

当前每快照每表一个文件：income、balance、indicator、features_long、features_panel；CSV、字典与质量报告随同快照。文件按稳定业务键排序，压缩和 row group 参数记录于清单。Arrow schema 显式保留 string 代码、date32 报告期、UTC timestamp、float64 值和真正 NULL；状态和版本不能在导出时丢失。

面板以冻结公司×12输出季度为网格，15 因子按列；缺值有原因表/长表引用，网格行数不充当真实可用覆盖。长表保留全部 9,000 理论格及逐因子状态。规范财务表包括输入上下文。

扩量后先测列投影、期间过滤、文件数及行组，再考虑报告年度粗分区；不按公司×季度×因子切碎。SQLite、CSV和Parquet对同一快照的值、状态、单位、边界及版本一致。[PyArrow Parquet 官方说明](https://arrow.apache.org/docs/python/parquet.html)

## 7. 迁移、备份与保留

编号 SQL 迁移维护 schema_version；profile 表达业务处理版本，两者不同。迁移前校验库版本并用 SQLite backup API 一致备份，记录源码/依赖、字典、schema 和摘要；短事务迁移，失败回滚，不无痕升级无法识别的库。

备份包含一致数据库、其引用原文和全部已发布清单/产物/回执；工作区写锁协调文件集合。恢复到独立目录，执行 integrity_check、foreign_key_check、原文摘要、快照文件及查询核验，运行真实离线 demo。数据库 busy/损坏明确失败，不采用随意复制活动库替代一致备份。[SQLite backup API](https://sqlite.org/backup.html)

首版不自动删除历史版本或原文。未来清理先做引用闭包和 dry-run，保护已发布快照、血缘、失败恢复及备份；没有实际磁盘需求时不实现清理框架。普通备份命令与完整恢复记录属于正式交付。

