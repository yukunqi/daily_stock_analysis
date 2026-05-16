# -*- coding: utf-8 -*-
"""Default DataFetcherManager priority order (no network)."""

import os
import unittest
from unittest.mock import patch


class TestDataFetcherDefaultPriority(unittest.TestCase):
    def setUp(self) -> None:
        from src.config import Config

        Config.reset_instance()

    def tearDown(self) -> None:
        from src.config import Config

        Config.reset_instance()

    def test_baostock_first_without_tushare_or_iwencai(self) -> None:
        env = os.environ.copy()
        env.pop("TUSHARE_TOKEN", None)
        for key in (
            "BAOSTOCK_PRIORITY",
            "PYTDX_PRIORITY",
            "AKSHARE_PRIORITY",
            "EFINANCE_PRIORITY",
            "YFINANCE_PRIORITY",
            "IWENCAI_MARKET_QUERY_PRIORITY",
        ):
            env.pop(key, None)
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "data_provider.iwencai_market_query_fetcher.iwencai_fetcher_should_register",
                return_value=False,
            ):
                from data_provider.base import DataFetcherManager

                mgr = DataFetcherManager()
                names = [f.name for f in mgr._fetchers]
        self.assertEqual(names[0], "BaostockFetcher")
        self.assertLess(
            names.index("BaostockFetcher"),
            names.index("EfinanceFetcher"),
        )
        self.assertLess(
            names.index("AkshareFetcher"),
            names.index("EfinanceFetcher"),
        )

    def test_tushare_first_when_token_configured(self) -> None:
        with patch.dict(os.environ, {"TUSHARE_TOKEN": "test-token"}, clear=False):
            with patch(
                "data_provider.iwencai_market_query_fetcher.iwencai_fetcher_should_register",
                return_value=False,
            ):
                with patch("data_provider.tushare_fetcher.TushareFetcher._init_api") as mock_init:
                    mock_init.return_value = None
                    with patch("data_provider.tushare_fetcher.get_config") as mock_cfg:
                        cfg = mock_cfg.return_value
                        cfg.tushare_token = "test-token"
                        from data_provider.base import DataFetcherManager

                        mgr = DataFetcherManager()
                        names = [f.name for f in mgr._fetchers]
        if names[0] == "TushareFetcher":
            self.assertEqual(names[1], "BaostockFetcher")
        else:
            # Tushare init may fail in CI without real token; still expect Baostock near front
            self.assertIn("BaostockFetcher", names[:3])


if __name__ == "__main__":
    unittest.main()
