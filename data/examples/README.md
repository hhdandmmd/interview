# 小体量公开回测样例

`backtest_real`仅保留十家公司的固定财务信号、腾讯后复权日线和来源清单，共4个输入文件，合计约0.3 MiB。原始观察时间、来源字段及SHA摘要保持不变；不包含E盘私有行情库。图表、成员和收益等结果运行时生成，完整交付ZIP中的报告由打包脚本离线复算。

因子为披露累计加权ROE（`roe_weighted_ytd`），五组等权、持有20个交易日。`publication_proxy`按公告及已知更新日期近似放出当前财报值，历史财报版本未核验，不能作为严格PIT结果。后复权价格未单独核验现金分红。

在项目根目录离线复算：

```powershell
.\.venv\Scripts\python.exe -m finpipe backtest --signals data/examples/backtest_real/signals.json --market data/examples/backtest_real/market --time-policy publication_proxy --start 2023-01-01 --end 2025-12-31 --horizon 20 --output output/公开样例回测
```

输出目录需为新目录。完整交付包中的同一份示例位于`examples/backtest_real`，无需网络即可查看HTML或复算。
