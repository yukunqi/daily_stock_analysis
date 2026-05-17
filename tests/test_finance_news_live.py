# -*- coding: utf-8 -*-
"""
Opt-in live integration tests for finance news intelligence providers.

These tests are intentionally skipped by default. Enable them with:

    RUN_FINANCE_NEWS_LIVE=1 python -m unittest tests.test_finance_news_live
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Mock newspaper before search_service import when the optional dependency is absent.
if "newspaper" not in sys.modules:
    mock_np = MagicMock()
    mock_np.Article = MagicMock()
    mock_np.Config = MagicMock()
    sys.modules["newspaper"] = mock_np

_REPO_ROOT = Path(__file__).resolve().parents[1]
if load_dotenv is not None:
    load_dotenv(_REPO_ROOT / ".env", override=False)

from src.market_analyzer import MarketAnalyzer
from src.search_service import RSSHubFinanceSearchProvider, SearchResponse, SearchResult, SearchService


def _live_enabled() -> bool:
    """Return whether live finance-news tests should run."""
    return os.getenv("RUN_FINANCE_NEWS_LIVE", "").strip() == "1"


def _skip_unless_live(test_case: unittest.TestCase) -> None:
    """Skip tests unless the explicit live-test flag is enabled."""
    if not _live_enabled():
        test_case.skipTest("Set RUN_FINANCE_NEWS_LIVE=1 to run finance-news live integration tests")


class FinanceNewsLiveIntegrationTestCase(unittest.TestCase):
    """Live integration tests for finance news providers and service entry points."""

    def setUp(self) -> None:
        _skip_unless_live(self)

    def test_eastmoney_provider_returns_stock_news_and_excludes_notice_report(self) -> None:
        """EastmoneyNews should return recent stock news while default filters exclude notices/reports."""
        api_key = os.getenv("EASTMONEY_NEWS_API_KEY", "").strip()
        if not api_key:
            self.skipTest("EASTMONEY_NEWS_API_KEY is required for EastmoneyNews live validation")

        env = {
            "EASTMONEY_NEWS_ENABLED": "true",
            "EASTMONEY_NEWS_API_KEY": api_key,
            "EASTMONEY_NEWS_INCLUDE_TYPES": "",
            "EASTMONEY_NEWS_EXCLUDE_TYPES": "",
            "RSSHUB_FINANCE_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False):
            service = SearchService(news_max_age_days=7)
            response = service.search_stock_news("688256", "寒武纪", max_results=5)

        self.assertTrue(response.success, response.error_message)
        self.assertEqual(response.provider, "EastmoneyNews")
        self.assertGreater(len(response.results), 0)
        for result in response.results:
            snippet = (result.snippet or "").upper()
            self.assertNotIn("INFORMATIONTYPE: NOTICE", snippet)
            self.assertNotIn("INFORMATIONTYPE: REPORT", snippet)

    def test_rsshub_provider_fetches_configured_finance_route(self) -> None:
        """RSSHubFinance should fetch at least one configured finance route."""
        base_url = os.getenv("RSSHUB_BASE_URL", "").strip()
        if not base_url:
            self.skipTest("RSSHUB_BASE_URL is required for RSSHubFinance live validation")

        routes_env = os.getenv("RSSHUB_FINANCE_ROUTES", "").strip()
        routes = [route.strip() for route in routes_env.split(",") if route.strip()] or ["/cls/telegraph"]
        provider = RSSHubFinanceSearchProvider(base_url=base_url, routes=[routes[0]])

        response = provider.search("A股 市场 财经 新闻 最新", max_results=3, days=30)

        self.assertTrue(response.success, response.error_message)
        self.assertEqual(response.provider, "RSSHubFinance")
        self.assertGreater(len(response.results), 0)

    def test_search_service_comprehensive_intel_runs_for_real_a_share_symbol(self) -> None:
        """SearchService.search_comprehensive_intel should run against one real A-share symbol."""
        api_key = os.getenv("EASTMONEY_NEWS_API_KEY", "").strip()
        if not api_key:
            self.skipTest("EASTMONEY_NEWS_API_KEY is required for comprehensive live validation")

        env = {
            "EASTMONEY_NEWS_ENABLED": "true",
            "EASTMONEY_NEWS_API_KEY": api_key,
            "RSSHUB_FINANCE_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=False), patch("src.search_service.time.sleep"):
            service = SearchService(news_max_age_days=7)
            results = service.search_comprehensive_intel("688256", "寒武纪", max_searches=1)

        self.assertIn("latest_news", results)
        response = results["latest_news"]
        self.assertTrue(response.success, response.error_message)
        self.assertEqual(response.provider, "EastmoneyNews")
        self.assertGreater(len(response.results), 0)


class MarketNewsDedupRegressionTestCase(unittest.TestCase):
    """Regression coverage for market-news deduplication without live network calls."""

    def test_market_analyzer_search_market_news_deduplicates_by_url_or_title(self) -> None:
        """MarketAnalyzer.search_market_news should drop repeated URL/title entries across feed queries."""
        duplicated_by_url = SearchResult(
            title="A股市场热点扩散",
            snippet="市场热点扩散。",
            url="https://example.com/news/1",
            source="RSSHub",
        )
        same_url_new_title = SearchResult(
            title="不同标题但同一链接",
            snippet="重复链接。",
            url="https://example.com/news/1",
            source="RSSHub",
        )
        duplicated_by_title = SearchResult(
            title="无链接重复标题",
            snippet="第一次无链接。",
            url="",
            source="RSSHub",
        )
        same_title_without_url = SearchResult(
            title="无链接重复标题",
            snippet="第二次无链接。",
            url="",
            source="RSSHub",
        )

        fake_search_service = SimpleNamespace(
            search_stock_news=MagicMock(
                side_effect=[
                    SearchResponse(
                        query="query-1",
                        results=[duplicated_by_url, duplicated_by_title],
                        provider="RSSHubFinance",
                    ),
                    SearchResponse(
                        query="query-2",
                        results=[same_url_new_title, same_title_without_url],
                        provider="RSSHubFinance",
                    ),
                ]
            )
        )
        analyzer = MarketAnalyzer(search_service=fake_search_service)
        analyzer.profile = SimpleNamespace(news_queries=["A股 市场", "财经 新闻"])

        news = analyzer.search_market_news()

        self.assertEqual(len(news), 2)
        self.assertEqual(news[0].title, "A股市场热点扩散")
        self.assertEqual(news[1].title, "无链接重复标题")


if __name__ == "__main__":
    unittest.main()
