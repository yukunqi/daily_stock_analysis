# -*- coding: utf-8 -*-
"""
Unit tests for optional free finance news providers.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

# Mock newspaper before search_service import (optional dependency).
if "newspaper" not in sys.modules:
    mock_np = MagicMock()
    mock_np.Article = MagicMock()
    mock_np.Config = MagicMock()
    sys.modules["newspaper"] = mock_np

from src.search_service import SearchService


class FinanceNewsProvidersTestCase(unittest.TestCase):
    """Tests for EastmoneyNews and RSSHubFinance providers."""

    @patch.dict(os.environ, {"EASTMONEY_NEWS_ENABLED": "true", "EASTMONEY_NEWS_API_KEY": "test-key"}, clear=True)
    @patch("src.search_service.requests.post")
    def test_eastmoney_news_provider_parses_nested_llm_response(self, mock_post: MagicMock) -> None:
        """EastmoneyNews should parse nested llmSearchResponse articles."""
        recent_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d 09:30:00")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "success": True,
            "data": {
                "data": {
                    "llmSearchResponse": json.dumps(
                        {
                            "data": [
                                {
                                    "title": "寒武纪:2026年第一季度报告",
                                    "content": "公告内容不应进入新闻流。",
                                    "date": recent_date,
                                    "informationType": "NOTICE",
                                    "jumpUrl": "https://example.com/notice/1",
                                },
                                {
                                    "title": "寒武纪受益国产 AI 芯片需求",
                                    "content": "国产 GPU 需求升温，寒武纪思元产品受到市场关注。",
                                    "date": recent_date,
                                    "informationType": "INV_NEWS",
                                    "source": "测试财经",
                                    "jumpUrl": "https://example.com/news/1",
                                },
                            ]
                        }
                    )
                }
            },
        }
        mock_post.return_value = mock_response

        service = SearchService(news_max_age_days=7)
        response = service.search_stock_news("688256", "寒武纪", max_results=3)

        self.assertTrue(response.success)
        self.assertEqual(response.provider, "EastmoneyNews")
        self.assertEqual(len(response.results), 1)
        self.assertEqual(response.results[0].title, "寒武纪受益国产 AI 芯片需求")
        self.assertEqual(response.results[0].source, "测试财经")
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["headers"]["apikey"], "test-key")

    @patch.dict(
        os.environ,
        {
            "RSSHUB_FINANCE_ENABLED": "true",
            "RSSHUB_BASE_URL": "http://rsshub.local",
            "RSSHUB_FINANCE_ROUTES": "/cls/telegraph",
        },
        clear=True,
    )
    @patch("src.search_service.requests.get")
    def test_rsshub_finance_provider_filters_feed_items_by_stock_query(self, mock_get: MagicMock) -> None:
        """RSSHubFinance should fetch configured routes and match stock-specific items."""
        recent_pub_date = format_datetime(
            datetime.now(timezone.utc) - timedelta(days=1),
            usegmt=True,
        )
        feed_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>寒武纪产业链订单预期升温</title>
      <description>AI 芯片国产替代继续受到市场关注。</description>
      <link>https://example.com/rss/1</link>
      <pubDate>{recent_pub_date}</pubDate>
      <source>财联社</source>
    </item>
    <item>
      <title>白酒板块午后走强</title>
      <description>消费板块活跃。</description>
      <link>https://example.com/rss/2</link>
      <pubDate>Sat, 16 May 2026 08:00:00 GMT</pubDate>
      <source>财联社</source>
    </item>
  </channel>
</rss>
""".format(recent_pub_date=recent_pub_date)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = feed_xml
        mock_get.return_value = mock_response

        service = SearchService(news_max_age_days=7)
        response = service.search_stock_news("688256", "寒武纪", max_results=5)

        self.assertTrue(response.success)
        self.assertEqual(response.provider, "RSSHubFinance")
        self.assertEqual(len(response.results), 1)
        self.assertIn("寒武纪", response.results[0].title)
        mock_get.assert_called_once_with(
            "http://rsshub.local/cls/telegraph",
            headers={"User-Agent": "daily-stock-analysis/finance-rsshub"},
            timeout=10,
        )


if __name__ == "__main__":
    unittest.main()
