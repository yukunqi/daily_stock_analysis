# -*- coding: utf-8 -*-
"""
IwencaiMarketQueryFetcher - optional Tonghuashun iWencai OpenAPI data source.

Uses the installed skill CLI (skills/hithink-market-query/scripts/cli.py) with
IWENCAI_API_KEY. The fetcher keeps iWencai-specific query/parsing details behind
the existing BaseFetcher interface so the analysis pipeline remains provider-neutral.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from .base import BaseFetcher, DataFetchError, normalize_stock_code
from .realtime_types import ChipDistribution, RealtimeSource, UnifiedRealtimeQuote, safe_float, safe_int
from .us_index_mapping import is_us_stock_code

logger = logging.getLogger(__name__)

_DATE_SUFFIX_RE = re.compile(r"\[(\d{8})\]")


def _iwencai_is_hk_code(stock_code: str) -> bool:
    """HK stock detection (aligned with akshare_fetcher._is_hk_code, no akshare import)."""
    code = stock_code.lower()
    if code.startswith("hk"):
        numeric_part = code[2:]
        return numeric_part.isdigit() and 1 <= len(numeric_part) <= 5
    return code.isdigit() and len(code) == 5


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_cli_path() -> Path:
    return _repo_root() / "skills" / "hithink-market-query" / "scripts" / "cli.py"


def _stringify_iwencai_code(ts: Any) -> str:
    """Normalize API stock code cell for comparison (handles int/float from JSON)."""
    if ts is None:
        return ""
    if isinstance(ts, bool):
        return str(ts)
    if isinstance(ts, int):
        return str(ts)
    if isinstance(ts, float):
        if ts != ts:  # NaN
            return ""
        r = round(ts)
        if abs(ts - r) < 1e-9:
            return str(int(r))
        return str(ts).strip()
    return str(ts).strip()


def _code_from_iwencai_ts(ts_code: str) -> str:
    s = (ts_code or "").strip().upper()
    if "." in s:
        base, suf = s.rsplit(".", 1)
        if suf in ("SH", "SZ", "BJ") and base.isdigit():
            return base
    return normalize_stock_code(s)


def _latest_date_suffix(row: Dict[str, Any]) -> Optional[str]:
    """Pick the latest YYYYMMDD from column names like 收盘价[20260511]."""
    dates: List[str] = []
    for k in row.keys():
        if not isinstance(k, str):
            continue
        m = _DATE_SUFFIX_RE.search(k)
        if m:
            dates.append(m.group(1))
    if not dates:
        return None
    return max(dates)


def _get_by_suffix(row: Dict[str, Any], prefix: str, date_suffix: Optional[str]) -> Any:
    if date_suffix:
        key = f"{prefix}[{date_suffix}]"
        if key in row:
            return row[key]
    for k, v in row.items():
        if isinstance(k, str) and k.startswith(prefix) and "[" in k:
            return v
    return None


def _get_by_prefixes(row: Dict[str, Any], prefixes: Iterable[str], date_suffix: Optional[str] = None) -> Any:
    for prefix in prefixes:
        val = _get_by_suffix(row, prefix, date_suffix)
        if val is not None:
            return val
    for k, v in row.items():
        if not isinstance(k, str):
            continue
        if any(k.startswith(prefix) for prefix in prefixes):
            return v
    return None


def _first_row(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not payload.get("success") or "datas" not in payload:
        return None
    datas = payload.get("datas") or []
    if not isinstance(datas, list) or not datas or not isinstance(datas[0], dict):
        return None
    return datas[0]


def iwencai_cli_query(
    query: str,
    *,
    limit: int = 8,
    timeout: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Run the SkillHub CLI and return parsed JSON payload."""
    from src.config import get_config

    cfg = get_config()
    if not cfg.iwencai_market_query_enabled:
        return None
    if not (os.environ.get("IWENCAI_API_KEY") or "").strip():
        logger.debug("[Iwencai] IWENCAI_API_KEY not set, skip query")
        return None

    cli_path = Path(cfg.iwencai_cli_path) if cfg.iwencai_cli_path else _default_cli_path()
    if not cli_path.is_file():
        logger.warning("[Iwencai] CLI not found: %s", cli_path)
        return None

    effective_timeout = max(5, int(timeout or cfg.iwencai_subprocess_timeout_sec))
    cmd = [
        sys.executable,
        str(cli_path),
        "--query",
        query,
        "--limit",
        str(limit),
        "--timeout",
        str(effective_timeout),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cli_path.parent.parent),
            capture_output=True,
            text=True,
            timeout=effective_timeout + 5,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        logger.warning("[Iwencai] subprocess timeout for query=%s", query)
        return None
    except Exception as e:
        logger.warning("[Iwencai] subprocess failed for query=%s: %s", query, e)
        return None

    raw_out = (proc.stdout or "").strip()
    if not raw_out:
        err_tail = (proc.stderr or "")[-500:]
        logger.warning("[Iwencai] empty stdout exit=%s query=%s stderr_tail=%r", proc.returncode, query, err_tail)
        return None

    try:
        payload = json.loads(raw_out)
    except json.JSONDecodeError:
        # CLI stdout can be polluted by transient upstream text; keep this as debug
        # to avoid warning flood while upper layers handle fallback providers.
        logger.debug("[Iwencai] invalid JSON stdout for query=%s", query)
        return None

    if proc.returncode != 0:
        logger.warning(
            "[Iwencai] CLI exit %s for query=%s keys=%s",
            proc.returncode,
            query,
            list(payload.keys()) if isinstance(payload, dict) else type(payload),
        )
        return None
    return payload


def parse_iwencai_cli_payload(
    payload: Dict[str, Any],
    stock_code: str,
) -> Optional[UnifiedRealtimeQuote]:
    """
    Map hithink-market-query CLI stdout JSON to UnifiedRealtimeQuote.

    Expects the wrapped shape from cli.py main(): success, datas, ...
    """
    if not payload.get("success") or "datas" not in payload:
        return None
    datas = payload.get("datas") or []
    if not isinstance(datas, list) or not datas:
        return None

    want = normalize_stock_code(stock_code)
    row: Optional[Dict[str, Any]] = None
    for item in datas:
        if not isinstance(item, dict):
            continue
        ts = item.get("股票代码") or item.get("code")
        if ts is not None and _code_from_iwencai_ts(_stringify_iwencai_code(ts)) == want:
            row = item
            break
    if row is None and datas and isinstance(datas[0], dict):
        row = datas[0]

    if row is None:
        return None

    date_suf = _latest_date_suffix(row)

    name = str(row.get("股票简称") or row.get("name") or "").strip()
    price = safe_float(row.get("最新价"))
    if price is None or price <= 0:
        close_key = None
        if date_suf:
            close_key = f"收盘价[{date_suf}]"
        if close_key and close_key in row:
            price = safe_float(row.get(close_key))
    if price is None or price <= 0:
        return None

    change_pct = safe_float(row.get("最新涨跌幅"))
    if change_pct is None and date_suf:
        change_pct = safe_float(_get_by_suffix(row, "涨跌幅", date_suf))

    change_amount = safe_float(_get_by_suffix(row, "涨跌_前复权", date_suf))
    if change_amount is None:
        change_amount = safe_float(row.get("涨跌额"))

    open_p = safe_float(_get_by_suffix(row, "开盘价_前复权", date_suf))
    high = safe_float(_get_by_suffix(row, "最高价_前复权", date_suf))
    low = safe_float(_get_by_suffix(row, "最低价_前复权", date_suf))

    vol = safe_int(_get_by_suffix(row, "成交量", date_suf))
    amt = safe_float(_get_by_suffix(row, "成交额", date_suf))
    turnover = safe_float(_get_by_suffix(row, "换手率", date_suf))
    amplitude = safe_float(_get_by_suffix(row, "振幅", date_suf))
    volume_ratio = safe_float(_get_by_suffix(row, "量比", date_suf))
    pe_ratio = safe_float(_get_by_prefixes(row, ("市盈率", "动态市盈率"), date_suf))
    pb_ratio = safe_float(_get_by_suffix(row, "市净率", date_suf))
    total_mv = safe_float(_get_by_suffix(row, "总市值", date_suf))
    circ_mv = safe_float(_get_by_suffix(row, "流通市值", date_suf))

    pre_close: Optional[float] = None
    if price is not None and change_amount is not None:
        pre_close = price - change_amount

    return UnifiedRealtimeQuote(
        code=want,
        name=name,
        source=RealtimeSource.IWENCAI_MARKET_QUERY,
        price=price,
        change_pct=change_pct,
        change_amount=change_amount,
        volume=vol,
        amount=amt,
        volume_ratio=volume_ratio,
        turnover_rate=turnover,
        amplitude=amplitude,
        open_price=open_p,
        high=high,
        low=low,
        pre_close=pre_close,
        pe_ratio=pe_ratio,
        pb_ratio=pb_ratio,
        total_mv=total_mv,
        circ_mv=circ_mv,
    )


class IwencaiMarketQueryFetcher(BaseFetcher):
    """
    iWencai-backed data source for A-share quotes, K-line history, chip data,
    market overview, sectors, and lightweight fundamentals.
    """

    name = "IwencaiMarketQueryFetcher"
    priority = int(os.getenv("IWENCAI_MARKET_QUERY_PRIORITY", "-1"))

    def __init__(self) -> None:
        from src.config import get_config

        cfg = get_config()
        self._cli_path = Path(cfg.iwencai_cli_path) if cfg.iwencai_cli_path else _default_cli_path()
        self._skill_cwd = self._cli_path.parent.parent
        self._query_template = cfg.iwencai_market_query_template
        self._timeout = max(5, int(cfg.iwencai_subprocess_timeout_sec))

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        stock_code = normalize_stock_code(stock_code)
        if is_us_stock_code(stock_code) or _iwencai_is_hk_code(stock_code):
            raise DataFetchError("Iwencai only handles A-share daily K-line data")

        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            query_days = max(30, min(260, (end_dt - start_dt).days + 1))
        except Exception:
            query_days = 60

        query = f"{stock_code}近{query_days}日开盘价最高价最低价收盘价成交量成交额涨跌幅"
        payload = iwencai_cli_query(query, limit=5, timeout=self._timeout)
        row = self._pick_stock_row(payload, stock_code)
        if row is None:
            raise DataFetchError(f"Iwencai returned no K-line rows for {stock_code}")

        dates = sorted(
            {
                _DATE_SUFFIX_RE.search(k).group(1)
                for k in row
                if isinstance(k, str) and _DATE_SUFFIX_RE.search(k)
            }
        )
        records: List[Dict[str, Any]] = []
        for d in dates:
            close = safe_float(_get_by_suffix(row, "收盘价", d))
            volume = safe_float(_get_by_suffix(row, "成交量", d))
            if close is None or volume is None:
                continue
            records.append(
                {
                    "date": datetime.strptime(d, "%Y%m%d").strftime("%Y-%m-%d"),
                    "open": safe_float(_get_by_suffix(row, "开盘价", d)),
                    "high": safe_float(_get_by_suffix(row, "最高价", d)),
                    "low": safe_float(_get_by_suffix(row, "最低价", d)),
                    "close": close,
                    "volume": volume,
                    "amount": safe_float(_get_by_suffix(row, "成交额", d)),
                    "pct_chg": safe_float(_get_by_suffix(row, "涨跌幅", d)),
                }
            )

        if not records:
            raise DataFetchError(f"Iwencai K-line fields missing for {stock_code}")

        logger.info("[Iwencai] daily K-line OK for %s rows=%s", stock_code, len(records))
        return pd.DataFrame(records)

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        return df

    def _pick_stock_row(self, payload: Optional[Dict[str, Any]], stock_code: str) -> Optional[Dict[str, Any]]:
        if not payload or not payload.get("success"):
            return None
        datas = payload.get("datas") or []
        want = normalize_stock_code(stock_code)
        for item in datas:
            if not isinstance(item, dict):
                continue
            ts = item.get("股票代码") or item.get("code")
            if ts is not None and _code_from_iwencai_ts(_stringify_iwencai_code(ts)) == want:
                return item
        return datas[0] if datas and isinstance(datas[0], dict) else None

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        stock_code = normalize_stock_code(stock_code)
        if is_us_stock_code(stock_code) or _iwencai_is_hk_code(stock_code):
            logger.debug("[Iwencai] skip non-A-share code %s", stock_code)
            return None

        from src.config import get_config

        cfg = get_config()
        if not cfg.iwencai_market_query_enabled:
            return None
        if not (os.environ.get("IWENCAI_API_KEY") or "").strip():
            logger.debug("[Iwencai] IWENCAI_API_KEY not set, skip")
            return None
        if not self._cli_path.is_file():
            logger.warning("[Iwencai] CLI not found: %s", self._cli_path)
            return None

        query = self._query_template.format(code=stock_code)
        payload = iwencai_cli_query(query, limit=8, timeout=self._timeout)
        if not payload:
            return None

        quote = parse_iwencai_cli_payload(payload, stock_code)
        if quote and quote.has_basic_data():
            logger.info("[Iwencai] realtime quote OK for %s price=%s", stock_code, quote.price)
            return quote

        return None

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        quote = self.get_realtime_quote(stock_code)
        if quote and quote.name:
            return quote.name
        return None

    def get_chip_distribution(self, stock_code: str) -> Optional[ChipDistribution]:
        stock_code = normalize_stock_code(stock_code)
        query = f"{stock_code}筹码分布获利比例平均成本90%成本集中度70%成本集中度"
        payload = iwencai_cli_query(query, limit=3, timeout=self._timeout)
        row = self._pick_stock_row(payload, stock_code)
        if row is None:
            return None

        date_suf = _latest_date_suffix(row)
        profit = safe_float(_get_by_suffix(row, "收盘获利", date_suf), 0.0) or 0.0
        avg_cost = safe_float(_get_by_suffix(row, "平均成本", date_suf), 0.0) or 0.0
        conc90 = safe_float(_get_by_suffix(row, "集中度90", date_suf), 0.0) or 0.0
        conc70 = safe_float(_get_by_suffix(row, "集中度70", date_suf), 0.0) or 0.0
        if not avg_cost and not profit and not conc90:
            return None

        chip = ChipDistribution(
            code=stock_code,
            date=datetime.strptime(date_suf, "%Y%m%d").strftime("%Y-%m-%d") if date_suf else "",
            source="iwencai_market",
            profit_ratio=profit / 100 if profit > 1 else profit,
            avg_cost=avg_cost,
            concentration_90=conc90 / 100 if conc90 > 1 else conc90,
            concentration_70=conc70 / 100 if conc70 > 1 else conc70,
        )
        logger.info("[Iwencai] chip distribution OK for %s", stock_code)
        return chip

    def get_main_indices(self, region: str = "cn") -> Optional[List[Dict[str, Any]]]:
        if (region or "cn").lower() != "cn":
            return None
        query = (
            "上证指数深证成指创业板指科创50沪深300上证50"
            "最新价涨跌幅成交额开盘价最高价最低价振幅"
        )
        payload = iwencai_cli_query(query, limit=10, timeout=self._timeout)
        datas = (payload or {}).get("datas") or []
        results: List[Dict[str, Any]] = []
        for row in datas:
            if not isinstance(row, dict):
                continue
            date_suf = _latest_date_suffix(row)
            code = _stringify_iwencai_code(row.get("指数代码") or row.get("股票代码"))
            name = str(row.get("指数简称") or row.get("股票简称") or "").strip()
            current = safe_float(row.get("最新价")) or safe_float(_get_by_suffix(row, "收盘价", date_suf))
            if not code or not name or current is None:
                continue
            change_pct = safe_float(row.get("最新涨跌幅:前复权"))
            if change_pct is None:
                change_pct = safe_float(row.get("最新涨跌幅")) or safe_float(
                    _get_by_suffix(row, "涨跌幅", date_suf)
                )
            results.append(
                {
                    "code": code,
                    "name": name,
                    "current": current,
                    "change": safe_float(_get_by_suffix(row, "涨跌", date_suf), 0.0) or 0.0,
                    "change_pct": change_pct or 0.0,
                    "open": safe_float(_get_by_suffix(row, "开盘价", date_suf), 0.0) or 0.0,
                    "high": safe_float(_get_by_suffix(row, "最高价", date_suf), 0.0) or 0.0,
                    "low": safe_float(_get_by_suffix(row, "最低价", date_suf), 0.0) or 0.0,
                    "prev_close": 0.0,
                    "volume": safe_float(_get_by_suffix(row, "成交量", date_suf), 0.0) or 0.0,
                    "amount": safe_float(_get_by_suffix(row, "成交额", date_suf), 0.0) or 0.0,
                    "amplitude": safe_float(_get_by_suffix(row, "振幅", date_suf), 0.0) or 0.0,
                }
            )
        if results:
            logger.info("[Iwencai] main indices OK count=%s", len(results))
        return results or None

    def get_market_stats(self) -> Optional[Dict[str, Any]]:
        query = "今日A股上涨家数下跌家数平盘家数涨停家数跌停家数两市成交额"
        payload = iwencai_cli_query(query, limit=5, timeout=self._timeout)
        row = _first_row(payload or {})
        if row is None:
            return None
        date_suf = _latest_date_suffix(row)
        total_amount = safe_float(_get_by_suffix(row, "成交额", date_suf), 0.0) or 0.0
        stats = {
            "up_count": safe_int(_get_by_suffix(row, "上涨家数", date_suf), 0) or 0,
            "down_count": safe_int(_get_by_suffix(row, "下跌家数", date_suf), 0) or 0,
            "flat_count": safe_int(_get_by_suffix(row, "平盘家数", date_suf), 0) or 0,
            "limit_up_count": safe_int(_get_by_suffix(row, "涨停家数", date_suf), 0) or 0,
            "limit_down_count": safe_int(_get_by_suffix(row, "跌停家数", date_suf), 0) or 0,
            "total_amount": total_amount / 1e8 if total_amount > 1e6 else total_amount,
        }
        logger.info("[Iwencai] market stats OK")
        return stats

    def get_sector_rankings(self, n: int = 5) -> Optional[Tuple[List[Dict], List[Dict]]]:
        top = self._query_sector_rankings("前", n)
        bottom = self._query_sector_rankings("后", n)
        if top or bottom:
            logger.info("[Iwencai] sector rankings OK top=%s bottom=%s", len(top), len(bottom))
            return top, bottom
        return None

    def _query_sector_rankings(self, direction: str, n: int) -> List[Dict]:
        payload = iwencai_cli_query(
            f"今日行业板块涨跌幅排名{direction}{n}",
            limit=max(n, 5),
            timeout=self._timeout,
        )
        datas = (payload or {}).get("datas") or []
        sectors: List[Dict] = []
        for row in datas[:n]:
            if not isinstance(row, dict):
                continue
            date_suf = _latest_date_suffix(row)
            name = str(row.get("指数简称") or row.get("板块名称") or "").strip()
            change_pct = safe_float(row.get("最新涨跌幅:前复权"))
            if change_pct is None:
                change_pct = safe_float(_get_by_suffix(row, "涨跌幅", date_suf))
            if name and change_pct is not None:
                sectors.append({"name": name, "change_pct": change_pct})
        return sectors

    def get_base_info(self, stock_code: str) -> Optional[Dict[str, Any]]:
        stock_code = normalize_stock_code(stock_code)
        query = f"{stock_code}所属行业概念板块市盈率市净率总市值流通市值营业收入净利润ROE"
        payload = iwencai_cli_query(query, limit=5, timeout=self._timeout)
        row = self._pick_stock_row(payload, stock_code)
        if row is None:
            return None
        logger.info("[Iwencai] base info OK for %s", stock_code)
        return row


def iwencai_fetcher_should_register() -> bool:
    """Whether to append IwencaiMarketQueryFetcher in DataFetcherManager."""
    from src.config import get_config

    cfg = get_config()
    if not cfg.iwencai_market_query_enabled:
        return False
    if not (os.environ.get("IWENCAI_API_KEY") or "").strip():
        return False
    path = Path(cfg.iwencai_cli_path) if cfg.iwencai_cli_path else _default_cli_path()
    return path.is_file()
