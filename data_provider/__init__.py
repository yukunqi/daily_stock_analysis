# -*- coding: utf-8 -*-
"""
===================================
数据源策略层 - 包初始化
===================================

本包实现策略模式管理多个数据源，实现：
1. 统一的数据获取接口
2. 自动故障切换
3. 防封禁流控策略

数据源优先级（动态调整，数字越小越优先）：
【配置了 TUSHARE_TOKEN 时】
1. TushareFetcher (Priority -1) — 最高
2. BaostockFetcher (0) → PytdxFetcher (1) → AkshareFetcher (2)
3. EfinanceFetcher (3) → IwencaiMarketQueryFetcher (4, 启用时) → YfinanceFetcher (5)

【未配置 TUSHARE_TOKEN 时】
1. BaostockFetcher (0) — 默认日线主源
2. PytdxFetcher (1) → AkshareFetcher (2) → TushareFetcher (2, 不可用则跳过)
3. EfinanceFetcher (3) → Iwencai (4) → Yfinance (5)

实时行情顺序由 REALTIME_SOURCE_PRIORITY 单独控制（默认仍优先问财/腾讯等）。

提示：优先级数字越小越优先，同优先级按初始化顺序排列

Heavy fetcher modules are lazy-loaded via __getattr__ so ``import data_provider``
only pulls BaseFetcher / DataFetcherManager dependencies by default.
"""

import importlib
from typing import Any

from .base import BaseFetcher, DataFetcherManager
from .us_index_mapping import is_us_index_code, is_us_stock_code, get_us_index_yf_symbol, US_INDEX_MAPPING

_LAZY_EXPORTS = {
    "EfinanceFetcher": ("efinance_fetcher", "EfinanceFetcher"),
    "AkshareFetcher": ("akshare_fetcher", "AkshareFetcher"),
    "TushareFetcher": ("tushare_fetcher", "TushareFetcher"),
    "PytdxFetcher": ("pytdx_fetcher", "PytdxFetcher"),
    "BaostockFetcher": ("baostock_fetcher", "BaostockFetcher"),
    "YfinanceFetcher": ("yfinance_fetcher", "YfinanceFetcher"),
    "IwencaiMarketQueryFetcher": ("iwencai_market_query_fetcher", "IwencaiMarketQueryFetcher"),
    "is_hk_stock_code": ("akshare_fetcher", "is_hk_stock_code"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        mod_name, attr_name = _LAZY_EXPORTS[name]
        mod = importlib.import_module(f"{__name__}.{mod_name}")
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseFetcher",
    "DataFetcherManager",
    "EfinanceFetcher",
    "AkshareFetcher",
    "TushareFetcher",
    "PytdxFetcher",
    "BaostockFetcher",
    "YfinanceFetcher",
    "IwencaiMarketQueryFetcher",
    "is_us_index_code",
    "is_us_stock_code",
    "is_hk_stock_code",
    "get_us_index_yf_symbol",
    "US_INDEX_MAPPING",
]
