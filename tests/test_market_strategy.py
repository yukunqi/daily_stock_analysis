# -*- coding: utf-8 -*-
"""Tests for market strategy blueprints."""

import unittest
from unittest.mock import MagicMock

from src.core.market_strategy import get_market_strategy_blueprint
from src.market_analyzer import MarketAnalyzer, MarketOverview


class TestMarketStrategyBlueprint(unittest.TestCase):
    """Validate CN/US strategy blueprint basics."""

    def test_cn_blueprint_contains_action_framework(self):
        blueprint = get_market_strategy_blueprint("cn")
        block = blueprint.to_prompt_block()

        self.assertIn("A股市场三段式复盘策略", block)
        self.assertIn("Action Framework", block)
        self.assertIn("进攻", block)

    def test_us_blueprint_contains_regime_strategy(self):
        blueprint = get_market_strategy_blueprint("us")
        block = blueprint.to_prompt_block()

        self.assertIn("US Market Regime Strategy", block)
        self.assertIn("Risk-on", block)
        self.assertIn("Macro & Flows", block)


class TestMarketAnalyzerStrategyPrompt(unittest.TestCase):
    """Validate strategy section is injected into prompt/report."""

    def test_cn_prompt_contains_strategy_plan_section(self):
        analyzer = MarketAnalyzer(region="cn")
        prompt = analyzer._build_review_prompt(MarketOverview(date="2026-02-24"), [])

        self.assertIn("策略计划", prompt)
        self.assertIn("A股市场三段式复盘策略", prompt)

    def test_us_prompt_contains_strategy_plan_section(self):
        analyzer = MarketAnalyzer(region="us")
        prompt = analyzer._build_review_prompt(MarketOverview(date="2026-02-24"), [])

        self.assertIn("Strategy Plan", prompt)
        self.assertIn("US Market Regime Strategy", prompt)

    def test_market_news_uses_trend_radar_without_active_search(self):
        analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
        analyzer.trend_radar_news_service = MagicMock()
        analyzer.trend_radar_news_service.build_market_news_items.return_value = [
            {"title": "全球通胀加剧债市风暴", "snippet": "buckets: macro, liquidity"}
        ]
        analyzer.search_service = MagicMock()

        news = analyzer.search_market_news(MarketOverview(date="2026-05-16"))

        self.assertEqual(len(news), 1)
        self.assertIn("通胀", news[0]["title"])
        analyzer.search_service.search_stock_news.assert_not_called()

    def test_market_news_falls_back_to_structured_snapshot_without_active_search(self):
        analyzer = MarketAnalyzer.__new__(MarketAnalyzer)
        analyzer.trend_radar_news_service = None
        analyzer.search_service = MagicMock()

        news = analyzer.search_market_news(MarketOverview(date="2026-05-16"))

        self.assertGreaterEqual(len(news), 1)
        analyzer.search_service.search_stock_news.assert_not_called()


if __name__ == "__main__":
    unittest.main()
