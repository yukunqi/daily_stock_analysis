# -*- coding: utf-8 -*-
"""Tests for the nightly opportunity report flow."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.config import Config
from src.core import opportunity_report
from src.core.opportunity_report import (
    OPPORTUNITY_HISTORY_CODE,
    OPPORTUNITY_REPORT_TYPE,
    OpportunityInputs,
    generate_opportunity_payload,
    get_latest_opportunity_performance_markdown,
    run_opportunity_report,
)
from src.market_analyzer import MarketOverview
from src.storage import AnalysisHistory, DatabaseManager


class OpportunityReportTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config._instance = None

    def _overview(self) -> MarketOverview:
        return MarketOverview(
            date="2026-05-20",
            up_count=3200,
            down_count=1800,
            flat_count=120,
            limit_up_count=75,
            limit_down_count=8,
            total_amount=11500,
            top_sectors=[{"name": "机器人", "change_pct": 3.2}],
            bottom_sectors=[{"name": "白酒", "change_pct": -1.4}],
        )

    def _inputs(self) -> OpportunityInputs:
        return OpportunityInputs(
            overview=self._overview(),
            news=[{"title": "机器人产业政策加码", "snippet": "产业链催化延续", "source": "TrendRadar"}],
            concept_top=[{"name": "人形机器人", "change_pct": 4.5}],
            concept_bottom=[],
            hot_stocks=[{"rank": 1, "code": "300750", "name": "宁德时代", "price": 210.0, "change_pct": 2.1}],
            limit_up_pool=[
                {
                    "code": "002050",
                    "name": "三花智控",
                    "price": 28.0,
                    "change_pct": 10.0,
                    "industry": "机器人",
                    "consecutive_boards": 1,
                }
            ],
            latest_market_review="## 今日大盘\n\n机器人板块领涨，成交额放大。",
        )

    def test_fallback_payload_contains_required_sections(self) -> None:
        payload = generate_opportunity_payload(self._inputs(), analyzer=None, language="zh")

        self.assertIn("opportunity_sectors", payload)
        self.assertEqual(payload["opportunity_sectors"][0]["sector"], "人形机器人")
        self.assertEqual(payload["stock_recommendations"][0]["code"], "002050")
        self.assertIn("明日投资机会报告", payload["report_markdown"])
        self.assertIn("个股推荐", payload["report_markdown"])

    def test_run_opportunity_report_persists_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_db_path = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = os.path.join(temp_dir, "opportunity.db")
            Config._instance = None
            DatabaseManager.reset_instance()
            try:
                notifier = MagicMock()
                notifier.save_report_to_file.return_value = os.path.join(temp_dir, "report.md")
                notifier.is_available.return_value = False

                fake_market_analyzer = MagicMock()
                fake_market_analyzer.get_market_overview.return_value = self._overview()
                fake_market_analyzer.search_market_news.return_value = self._inputs().news
                fake_market_analyzer.data_manager.get_concept_rankings.return_value = (
                    self._inputs().concept_top,
                    [],
                )
                fake_market_analyzer.data_manager.get_hot_stocks.return_value = self._inputs().hot_stocks
                fake_market_analyzer.data_manager.get_limit_up_pool.return_value = self._inputs().limit_up_pool

                with patch.object(opportunity_report, "get_config", return_value=SimpleNamespace(report_language="zh")), \
                     patch.object(opportunity_report, "MarketAnalyzer", return_value=fake_market_analyzer):
                    report = run_opportunity_report(
                        notifier=notifier,
                        analyzer=None,
                        search_service=None,
                        send_notification=False,
                        query_id="opp-test-001",
                    )

                self.assertIsNotNone(report)
                db = DatabaseManager.get_instance()
                with db.get_session() as session:
                    row = session.query(AnalysisHistory).filter(
                        AnalysisHistory.query_id == "opp-test-001"
                    ).first()
                    self.assertIsNotNone(row)
                    self.assertEqual(row.code, OPPORTUNITY_HISTORY_CODE)
                    self.assertEqual(row.report_type, OPPORTUNITY_REPORT_TYPE)
                    self.assertIn("明日投资机会报告", row.news_content)
            finally:
                DatabaseManager.reset_instance()
                Config._instance = None
                if old_db_path is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = old_db_path

    def test_performance_block_reads_prior_recommendations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            old_db_path = os.environ.get("DATABASE_PATH")
            os.environ["DATABASE_PATH"] = os.path.join(temp_dir, "opportunity_perf.db")
            Config._instance = None
            DatabaseManager.reset_instance()
            try:
                db = DatabaseManager.get_instance()
                with db.get_session() as session:
                    session.add(
                        AnalysisHistory(
                            query_id="opp-yesterday",
                            code=OPPORTUNITY_HISTORY_CODE,
                            name="明日机会",
                            report_type=OPPORTUNITY_REPORT_TYPE,
                            sentiment_score=65,
                            operation_advice="查看机会",
                            trend_prediction="偏暖",
                            analysis_summary="明日机会",
                            raw_result=DatabaseManager._safe_json_dumps(
                                {
                                    "dashboard": {
                                        "opportunity_report": {
                                            "stock_recommendations": [
                                                {
                                                    "code": "002050",
                                                    "name": "三花智控",
                                                    "sector": "机器人",
                                                    "entry_price": 28.0,
                                                }
                                            ]
                                        }
                                    }
                                }
                            ),
                            news_content="# 明日机会",
                            created_at=datetime.now() - timedelta(days=1),
                        )
                    )
                    session.commit()

                data_manager = MagicMock()
                data_manager.get_realtime_quote.return_value = SimpleNamespace(
                    price=30.8,
                    change_pct=4.2,
                )
                block = get_latest_opportunity_performance_markdown(data_manager=data_manager)

                self.assertIn("前一晚机会跟踪", block)
                self.assertIn("三花智控(002050)", block)
                self.assertIn("+10.00%", block)
                self.assertIn("明显验证", block)
            finally:
                DatabaseManager.reset_instance()
                Config._instance = None
                if old_db_path is None:
                    os.environ.pop("DATABASE_PATH", None)
                else:
                    os.environ["DATABASE_PATH"] = old_db_path


if __name__ == "__main__":
    unittest.main()
