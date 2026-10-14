"""日常运行配置：修改股票、来源和报告期后，执行main.py或双击run.cmd。"""

from pathlib import Path

from finpipe.config import RunConfig

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
)
