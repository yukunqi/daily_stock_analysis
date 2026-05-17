# -*- coding: utf-8 -*-
"""
TrendRadar local news reader and deterministic matching helpers.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.data.stock_mapping import STOCK_NAME_MAP
from src.services.news_content_fetcher import NewsContentFetcher

logger = logging.getLogger(__name__)


DEFAULT_TREND_RADAR_OUTPUT_DIR = "/Users/yukunqi_1/git_project/TrendRadar/output"


@dataclass(frozen=True)
class TrendRadarNewsItem:
    """Normalized passive news item read from TrendRadar local storage."""

    title: str
    url: str
    source: str
    rank: Optional[int] = None
    published_at: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    crawl_count: int = 1
    summary: str = ""
    origin: str = "trendradar"

    @property
    def snippet(self) -> str:
        """Return a compact description compatible with existing prompt rendering."""
        parts = []
        if self.rank is not None:
            parts.append(f"rank: {self.rank}")
        if self.crawl_count:
            parts.append(f"crawl_count: {self.crawl_count}")
        if self.summary:
            parts.append(self.summary)
        return "; ".join(parts)

    def to_prompt_dict(self) -> Dict[str, str]:
        """Return a dict shape accepted by MarketAnalyzer prompt rendering."""
        return {
            "title": self.title,
            "snippet": self.snippet,
            "url": self.url,
            "source": self.source,
            "published_date": self.published_at or "",
        }


@dataclass(frozen=True)
class TrendRadarNewsMatch:
    """A stock/news match with deterministic reason metadata."""

    item: TrendRadarNewsItem
    match_type: str
    reason: str


class TrendRadarNewsService:
    """Read TrendRadar SQLite output and build compact news contexts."""

    COMMON_ALIASES: Dict[str, Tuple[str, ...]] = {
        "600519": ("茅台", "贵州茅台"),
        "300750": ("宁德", "宁德时代"),
        "002594": ("比亚迪", "BYD", "新能源车"),
        "000858": ("五粮液", "白酒"),
        "601127": ("赛力斯", "问界", "AITO"),
    }

    MACRO_BUCKETS: Dict[str, Tuple[str, ...]] = {
        "macro": ("通胀", "CPI", "PPI", "PMI", "经济", "衰退", "增长"),
        "policy": ("政策", "国常会", "商务部", "监管", "央行", "财政", "关税", "中美"),
        "liquidity": ("美联储", "利率", "降息", "加息", "收益率", "美债", "流动性"),
        "geopolitical": ("冲突", "战争", "制裁", "中东", "伊朗", "俄罗斯", "地缘"),
        "industry": ("AI", "芯片", "半导体", "算力", "光模块", "新能源", "汽车", "白酒"),
        "commodity": ("原油", "石油", "黄金", "铜", "金属", "大宗"),
        "sentiment": ("美股", "A股", "私募", "基金", "IPO", "调仓", "风险偏好"),
    }

    def __init__(
        self,
        output_dir: str,
        days: int = 1,
        limit: int = 100,
        content_fetcher: Optional[NewsContentFetcher] = None,
        fetch_content_enabled: bool = False,
        content_max_items: int = 3,
    ):
        self.output_dir = Path(output_dir).expanduser()
        self.days = max(1, int(days or 1))
        self.limit = max(1, int(limit or 100))
        self.content_fetcher = content_fetcher
        self.fetch_content_enabled = bool(fetch_content_enabled)
        self.content_max_items = max(0, int(content_max_items or 0))

    @classmethod
    def from_config(cls, config=None) -> "TrendRadarNewsService":
        """Create a service from global or provided Config."""
        if config is None:
            from src.config import get_config

            config = get_config()
        raw_fetch_enabled = getattr(config, "trend_radar_fetch_content_enabled", True)
        fetch_content_enabled = (
            raw_fetch_enabled if isinstance(raw_fetch_enabled, bool) else str(raw_fetch_enabled).strip().lower() == "true"
        )
        content_fetcher = NewsContentFetcher.from_config(config) if fetch_content_enabled else None
        return cls(
            output_dir=getattr(config, "trend_radar_output_dir", DEFAULT_TREND_RADAR_OUTPUT_DIR),
            days=getattr(config, "trend_radar_news_days", 1),
            limit=getattr(config, "trend_radar_news_limit", 100),
            content_fetcher=content_fetcher,
            fetch_content_enabled=fetch_content_enabled,
            content_max_items=getattr(config, "trend_radar_content_max_items", 3),
        )

    def get_recent_news(self, reference_date: Optional[date] = None) -> List[TrendRadarNewsItem]:
        """Read recent TrendRadar news items from local SQLite databases."""
        news_dir = self._news_dir()
        if not news_dir.exists():
            logger.warning("[TrendRadar] news directory not found: %s", news_dir)
            return []

        ref = reference_date or date.today()
        items: List[TrendRadarNewsItem] = []
        for day_offset in range(self.days):
            day = ref - timedelta(days=day_offset)
            db_path = news_dir / f"{day.isoformat()}.db"
            if not db_path.exists():
                logger.info("[TrendRadar] news database not found: %s", db_path)
                continue
            items.extend(self._read_db(db_path, day))
            if len(items) >= self.limit * 2:
                break

        return self._dedupe_items(items)[: self.limit]

    def match_stock_news(
        self,
        stock_code: str,
        stock_name: str,
        items: Optional[Sequence[TrendRadarNewsItem]] = None,
        aliases: Optional[Sequence[str]] = None,
    ) -> List[TrendRadarNewsMatch]:
        """Return deterministic TrendRadar matches for one watchlist stock."""
        source_items = list(items) if items is not None else self.get_recent_news()
        direct_terms, soft_terms = self._stock_terms(stock_code, stock_name, aliases)
        matches: List[TrendRadarNewsMatch] = []
        for item in source_items:
            haystack = f"{item.title} {item.summary}".lower()
            direct_hit = self._first_matching_term(haystack, direct_terms)
            if direct_hit:
                matches.append(TrendRadarNewsMatch(item=item, match_type="direct", reason=direct_hit))
                continue
            soft_hit = self._first_matching_term(haystack, soft_terms)
            if soft_hit:
                matches.append(TrendRadarNewsMatch(item=item, match_type="industry", reason=soft_hit))
        matches.sort(key=lambda match: 0 if match.match_type == "direct" else 1)
        return matches

    def build_stock_news_context(
        self,
        stock_code: str,
        stock_name: str,
        items: Optional[Sequence[TrendRadarNewsItem]] = None,
        max_items: int = 5,
    ) -> str:
        """Build compact Markdown context for one stock."""
        matches = self.match_stock_news(stock_code, stock_name, items=items)
        if not matches:
            return ""

        lines = [f"【TrendRadar News Context | {stock_name}({stock_code})】"]
        for idx, match in enumerate(matches[:max_items], 1):
            item = match.item
            meta = f"{item.source}"
            if item.rank is not None:
                meta += f" rank {item.rank}"
            if item.last_seen_at:
                meta += f" {item.last_seen_at}"
            lines.append(f"{idx}. [{match.match_type}:{match.reason}] {item.title}")
            lines.append(f"   来源: {meta}")
            if item.url:
                lines.append(f"   URL: {item.url}")
            excerpt = self._fetch_content_excerpt(item, idx)
            if excerpt:
                lines.append("   正文摘录:")
                lines.extend(f"   {line}" for line in excerpt.splitlines())
        return "\n".join(lines)

    def build_market_news_items(
        self,
        items: Optional[Sequence[TrendRadarNewsItem]] = None,
        max_items: int = 8,
    ) -> List[Dict[str, str]]:
        """Build market-news dicts compatible with MarketAnalyzer prompt rendering."""
        source_items = list(items) if items is not None else self.get_recent_news()
        scored: List[Tuple[int, TrendRadarNewsItem, List[str]]] = []
        for item in source_items:
            buckets = self._macro_buckets_for_item(item)
            score = len(buckets) * 10
            if item.rank is not None:
                score += max(0, 20 - item.rank)
            scored.append((score, item, buckets))

        scored.sort(key=lambda row: row[0], reverse=True)
        result: List[Dict[str, str]] = []
        for _, item, buckets in scored[:max_items]:
            snippet_parts = []
            if buckets:
                snippet_parts.append(f"buckets: {', '.join(buckets)}")
            if item.source:
                snippet_parts.append(f"source: {item.source}")
            if item.rank is not None:
                snippet_parts.append(f"rank: {item.rank}")
            result.append(
                {
                    "title": item.title,
                    "snippet": "; ".join(snippet_parts),
                    "url": item.url,
                    "source": item.source,
                }
            )
        return result

    def diagnostics(self, reference_date: Optional[date] = None) -> Dict[str, object]:
        """Return lightweight TrendRadar storage diagnostics."""
        news_dir = self._news_dir()
        ref = reference_date or date.today()
        db_path = news_dir / f"{ref.isoformat()}.db"
        result: Dict[str, object] = {
            "output_dir": str(self.output_dir),
            "news_dir": str(news_dir),
            "database": str(db_path),
            "exists": db_path.exists(),
            "sources": {},
            "total": 0,
        }
        if not db_path.exists():
            return result
        try:
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute(
                    """
                    SELECT COALESCE(p.name, n.platform_id) AS source, COUNT(*) AS cnt
                    FROM news_items n
                    LEFT JOIN platforms p ON p.id = n.platform_id
                    GROUP BY source
                    ORDER BY cnt DESC
                    """
                ).fetchall()
            result["sources"] = {str(source): int(cnt) for source, cnt in rows}
            result["total"] = sum(int(cnt) for _, cnt in rows)
        except sqlite3.Error as exc:
            result["error"] = str(exc)
        return result

    def _news_dir(self) -> Path:
        if self.output_dir.name == "news":
            return self.output_dir
        return self.output_dir / "news"

    def _fetch_content_excerpt(self, item: TrendRadarNewsItem, idx: int) -> str:
        if not self.fetch_content_enabled or not self.content_fetcher:
            return ""
        if idx > self.content_max_items or not item.url:
            return ""
        try:
            content = self.content_fetcher.fetch(item.url, title=item.title, source=item.source)
        except Exception as exc:
            logger.debug("[TrendRadar] failed to fetch article content for %s: %s", item.url, exc)
            return ""
        if content.status != "success" or not content.excerpt:
            logger.debug("[TrendRadar] article content unavailable for %s: %s", item.url, content.status)
            return ""
        return content.excerpt

    def _read_db(self, db_path: Path, db_date: date) -> List[TrendRadarNewsItem]:
        try:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                if not self._has_required_tables(conn):
                    logger.warning("[TrendRadar] database missing required tables: %s", db_path)
                    return []
                rows = conn.execute(
                    """
                    SELECT
                        n.title,
                        n.url,
                        n.mobile_url,
                        n.rank,
                        n.first_crawl_time,
                        n.last_crawl_time,
                        n.crawl_count,
                        COALESCE(p.name, n.platform_id) AS source
                    FROM news_items n
                    LEFT JOIN platforms p ON p.id = n.platform_id
                    ORDER BY n.last_crawl_time DESC, n.rank ASC, n.id DESC
                    LIMIT ?
                    """,
                    (self.limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("[TrendRadar] failed to read %s: %s", db_path, exc)
            return []

        items = []
        for row in rows:
            title = (row["title"] or "").strip()
            if not title:
                continue
            first_seen = self._combine_date_time(db_date, row["first_crawl_time"])
            last_seen = self._combine_date_time(db_date, row["last_crawl_time"])
            items.append(
                TrendRadarNewsItem(
                    title=title,
                    url=(row["url"] or row["mobile_url"] or "").strip(),
                    source=(row["source"] or "TrendRadar").strip(),
                    rank=int(row["rank"]) if row["rank"] is not None else None,
                    published_at=last_seen,
                    first_seen_at=first_seen,
                    last_seen_at=last_seen,
                    crawl_count=int(row["crawl_count"] or 1),
                )
            )
        return items

    @staticmethod
    def _has_required_tables(conn: sqlite3.Connection) -> bool:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        tables = {row[0] for row in rows}
        return {"news_items", "platforms"}.issubset(tables)

    @staticmethod
    def _combine_date_time(db_date: date, time_text: object) -> str:
        raw = str(time_text or "").strip()
        if re.match(r"^\d{2}-\d{2}$", raw):
            raw = raw.replace("-", ":")
        if re.match(r"^\d{1,2}:\d{2}$", raw):
            return f"{db_date.isoformat()} {raw}"
        if raw:
            return f"{db_date.isoformat()} {raw}"
        return db_date.isoformat()

    @staticmethod
    def _dedupe_items(items: Iterable[TrendRadarNewsItem]) -> List[TrendRadarNewsItem]:
        seen = set()
        result = []
        for item in items:
            key = item.url.strip() if item.url else f"{item.source}|{TrendRadarNewsService._norm_text(item.title)}"
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @classmethod
    def _stock_terms(
        cls,
        stock_code: str,
        stock_name: str,
        aliases: Optional[Sequence[str]],
    ) -> Tuple[List[str], List[str]]:
        code = (stock_code or "").strip()
        name = (stock_name or "").strip()
        direct = [term for term in [code, name, STOCK_NAME_MAP.get(code, "")] if term]
        direct.extend(cls.COMMON_ALIASES.get(code, ()))
        if aliases:
            direct.extend([alias for alias in aliases if alias])
        direct_unique = cls._unique_terms(direct)

        soft = []
        if code in {"300750", "002594", "601127"}:
            soft.extend(["新能源", "汽车", "动力电池"])
        if code in {"600519", "000858"}:
            soft.extend(["白酒", "消费"])
        return direct_unique, [term.lower() for term in cls._unique_terms(soft) if term not in direct_unique]

    @staticmethod
    def _unique_terms(terms: Sequence[str]) -> List[str]:
        result = []
        seen = set()
        for term in terms:
            clean = str(term).strip()
            if not clean:
                continue
            key = clean.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(clean)
        return result

    @staticmethod
    def _first_matching_term(haystack: str, terms: Sequence[str]) -> Optional[str]:
        for term in terms:
            if term and term.lower() in haystack:
                return term
        return None

    @staticmethod
    def _norm_text(text: str) -> str:
        return re.sub(r"\s+", "", (text or "").strip().lower())

    @classmethod
    def _macro_buckets_for_item(cls, item: TrendRadarNewsItem) -> List[str]:
        haystack = f"{item.title} {item.summary}".lower()
        buckets = []
        for bucket, terms in cls.MACRO_BUCKETS.items():
            if any(term.lower() in haystack for term in terms):
                buckets.append(bucket)
        return buckets


def get_trend_radar_news_service(config=None) -> Optional[TrendRadarNewsService]:
    """Return an enabled TrendRadar service, or None when disabled."""
    if config is None:
        from src.config import get_config

        config = get_config()
    enabled = getattr(config, "trend_radar_news_enabled", False)
    if enabled is not True and str(enabled).strip().lower() != "true":
        return None
    return TrendRadarNewsService.from_config(config)
