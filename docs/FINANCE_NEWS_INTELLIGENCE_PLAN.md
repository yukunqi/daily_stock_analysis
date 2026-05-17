# Finance News Intelligence Plan

This document captures the current state and the next implementation steps for the finance-news intelligence layer.

## Direction

The project will not use active news search for the next finance-news intelligence implementation.

The implementation priority is to consume passive news collected by the local TrendRadar project:

```text
TrendRadar
  -> listens to configured news sources
  -> filters and stores matched news locally
  -> writes SQLite files under output/news/YYYY-MM-DD.db

daily_stock_analysis
  -> reads local TrendRadar news
  -> matches news to watchlist stocks and macro themes
  -> generates compact impact context
  -> injects that context into stock analysis and market review
```

This keeps source collection, keyword monitoring, and notification ownership inside TrendRadar, while this project focuses
on stock reasoning, macro interpretation, and report generation.

## Current State

Existing but not part of the next implementation path:

- `EastmoneyNewsSearchProvider` is integrated into `SearchService`.
  - It supports semantic finance-news search through the Eastmoney news endpoint.
  - It requires an API key for the currently integrated HTTP endpoint.
  - It defaults to excluding `NOTICE` and `REPORT` records so announcements and research reports do not pollute the news feed.
  - It supports optional `EASTMONEY_NEWS_INCLUDE_TYPES` and `EASTMONEY_NEWS_EXCLUDE_TYPES` overrides.
- `RSSHubFinanceSearchProvider` is integrated into `SearchService`.
  - It supports self-hosted RSSHub finance routes.
  - Default routes include 财联社电报, 金十数据, 汇通网快讯, 证券时报, and 财经网滚动.
  - It parses RSS/Atom XML, deduplicates feed items, applies local query matching, and maps common routes to readable source names.
- `MarketAnalyzer.search_market_news()` deduplicates market news by URL/title.
- Opt-in live integration tests are available in `tests/test_finance_news_live.py`.
  - They are gated by `RUN_FINANCE_NEWS_LIVE=1`, so normal CI remains deterministic.
  - Eastmoney live checks require `EASTMONEY_NEWS_API_KEY`.
  - RSSHub live checks require `RSSHUB_BASE_URL`.
- Documentation and configuration entries were added to `.env.example`, `README.md`, `docs/CHANGELOG.md`, and the Web UI config registry.
- Unit tests were added for Eastmoney and RSSHub finance providers.

These capabilities remain in the repository, but the TrendRadar integration plan below does not depend on them and should
not call them as fallback news sources.

Observed local TrendRadar state:

- Repository path: `/Users/yukunqi_1/git_project/TrendRadar`
- Docker service: `trendradar`
- Web output: `http://localhost:8080/index.html`
- Local news databases: `/Users/yukunqi_1/git_project/TrendRadar/output/news/YYYY-MM-DD.db`
- Current known hot-list schema:
  - `news_items(title, platform_id, rank, url, mobile_url, first_crawl_time, last_crawl_time, crawl_count, ...)`
  - `platforms(id, name, ...)`
- Example local run generated `/Users/yukunqi_1/git_project/TrendRadar/output/news/2026-05-16.db`.

Live validation already performed:

- Historical main entrypoint E2E with active search:
  - Command shape: `main.py --stocks 688256 --no-notify --no-market-review --force-run`
  - Stock: 寒武纪 `688256`
  - Result: success `1`, failed `0`
  - EastmoneyNews was used for multi-dimensional intelligence search.
  - News context was included in the LLM prompt.
  - Final AI result changed from a pure technical buy signal into a more cautious `持有/观望 | 评分 55 | 震荡`.
- RSSHub smoke:
  - Local RSSHub dev server returned `200` for `/cls/telegraph`.
  - `RSSHubFinance` successfully consumed 财联社电报 items.
  - `MarketAnalyzer.search_market_news()` returned deduplicated market news.
- TrendRadar smoke:
  - Docker service started successfully.
  - A local SQLite news database was generated.
  - Today's watchlist (`600519,300750,002594,000858,601127`) had at least one direct match:
    - `茅台再调价！多款非标产品提价 终端市场价应声上涨`

## Revised Gaps

- TrendRadar local SQLite news is not yet readable by `daily_stock_analysis`.
- There is no canonical internal schema for passive news items.
- Current stock analysis only consumes active search context; it cannot yet consume TrendRadar context.
- Current market review does not yet use TrendRadar macro/news context.
- Stock matching is currently ad hoc; it needs watchlist code/name/alias/industry matching.
- Macro and sector impact extraction is not modeled as a first-class workflow.
- Active search provider quality and dedup work is out of scope for this plan.
- RSSHub production hardening is out of scope because TrendRadar already owns source collection.

## Next Features

### 1. TrendRadar Local News Reader

Goal: make TrendRadar's local news database available inside this project without coupling to TrendRadar runtime internals.

Configuration:

- `TREND_RADAR_NEWS_ENABLED=false`
- `TREND_RADAR_OUTPUT_DIR=/Users/yukunqi_1/git_project/TrendRadar/output`
- `TREND_RADAR_NEWS_DAYS=1`
- `TREND_RADAR_NEWS_LIMIT=100`

Reader behavior:

- Read `output/news/YYYY-MM-DD.db` for today or the latest N days.
- Join `news_items` with `platforms`.
- Normalize rows into a project-owned data shape:
  - `title`
  - `summary` or empty string
  - `url`
  - `source`
  - `rank`
  - `published_at` or crawl date/time
  - `first_seen_at`
  - `last_seen_at`
  - `crawl_count`
  - `origin`: `trendradar`
- Deduplicate by URL first, then by normalized title/source.
- Fail gracefully when the directory or date DB is missing.
- Add focused unit tests with a temporary SQLite database.

Suggested module:

- `src/services/trend_radar_news_service.py`

### 2. Watchlist News Matching

Goal: find which TrendRadar news items are relevant to configured watchlist stocks.

Inputs:

- Existing watchlist stock codes from `STOCK_LIST`.
- Stock names from existing name prefetch/resolver where available.
- Optional alias map for common company/product names.

Matching behavior:

- Direct match:
  - stock code
  - stock name
  - common aliases
- Soft match:
  - industry keywords
  - product keywords
  - supply-chain terms
- Output per stock:
  - matched news items
  - direct vs indirect match reason
  - source/rank/time metadata

Initial implementation can be deterministic keyword matching. LLM-based relevance scoring can come later.

### 3. Stock News Impact Context

Goal: convert matched TrendRadar news into compact context for individual stock analysis.

Proposed output schema:

- `event_title`
- `match_type`: direct, industry, macro, supply_chain
- `related_stock`
- `related_industries`
- `direction`: bullish, bearish, neutral, mixed, unknown
- `time_horizon`: intraday, short_term, medium_term, long_term
- `impact_path`
- `confidence`
- `evidence`
- `watch_items`

Integration target:

- Inject a concise "TrendRadar News Context" block before the final stock-analysis prompt.
- When matched TrendRadar items include URLs, fetch a small number of article body excerpts before prompt injection.
- Cache fetched article text locally so repeated daily analysis does not refetch the same URLs.
- Prefer direct stock matches over industry-level matches when deciding which URLs receive the limited fetch budget.
- If article fetching fails or returns empty content, degrade to title/source/rank/URL context instead of blocking analysis.
- Do not call active news search as a fallback for this context.

### 4. Macro Market News Context

Goal: use all TrendRadar news to improve market review and top-down opportunity discovery.

Workflow:

- Collect broad TrendRadar news for the analysis window.
- Classify or bucket events into:
  - macro
  - policy
  - liquidity
  - geopolitical
  - industry
  - commodity
  - earnings
  - sentiment
- Derive:
  - market risk appetite
  - likely pressure points
  - likely beneficiary sectors
  - watchlist candidates or watchlist risks
- Inject this summary into `MarketAnalyzer.generate_market_review()`.

Example:

- News: global inflation pressure, oil rising, long-end yields rising.
- Impact path:
  - Higher inflation uncertainty.
  - Higher yields and valuation compression.
  - Pressure on long-duration AI/growth assets.
  - Potential support for energy and inflation-sensitive sectors.

### 5. Optional TrendRadar Diagnostics

Goal: make deployment and debugging clear.

Planned checks:

- Verify `TREND_RADAR_OUTPUT_DIR` exists.
- Verify at least one `output/news/YYYY-MM-DD.db` exists within `TREND_RADAR_NEWS_DAYS`.
- Verify SQLite schema contains `news_items` and `platforms`.
- Expose a small CLI/debug method to print latest item count by source.

## Deferred / Lower Priority

These items are not removed permanently, but they are no longer the next implementation priority:

- Active search quality ranking for Eastmoney/RSSHub/Bocha/Tavily results.
- Similar-article clustering across active search providers.
- RSSHub production setup documentation.
- Provider-level health checks for active search providers.
- Repeated fund-holding floating-loss article suppression/downranking.

## Suggested Session Split

Use one fresh session per feature:

1. `trendradar-local-news-reader`
2. `trendradar-watchlist-news-matching`
3. `trendradar-stock-impact-context`
4. `trendradar-macro-market-context`
5. `trendradar-diagnostics-and-docs`
