# -*- coding: utf-8 -*-
"""Tests for TrendRadar local news reader and matching helpers."""

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from src.services.trend_radar_news_service import TrendRadarNewsItem, TrendRadarNewsService


class FakeContent:
    status = "success"
    excerpt = "这是新闻正文第一段。\n这是新闻正文第二段。"


class FakeContentFetcher:
    def __init__(self) -> None:
        self.calls = []

    def fetch(self, url: str, title: str = "", source: str = "") -> FakeContent:
        self.calls.append((url, title, source))
        return FakeContent()


class TrendRadarNewsServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name) / "output"
        self.news_dir = self.output_dir / "news"
        self.news_dir.mkdir(parents=True)
        self.db_path = self.news_dir / "2026-05-16.db"
        self._create_db(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _create_db(path: Path) -> None:
        with sqlite3.connect(str(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE platforms (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL
                );
                CREATE TABLE news_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    url TEXT DEFAULT '',
                    mobile_url TEXT DEFAULT '',
                    first_crawl_time TEXT NOT NULL,
                    last_crawl_time TEXT NOT NULL,
                    crawl_count INTEGER DEFAULT 1
                );
                INSERT INTO platforms(id, name) VALUES
                    ('cls-hot', '财联社热门'),
                    ('wallstreetcn-hot', '华尔街见闻');
                INSERT INTO news_items(title, platform_id, rank, url, first_crawl_time, last_crawl_time, crawl_count)
                VALUES
                    ('茅台再调价！多款非标产品提价', 'cls-hot', 1, 'https://example.com/maotai', '09-00', '09-10', 2),
                    ('茅台再调价！终端市场价应声上涨', 'cls-hot', 2, 'https://example.com/maotai', '09-00', '09-20', 3),
                    ('全球通胀加剧债市风暴，动摇AI牛市', 'wallstreetcn-hot', 3, 'https://example.com/macro', '10-00', '10-20', 1),
                    ('无链接重复标题', 'cls-hot', 4, '', '11-00', '11-10', 1),
                    ('无链接重复标题', 'wallstreetcn-hot', 5, '', '11-00', '11-20', 1);
                """
            )

    def test_get_recent_news_reads_and_deduplicates_sqlite_items(self) -> None:
        service = TrendRadarNewsService(str(self.output_dir), days=1, limit=10)

        items = service.get_recent_news(reference_date=date(2026, 5, 16))

        self.assertEqual(len(items), 4)
        self.assertEqual(items[0].published_at, "2026-05-16 11:20")
        self.assertTrue(any(item.title.startswith("茅台再调价") for item in items))
        self.assertEqual(sum(1 for item in items if item.url == "https://example.com/maotai"), 1)

    def test_missing_directory_returns_empty_list(self) -> None:
        service = TrendRadarNewsService(str(Path(self.temp_dir.name) / "missing"), days=1, limit=10)

        self.assertEqual(service.get_recent_news(reference_date=date(2026, 5, 16)), [])

    def test_missing_tables_returns_empty_list(self) -> None:
        bad_output = Path(self.temp_dir.name) / "bad_output"
        bad_news = bad_output / "news"
        bad_news.mkdir(parents=True)
        with sqlite3.connect(str(bad_news / "2026-05-16.db")) as conn:
            conn.execute("CREATE TABLE other(id INTEGER)")
        service = TrendRadarNewsService(str(bad_output), days=1, limit=10)

        self.assertEqual(service.get_recent_news(reference_date=date(2026, 5, 16)), [])

    def test_match_stock_news_matches_direct_and_soft_terms(self) -> None:
        service = TrendRadarNewsService(str(self.output_dir), days=1, limit=10)
        items = [
            TrendRadarNewsItem(title="茅台再调价！多款非标产品提价", url="u1", source="财联社"),
            TrendRadarNewsItem(title="动力电池产业链订单回暖", url="u2", source="财联社"),
            TrendRadarNewsItem(title="无关体育新闻", url="u3", source="澎湃"),
        ]

        maotai_matches = service.match_stock_news("600519", "贵州茅台", items=items)
        catl_matches = service.match_stock_news("300750", "宁德时代", items=items)

        self.assertEqual(len(maotai_matches), 1)
        self.assertEqual(maotai_matches[0].match_type, "direct")
        self.assertEqual(len(catl_matches), 1)
        self.assertEqual(catl_matches[0].match_type, "industry")

    def test_direct_stock_matches_are_ranked_before_industry_matches(self) -> None:
        service = TrendRadarNewsService(str(self.output_dir), days=1, limit=10)
        items = [
            TrendRadarNewsItem(title="消费板块政策支持", url="u1", source="财联社"),
            TrendRadarNewsItem(title="茅台再调价！多款非标产品提价", url="u2", source="财联社"),
        ]

        matches = service.match_stock_news("600519", "贵州茅台", items=items)

        self.assertEqual([match.match_type for match in matches], ["direct", "industry"])

    def test_build_context_and_market_items(self) -> None:
        service = TrendRadarNewsService(str(self.output_dir), days=1, limit=10)
        items = service.get_recent_news(reference_date=date(2026, 5, 16))

        stock_context = service.build_stock_news_context("600519", "贵州茅台", items=items)
        market_items = service.build_market_news_items(items=items, max_items=2)

        self.assertIn("TrendRadar News Context", stock_context)
        self.assertIn("茅台再调价", stock_context)
        self.assertEqual(len(market_items), 2)
        self.assertTrue(any("buckets:" in item["snippet"] for item in market_items))

    def test_build_context_fetches_article_excerpt_for_matched_urls(self) -> None:
        fetcher = FakeContentFetcher()
        service = TrendRadarNewsService(
            str(self.output_dir),
            days=1,
            limit=10,
            content_fetcher=fetcher,
            fetch_content_enabled=True,
            content_max_items=1,
        )
        items = [
            TrendRadarNewsItem(title="茅台再调价！多款非标产品提价", url="https://example.com/a", source="财联社"),
            TrendRadarNewsItem(title="贵州茅台渠道反馈积极", url="https://example.com/b", source="财联社"),
        ]

        stock_context = service.build_stock_news_context("600519", "贵州茅台", items=items, max_items=2)

        self.assertIn("正文摘录", stock_context)
        self.assertIn("这是新闻正文第一段", stock_context)
        self.assertIn("https://example.com/b", stock_context)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(fetcher.calls[0][0], "https://example.com/a")

    def test_diagnostics_counts_sources(self) -> None:
        service = TrendRadarNewsService(str(self.output_dir), days=1, limit=10)

        diagnostics = service.diagnostics(reference_date=date(2026, 5, 16))

        self.assertTrue(diagnostics["exists"])
        self.assertEqual(diagnostics["total"], 5)
        self.assertEqual(diagnostics["sources"]["财联社热门"], 3)


if __name__ == "__main__":
    unittest.main()
