# -*- coding: utf-8 -*-
"""Tests for URL article content extraction and caching."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.services.news_content_fetcher import NewsContentFetcher


class NewsContentFetcherTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.temp_dir.name) / "news_cache.db"
        self.fetcher = NewsContentFetcher(str(self.cache_path), timeout=1, max_chars=80, cache_ttl_hours=24)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_fetch_uses_newspaper_and_caches_success(self) -> None:
        with patch.object(self.fetcher, "_extract_with_newspaper", return_value="第一段内容。\n\n第二段内容。") as extract, \
             patch.object(self.fetcher, "_extract_with_bs4", return_value="") as fallback:
            first = self.fetcher.fetch("https://example.com/a", title="新闻标题", source="来源")
            second = self.fetcher.fetch("https://example.com/a")

        self.assertEqual(first.status, "success")
        self.assertFalse(first.from_cache)
        self.assertEqual(second.status, "success")
        self.assertTrue(second.from_cache)
        self.assertIn("第一段内容", second.excerpt)
        extract.assert_called_once()
        fallback.assert_not_called()

    def test_fetch_falls_back_to_bs4_when_newspaper_empty(self) -> None:
        with patch.object(self.fetcher, "_extract_with_newspaper", return_value="") as extract, \
             patch.object(self.fetcher, "_extract_with_bs4", return_value="网页正文来自 p 标签。") as fallback:
            result = self.fetcher.fetch("https://example.com/b")

        self.assertEqual(result.status, "success")
        self.assertIn("网页正文", result.content)
        extract.assert_called_once()
        fallback.assert_called_once()

    def test_invalid_url_does_not_touch_network(self) -> None:
        with patch.object(self.fetcher, "_extract_with_newspaper") as extract:
            result = self.fetcher.fetch("not-a-url")

        self.assertEqual(result.status, "invalid")
        extract.assert_not_called()

    def test_bs4_extracts_article_text(self) -> None:
        response = MagicMock()
        response.text = """
        <html><body><header>nav</header><article><p>第一段</p><p>第二段</p></article></body></html>
        """
        response.raise_for_status.return_value = None

        with patch("src.services.news_content_fetcher.requests.get", return_value=response):
            text = self.fetcher._extract_with_bs4("https://example.com/c")

        self.assertIn("第一段", text)
        self.assertIn("第二段", text)


if __name__ == "__main__":
    unittest.main()
