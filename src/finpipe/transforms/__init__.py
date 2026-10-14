"""来源规范化与字段清洗；只处理传入事实，不执行任何外部读写。"""

from finpipe.transforms.cleaning import normalize

__all__ = ["normalize"]
