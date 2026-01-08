#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collect-company-facts skill runner."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

SKILL_NAME = "collect-company-facts"
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_FORMS = ["10-K", "10-Q", "8-K", "DEF14A", "20-F", "6-K"]


def _find_repo_root(start: Path) -> Path:
    for parent in [start] + list(start.parents):
        if (parent / "company_research_runtime").exists():
            return parent
    return start.parents[4]


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from company_research_runtime import (  # noqa: E402
    append_evidence,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    atomic_write_yaml,
    build_needs,
    build_run_meta,
    build_run_result,
    company_paths,
    default_run_id,
    update_artifacts_state,
    write_meta,
    write_needs,
    write_result,
)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _load_payload(path: Path | None, inline_json: str | None) -> Any | None:
    if inline_json:
        return json.loads(inline_json)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _parse_as_of(value: str | None) -> date | str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return value


def _as_of_str(value: date | str | None) -> str:
    if value is None:
        return date.today().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    candidate = str(value)
    if len(candidate) >= 10 and candidate[4] == "-":
        try:
            return date.fromisoformat(candidate[:10])
        except ValueError:
            return None
    if len(candidate) == 8 and candidate.isdigit():
        try:
            return date(int(candidate[:4]), int(candidate[4:6]), int(candidate[6:]))
        except ValueError:
            return None
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_accession(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("accession") or value.get("accession_number")
    text = str(value).strip()
    return text or None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    if isinstance(value, int):
        return value != 0
    return False


def _normalize_filing(filing: Mapping[str, Any]) -> dict[str, Any]:
    accession = _normalize_accession(
        filing.get("accession")
        or filing.get("accession_number")
        or filing.get("accessionNumber")
        or filing.get("accessionNo")
    )
    form = (
        filing.get("form")
        or filing.get("form_type")
        or filing.get("formType")
        or filing.get("filing_type")
        or filing.get("type")
    )
    form = form.upper() if isinstance(form, str) else form
    filed_at = (
        filing.get("filed_at")
        or filing.get("filedAt")
        or filing.get("filing_date")
        or filing.get("filingDate")
        or filing.get("date")
    )
    period_end = (
        filing.get("period_end")
        or filing.get("period_of_report")
        or filing.get("periodOfReport")
        or filing.get("report_date")
        or filing.get("reportDate")
    )
    has_xbrl = _coerce_bool(
        filing.get("has_xbrl")
        or filing.get("hasXbrl")
        or filing.get("is_xbrl")
        or filing.get("xbrl")
    )
    return {
        "form": form,
        "filed_at": filed_at,
        "period_end": period_end,
        "accession": accession,
        "has_xbrl": has_xbrl,
    }


def _extract_filings(payload: Any | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("filings", "results", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if any(key in payload for key in ("accession", "accession_number", "form")):
            return [payload]
        collected: list[dict[str, Any]] = []
        for value in payload.values():
            if isinstance(value, list):
                collected.extend([item for item in value if isinstance(item, dict)])
        if collected:
            return collected
    return []


def _merge_filings(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for filing in existing + incoming:
        accession = filing.get("accession")
        if not accession:
            continue
        merged[accession] = filing
    return list(merged.values())


def _filter_filings_by_lookback(
    filings: list[dict[str, Any]],
    *,
    as_of: date,
    lookback_years: int,
) -> list[dict[str, Any]]:
    cutoff = as_of - timedelta(days=365 * lookback_years)
    filtered: list[dict[str, Any]] = []
    for filing in filings:
        filed_date = _parse_date(str(filing.get("filed_at") or ""))
        period_date = _parse_date(str(filing.get("period_end") or ""))
        compare_date = filed_date or period_date
        if compare_date is None or compare_date >= cutoff:
            filtered.append(filing)
    return filtered


def _filing_sort_key(filing: dict[str, Any]) -> str:
    return str(filing.get("filed_at") or filing.get("period_end") or "")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _dedupe_records(records: Iterable[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        key = key_fn(record)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def _article_published_at(article: dict[str, Any]) -> str | None:
    return (
        article.get("published_at")
        or article.get("seendate")
        or article.get("date")
        or article.get("publishedAt")
    )


def _article_key(article: dict[str, Any]) -> str | None:
    url = article.get("url") or article.get("source_url")
    if url:
        return str(url)
    title = article.get("title") or ""
    published_at = _article_published_at(article) or ""
    key = f"{title}|{published_at}".strip("|")
    return key or None


def _normalize_article(article: dict[str, Any], fetched_at: str, query: str | None) -> dict[str, Any]:
    normalized = dict(article)
    if "published_at" not in normalized:
        published_at = _article_published_at(article)
        if published_at:
            normalized["published_at"] = published_at
    normalized["fetched_at"] = fetched_at
    if query:
        normalized["query"] = query
    return normalized


def _generate_news_digest(articles: list[dict[str, Any]], as_of_label: str) -> dict[str, Any]:
    if not articles:
        return {"as_of": as_of_label, "total_articles": 0, "top_themes": [], "key_events": []}

    themes = Counter()
    for article in articles:
        title = (article.get("title") or "").lower()
        if any(word in title for word in ("earning", "revenue", "profit", "quarter", "guidance")):
            themes["earnings"] += 1
        if any(word in title for word in ("acquisition", "merger", "deal", "buyout")):
            themes["m_and_a"] += 1
        if any(word in title for word in ("lawsuit", "sec", "investigation", "fraud")):
            themes["legal"] += 1
        if any(word in title for word in ("ceo", "cfo", "chief", "executive", "appoint")):
            themes["management"] += 1
        if any(word in title for word in ("product", "launch", "release", "upgrade")):
            themes["product"] += 1
        if any(word in title for word in ("regulation", "antitrust", "doj", "ftc")):
            themes["regulatory"] += 1

    published_dates = [
        article.get("published_at") or article.get("seendate")
        for article in articles
        if article.get("published_at") or article.get("seendate")
    ]

    key_events = [
        {
            "published_at": article.get("published_at") or article.get("seendate"),
            "title": article.get("title"),
            "url": article.get("url") or article.get("source_url"),
            "source": article.get("source") or article.get("domain"),
        }
        for article in sorted(articles, key=lambda item: str(_article_published_at(item) or ""), reverse=True)[:10]
    ]

    digest = {
        "as_of": as_of_label,
        "total_articles": len(articles),
        "date_range": [min(published_dates), max(published_dates)] if published_dates else [],
        "top_themes": [
            {"theme": theme, "count": count} for theme, count in themes.most_common(6)
        ],
        "key_events": key_events,
    }
    return digest


def _paper_key(paper: dict[str, Any]) -> str | None:
    doi = paper.get("doi") or paper.get("DOI")
    if doi:
        return str(doi)
    paper_id = paper.get("id") or paper.get("openalex_id")
    if paper_id:
        return str(paper_id)
    title = paper.get("title") or paper.get("display_name") or ""
    year = paper.get("publication_year") or paper.get("year") or ""
    key = f"{title}|{year}".strip("|")
    return key or None


def _normalize_paper(paper: dict[str, Any], fetched_at: str, query: str | None) -> dict[str, Any]:
    normalized = dict(paper)
    normalized["fetched_at"] = fetched_at
    if query:
        normalized["query"] = query
    return normalized


def _paper_score(paper: dict[str, Any]) -> float:
    score = paper.get("relevance_score")
    if isinstance(score, (int, float)):
        return float(score)
    cited = paper.get("cited_by_count") or paper.get("citedByCount")
    if isinstance(cited, (int, float)):
        return float(cited)
    year = paper.get("publication_year") or paper.get("year")
    if isinstance(year, (int, float)):
        return float(year)
    return 0.0


def _generate_papers_digest(papers: list[dict[str, Any]], as_of_label: str) -> dict[str, Any]:
    if not papers:
        return {"as_of": as_of_label, "total_papers": 0, "relevant_papers": []}

    def extract_title(paper: dict[str, Any]) -> str | None:
        return paper.get("title") or paper.get("display_name") or paper.get("name")

    def extract_url(paper: dict[str, Any]) -> str | None:
        return (
            paper.get("url")
            or paper.get("landing_page_url")
            or paper.get("id")
            or paper.get("openalex_id")
            or paper.get("doi")
        )

    def extract_venue(paper: dict[str, Any]) -> str | None:
        if isinstance(paper.get("host_venue"), dict):
            return paper.get("host_venue", {}).get("display_name")
        return paper.get("journal") or paper.get("venue")

    ranked = sorted(papers, key=_paper_score, reverse=True)
    selected = ranked[:20]
    digest_items: list[dict[str, Any]] = []
    for paper in selected:
        digest_items.append(
            {
                "title": extract_title(paper),
                "year": paper.get("publication_year") or paper.get("year"),
                "venue": extract_venue(paper),
                "url": extract_url(paper),
                "doi": paper.get("doi") or paper.get("DOI"),
                "cited_by_count": paper.get("cited_by_count") or paper.get("citedByCount"),
                "summary": paper.get("summary") or paper.get("abstract"),
                "score": _paper_score(paper),
            }
        )

    return {
        "as_of": as_of_label,
        "total_papers": len(papers),
        "relevant_papers": digest_items,
    }


def _is_relevant_for_papers(company: dict[str, Any]) -> bool:
    sic = str(company.get("sic") or "")
    if sic and sic[:2] in {"28", "35", "36", "38", "73"}:
        return True
    name = (company.get("company_name") or "").lower()
    keywords = [
        "biotech",
        "pharma",
        "pharmaceutical",
        "semiconductor",
        "software",
        "technology",
        "medical",
        "diagnostic",
        "materials",
    ]
    return any(keyword in name for keyword in keywords)


def _generate_evidence_id(prefix: str = "E") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"


def _append_evidence_record(
    *,
    evidence_path: Path,
    ticker: str,
    claim: str,
    sources: list[dict[str, Any]],
    confidence: float = 0.85,
) -> None:
    record = {
        "id": _generate_evidence_id("E"),
        "created_at": datetime.now(timezone.utc).date().isoformat(),
        "ticker": ticker,
        "skill": SKILL_NAME,
        "claim": claim,
        "confidence": confidence,
        "sources": sources,
    }
    append_evidence(evidence_path, record)


def _persist_inputs(
    run_dir: Path,
    *,
    filings_payloads: list[Any],
    filing_content_payload: Any | None,
    news_payload: Any | None,
    papers_payload: Any | None,
) -> list[str]:
    inputs_dir = run_dir / "inputs"
    persisted: list[str] = []
    if filings_payloads:
        atomic_write_json(
            inputs_dir / "filings_payloads.json",
            filings_payloads,
            ensure_ascii=False,
            default=str,
        )
        persisted.append("inputs/filings_payloads.json")
    if filing_content_payload is not None:
        atomic_write_json(
            inputs_dir / "filing_content_payload.json",
            filing_content_payload,
            ensure_ascii=False,
            default=str,
        )
        persisted.append("inputs/filing_content_payload.json")
    if news_payload is not None:
        atomic_write_json(
            inputs_dir / "news_payload.json",
            news_payload,
            ensure_ascii=False,
            default=str,
        )
        persisted.append("inputs/news_payload.json")
    if papers_payload is not None:
        atomic_write_json(
            inputs_dir / "papers_payload.json",
            papers_payload,
            ensure_ascii=False,
            default=str,
        )
        persisted.append("inputs/papers_payload.json")
    return persisted


def _resolve_filing_payload_map(payload: Any | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _resolve_filing_content(payload_map: dict[str, Any], accession: str) -> Any | None:
    if accession in payload_map:
        return payload_map[accession]
    alt = accession.replace("-", "")
    return payload_map.get(alt)


def _write_filing_raw(
    *,
    raw_sec_dir: Path,
    filing: dict[str, Any],
    content_payload: Any | None,
) -> None:
    accession = filing.get("accession")
    if not accession:
        return
    target_dir = raw_sec_dir / accession
    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_yaml(target_dir / "meta.yaml", filing)

    if isinstance(content_payload, dict):
        if "sections" in content_payload:
            atomic_write_json(target_dir / "sections.json", content_payload["sections"])
        if "content" in content_payload:
            atomic_write_text(target_dir / "content.txt", str(content_payload["content"]))
        if "html" in content_payload:
            atomic_write_text(target_dir / "content.html", str(content_payload["html"]))
        return

    if isinstance(content_payload, str):
        atomic_write_text(target_dir / "content.txt", content_payload)


def run(
    ticker: str,
    *,
    as_of: date | str | None = None,
    lookback_years: int = 10,
    lookback_days_news: int = 180,
    papers_mode: str = "auto",
    force_refresh: bool = False,
    filings_payloads: list[Any] | None = None,
    filing_content_payload: Any | None = None,
    news_payload: Any | None = None,
    papers_payload: Any | None = None,
    news_query: str | None = None,
    papers_query: str | None = None,
    demo: bool = False,
    timezone_name: str = DEFAULT_TIMEZONE,
    persist_inputs: bool = False,
) -> dict[str, Any]:
    ticker = ticker.upper()
    as_of_value = as_of or date.today()
    as_of_label = _as_of_str(as_of_value)

    paths = company_paths(ticker)
    paths.ensure_base_dirs()

    run_id = default_run_id(timezone=timezone_name)
    run_dir = paths.run_dir(run_id)
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)

    filings_payloads = filings_payloads or []
    persisted_inputs: list[str] = []
    if persist_inputs and not demo:
        persisted_inputs = _persist_inputs(
            run_dir,
            filings_payloads=filings_payloads,
            filing_content_payload=filing_content_payload,
            news_payload=news_payload,
            papers_payload=papers_payload,
        )

    meta = build_run_meta(
        skill=SKILL_NAME,
        ticker=ticker,
        run_id=run_id,
        as_of=as_of_value,
        timezone=timezone_name,
        lookback_years=lookback_years,
        lookback_days_news=lookback_days_news,
        papers_mode=papers_mode,
        force_refresh=force_refresh,
        forms=DEFAULT_FORMS,
        inputs_persisted=persisted_inputs,
    )
    write_meta(run_dir, meta)

    warnings: list[str] = []
    missing: list[str] = []

    company = _load_yaml(paths.company_yaml)
    if not company.get("cik"):
        missing.append("company.yaml.cik")
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "company.yaml",
                    "producer_skill": "company-foundation",
                    "reason": "Missing CIK needed to query SEC filings",
                }
            ],
            suggested_plan=["company-foundation", SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)
        result = build_run_result(
            skill=SKILL_NAME,
            ticker=ticker,
            run_id=run_id,
            status="blocked",
            as_of=as_of_value,
            timezone=timezone_name,
            missing=missing,
            warnings=warnings,
            outputs=[],
        )
        write_result(run_dir, result)
        return result

    cik = str(company.get("cik"))
    company_name = company.get("company_name") or ticker

    filing_content_map = _resolve_filing_payload_map(filing_content_payload)

    existing_index = _load_yaml(paths.current_dir / "filings_index.yaml")
    existing_filings = existing_index.get("filings") if isinstance(existing_index, dict) else []
    existing_filings = existing_filings if isinstance(existing_filings, list) else []
    existing_accessions = {
        filing.get("accession") for filing in existing_filings if isinstance(filing, dict) and filing.get("accession")
    }

    raw_filings: list[dict[str, Any]] = []
    for payload in filings_payloads:
        raw_filings.extend(_extract_filings(payload))

    if demo and not raw_filings:
        raw_filings = [
            {
                "form": "10-K",
                "filed_at": as_of_label,
                "period_end": as_of_label,
                "accession": "0000000000-00-000000",
                "has_xbrl": True,
            }
        ]

    normalized_filings = [_normalize_filing(filing) for filing in raw_filings]
    normalized_filings = [filing for filing in normalized_filings if filing.get("accession")]

    filings_skipped = False
    new_accessions: set[str] = set()
    if not normalized_filings and existing_filings and not force_refresh:
        filings_index = existing_index
        filings_skipped = True
    else:
        merged_filings = _merge_filings(existing_filings, normalized_filings)
        merged_filings = _filter_filings_by_lookback(
            merged_filings,
            as_of=as_of_value if isinstance(as_of_value, date) else date.today(),
            lookback_years=lookback_years,
        )
        filings_index = {"as_of": as_of_label, "filings": merged_filings}

    if not isinstance(filings_index, dict):
        filings_index = {"as_of": as_of_label, "filings": []}

    filings_list = filings_index.get("filings") if isinstance(filings_index, dict) else []
    filings_list = filings_list if isinstance(filings_list, list) else []
    for filing in filings_list:
        if not isinstance(filing, dict):
            continue
        accession = filing.get("accession")
        if accession and accession not in existing_accessions:
            new_accessions.add(accession)

    raw_sec_dir = paths.raw_dir / "sec"
    raw_sec_dir.mkdir(parents=True, exist_ok=True)
    if not filings_skipped:
        for filing in normalized_filings:
            accession = filing.get("accession")
            if not accession:
                continue
            if accession in existing_accessions and not force_refresh:
                continue
            content_payload = _resolve_filing_content(filing_content_map, accession)
            _write_filing_raw(raw_sec_dir=raw_sec_dir, filing=filing, content_payload=content_payload)

    if isinstance(filings_index, dict):
        filings_index["filings"] = [
            {
                **filing,
                "local_dir": f"raw/sec/{filing.get('accession')}/" if filing.get("accession") else None,
            }
            for filing in filings_list
            if isinstance(filing, dict)
        ]
        filings_index["filings"].sort(key=_filing_sort_key, reverse=True)

    filings_output_path = outputs_dir / "filings_index.yaml"
    atomic_write_yaml(filings_output_path, filings_index)

    current_filings_path = paths.current_dir / "filings_index.yaml"
    if filings_list and not filings_skipped:
        atomic_write_yaml(current_filings_path, filings_index)
    elif filings_list and not current_filings_path.exists():
        atomic_write_yaml(current_filings_path, filings_index)

    filings_status = "skipped" if filings_skipped else "ok"
    if not filings_list:
        filings_status = "blocked"
        warnings.append("SEC filings list unavailable or empty")
        missing.append("current/filings_index.yaml")

    news_articles_raw: list[dict[str, Any]] = []
    if isinstance(news_payload, list):
        news_articles_raw = [item for item in news_payload if isinstance(item, dict)]
    elif isinstance(news_payload, dict):
        for key in ("articles", "results", "data"):
            value = news_payload.get(key)
            if isinstance(value, list):
                news_articles_raw = [item for item in value if isinstance(item, dict)]
                break

    if demo and not news_articles_raw:
        news_articles_raw = [
            {
                "title": f"{ticker} announces demo earnings",
                "url": "https://example.com/demo",
                "seendate": as_of_label,
                "source": "demo",
            }
        ]

    raw_news_path = paths.raw_dir / "news" / "news.jsonl"
    existing_news = _read_jsonl(raw_news_path)
    fetched_at = _now_iso()

    normalized_news = [
        _normalize_article(article, fetched_at=fetched_at, query=news_query) for article in news_articles_raw
    ]
    merged_news = normalized_news if force_refresh else existing_news + normalized_news
    merged_news = _dedupe_records(merged_news, _article_key)

    news_skipped = False
    if not normalized_news and existing_news and not force_refresh:
        news_skipped = True
        news_digest = _load_yaml(paths.current_dir / "news_digest.yaml")
        if not news_digest:
            news_digest = _generate_news_digest([], as_of_label)
    else:
        atomic_write_jsonl(raw_news_path, merged_news)
        news_digest = _generate_news_digest(merged_news, as_of_label)

    news_output_path = outputs_dir / "news_digest.yaml"
    atomic_write_yaml(news_output_path, news_digest)
    current_news_path = paths.current_dir / "news_digest.yaml"
    if (not news_skipped) or not current_news_path.exists():
        atomic_write_yaml(current_news_path, news_digest)

    news_status = "skipped" if news_skipped else "ok"
    if not merged_news and not news_skipped:
        warnings.append("No news articles captured")

    raw_papers_path = paths.raw_dir / "papers" / "papers.jsonl"
    existing_papers = _read_jsonl(raw_papers_path)

    papers_skipped = False
    papers_digest: dict[str, Any]
    papers_records: list[dict[str, Any]] = []

    if papers_mode == "off":
        papers_skipped = True
        papers_digest = {"as_of": as_of_label, "total_papers": 0, "relevant_papers": [], "skipped": "off"}
        if not raw_papers_path.exists():
            atomic_write_jsonl(raw_papers_path, [])
    else:
        relevant = _is_relevant_for_papers(company)
        if papers_mode == "auto" and not relevant:
            papers_skipped = True
            papers_digest = {
                "as_of": as_of_label,
                "total_papers": 0,
                "relevant_papers": [],
                "skipped": "not_relevant_industry",
            }
            if not raw_papers_path.exists():
                atomic_write_jsonl(raw_papers_path, [])
        else:
            papers_payload_data = papers_payload
            if isinstance(papers_payload_data, dict):
                for key in ("results", "data", "papers"):
                    value = papers_payload_data.get(key)
                    if isinstance(value, list):
                        papers_payload_data = value
                        break
            if isinstance(papers_payload_data, list):
                papers_records = [item for item in papers_payload_data if isinstance(item, dict)]
            if demo and not papers_records:
                papers_records = [
                    {
                        "title": f"{company_name} demo research",
                        "publication_year": date.today().year,
                        "doi": "10.0000/demo",
                    }
                ]

            normalized_papers = [
                _normalize_paper(paper, fetched_at=fetched_at, query=papers_query) for paper in papers_records
            ]
            merged_papers = normalized_papers if force_refresh else existing_papers + normalized_papers
            merged_papers = _dedupe_records(merged_papers, _paper_key)
            atomic_write_jsonl(raw_papers_path, merged_papers)
            papers_digest = _generate_papers_digest(merged_papers, as_of_label)

    papers_output_path = outputs_dir / "papers_digest.yaml"
    atomic_write_yaml(papers_output_path, papers_digest)
    current_papers_path = paths.current_dir / "papers_digest.yaml"
    if (not papers_skipped) or not current_papers_path.exists():
        atomic_write_yaml(current_papers_path, papers_digest)

    papers_status = "skipped" if papers_skipped else "ok"
    if not papers_records and not papers_skipped:
        warnings.append("No papers captured")

    if filings_status == "blocked":
        needs = build_needs(
            blocked_by=[
                {
                    "artifact": "current/filings_index.yaml",
                    "producer_skill": SKILL_NAME,
                    "reason": "SEC filings list unavailable",
                }
            ],
            suggested_plan=[SKILL_NAME],
            priority="high",
        )
        write_needs(run_dir, needs)

    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/filings_index.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=current_filings_path if current_filings_path.exists() else None,
        extra={"status": filings_status, "count": len(filings_list)},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/news_digest.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=current_news_path if current_news_path.exists() else None,
        extra={"status": news_status, "count": len(merged_news) if not news_skipped else 0},
    )
    update_artifacts_state(
        paths.artifacts_state_yaml,
        artifact="current/papers_digest.yaml",
        run_id=run_id,
        skill=SKILL_NAME,
        file_path=current_papers_path if current_papers_path.exists() else None,
        extra={"status": papers_status, "count": len(papers_records)},
    )

    if new_accessions:
        _append_evidence_record(
            evidence_path=paths.evidence_jsonl,
            ticker=ticker,
            claim=f"Collected {len(new_accessions)} new SEC filings for {ticker}",
            sources=[{"type": "sec_edgar_mcp", "tool": "get_recent_filings", "count": len(new_accessions)}],
            confidence=0.9,
        )
    if normalized_news:
        _append_evidence_record(
            evidence_path=paths.evidence_jsonl,
            ticker=ticker,
            claim=f"Captured {len(normalized_news)} news articles for {ticker}",
            sources=[{"type": "gdelt", "tool": "search_articles", "count": len(normalized_news)}],
            confidence=0.8,
        )
    if papers_records:
        _append_evidence_record(
            evidence_path=paths.evidence_jsonl,
            ticker=ticker,
            claim=f"Captured {len(papers_records)} papers for {ticker}",
            sources=[{"type": "openalex", "tool": "search_works", "count": len(papers_records)}],
            confidence=0.75,
        )

    status: str
    if filings_status == "blocked":
        status = "blocked"
    elif filings_status == "skipped" and news_status == "skipped" and papers_status == "skipped":
        status = "skipped"
    elif "No news articles captured" in warnings or "No papers captured" in warnings:
        status = "partial"
    else:
        status = "ok"

    result = build_run_result(
        skill=SKILL_NAME,
        ticker=ticker,
        run_id=run_id,
        status=status,
        as_of=as_of_value,
        timezone=timezone_name,
        missing=missing,
        warnings=warnings,
        outputs=[
            "current/filings_index.yaml",
            "current/news_digest.yaml",
            "current/papers_digest.yaml",
        ],
        filings_skipped=filings_skipped,
        news_skipped=news_skipped,
        papers_skipped=papers_skipped,
        cik=cik,
    )
    write_result(run_dir, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="collect-company-facts runner")
    parser.add_argument("ticker", help="Stock ticker symbol")
    parser.add_argument("--as-of", dest="as_of", help="Snapshot date YYYY-MM-DD")
    parser.add_argument("--lookback-years", type=int, default=10)
    parser.add_argument("--lookback-days-news", type=int, default=180)
    parser.add_argument("--papers-mode", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--filings-json", action="append", help="Inline JSON payload for filings")
    parser.add_argument("--filings-path", action="append", type=Path, help="Path to filings payload")
    parser.add_argument("--filing-content-json", help="Inline JSON map accession->content")
    parser.add_argument("--filing-content-path", type=Path, help="Path to filing content map")
    parser.add_argument("--news-json", help="Inline JSON payload for news articles")
    parser.add_argument("--news-path", type=Path, help="Path to news payload")
    parser.add_argument("--news-query", help="Query string used for news search")
    parser.add_argument("--papers-json", help="Inline JSON payload for papers")
    parser.add_argument("--papers-path", type=Path, help="Path to papers payload")
    parser.add_argument("--papers-query", help="Query string used for papers search")
    parser.add_argument("--demo", action="store_true", help="Use demo data instead of MCP results")
    parser.add_argument(
        "--persist-inputs",
        action="store_true",
        help="Persist input payloads under runs/{run_id}/inputs",
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    args = parser.parse_args()

    filings_payloads: list[Any] = []
    if args.filings_json:
        for payload in args.filings_json:
            filings_payloads.append(json.loads(payload))
    if args.filings_path:
        for payload_path in args.filings_path:
            payload = _load_payload(payload_path, None)
            if payload is not None:
                filings_payloads.append(payload)

    filing_content_payload = _load_payload(args.filing_content_path, args.filing_content_json)
    news_payload = _load_payload(args.news_path, args.news_json)
    papers_payload = _load_payload(args.papers_path, args.papers_json)
    as_of_value = _parse_as_of(args.as_of)

    result = run(
        args.ticker,
        as_of=as_of_value,
        lookback_years=args.lookback_years,
        lookback_days_news=args.lookback_days_news,
        papers_mode=args.papers_mode,
        force_refresh=args.force_refresh,
        filings_payloads=filings_payloads,
        filing_content_payload=filing_content_payload,
        news_payload=news_payload,
        papers_payload=papers_payload,
        news_query=args.news_query,
        papers_query=args.papers_query,
        demo=args.demo,
        timezone_name=args.timezone,
        persist_inputs=args.persist_inputs,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
