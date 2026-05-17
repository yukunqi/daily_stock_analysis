# -*- coding: utf-8 -*-
"""
News article content extraction and local cache helpers.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from newspaper import Article, Config

logger = logging.getLogger(__name__)


DEFAULT_NEWS_CONTENT_CACHE_PATH = "./data/news_content_cache.db"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class NewsContent:
    """Fetched and normalized article content."""

    url: str
    title: str = ""
    source: str = ""
    content: str = ""
    excerpt: str = ""
    status: str = "empty"
    error: str = ""
    fetched_at: str = ""
    from_cache: bool = False


class NewsContentFetcher:
    """Fetch article text by URL and cache normalized results in SQLite."""

    def __init__(
        self,
        cache_path: str = DEFAULT_NEWS_CONTENT_CACHE_PATH,
        timeout: int = 8,
        max_chars: int = 2500,
        cache_ttl_hours: int = 168,
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        self.cache_path = Path(cache_path).expanduser()
        self.timeout = max(1, int(timeout or 8))
        self.max_chars = max(200, int(max_chars or 2500))
        self.cache_ttl_hours = max(1, int(cache_ttl_hours or 168))
        self.user_agent = user_agent or DEFAULT_USER_AGENT

    @classmethod
    def from_config(cls, config=None) -> "NewsContentFetcher":
        """Create a fetcher from global or provided Config."""
        if config is None:
            from src.config import get_config

            config = get_config()
        return cls(
            cache_path=getattr(config, "news_content_cache_path", DEFAULT_NEWS_CONTENT_CACHE_PATH),
            timeout=getattr(config, "news_content_fetch_timeout", 8),
            max_chars=getattr(config, "news_content_max_chars", 2500),
            cache_ttl_hours=getattr(config, "news_content_cache_ttl_hours", 168),
        )

    def fetch(self, url: str, title: str = "", source: str = "") -> NewsContent:
        """Fetch URL content, using a fresh successful cache entry when available."""
        clean_url = (url or "").strip()
        if not self._is_supported_url(clean_url):
            return NewsContent(url=clean_url, title=title, source=source, status="invalid", error="unsupported url")

        cached = self._read_cache(clean_url)
        if cached:
            return NewsContent(
                url=clean_url,
                title=cached["title"] or title,
                source=cached["source"] or source,
                content=cached["content"],
                excerpt=cached["excerpt"],
                status=cached["status"],
                error=cached["error"],
                fetched_at=cached["fetched_at"],
                from_cache=True,
            )

        fetched_at = datetime.utcnow().replace(microsecond=0).isoformat()
        try:
            content = self._extract_with_newspaper(clean_url)
            if not content:
                content = self._extract_with_bs4(clean_url)
            content = self._normalize_content(content)
            if content:
                excerpt = self._make_excerpt(content)
                result = NewsContent(
                    url=clean_url,
                    title=title,
                    source=source,
                    content=content,
                    excerpt=excerpt,
                    status="success",
                    fetched_at=fetched_at,
                )
            else:
                result = NewsContent(
                    url=clean_url,
                    title=title,
                    source=source,
                    status="empty",
                    error="no article text extracted",
                    fetched_at=fetched_at,
                )
        except Exception as exc:
            logger.debug("[NewsContent] fetch failed for %s: %s", clean_url, exc)
            result = NewsContent(
                url=clean_url,
                title=title,
                source=source,
                status="error",
                error=str(exc)[:500],
                fetched_at=fetched_at,
            )

        self._write_cache(result)
        return result

    def _extract_with_newspaper(self, url: str) -> str:
        config = Config()
        config.browser_user_agent = self.user_agent
        config.request_timeout = self.timeout
        config.fetch_images = False
        config.memoize_articles = False

        article = Article(url, config=config, language="zh")
        article.download()
        article.parse()
        return article.text or ""

    def _extract_with_bs4(self, url: str) -> str:
        response = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
            tag.decompose()

        candidates = []
        for selector in ["article", "main", ".article", ".content", ".article-content", "#article", "#content"]:
            node = soup.select_one(selector)
            if node:
                text = node.get_text("\n", strip=True)
                if text:
                    candidates.append(text)
        if candidates:
            return max(candidates, key=len)

        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        return "\n".join(p for p in paragraphs if p)

    def _read_cache(self, url: str) -> Optional[dict]:
        self._ensure_cache()
        cutoff = datetime.utcnow() - timedelta(hours=self.cache_ttl_hours)
        with sqlite3.connect(str(self.cache_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT url, title, source, content, excerpt, status, error, fetched_at
                FROM news_content_cache
                WHERE url = ?
                """,
                (url,),
            ).fetchone()
        if not row or row["status"] != "success":
            return None
        try:
            fetched_at = datetime.fromisoformat(row["fetched_at"])
        except (TypeError, ValueError):
            return None
        if fetched_at < cutoff:
            return None
        return dict(row)

    def _write_cache(self, content: NewsContent) -> None:
        self._ensure_cache()
        content_hash = hashlib.sha256(content.content.encode("utf-8")).hexdigest() if content.content else ""
        with sqlite3.connect(str(self.cache_path)) as conn:
            conn.execute(
                """
                INSERT INTO news_content_cache(
                    url, title, source, content, excerpt, content_hash, status, error, fetched_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,
                    source=excluded.source,
                    content=excluded.content,
                    excerpt=excluded.excerpt,
                    content_hash=excluded.content_hash,
                    status=excluded.status,
                    error=excluded.error,
                    fetched_at=excluded.fetched_at,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    content.url,
                    content.title,
                    content.source,
                    content.content,
                    content.excerpt,
                    content_hash,
                    content.status,
                    content.error,
                    content.fetched_at,
                ),
            )

    def _ensure_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.cache_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS news_content_cache (
                    url TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    content TEXT DEFAULT '',
                    excerpt TEXT DEFAULT '',
                    content_hash TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT DEFAULT '',
                    fetched_at TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _normalize_content(self, content: str) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in (content or "").splitlines()]
        text = "\n".join(line for line in lines if line)
        return text[: self.max_chars]

    def _make_excerpt(self, content: str) -> str:
        return content[: self.max_chars]

    @staticmethod
    def _is_supported_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
