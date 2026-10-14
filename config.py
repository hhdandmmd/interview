"""日常运行配置：修改股票、来源和报告期后，执行main.py或双击run.cmd。"""

from pathlib import Path

from finpipe.config import BacktestConfig, RunConfig

CONFIG = RunConfig(
    # 支持多只非金融A股；上海用.SH，深圳用.SZ，北京用.BJ。
    stocks=["600519.SH"],
    # 当前真实接入东方财富，其他网站须完成接入后才能使用。
    source="eastmoney",
    # 指定要输出的财报季度；上一年比较数据由程序自动采集。
    start_period="2025Q1",
    end_period="2025Q4",
    # 固定在项目目录下，不受启动时所在目录影响。
    output_dir=Path(__file__).resolve().parent / "output" / "financial",
    backtest=BacktestConfig(
        # 启用五组回测须在stocks中选择至少五只股票。
        enabled=False,
        # 多个因子分别回测，共用行情；名称见各因子目录或导出的数据字典。
        factors=["roe_weighted_ytd", "revenue_growth_yoy"],
        groups=5,
        horizon=20,
        # observed使用真实已知时间；历史公告近似研究须显式选择publication_proxy。
        time_policy="observed",
        # 未指定market_dir时自动获取腾讯后复权日线；本地目录可离线复用行情。
    ),
)
