---
name: collect-company-facts
description: "Collect SEC filings plus news and papers for a ticker and write filings_index.yaml and digests under current/. Use when building the evidence pool or when filings_index.yaml/news_digest.yaml/papers_digest.yaml need refresh."
version: v0.1
---

# collect-company-facts

## Overview
Build a stable, incremental, and traceable evidence pool by collecting SEC filings, news, and technical papers.

## Inputs
- ticker (required)
- as_of (optional, default today)
- lookback_years (optional, default 10)
- lookback_days_news (optional, default 180)
- papers_mode (optional, auto|on|off, default auto)
- force_refresh (optional, default false)

## Hard dependencies
- company/{TICKER}/company.yaml with a valid cik

## Outputs
- current/filings_index.yaml
- raw/sec/{accession}/...
- raw/news/news.jsonl
- current/news_digest.yaml
- raw/papers/papers.jsonl
- current/papers_digest.yaml
- runs/{run_id}/meta.yaml
- runs/{run_id}/result.yaml
- runs/{run_id}/needs.yaml (only when blocked)

## MCP tools
- sec_edgar_mcp.get_recent_filings
- sec_edgar_mcp.get_filing_content
- sec_edgar_mcp.get_filing_sections
- gdelt.search_articles
- openalex.search_works (plus arxiv/pubmed when relevant)
- fs

## Workflow
1. Load company.yaml, extract cik and company_name.
2. SEC filings:
   - Query forms: 10-K, 10-Q, 8-K, DEF14A (optionally 20-F, 6-K).
   - Merge with existing filings_index; only new accessions are downloaded.
   - Store per-accession raw data under raw/sec/{accession}/ (Phase 1 can store sections or content only).
   - Write current/filings_index.yaml with as_of, form, filed_at, period_end, accession, has_xbrl, local_dir.
3. News:
   - Query gdelt with ticker + company_name (see references/query_templates.md).
   - Deduplicate by url (fallback to title+published_at).
   - Append to raw/news/news.jsonl and write current/news_digest.yaml.
4. Papers:
   - papers_mode=auto only fetches for tech/biotech/materials-style firms; on/off overrides.
   - Save raw/papers/papers.jsonl and write current/papers_digest.yaml (may be empty but must exist).
5. Update artifacts_state.yaml and append evidence records.

## Incremental rules
- SEC filings: do not re-download accessions already in filings_index.
- News: dedupe by url; when no new payload and not force_refresh, keep existing digest.
- Papers: if auto + not relevant industry, write digest with skipped reason; otherwise dedupe by doi/id/title.

## Blocked conditions
- company.yaml missing or cik empty -> blocked, needs.yaml points to company-foundation.
- SEC filings list unavailable and no existing filings_index -> blocked.

## Script
- scripts/run.py implements the workflow and accepts optional JSON inputs for tool results.
- references/query_templates.md provides optional query templates for news/papers.
