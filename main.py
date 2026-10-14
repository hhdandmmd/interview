"""项目日常入口：使用根目录config.py，同步完成财报处理与因子生成。"""

from pathlib import Path

from finpipe.config import RunConfig
from finpipe.runner import load_settings
from finpipe.runner import main as start
from finpipe.runner import run as run_pipeline


def run(config: RunConfig | None = None) -> dict:
    """直接调用run()使用CONFIG，也允许传入独立运行配置；不在导入时联网。"""
    return run_pipeline(
        load_settings(Path(__file__).with_name("config.py")) if config is None else config
    )


if __name__ == "__main__":
    raise SystemExit(start(Path(__file__).with_name("config.py")))
