# -*- coding: utf-8 -*-
"""Nightly next-session opportunity report.

The report reuses the existing market-review data path instead of adding a
parallel market-data stack:
- market indices, breadth, sector rankings from ``MarketAnalyzer``
- TrendRadar / configured search news through ``MarketAnalyzer.search_market_news``
- concept rankings, hot stocks, and limit-up pool from ``DataFetcherManager``
- prior market-review markdown from ``AnalysisHistory``
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, select

from data_provider.base import DataFetcherManager
from src.analyzer import AnalysisResult, GeminiAnalyzer
from src.config import get_config
from src.core.market_review import MARKET_REVIEW_REPORT_TYPE
from src.market_analyzer import MarketAnalyzer, MarketOverview
from src.notification import NotificationService
from src.report_language import normalize_report_language
from src.search_service import SearchService
from src.storage import AnalysisHistory, DatabaseManager

logger = logging.getLogger(__name__)

OPPORTUNITY_HISTORY_CODE = "OPPORT"
OPPORTUNITY_REPORT_TYPE = "opportunity"


@dataclass
class OpportunityInputs:
    """Collected context for the nightly opportunity report."""

    overview: MarketOverview
    news: List[Any]
    concept_top: List[Dict[str, Any]]
    concept_bottom: List[Dict[str, Any]]
    hot_stocks: List[Dict[str, Any]]
    limit_up_pool: List[Dict[str, Any]]
    latest_market_review: str = ""


def run_opportunity_report(
    notifier: NotificationService,
    analyzer: Optional[GeminiAnalyzer] = None,
    search_service: Optional[SearchService] = None,
    send_notification: bool = True,
    query_id: Optional[str] = None,
) -> Optional[str]:
    """Generate, persist, and optionally send the nightly opportunity report."""
    logger.info("开始执行明日机会分析...")
    config = get_config()
    language = normalize_report_language(getattr(config, "report_language", "zh"))

    try:
        market_analyzer = MarketAnalyzer(
            search_service=search_service,
            analyzer=analyzer,
            region="cn",
        )
        inputs = collect_opportunity_inputs(market_analyzer)
        payload = generate_opportunity_payload(inputs, analyzer, language=language)
        markdown_report = payload.get("report_markdown") or _build_template_report(inputs, payload, language)
        markdown_report = _ensure_report_title(markdown_report, language)

        date_str = datetime.now().strftime("%Y%m%d")
        report_filename = f"opportunity_report_{date_str}.md"
        filepath = notifier.save_report_to_file(markdown_report, report_filename)
        logger.info("明日机会报告已保存: %s", filepath)

        _persist_opportunity_history(
            markdown_report=markdown_report,
            payload=payload,
            inputs=inputs,
            config=config,
            query_id=query_id,
        )

        if send_notification and notifier.is_available():
            success = notifier.send(markdown_report, email_send_to_all=True, route_type="report")
            if success:
                logger.info("明日机会报告推送成功")
            else:
                logger.warning("明日机会报告推送失败")
        elif not send_notification:
            logger.info("已跳过推送通知 (--no-notify)")

        return markdown_report
    except Exception as exc:
        logger.error("明日机会分析失败: %s", exc, exc_info=True)
        return None


def collect_opportunity_inputs(market_analyzer: MarketAnalyzer) -> OpportunityInputs:
    """Collect deterministic inputs for opportunity discovery."""
    overview = market_analyzer.get_market_overview()
    news = market_analyzer.search_market_news(overview)
    data_manager: DataFetcherManager = market_analyzer.data_manager

    concept_top: List[Dict[str, Any]] = []
    concept_bottom: List[Dict[str, Any]] = []
    hot_stocks: List[Dict[str, Any]] = []
    limit_up_pool: List[Dict[str, Any]] = []

    try:
        concept_top, concept_bottom = data_manager.get_concept_rankings(5)
    except Exception as exc:
        logger.warning("[机会] 获取概念排行失败，继续使用行业板块数据: %s", exc)

    try:
        hot_stocks = data_manager.get_hot_stocks(12)
    except Exception as exc:
        logger.warning("[机会] 获取人气股失败，继续使用其他候选: %s", exc)

    try:
        limit_up_pool = data_manager.get_limit_up_pool(n=20)
    except Exception as exc:
        logger.warning("[机会] 获取涨停池失败，继续使用其他候选: %s", exc)

    return OpportunityInputs(
        overview=overview,
        news=news,
        concept_top=concept_top or [],
        concept_bottom=concept_bottom or [],
        hot_stocks=hot_stocks or [],
        limit_up_pool=limit_up_pool or [],
        latest_market_review=_load_latest_market_review_markdown(),
    )


def generate_opportunity_payload(
    inputs: OpportunityInputs,
    analyzer: Optional[GeminiAnalyzer],
    *,
    language: str = "zh",
) -> Dict[str, Any]:
    """Generate a structured payload. Falls back to deterministic template data."""
    fallback = _build_fallback_payload(inputs, language)
    if not analyzer or not analyzer.is_available():
        logger.warning("[机会] AI 分析器未配置或不可用，使用模板生成报告")
        return fallback

    prompt = _build_opportunity_prompt(inputs, language)
    logger.info("[机会] 调用大模型生成明日机会报告...")
    response = analyzer.generate_text(prompt, max_tokens=8192, temperature=0.7)
    parsed = _parse_json_object(response)
    if not parsed:
        logger.warning("[机会] 大模型未返回可解析 JSON，使用模板生成报告")
        return fallback

    normalized = _normalize_payload(parsed, fallback)
    if not normalized.get("report_markdown"):
        normalized["report_markdown"] = _build_template_report(inputs, normalized, language)
    logger.info("[机会] 明日机会报告生成成功，推荐个股 %d 个", len(normalized["stock_recommendations"]))
    return normalized


def get_latest_opportunity_performance_markdown(
    *,
    lookback_days: int = 5,
    max_stocks: int = 5,
    data_manager: Optional[DataFetcherManager] = None,
) -> Optional[str]:
    """Build a markdown block that evaluates the latest prior opportunity report."""
    record = _load_latest_prior_opportunity_record(lookback_days=lookback_days)
    if record is None:
        return None

    recommendations = _extract_recommendations_from_record(record)
    if not recommendations:
        return None

    data_manager = data_manager or DataFetcherManager()
    created_at = record.created_at.strftime("%Y-%m-%d %H:%M") if record.created_at else "未知时间"
    lines = [
        "### 八、前一晚机会跟踪",
        f"> 来源：{created_at} 生成的明日机会报告；用于验证机会方向是否获得次日市场确认。",
        "",
        "| 板块 | 个股 | 昨晚参考价 | 当前价 | 表现 | 验证结论 |",
        "|------|------|------------|--------|------|----------|",
    ]

    for rec in recommendations[:max_stocks]:
        code = str(rec.get("code") or "").strip()
        name = str(rec.get("name") or "").strip() or code
        sector = str(rec.get("sector") or "").strip() or "-"
        entry_price = _safe_float(rec.get("entry_price") or rec.get("price"))
        current_price = None
        quote_change_pct = None
        if code:
            try:
                try:
                    quote = data_manager.get_realtime_quote(code, log_final_failure=False)
                except TypeError:
                    quote = data_manager.get_realtime_quote(code)
                current_price = _safe_float(getattr(quote, "price", None))
                quote_change_pct = _safe_float(getattr(quote, "change_pct", None))
                if quote and not name:
                    name = getattr(quote, "name", "") or code
            except Exception as exc:
                logger.debug("[机会] 获取推荐股表现失败 code=%s: %s", code, exc)

        performance = "N/A"
        verdict = "待补充行情"
        if entry_price and current_price:
            gain = (current_price - entry_price) / entry_price * 100
            performance = _format_signed_pct(gain)
            verdict = _performance_verdict(gain)
        elif quote_change_pct is not None:
            performance = f"当日{_format_signed_pct(quote_change_pct)}"
            verdict = _performance_verdict(quote_change_pct)

        stock_label = f"{name}({code})" if code and name != code else (code or name or "-")
        lines.append(
            f"| {_escape_table_cell(sector)} | {_escape_table_cell(stock_label)} | "
            f"{_format_price(entry_price)} | {_format_price(current_price)} | {performance} | {verdict} |"
        )

    return "\n".join(lines)


def _persist_opportunity_history(
    *,
    markdown_report: str,
    payload: Dict[str, Any],
    inputs: OpportunityInputs,
    config: object,
    query_id: Optional[str],
) -> int:
    report_language = normalize_report_language(getattr(config, "report_language", "zh"))
    result = AnalysisResult(
        code=OPPORTUNITY_HISTORY_CODE,
        name="明日机会",
        sentiment_score=_sentiment_score_from_payload(payload),
        trend_prediction=str(payload.get("market_sentiment") or "机会观察"),
        operation_advice="查看机会",
        analysis_summary=_summarize_markdown(markdown_report),
        report_language=report_language,
        dashboard={"opportunity_report": payload},
        news_summary=markdown_report,
        raw_response=markdown_report,
        data_sources="opportunity_report",
    )
    context_snapshot = {
        "report_kind": OPPORTUNITY_REPORT_TYPE,
        "market_date": inputs.overview.date,
        "opportunity_sectors": payload.get("opportunity_sectors", []),
        "stock_recommendations": payload.get("stock_recommendations", []),
        "source_counts": {
            "news": len(inputs.news),
            "concept_top": len(inputs.concept_top),
            "hot_stocks": len(inputs.hot_stocks),
            "limit_up_pool": len(inputs.limit_up_pool),
        },
    }
    history_query_id = query_id or f"opportunity_{uuid.uuid4().hex}"
    saved = DatabaseManager.get_instance().save_analysis_history(
        result=result,
        query_id=history_query_id,
        report_type=OPPORTUNITY_REPORT_TYPE,
        news_content=markdown_report,
        context_snapshot=context_snapshot,
        save_snapshot=True,
    )
    if saved:
        logger.info("明日机会历史记录已保存: query_id=%s", history_query_id)
    else:
        logger.warning("明日机会历史记录保存失败: query_id=%s", history_query_id)
    return saved


def _build_opportunity_prompt(inputs: OpportunityInputs, language: str) -> str:
    sectors_text = _format_sector_rows(inputs.overview.top_sectors, title="行业领涨")
    concepts_text = _format_sector_rows(inputs.concept_top, title="概念领涨")
    bottom_text = _format_sector_rows(inputs.overview.bottom_sectors, title="行业领跌")
    news_text = _format_news_rows(inputs.news)
    hot_text = _format_stock_rows(inputs.hot_stocks, "人气股")
    limit_text = _format_stock_rows(inputs.limit_up_pool, "涨停/连板候选")
    review_text = inputs.latest_market_review[:3000] if inputs.latest_market_review else "暂无已保存的大盘复盘正文"

    return f"""你是一位A股盘后策略分析师。请基于【今日新闻】【大盘复盘】【板块/概念表现】【人气股/涨停池】生成“明日投资机会报告”。

输出必须是一个 JSON 对象，不要输出代码块。JSON 字段：
- market_sentiment: 字符串，描述明日市场情绪
- opportunity_sectors: 数组，每项包含 sector, confidence, logic, watch_points
- stock_recommendations: 数组，每项包含 code, name, sector, entry_price, reason, risk, trigger
- report_markdown: 字符串，完整 Markdown 报告

报告至少包含：
1. 第二天的投资机会板块是什么
2. 为什么是它，推理逻辑是什么
3. 板块里面的个股推荐哪个，为什么

约束：
- 只基于给定候选和证据推理，不要编造行情数据
- 个股推荐优先从【涨停/连板候选】和【人气股】中选择，最多 5 只
- 必须写清楚失效条件和风险提示
- 这不是投资建议，结尾必须说明“仅供参考，不构成投资建议”
- report_markdown 使用中文

# 今日市场日期
{inputs.overview.date}

# 市场宽度
- 上涨: {inputs.overview.up_count}
- 下跌: {inputs.overview.down_count}
- 平盘: {inputs.overview.flat_count}
- 涨停: {inputs.overview.limit_up_count}
- 跌停: {inputs.overview.limit_down_count}
- 成交额: {inputs.overview.total_amount:.0f} 亿

# 板块与概念
{sectors_text}

{concepts_text}

{bottom_text}

# 今日新闻
{news_text}

# 已保存大盘复盘摘要
{review_text}

# 个股候选
{limit_text}

{hot_text}
"""


def _build_fallback_payload(inputs: OpportunityInputs, language: str) -> Dict[str, Any]:
    sector_pool = (inputs.concept_top or []) + (inputs.overview.top_sectors or [])
    sectors = []
    for item in sector_pool[:3]:
        sector_name = str(item.get("name") or item.get("sector") or "").strip()
        if not sector_name:
            continue
        sectors.append({
            "sector": sector_name,
            "confidence": "medium",
            "logic": f"当日涨幅居前，涨跌幅 {_format_signed_pct(item.get('change_pct'))}，需要结合新闻催化与次日量能确认。",
            "watch_points": "观察开盘强度、成交额延续和板块内个股扩散。",
        })
    if not sectors:
        sectors.append({
            "sector": "强于指数的主线板块",
            "confidence": "low",
            "logic": "板块数据不足，暂以市场宽度、新闻催化和人气股强度做观察。",
            "watch_points": "等待板块榜和量能确认后再提高确定性。",
        })

    recommendations = _select_fallback_recommendations(inputs, sectors)
    payload = {
        "market_sentiment": _fallback_market_sentiment(inputs.overview),
        "opportunity_sectors": sectors,
        "stock_recommendations": recommendations,
    }
    payload["report_markdown"] = _build_template_report(inputs, payload, language)
    return payload


def _select_fallback_recommendations(
    inputs: OpportunityInputs,
    sectors: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    sector_names = [str(item.get("sector") or "") for item in sectors]
    rows: List[Dict[str, Any]] = []
    seen_codes = set()

    def add_row(row: Dict[str, Any], source: str) -> None:
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "").strip()
        if not code and not name:
            return
        dedupe_key = code or name
        if dedupe_key in seen_codes:
            return
        industry = str(row.get("industry") or row.get("sector") or "").strip()
        price = _safe_float(row.get("price"))
        seen_codes.add(dedupe_key)
        rows.append({
            "code": code,
            "name": name,
            "sector": industry or (sector_names[0] if sector_names else ""),
            "entry_price": price,
            "reason": f"{source}候选，当前热度靠前；需等待次日板块共振和量能确认。",
            "risk": "若开盘冲高回落、板块未扩散或指数转弱，应降低关注优先级。",
            "trigger": "次日高开后不破分时均线，或回踩承接后重新放量。",
        })

    for sector in sector_names:
        for row in inputs.limit_up_pool:
            industry = str(row.get("industry") or "")
            if sector and industry and (sector in industry or industry in sector):
                add_row(row, "涨停/连板")
    for row in inputs.limit_up_pool:
        add_row(row, "涨停/连板")
        if len(rows) >= 3:
            break
    for row in inputs.hot_stocks:
        add_row(row, "人气榜")
        if len(rows) >= 5:
            break
    return rows[:5]


def _build_template_report(
    inputs: OpportunityInputs,
    payload: Dict[str, Any],
    language: str,
) -> str:
    sectors = payload.get("opportunity_sectors") or []
    recs = payload.get("stock_recommendations") or []
    sector_lines = []
    for idx, item in enumerate(sectors[:3], 1):
        sector_lines.append(
            f"{idx}. **{item.get('sector', '-')}**（置信度：{item.get('confidence', 'medium')}）\n"
            f"   - 逻辑：{item.get('logic', '-')}\n"
            f"   - 观察点：{item.get('watch_points', '-')}"
        )
    rec_lines = [
        "| 板块 | 个股 | 参考价 | 推荐逻辑 | 风险/失效条件 |",
        "|------|------|--------|----------|----------------|",
    ]
    for rec in recs[:5]:
        stock_label = f"{rec.get('name') or '-'}({rec.get('code') or '-'})"
        rec_lines.append(
            f"| {_escape_table_cell(str(rec.get('sector') or '-'))} | "
            f"{_escape_table_cell(stock_label)} | {_format_price(_safe_float(rec.get('entry_price')))} | "
            f"{_escape_table_cell(str(rec.get('reason') or '-'))} | "
            f"{_escape_table_cell(str(rec.get('risk') or rec.get('trigger') or '-'))} |"
        )
    if not recs:
        rec_lines.append("| - | 暂无候选 | - | 数据不足，等待次日确认 | 不追高 |")

    news_titles = [
        _compact_text(_get_item_field(item, "title"), 80)
        for item in inputs.news[:5]
        if _get_item_field(item, "title")
    ]
    news_block = "\n".join([f"- {title}" for title in news_titles]) or "- 暂无可用新闻，降低题材确定性。"

    return f"""# 🌙 明日投资机会报告

## 一、明日市场情绪
{payload.get('market_sentiment') or _fallback_market_sentiment(inputs.overview)}

## 二、机会板块
{chr(10).join(sector_lines)}

## 三、推理逻辑
- 盘面宽度：上涨 {inputs.overview.up_count} 家、下跌 {inputs.overview.down_count} 家，涨停 {inputs.overview.limit_up_count} 家，跌停 {inputs.overview.limit_down_count} 家。
- 板块强度：优先参考行业/概念涨幅榜、涨停池和人气股是否互相印证。
- 新闻催化：关注是否有政策、产业、业绩或事件催化能支撑次日资金继续聚焦。

## 四、个股推荐
{chr(10).join(rec_lines)}

## 五、今日新闻线索
{news_block}

## 六、次日验证条件
- 机会板块需要在开盘后继续保持相对指数强度。
- 推荐个股若出现高开低走、放量滞涨或板块内部掉队，应视为推荐逻辑弱化。
- 若指数与成交额同时转弱，优先降低进攻仓位。

仅供参考，不构成投资建议。
"""


def _normalize_payload(parsed: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(fallback)
    if isinstance(parsed.get("market_sentiment"), str):
        payload["market_sentiment"] = parsed["market_sentiment"].strip()
    if isinstance(parsed.get("opportunity_sectors"), list):
        sectors = [item for item in parsed["opportunity_sectors"] if isinstance(item, dict)]
        if sectors:
            payload["opportunity_sectors"] = sectors[:5]
    if isinstance(parsed.get("stock_recommendations"), list):
        recs = [item for item in parsed["stock_recommendations"] if isinstance(item, dict)]
        if recs:
            payload["stock_recommendations"] = recs[:5]
    if isinstance(parsed.get("report_markdown"), str) and parsed["report_markdown"].strip():
        payload["report_markdown"] = parsed["report_markdown"].strip()
    return payload


def _parse_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start:end + 1]
    try:
        data = json.loads(candidate)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _load_latest_market_review_markdown() -> str:
    try:
        db = DatabaseManager.get_instance()
        with db.get_session() as session:
            record = session.execute(
                select(AnalysisHistory)
                .where(AnalysisHistory.report_type == MARKET_REVIEW_REPORT_TYPE)
                .order_by(desc(AnalysisHistory.created_at))
                .limit(1)
            ).scalar_one_or_none()
            if record is None:
                return ""
            raw = _safe_json_loads(record.raw_result)
            if isinstance(raw, dict):
                for field in ("raw_response", "market_review_report"):
                    value = raw.get(field)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            return (record.news_content or record.analysis_summary or "").strip()
    except Exception as exc:
        logger.debug("[机会] 读取最近大盘复盘失败: %s", exc)
        return ""


def _load_latest_prior_opportunity_record(*, lookback_days: int) -> Optional[AnalysisHistory]:
    try:
        today_start = datetime.combine(date.today(), time.min)
        cutoff = today_start - timedelta(days=max(1, lookback_days))
        db = DatabaseManager.get_instance()
        with db.get_session() as session:
            return session.execute(
                select(AnalysisHistory)
                .where(
                    AnalysisHistory.report_type == OPPORTUNITY_REPORT_TYPE,
                    AnalysisHistory.created_at < today_start,
                    AnalysisHistory.created_at >= cutoff,
                )
                .order_by(desc(AnalysisHistory.created_at))
                .limit(1)
            ).scalar_one_or_none()
    except Exception as exc:
        logger.debug("[机会] 读取前一晚机会记录失败: %s", exc)
        return None


def _extract_recommendations_from_record(record: AnalysisHistory) -> List[Dict[str, Any]]:
    raw = _safe_json_loads(record.raw_result)
    if isinstance(raw, dict):
        dashboard = raw.get("dashboard")
        if isinstance(dashboard, dict):
            opportunity = dashboard.get("opportunity_report")
            if isinstance(opportunity, dict) and isinstance(opportunity.get("stock_recommendations"), list):
                return [item for item in opportunity["stock_recommendations"] if isinstance(item, dict)]
        if isinstance(raw.get("stock_recommendations"), list):
            return [item for item in raw["stock_recommendations"] if isinstance(item, dict)]

    snapshot = _safe_json_loads(record.context_snapshot)
    if isinstance(snapshot, dict) and isinstance(snapshot.get("stock_recommendations"), list):
        return [item for item in snapshot["stock_recommendations"] if isinstance(item, dict)]
    return []


def _safe_json_loads(value: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _format_sector_rows(rows: List[Dict[str, Any]], *, title: str) -> str:
    if not rows:
        return f"{title}: 暂无"
    lines = [f"{title}:"]
    for idx, row in enumerate(rows[:5], 1):
        name = row.get("name") or row.get("sector") or "-"
        lines.append(f"{idx}. {name}: {_format_signed_pct(row.get('change_pct'))}")
    return "\n".join(lines)


def _format_stock_rows(rows: List[Dict[str, Any]], title: str) -> str:
    if not rows:
        return f"{title}: 暂无"
    lines = [f"{title}:"]
    for idx, row in enumerate(rows[:12], 1):
        parts = [
            f"{idx}. {row.get('name') or '-'}({row.get('code') or '-'})",
            f"价格={_format_price(_safe_float(row.get('price')))}",
            f"涨跌幅={_format_signed_pct(row.get('change_pct'))}",
        ]
        if row.get("industry"):
            parts.append(f"行业={row.get('industry')}")
        if row.get("consecutive_boards") is not None:
            parts.append(f"连板={row.get('consecutive_boards')}")
        if row.get("source"):
            parts.append(f"来源={row.get('source')}")
        lines.append("；".join(parts))
    return "\n".join(lines)


def _format_news_rows(news: List[Any]) -> str:
    if not news:
        return "暂无"
    lines = []
    for idx, item in enumerate(news[:8], 1):
        title = _compact_text(_get_item_field(item, "title"), 100)
        snippet = _compact_text(_get_item_field(item, "snippet"), 180)
        source = _compact_text(_get_item_field(item, "source"), 40)
        url = _compact_text(_get_item_field(item, "url"), 160)
        lines.append(f"{idx}. {title or '-'} | {snippet or '-'} | {source or '-'} | {url or '-'}")
    return "\n".join(lines)


def _get_item_field(item: Any, field: str) -> str:
    if hasattr(item, field):
        value = getattr(item, field, "") or ""
    elif isinstance(item, dict):
        value = item.get(field, "") or ""
    else:
        value = ""
    return str(value).strip()


def _fallback_market_sentiment(overview: MarketOverview) -> str:
    participants = overview.up_count + overview.down_count
    up_ratio = overview.up_count / participants if participants else 0.5
    if up_ratio >= 0.6 and overview.limit_up_count >= overview.limit_down_count:
        return "风险偏好偏暖，明日可关注主线延续和强势板块扩散。"
    if up_ratio <= 0.4:
        return "风险偏好偏弱，明日机会以低吸确认和防守反击为主。"
    return "市场情绪分化，明日重点观察领涨板块持续性和成交额确认。"


def _sentiment_score_from_payload(payload: Dict[str, Any]) -> int:
    sectors = payload.get("opportunity_sectors") or []
    high = sum(1 for item in sectors if str(item.get("confidence", "")).lower() == "high")
    medium = sum(1 for item in sectors if str(item.get("confidence", "")).lower() == "medium")
    return max(45, min(80, 55 + high * 8 + medium * 3))


def _ensure_report_title(markdown_report: str, language: str) -> str:
    text = (markdown_report or "").strip()
    if text.startswith("#"):
        return text
    return f"# 🌙 明日投资机会报告\n\n{text}"


def _summarize_markdown(markdown_report: str) -> str:
    for line in (markdown_report or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if text and not text.startswith("|") and not text.startswith("---"):
            return text[:200]
    return "明日机会报告已生成。"


def _compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _format_signed_pct(value: Any) -> str:
    numeric = _safe_float(value)
    if numeric is None:
        return "N/A"
    return f"{numeric:+.2f}%"


def _format_price(value: Optional[float]) -> str:
    if value is None or value <= 0:
        return "N/A"
    return f"{value:.2f}"


def _performance_verdict(gain_pct: float) -> str:
    if gain_pct >= 3:
        return "明显验证"
    if gain_pct >= 0:
        return "初步验证"
    if gain_pct > -3:
        return "未明显验证"
    return "验证失败"


def _escape_table_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
