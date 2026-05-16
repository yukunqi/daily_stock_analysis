# Finance News Intelligence Plan

This document captures the current state and the next implementation steps for the finance-news intelligence layer.

## Current State

Implemented and live-tested:

- `EastmoneyNewsSearchProvider` is integrated into `SearchService`.
  - It supports semantic finance-news search through the Eastmoney news endpoint.
  - It defaults to excluding `NOTICE` and `REPORT` records so announcements and research reports do not pollute the news feed.
  - It supports optional `EASTMONEY_NEWS_INCLUDE_TYPES` and `EASTMONEY_NEWS_EXCLUDE_TYPES` overrides.
- `RSSHubFinanceSearchProvider` is integrated into `SearchService`.
  - It supports self-hosted RSSHub finance routes.
  - Default routes include 财联社电报, 金十数据, 汇通网快讯, 证券时报, and 财经网滚动.
  - It parses RSS/Atom XML, deduplicates feed items, applies local query matching, and maps common routes to readable source names.
- `MarketAnalyzer.search_market_news()` now deduplicates market news by URL/title.
- Documentation and configuration entries were added to `.env.example`, `README.md`, `docs/CHANGELOG.md`, and the Web UI config registry.
- Unit tests were added for Eastmoney and RSSHub finance providers.

Live validation already performed:

- Main entrypoint E2E:
  - Command shape: `main.py --stocks 688256 --no-notify --no-market-review --force-run`
  - Stock: 寒武纪 `688256`
  - Result: success `1`, failed `0`
  - EastmoneyNews was used for multi-dimensional intelligence search.
  - News context was included in the LLM prompt.
  - Final AI result changed from a pure technical buy signal into a more cautious `持有/观望 | 评分 55 | 震荡`.
- RSSHub live smoke:
  - Local RSSHub dev server returned `200` for `/cls/telegraph`.
  - `RSSHubFinance` successfully consumed 财联社电报 items.
  - `MarketAnalyzer.search_market_news()` returned deduplicated market news.

## Known Gaps

- Eastmoney results still contain noisy but valid finance-news articles, such as repeated fund-holding floating-loss articles.
- The current LLM prompt consumes news as context but does not produce a structured event-impact object.
- News source quality is not yet scored.
- Similar articles are not yet clustered into a single event.
- Macro-to-sector-to-stock transmission is not yet modeled as a first-class workflow.
- RSSHub requires a stable self-hosted service for production use.
- Live integration tests are manual; there is no opt-in automated live test yet.

## Next Features

### 1. Opt-in Live Integration Tests

Goal: make real data-source validation repeatable without making CI flaky.

Proposed test mode:

- `RUN_FINANCE_NEWS_LIVE=1`
- Optional `EASTMONEY_NEWS_API_KEY`
- Optional `RSSHUB_BASE_URL`

Test coverage:

- Eastmoney provider can return recent stock-related news.
- Eastmoney provider excludes `NOTICE` and `REPORT` by default.
- RSSHub provider can fetch at least one configured finance route.
- `SearchService.search_comprehensive_intel()` can run against one real A-share symbol.
- `MarketAnalyzer.search_market_news()` deduplicates feed results.

### 2. Structured News Impact Layer

Goal: separate news interpretation from general stock analysis.

Proposed output schema:

- `event_title`
- `event_type`: macro, industry, company, policy, geopolitical, liquidity, earnings, risk, sentiment
- `related_stocks`
- `related_industries`
- `direction`: bullish, bearish, neutral, mixed
- `time_horizon`: intraday, short_term, medium_term, long_term
- `impact_path`
- `confidence`
- `evidence`
- `watch_items`

This layer should run before the final stock-analysis prompt and provide a compact, structured summary.

### 3. News Quality and Deduplication

Goal: reduce repeated low-signal articles.

Planned improvements:

- URL-level deduplication.
- Title-similarity deduplication.
- Event clustering across similar articles.
- Source weighting.
- Weak relevance filtering.
- Repeated fund-holding floating-loss article suppression or downranking.

### 4. Macro News Discovery Workflow

Goal: support top-down opportunity discovery.

Workflow:

- Collect broad market news from RSSHub/GDELT/other free sources.
- Classify macro events.
- Map each event to likely asset-class impact.
- Map affected asset classes to industries.
- Map industries to candidate stocks.
- Generate a watchlist with rationale and confidence.

Example:

- CPI above expectation
- Higher rate-cut uncertainty or renewed hike expectations
- Higher yields and valuation compression
- Pressure on long-duration growth assets
- Watch high-valuation technology and growth sectors

### 5. Provider Configuration Hardening

Goal: make deployment cleaner.

Planned improvements:

- Document recommended RSSHub self-host deployment.
- Add provider health checks.
- Add provider-level timeouts and clearer diagnostics.
- Add config validation warnings for enabled providers without required runtime services.

## Suggested Session Split

Use one fresh session per feature:

1. `finance-news-live-tests`
2. `news-impact-schema`
3. `news-dedup-quality-ranking`
4. `macro-to-sector-stock-discovery`
5. `rsshub-production-setup-docs`
