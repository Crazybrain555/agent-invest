# Phase 1 核心估值链 Codex Skills 实施指南（可直接复制版）

> **基于 Codex Skills 官方规范 + sec_edgar_mcp 实际工具 + 分阶段实现策略**

---

## 〇、前置准备

### 0.1 目录结构

```bash
# Skills 放到项目级（git 管理）
/mnt/d/python_project/my-quant-project/
├── .codex/skills/company_research/
│   ├── company-foundation/
│   ├── collect-company-facts/
│   ├── extract-xbrl-timeseries/
│   ├── recast-economic-statements/
│   └── valuation-and-margin-of-safety/
│
├── company_research_runtime/          # 共享库
│   ├── __init__.py
│   ├── paths.py
│   ├── atomic_io.py
│   ├── runlog.py
│   ├── artifacts_state.py
│   ├── evidence.py
│   └── hashing.py
│
└── scripts/
    └── smoke_test_phase1.py

# 运行产物目录
/home/help/mcp/work/company_research/
├── registry.jsonl
├── value_summary.csv
└── company/{TICKER}/
    ├── company.yaml
    ├── latest.json
    ├── current/
    ├── raw/
    └── runs/
```

### 0.2 创建目录

```bash
# 创建 Skills 目录
mkdir -p /mnt/d/python_project/my-quant-project/.codex/skills/company_research/{company-foundation,collect-company-facts,extract-xbrl-timeseries,recast-economic-statements,valuation-and-margin-of-safety}/{scripts,references}

# 创建共享库目录
mkdir -p /mnt/d/python_project/my-quant-project/company_research_runtime

# 创建工作目录
mkdir -p /home/help/mcp/work/company_research/company
```

---

## 一、共享 Runtime（强烈建议先做）

### 1.1 company_research_runtime/__init__.py

```python
"""Company Research Runtime - shared utilities for all skills."""
from .paths import *
from .atomic_io import *
from .runlog import *
from .artifacts_state import *
from .evidence import *
```

### 1.2 company_research_runtime/paths.py

```python
"""Path utilities for company research."""
from pathlib import Path
from datetime import datetime
import pytz

BASE_PATH = Path("/home/help/mcp/work/company_research")
TZ = pytz.timezone("America/New_York")

def get_company_dir(ticker: str) -> Path:
    return BASE_PATH / "company" / ticker.upper()

def get_current_dir(ticker: str) -> Path:
    return get_company_dir(ticker) / "current"

def get_raw_dir(ticker: str) -> Path:
    return get_company_dir(ticker) / "raw"

def get_runs_dir(ticker: str) -> Path:
    return get_company_dir(ticker) / "runs"

def generate_run_id() -> str:
    return datetime.now(TZ).strftime("%Y%m%d_%H%M%S")

def get_run_dir(ticker: str, run_id: str) -> Path:
    return get_runs_dir(ticker) / run_id

def ensure_dirs(ticker: str):
    """Create all required directories for a ticker."""
    dirs = [
        get_current_dir(ticker) / "xbrl_atlas",
        get_current_dir(ticker) / "economic",
        get_current_dir(ticker) / "diagnostics",
        get_current_dir(ticker) / "valuation",
        get_raw_dir(ticker) / "sec",
        get_raw_dir(ticker) / "news",
        get_raw_dir(ticker) / "papers",
        get_runs_dir(ticker),
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
```

### 1.3 company_research_runtime/atomic_io.py

```python
"""Atomic file operations."""
import yaml
import json
import tempfile
import shutil
from pathlib import Path
import pandas as pd

def atomic_write_yaml(path: Path, data: dict):
    """Write YAML atomically (write to temp, then move)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', 
                                      dir=path.parent, delete=False) as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        temp_path = f.name
    
    shutil.move(temp_path, path)

def atomic_write_jsonl(path: Path, records: list):
    """Write JSONL atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl',
                                      dir=path.parent, delete=False) as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
        temp_path = f.name
    
    shutil.move(temp_path, path)

def append_jsonl(path: Path, record: dict):
    """Append single record to JSONL."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')

def atomic_write_parquet(path: Path, df: pd.DataFrame):
    """Write Parquet atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    temp_path = path.with_suffix('.parquet.tmp')
    df.to_parquet(temp_path, index=False)
    shutil.move(temp_path, path)

def load_yaml(path: Path) -> dict:
    """Load YAML file, return empty dict if not exists."""
    path = Path(path)
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}

def load_parquet(path: Path) -> pd.DataFrame:
    """Load Parquet file, return empty DataFrame if not exists."""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)
```

### 1.4 company_research_runtime/runlog.py

```python
"""Run logging utilities."""
from datetime import datetime
from pathlib import Path
from .atomic_io import atomic_write_yaml
from .paths import TZ

def write_meta(run_dir: Path, ticker: str, skill: str, inputs: dict):
    """Write meta.yaml for a run."""
    meta = {
        "skill": skill,
        "ticker": ticker.upper(),
        "run_id": run_dir.name,
        "started_at": datetime.now(TZ).isoformat(),
        "inputs": inputs,
    }
    atomic_write_yaml(run_dir / "meta.yaml", meta)

def write_result(run_dir: Path, ticker: str, skill: str, status: str,
                 outputs: list = None, warnings: list = None, 
                 missing: list = None, as_of: str = None):
    """Write result.yaml for a run."""
    result = {
        "skill": skill,
        "ticker": ticker.upper(),
        "run_id": run_dir.name,
        "as_of": as_of or str(datetime.now(TZ).date()),
        "timezone": "America/New_York",
        "status": status,  # ok | partial | blocked | skipped | error
        "outputs": outputs or [],
        "warnings": warnings or [],
        "missing": missing or [],
        "completed_at": datetime.now(TZ).isoformat(),
    }
    atomic_write_yaml(run_dir / "result.yaml", result)
    return result

def write_needs(run_dir: Path, blocked_by: list, suggested_plan: list, priority: str = "high"):
    """Write needs.yaml when blocked."""
    needs = {
        "blocked_by": blocked_by,
        "suggested_plan": suggested_plan,
        "priority": priority,
    }
    atomic_write_yaml(run_dir / "needs.yaml", needs)
```

### 1.5 company_research_runtime/artifacts_state.py

```python
"""Artifacts state management."""
from datetime import datetime
from pathlib import Path
from .atomic_io import atomic_write_yaml, load_yaml
from .paths import get_current_dir, TZ

def update_artifacts_state(ticker: str, artifact_name: str, status: str, 
                           run_id: str = None, extra: dict = None):
    """Update artifacts_state.yaml with new artifact status."""
    state_path = get_current_dir(ticker) / "artifacts_state.yaml"
    state = load_yaml(state_path)
    
    if "artifacts" not in state:
        state["artifacts"] = {}
    
    state["artifacts"][artifact_name] = {
        "status": status,
        "updated_at": datetime.now(TZ).isoformat(),
        "run_id": run_id,
        **(extra or {})
    }
    
    atomic_write_yaml(state_path, state)

def get_artifact_status(ticker: str, artifact_name: str) -> dict:
    """Get status of a specific artifact."""
    state_path = get_current_dir(ticker) / "artifacts_state.yaml"
    state = load_yaml(state_path)
    return state.get("artifacts", {}).get(artifact_name, {})

def check_artifact_exists(ticker: str, artifact_name: str) -> bool:
    """Check if artifact exists and has ok/partial status."""
    status = get_artifact_status(ticker, artifact_name)
    return status.get("status") in ["ok", "partial"]
```

### 1.6 company_research_runtime/evidence.py

```python
"""Evidence and questions ledger."""
from datetime import datetime
from pathlib import Path
from .atomic_io import append_jsonl
from .paths import get_current_dir, TZ

def generate_evidence_id(prefix: str = "E") -> str:
    return f"{prefix}_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S_%f')}"

def append_evidence(ticker: str, skill: str, claim: str, confidence: float,
                    sources: list = None, notes: str = None):
    """Append evidence to evidence.jsonl."""
    evidence_path = get_current_dir(ticker) / "evidence.jsonl"
    record = {
        "id": generate_evidence_id("E"),
        "created_at": datetime.now(TZ).isoformat(),
        "skill": skill,
        "claim": claim,
        "confidence": confidence,
        "sources": sources or [],
        "notes": notes,
    }
    append_jsonl(evidence_path, record)

def append_question(ticker: str, skill: str, question: str, priority: str = "medium",
                    related_artifacts: list = None, notes: str = None):
    """Append question to questions.jsonl."""
    questions_path = get_current_dir(ticker) / "questions.jsonl"
    record = {
        "id": generate_evidence_id("Q"),
        "created_at": datetime.now(TZ).isoformat(),
        "skill": skill,
        "priority": priority,
        "question": question,
        "status": "open",
        "related_artifacts": related_artifacts or [],
        "notes": notes,
    }
    append_jsonl(questions_path, record)
```

### 1.7 company_research_runtime/hashing.py

```python
"""Hashing utilities for skip detection."""
import hashlib
import json
from pathlib import Path

def file_hash(path: Path) -> str:
    """Get SHA256 hash of file contents."""
    path = Path(path)
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

def inputs_fingerprint(inputs: dict) -> str:
    """Get fingerprint of inputs dict."""
    serialized = json.dumps(inputs, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]

def should_skip(ticker: str, artifact_name: str, current_fingerprint: str,
                state_path: Path = None) -> bool:
    """Check if artifact should be skipped based on fingerprint."""
    from .artifacts_state import get_artifact_status
    status = get_artifact_status(ticker, artifact_name)
    
    if status.get("status") not in ["ok", "partial"]:
        return False
    
    return status.get("fingerprint") == current_fingerprint
```

---

## 二、Skill 1: company-foundation

### 2.1 SKILL.md

```markdown
---
name: company-foundation
description: "Initialize ticker research folder with company.yaml and market_snapshot.yaml. Use when starting coverage or refreshing shares price EV for any ticker."
version: v0.1
---

# company-foundation

## What This Skill Does
1. Create folder tree under /home/help/mcp/work/company_research/company/{TICKER}/
2. Resolve identity via SEC EDGAR (ticker to CIK, company name, exchange, FY end)
3. Fetch market snapshot via trading_mcp (price, shares, market cap, EV)
4. Write to runs/{run_id}/ then atomically promote to current/

## MCP Tools
- sec_edgar_mcp.get_cik_by_ticker - resolve CIK from ticker
- sec_edgar_mcp.get_company_info - get company details
- trading_mcp.get_fundamental_stock_metrics - get price shares market_cap enterprise_value
- fs - write files

## Inputs
- ticker (required) - stock ticker symbol
- as_of (optional) - date for snapshot, defaults to today
- force_refresh (optional) - ignore existing data and refresh

## Hard Dependencies
None - this is the chain start

## Outputs
- company/{TICKER}/company.yaml
- company/{TICKER}/current/market_snapshot.yaml
- company/{TICKER}/current/artifacts_state.yaml
- company/{TICKER}/runs/{run_id}/result.yaml

## Skip Conditions
- company.yaml exists with valid cik AND NOT force_refresh -> skip identity
- market_snapshot.yaml as_of equals today AND all fields present -> skip market

## Workflow

### Step 1 - Initialize directories
```python
from company_research_runtime import paths
paths.ensure_dirs(ticker)
run_id = paths.generate_run_id()
run_dir = paths.get_run_dir(ticker, run_id)
run_dir.mkdir(parents=True)
```

### Step 2 - Check skip conditions
```python
from company_research_runtime import atomic_io, artifacts_state
company_path = paths.get_company_dir(ticker) / "company.yaml"
existing = atomic_io.load_yaml(company_path)

if existing.get("cik") and not force_refresh:
    # Skip identity resolution
    identity_skipped = True
```

### Step 3 - Resolve identity via sec_edgar_mcp
```python
# Call MCP tool
cik_result = sec_edgar_mcp.get_cik_by_ticker(ticker=ticker)
company_info = sec_edgar_mcp.get_company_info(identifier=ticker)

company_data = {
    "ticker": ticker.upper(),
    "company_name": company_info.get("name"),
    "cik": cik_result.get("cik"),
    "exchange": company_info.get("exchange"),
    "sic": company_info.get("sic"),
    "fiscal_year_end": company_info.get("fiscal_year_end", "12-31"),
    "currency": "USD",
}
```

### Step 4 - Fetch market snapshot via trading_mcp
```python
metrics = trading_mcp.get_fundamental_stock_metrics(ticker=ticker)

market_snapshot = {
    "as_of": str(as_of),
    "price": metrics.get("price"),
    "shares_outstanding": metrics.get("sharesOutstanding"),
    "shares_float": metrics.get("sharesFloat"),  # may be null
    "market_cap": metrics.get("marketCap"),
    "enterprise_value": metrics.get("enterpriseValue"),
    "net_debt": None,  # calculated if EV and market_cap present
    "source": "trading_mcp.get_fundamental_stock_metrics",
}

# Calculate net_debt if possible
if market_snapshot["enterprise_value"] and market_snapshot["market_cap"]:
    market_snapshot["net_debt"] = market_snapshot["enterprise_value"] - market_snapshot["market_cap"]
```

### Step 5 - Write outputs
```python
from company_research_runtime import runlog, evidence

# Write to run dir first
atomic_io.atomic_write_yaml(run_dir / "outputs" / "company.yaml", company_data)
atomic_io.atomic_write_yaml(run_dir / "outputs" / "market_snapshot.yaml", market_snapshot)

# Determine status
if not company_data.get("cik"):
    status = "blocked"
elif not market_snapshot.get("price"):
    status = "partial"
else:
    status = "ok"

# Write result
runlog.write_result(run_dir, ticker, "company-foundation", status,
    outputs=["company.yaml", "current/market_snapshot.yaml"])

# Promote to current if ok or partial
if status in ["ok", "partial"]:
    shutil.copy(run_dir / "outputs" / "company.yaml", paths.get_company_dir(ticker) / "company.yaml")
    shutil.copy(run_dir / "outputs" / "market_snapshot.yaml", paths.get_current_dir(ticker) / "market_snapshot.yaml")

# Update artifacts state
artifacts_state.update_artifacts_state(ticker, "company.yaml", status, run_id)
artifacts_state.update_artifacts_state(ticker, "market_snapshot.yaml", status, run_id)

# Write evidence
evidence.append_evidence(ticker, "company-foundation", 
    f"Identity resolved via SEC EDGAR CIK={company_data.get('cik')}", 
    confidence=0.95, sources=[{"type": "sec_edgar_mcp", "tool": "get_cik_by_ticker"}])
```

## Blocked Conditions
- sec_edgar_mcp returns no CIK AND fallback fails -> status=blocked
- trading_mcp completely unavailable -> status=partial (can still have identity)

## Definition of Done
After running on any ticker (e.g., AAPL):
- company/{TICKER}/company.yaml exists with cik field populated
- company/{TICKER}/current/market_snapshot.yaml exists with price and shares_outstanding
- company/{TICKER}/runs/{run_id}/result.yaml shows status ok or partial
```

### 2.2 scripts/run.py

```python
#!/usr/bin/env python3
"""
company-foundation skill runner.
Usage: python run.py TICKER [--as-of DATE] [--force-refresh]
"""
import sys
import argparse
import shutil
from datetime import date
from pathlib import Path

# Add runtime to path
sys.path.insert(0, str(Path(__file__).parents[4]))

from company_research_runtime import (
    paths, atomic_io, runlog, artifacts_state, evidence
)

SKILL_NAME = "company-foundation"

def run(ticker: str, as_of: date = None, force_refresh: bool = False):
    ticker = ticker.upper()
    as_of = as_of or date.today()
    
    # Step 1: Initialize
    paths.ensure_dirs(ticker)
    run_id = paths.generate_run_id()
    run_dir = paths.get_run_dir(ticker, run_id)
    run_dir.mkdir(parents=True)
    (run_dir / "outputs").mkdir()
    
    runlog.write_meta(run_dir, ticker, SKILL_NAME, {
        "ticker": ticker,
        "as_of": str(as_of),
        "force_refresh": force_refresh,
    })
    
    warnings = []
    
    # Step 2: Check skip for identity
    company_path = paths.get_company_dir(ticker) / "company.yaml"
    existing_company = atomic_io.load_yaml(company_path)
    identity_skipped = False
    
    if existing_company.get("cik") and not force_refresh:
        identity_skipped = True
        company_data = existing_company
        print(f"Identity skipped - cik={company_data['cik']} exists")
    else:
        # Step 3: Resolve identity
        # NOTE: In actual execution, Codex will call MCP tools
        # This is placeholder showing expected structure
        company_data = {
            "ticker": ticker,
            "company_name": None,  # From sec_edgar_mcp.get_company_info
            "cik": None,           # From sec_edgar_mcp.get_cik_by_ticker
            "exchange": None,
            "sic": None,
            "fiscal_year_end": "12-31",
            "currency": "USD",
        }
        print("TODO: Call sec_edgar_mcp.get_cik_by_ticker and get_company_info")
    
    # Step 4: Check skip for market
    market_path = paths.get_current_dir(ticker) / "market_snapshot.yaml"
    existing_market = atomic_io.load_yaml(market_path)
    market_skipped = False
    
    if existing_market.get("as_of") == str(as_of) and existing_market.get("price") and not force_refresh:
        market_skipped = True
        market_data = existing_market
        print(f"Market skipped - as_of={as_of} exists with price")
    else:
        # NOTE: Codex will call trading_mcp.get_fundamental_stock_metrics
        market_data = {
            "as_of": str(as_of),
            "price": None,
            "shares_outstanding": None,
            "shares_float": None,
            "market_cap": None,
            "enterprise_value": None,
            "net_debt": None,
            "source": "trading_mcp.get_fundamental_stock_metrics",
        }
        print("TODO: Call trading_mcp.get_fundamental_stock_metrics")
    
    # Step 5: Determine status
    if identity_skipped and market_skipped:
        status = "skipped"
    elif not company_data.get("cik"):
        status = "blocked"
        runlog.write_needs(run_dir, 
            blocked_by=[{"artifact": "CIK", "reason": "sec_edgar_mcp returned no CIK"}],
            suggested_plan=["retry with different identifier", "manual CIK lookup"])
    elif not market_data.get("price"):
        status = "partial"
        warnings.append("Market data incomplete - price missing")
    else:
        status = "ok"
    
    # Step 6: Write outputs
    atomic_io.atomic_write_yaml(run_dir / "outputs" / "company.yaml", company_data)
    atomic_io.atomic_write_yaml(run_dir / "outputs" / "market_snapshot.yaml", market_data)
    
    # Step 7: Promote to current
    if status in ["ok", "partial"]:
        if not identity_skipped:
            shutil.copy(run_dir / "outputs" / "company.yaml", company_path)
        if not market_skipped:
            shutil.copy(run_dir / "outputs" / "market_snapshot.yaml", market_path)
    
    # Step 8: Update state and evidence
    artifacts_state.update_artifacts_state(ticker, "company.yaml", status, run_id)
    artifacts_state.update_artifacts_state(ticker, "market_snapshot.yaml", status, run_id)
    
    if company_data.get("cik"):
        evidence.append_evidence(ticker, SKILL_NAME,
            f"Identity resolved CIK={company_data['cik']}", 0.95,
            sources=[{"type": "sec_edgar_mcp"}])
    
    # Step 9: Write result
    result = runlog.write_result(run_dir, ticker, SKILL_NAME, status,
        outputs=["company.yaml", "current/market_snapshot.yaml"],
        warnings=warnings,
        as_of=str(as_of))
    
    print(f"\n=== Result: {status} ===")
    print(f"Run: {run_dir}")
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker", help="Stock ticker")
    parser.add_argument("--as-of", type=date.fromisoformat, help="Date for snapshot")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    
    run(args.ticker, args.as_of, args.force_refresh)
```

---

## 三、Skill 2: collect-company-facts

### 3.1 SKILL.md

```markdown
---
name: collect-company-facts
description: "Collect SEC filings news and papers for a ticker. Use when building evidence pool or need filings_index.yaml news_digest.yaml papers_digest.yaml updated."
version: v0.1
---

# collect-company-facts

## What This Skill Does
1. Fetch SEC filings list via sec_edgar_mcp (10-K, 10-Q, 8-K, DEF14A)
2. Download filing content to raw/sec/{accession}/
3. Fetch news via gdelt.search_articles
4. Fetch papers via openalex.search_works (if relevant industry)
5. Generate digests under current/

## MCP Tools
- sec_edgar_mcp.get_recent_filings - list filings by CIK
- sec_edgar_mcp.get_filing_content - download filing text
- sec_edgar_mcp.get_filing_sections - get specific sections
- gdelt.search_articles - news search
- openalex.search_works - paper search
- fs - write files

## Inputs
- ticker (required)
- lookback_years (optional, default 10)
- lookback_days_news (optional, default 180)
- papers_mode (optional, auto|on|off, default auto)
- force_refresh (optional)

## Hard Dependencies
- company/{TICKER}/company.yaml with valid cik

## Outputs
- current/filings_index.yaml
- raw/sec/{accession}/...
- raw/news/news.jsonl
- current/news_digest.yaml
- raw/papers/papers.jsonl
- current/papers_digest.yaml

## Blocked Conditions
- company.yaml missing or cik empty -> blocked, needs company-foundation

## Workflow

### Step 1 - Load company.yaml and check CIK
```python
company_path = paths.get_company_dir(ticker) / "company.yaml"
company = atomic_io.load_yaml(company_path)

if not company.get("cik"):
    # Write needs.yaml pointing to company-foundation
    runlog.write_needs(run_dir,
        blocked_by=[{"artifact": "company.yaml", "producer_skill": "company-foundation", 
                     "reason": "Missing CIK"}],
        suggested_plan=["company-foundation"])
    return {"status": "blocked"}

cik = company["cik"]
company_name = company.get("company_name", ticker)
```

### Step 2 - Fetch SEC filings
```python
# Get existing accessions to avoid re-download
existing_index = atomic_io.load_yaml(paths.get_current_dir(ticker) / "filings_index.yaml")
existing_accessions = {f["accession"] for f in existing_index.get("filings", [])}

# Call sec_edgar_mcp for each form type
forms = ["10-K", "10-Q", "8-K", "DEF14A"]
all_filings = []

for form in forms:
    filings = sec_edgar_mcp.get_recent_filings(
        identifier=cik,
        form_type=form,
        # Note: adjust params based on actual tool schema
    )
    all_filings.extend(filings)

# Filter to new filings only
new_filings = [f for f in all_filings if f["accession"] not in existing_accessions]
```

### Step 3 - Download new filings
```python
for filing in new_filings:
    accession = filing["accession"]
    raw_dir = paths.get_raw_dir(ticker) / "sec" / accession
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    # Get filing content (Phase 1: just get sections, not full XBRL)
    content = sec_edgar_mcp.get_filing_content(
        identifier=cik,
        accession_number=accession
    )
    
    # Save to raw
    with open(raw_dir / "content.txt", "w") as f:
        f.write(content)
```

### Step 4 - Build filings_index.yaml
```python
filings_index = {
    "as_of": str(date.today()),
    "filings": [
        {
            "form": f.get("form"),
            "filed_at": f.get("filed_at"),
            "period_end": f.get("period_of_report"),
            "accession": f.get("accession"),
            "has_xbrl": f.get("has_xbrl", False),
            "local_dir": f"raw/sec/{f.get('accession')}/",
        }
        for f in all_filings
    ]
}
filings_index["filings"].sort(key=lambda x: x.get("filed_at", ""), reverse=True)

atomic_io.atomic_write_yaml(
    paths.get_current_dir(ticker) / "filings_index.yaml",
    filings_index
)
```

### Step 5 - Fetch news via gdelt
```python
query = f'"{ticker}" OR "{company_name}"'

articles = gdelt.search_articles(
    query=query,
    # timespan based on lookback_days_news
)

# Dedupe by URL
seen_urls = set()
unique_articles = []
for a in articles:
    if a.get("url") not in seen_urls:
        seen_urls.add(a.get("url"))
        unique_articles.append(a)

# Save raw
atomic_io.atomic_write_jsonl(
    paths.get_raw_dir(ticker) / "news" / "news.jsonl",
    unique_articles
)

# Generate digest
news_digest = generate_news_digest(unique_articles)
atomic_io.atomic_write_yaml(
    paths.get_current_dir(ticker) / "news_digest.yaml",
    news_digest
)
```

### Step 6 - Fetch papers (if papers_mode allows)
```python
if papers_mode == "off":
    papers_digest = {"as_of": str(date.today()), "total_papers": 0, "relevant_papers": []}
else:
    # Determine if relevant industry
    sic = company.get("sic", "")
    tech_sics = ["3571", "3572", "3674", "3825", "2834", "2836"]  # tech, pharma, etc.
    
    if papers_mode == "auto" and sic[:4] not in tech_sics:
        papers_digest = {"as_of": str(date.today()), "total_papers": 0, "skipped": "not_relevant_industry"}
    else:
        papers = openalex.search_works(query=company_name)
        # Process and save...
        papers_digest = generate_papers_digest(papers)

atomic_io.atomic_write_yaml(
    paths.get_current_dir(ticker) / "papers_digest.yaml",
    papers_digest
)
```

## Incremental Update Rules
- Compare existing accessions in filings_index, only download new
- News: if last fetch within 1 day and not force_refresh, skip or fetch incremental
- Papers: 30-90 day staleness check

## Definition of Done
- current/filings_index.yaml with at least 1 filing
- raw/sec/{accession}/ directories exist for each filing
- current/news_digest.yaml exists (may have 0 articles)
- current/papers_digest.yaml exists (may be empty)
```

### 3.2 scripts/run.py

```python
#!/usr/bin/env python3
"""collect-company-facts skill runner."""
import sys
import argparse
from datetime import date
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parents[4]))

from company_research_runtime import (
    paths, atomic_io, runlog, artifacts_state, evidence
)

SKILL_NAME = "collect-company-facts"

def generate_news_digest(articles: list) -> dict:
    """Generate news digest from articles."""
    if not articles:
        return {"as_of": str(date.today()), "total_articles": 0}
    
    themes = Counter()
    for a in articles:
        title = (a.get("title") or "").lower()
        if any(kw in title for kw in ["earning", "revenue", "profit", "quarter"]):
            themes["earnings"] += 1
        if any(kw in title for kw in ["acquisition", "merger", "deal"]):
            themes["m_and_a"] += 1
        if any(kw in title for kw in ["lawsuit", "sec", "investigation"]):
            themes["legal"] += 1
        if any(kw in title for kw in ["ceo", "executive", "appoint"]):
            themes["management"] += 1
    
    dates = [a.get("date") or a.get("published_at") for a in articles if a.get("date") or a.get("published_at")]
    
    return {
        "as_of": str(date.today()),
        "total_articles": len(articles),
        "date_range": [min(dates), max(dates)] if dates else [],
        "top_themes": [{"theme": t, "count": c} for t, c in themes.most_common(5)],
        "key_events": [
            {"date": a.get("date"), "title": a.get("title"), "source": a.get("source")}
            for a in sorted(articles, key=lambda x: x.get("date") or "", reverse=True)[:10]
        ]
    }

def run(ticker: str, lookback_years: int = 10, lookback_days_news: int = 180,
        papers_mode: str = "auto", force_refresh: bool = False):
    ticker = ticker.upper()
    
    # Initialize
    paths.ensure_dirs(ticker)
    run_id = paths.generate_run_id()
    run_dir = paths.get_run_dir(ticker, run_id)
    run_dir.mkdir(parents=True)
    
    runlog.write_meta(run_dir, ticker, SKILL_NAME, {
        "ticker": ticker,
        "lookback_years": lookback_years,
        "lookback_days_news": lookback_days_news,
        "papers_mode": papers_mode,
    })
    
    warnings = []
    outputs = []
    
    # Check hard dependency
    company_path = paths.get_company_dir(ticker) / "company.yaml"
    company = atomic_io.load_yaml(company_path)
    
    if not company.get("cik"):
        runlog.write_needs(run_dir,
            blocked_by=[{
                "artifact": "company.yaml",
                "producer_skill": "company-foundation",
                "reason": "Missing CIK needed to query SEC filings"
            }],
            suggested_plan=["company-foundation", "collect-company-facts"])
        
        runlog.write_result(run_dir, ticker, SKILL_NAME, "blocked",
            missing=["company.yaml with cik"])
        print("BLOCKED: Missing company.yaml with CIK")
        return {"status": "blocked"}
    
    cik = company["cik"]
    company_name = company.get("company_name", ticker)
    print(f"Using CIK={cik}, company_name={company_name}")
    
    # TODO: Actual MCP calls would happen here
    # For now, create placeholder outputs
    
    # Filings index (placeholder)
    filings_index = {
        "as_of": str(date.today()),
        "filings": []  # Will be populated by MCP calls
    }
    atomic_io.atomic_write_yaml(
        paths.get_current_dir(ticker) / "filings_index.yaml",
        filings_index
    )
    outputs.append("current/filings_index.yaml")
    
    # News digest (placeholder)
    news_digest = generate_news_digest([])
    atomic_io.atomic_write_yaml(
        paths.get_current_dir(ticker) / "news_digest.yaml",
        news_digest
    )
    outputs.append("current/news_digest.yaml")
    
    # Papers digest (placeholder)
    papers_digest = {"as_of": str(date.today()), "total_papers": 0, "relevant_papers": []}
    atomic_io.atomic_write_yaml(
        paths.get_current_dir(ticker) / "papers_digest.yaml",
        papers_digest
    )
    outputs.append("current/papers_digest.yaml")
    
    # Determine status
    if len(filings_index.get("filings", [])) == 0:
        status = "partial"
        warnings.append("No filings retrieved - MCP tools need to be called")
    else:
        status = "ok"
    
    # Update artifacts
    artifacts_state.update_artifacts_state(ticker, "filings_index.yaml", status, run_id)
    artifacts_state.update_artifacts_state(ticker, "news_digest.yaml", status, run_id)
    artifacts_state.update_artifacts_state(ticker, "papers_digest.yaml", status, run_id)
    
    result = runlog.write_result(run_dir, ticker, SKILL_NAME, status,
        outputs=outputs, warnings=warnings)
    
    print(f"\n=== Result: {status} ===")
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("--lookback-years", type=int, default=10)
    parser.add_argument("--lookback-days-news", type=int, default=180)
    parser.add_argument("--papers-mode", choices=["auto", "on", "off"], default="auto")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    
    run(args.ticker, args.lookback_years, args.lookback_days_news, 
        args.papers_mode, args.force_refresh)
```

---

## 四、Skill 3: extract-xbrl-timeseries (v0.1 浅树版)

### 4.1 SKILL.md

```markdown
---
name: extract-xbrl-timeseries
description: "Extract XBRL data into Statement Atlas with facts.parquet nodes edges paths. Use when building financial data foundation from SEC filings for recast."
version: v0.1-phase1
---

# extract-xbrl-timeseries

## What This Skill Does (v0.1 Phase 1)
Build minimal Statement Atlas to unblock recast-economic-statements:
1. Use sec_edgar_mcp.get_financials to get IS/BS/CF line items
2. Build facts.parquet with all financial facts
3. Build shallow nodes/edges/paths (depth=1 tree)
4. Save to current/xbrl_atlas/

## MCP Tools
- sec_edgar_mcp.get_financials - get financial statements
- sec_edgar_mcp.discover_xbrl_concepts - list available concepts
- sec_edgar_mcp.get_xbrl_concepts - get specific concept values
- fs - write files

## Inputs
- ticker (required)
- lookback_years (optional, default 10)
- force_refresh (optional)

## Hard Dependencies
- current/filings_index.yaml with has_xbrl filings
- OR sec_edgar_mcp.get_financials available

## Outputs
- current/xbrl_atlas/periods.yaml
- current/xbrl_atlas/nodes.parquet
- current/xbrl_atlas/edges.parquet
- current/xbrl_atlas/facts.parquet
- current/xbrl_atlas/paths.parquet

## v0.1 Strategy (快速跑通)
Use sec_edgar_mcp.get_financials instead of parsing raw XBRL.
This gives us line items directly without XBRL parsing complexity.

### Step 1 - Get financials via MCP
```python
# Get all statement types
for statement_type in ["income_statement", "balance_sheet", "cash_flow"]:
    data = sec_edgar_mcp.get_financials(
        identifier=ticker,
        statement_type=statement_type
    )
    # data contains line items with labels and values
```

### Step 2 - Build facts.parquet
```python
facts = []
for item in data:
    facts.append({
        "fact_id": f"{ticker}_{statement_type}_{item['label']}_{period_end}",
        "period_end": period_end,
        "fiscal_period": fiscal_period,  # FY or Q1/Q2/Q3/Q4
        "statement_type": map_statement_type(statement_type),  # IS/BS/CF
        "role_uri": None,  # Not available in v0.1
        "concept": item.get("concept") or f"synthetic:{slugify(item['label'])}",
        "label": item["label"],
        "value": item["value"],
        "unit": item.get("unit", "USD"),
        "decimals": item.get("decimals"),
        "accession": accession,
        "context_id": None,
        "dimensions": None,
    })

facts_df = pd.DataFrame(facts)
atomic_io.atomic_write_parquet(atlas_dir / "facts.parquet", facts_df)
```

### Step 3 - Build shallow tree (nodes/edges)
```python
# Create root nodes for each statement type
nodes = []
edges = []
order = 0

for stmt_type in ["IS", "BS", "CF"]:
    # Root node
    root_id = f"{stmt_type}_root"
    nodes.append({
        "node_id": root_id,
        "statement_type": stmt_type,
        "role_uri": None,
        "concept": root_id,
        "label": {"IS": "Income Statement", "BS": "Balance Sheet", "CF": "Cash Flow"}[stmt_type],
        "depth": 0,
        "order": 0,
    })
    
    # Child nodes for each line item
    stmt_facts = facts_df[facts_df["statement_type"] == stmt_type]
    for label in stmt_facts["label"].unique():
        order += 1
        child_id = f"{stmt_type}_{slugify(label)}"
        nodes.append({
            "node_id": child_id,
            "statement_type": stmt_type,
            "role_uri": None,
            "concept": stmt_facts[stmt_facts["label"] == label].iloc[0]["concept"],
            "label": label,
            "depth": 1,
            "order": order,
        })
        edges.append({
            "parent_node_id": root_id,
            "child_node_id": child_id,
            "arcrole": "presentation",
            "weight": 1.0,
        })

nodes_df = pd.DataFrame(nodes)
edges_df = pd.DataFrame(edges)
```

### Step 4 - Build paths.parquet
```python
paths = []
for _, row in facts_df.iterrows():
    stmt_type = row["statement_type"]
    label = row["label"]
    paths.append({
        "node_id": f"{stmt_type}_{slugify(label)}",
        "period_end": row["period_end"],
        "statement_type": stmt_type,
        "path_str": f"{stmt_type}/{label}",
        "value": row["value"],
        "accession": row["accession"],
    })

paths_df = pd.DataFrame(paths)
```

### Step 5 - Build periods.yaml
```python
periods = []
for period_end in facts_df["period_end"].unique():
    period_facts = facts_df[facts_df["period_end"] == period_end]
    periods.append({
        "period_end": period_end,
        "fiscal_period": period_facts.iloc[0]["fiscal_period"],
        "accession": period_facts.iloc[0]["accession"],
    })

atomic_io.atomic_write_yaml(atlas_dir / "periods.yaml", {"periods": periods})
```

## v0.2 Upgrade Path (后续)
Replace get_financials with actual XBRL parsing:
1. Download raw XBRL from raw/sec/{accession}/
2. Parse presentation/calculation linkbase
3. Build real tree structure
4. Keep same output schema

## Blocked Conditions
- filings_index.yaml missing -> blocked, needs collect-company-facts
- sec_edgar_mcp.get_financials returns empty for all periods -> blocked

## Partial Conditions
- Some periods missing data -> partial, log warnings
- Some statement types empty -> partial
```

### 4.2 scripts/run.py

```python
#!/usr/bin/env python3
"""extract-xbrl-timeseries skill runner (v0.1 shallow)."""
import sys
import re
import argparse
from datetime import date
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[4]))

from company_research_runtime import (
    paths, atomic_io, runlog, artifacts_state, evidence
)

SKILL_NAME = "extract-xbrl-timeseries"

def slugify(text: str) -> str:
    """Convert text to slug for ID."""
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')

def map_statement_type(api_type: str) -> str:
    """Map API statement type to standard IS/BS/CF."""
    mapping = {
        "income_statement": "IS",
        "balance_sheet": "BS", 
        "cash_flow": "CF",
        "cash_flow_statement": "CF",
    }
    return mapping.get(api_type.lower(), "OTHER")

def run(ticker: str, lookback_years: int = 10, force_refresh: bool = False):
    ticker = ticker.upper()
    
    # Initialize
    paths.ensure_dirs(ticker)
    run_id = paths.generate_run_id()
    run_dir = paths.get_run_dir(ticker, run_id)
    run_dir.mkdir(parents=True)
    
    runlog.write_meta(run_dir, ticker, SKILL_NAME, {
        "ticker": ticker,
        "lookback_years": lookback_years,
        "version": "v0.1-phase1",
    })
    
    warnings = []
    outputs = []
    atlas_dir = paths.get_current_dir(ticker) / "xbrl_atlas"
    atlas_dir.mkdir(parents=True, exist_ok=True)
    
    # Check dependency
    filings_index_path = paths.get_current_dir(ticker) / "filings_index.yaml"
    filings_index = atomic_io.load_yaml(filings_index_path)
    
    if not filings_index.get("filings"):
        # Can still proceed if sec_edgar_mcp.get_financials works
        warnings.append("filings_index.yaml empty, will rely on sec_edgar_mcp.get_financials")
    
    # TODO: Call sec_edgar_mcp.get_financials for each statement type
    # Placeholder data structure
    all_facts = []
    
    # Example placeholder - in real execution Codex calls MCP
    print("TODO: Call sec_edgar_mcp.get_financials for income_statement, balance_sheet, cash_flow")
    
    # Build DataFrames (even if empty for now)
    facts_df = pd.DataFrame(all_facts) if all_facts else pd.DataFrame(columns=[
        "fact_id", "period_end", "fiscal_period", "statement_type", "role_uri",
        "concept", "label", "value", "unit", "decimals", "accession", "context_id", "dimensions"
    ])
    
    # Build shallow tree
    nodes = []
    edges = []
    
    for stmt_type in ["IS", "BS", "CF"]:
        root_id = f"{stmt_type}_root"
        nodes.append({
            "node_id": root_id,
            "statement_type": stmt_type,
            "role_uri": None,
            "concept": root_id,
            "label": {"IS": "Income Statement", "BS": "Balance Sheet", "CF": "Cash Flow"}[stmt_type],
            "depth": 0,
            "order": 0,
        })
        
        if not facts_df.empty:
            stmt_facts = facts_df[facts_df["statement_type"] == stmt_type]
            for i, label in enumerate(stmt_facts["label"].unique()):
                child_id = f"{stmt_type}_{slugify(label)}"
                nodes.append({
                    "node_id": child_id,
                    "statement_type": stmt_type,
                    "role_uri": None,
                    "concept": child_id,
                    "label": label,
                    "depth": 1,
                    "order": i + 1,
                })
                edges.append({
                    "parent_node_id": root_id,
                    "child_node_id": child_id,
                    "arcrole": "presentation",
                    "weight": 1.0,
                })
    
    nodes_df = pd.DataFrame(nodes)
    edges_df = pd.DataFrame(edges) if edges else pd.DataFrame(columns=[
        "parent_node_id", "child_node_id", "arcrole", "weight"
    ])
    
    # Build paths
    paths_data = []
    if not facts_df.empty:
        for _, row in facts_df.iterrows():
            paths_data.append({
                "node_id": f"{row['statement_type']}_{slugify(row['label'])}",
                "period_end": row["period_end"],
                "statement_type": row["statement_type"],
                "path_str": f"{row['statement_type']}/{row['label']}",
                "value": row["value"],
                "accession": row["accession"],
            })
    
    paths_df = pd.DataFrame(paths_data) if paths_data else pd.DataFrame(columns=[
        "node_id", "period_end", "statement_type", "path_str", "value", "accession"
    ])
    
    # Build periods
    periods = []
    if not facts_df.empty:
        for period_end in facts_df["period_end"].unique():
            period_facts = facts_df[facts_df["period_end"] == period_end]
            periods.append({
                "period_end": str(period_end),
                "fiscal_period": period_facts.iloc[0]["fiscal_period"] if not period_facts.empty else "FY",
                "accession": period_facts.iloc[0]["accession"] if not period_facts.empty else None,
            })
    
    # Save all outputs
    atomic_io.atomic_write_yaml(atlas_dir / "periods.yaml", {"periods": periods})
    atomic_io.atomic_write_parquet(atlas_dir / "nodes.parquet", nodes_df)
    atomic_io.atomic_write_parquet(atlas_dir / "edges.parquet", edges_df)
    atomic_io.atomic_write_parquet(atlas_dir / "facts.parquet", facts_df)
    atomic_io.atomic_write_parquet(atlas_dir / "paths.parquet", paths_df)
    
    outputs = [
        "current/xbrl_atlas/periods.yaml",
        "current/xbrl_atlas/nodes.parquet",
        "current/xbrl_atlas/edges.parquet",
        "current/xbrl_atlas/facts.parquet",
        "current/xbrl_atlas/paths.parquet",
    ]
    
    # Determine status
    if facts_df.empty:
        status = "partial"
        warnings.append("facts.parquet is empty - MCP tools need to be called")
    else:
        status = "ok"
    
    # Update artifacts
    artifacts_state.update_artifacts_state(ticker, "xbrl_atlas", status, run_id)
    
    result = runlog.write_result(run_dir, ticker, SKILL_NAME, status,
        outputs=outputs, warnings=warnings)
    
    print(f"\n=== Result: {status} ===")
    print(f"Atlas: {atlas_dir}")
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("--lookback-years", type=int, default=10)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    
    run(args.ticker, args.lookback_years, args.force_refresh)
```

---

## 五、Skill 4: recast-economic-statements

### 5.1 SKILL.md

```markdown
---
name: recast-economic-statements
description: "Transform GAAP statements to economic statements with NOPAT ROIC FCF Owner Earnings. Use when need economic profit metrics from xbrl_atlas."
version: v0.1-phase1
---

# recast-economic-statements

## What This Skill Does (v0.1 Phase 1)
1. Map GAAP line items to economic concepts via label matching
2. Calculate core metrics: NOPAT, ROIC, FCF, Owner Earnings
3. Save recast_policy.yaml for traceability
4. Output economic_statements.parquet and core_metrics.parquet

## MCP Tools
- fs - read/write files

## Inputs
- ticker (required)
- policy_version (optional, default v0.1)
- force_refresh (optional)

## Hard Dependencies
- current/xbrl_atlas/facts.parquet
- current/xbrl_atlas/periods.yaml

## Outputs
- current/economic/recast_policy.yaml
- current/economic/economic_statements.parquet
- current/economic/core_metrics.parquet

## v0.1 Strategy (Phase 1 最小可用)
Focus on 3 必出指标 first:
- owner_earnings = CFO - maintenance_capex
- fcf = CFO - capex
- Basic NOPAT/ROIC

### Step 1 - Load facts
```python
facts_df = atomic_io.load_parquet(atlas_dir / "facts.parquet")
periods = atomic_io.load_yaml(atlas_dir / "periods.yaml")

if facts_df.empty:
    # blocked
    return
```

### Step 2 - Define label matchers
```python
LABEL_MATCHERS = {
    "revenue": [
        "total revenue", "revenues", "net revenue", "net sales", 
        "total net revenue", "sales"
    ],
    "operating_income": [
        "operating income", "income from operations", 
        "operating profit", "operating earnings"
    ],
    "cfo": [
        "net cash provided by operating activities",
        "cash flows from operating activities",
        "net cash from operating activities"
    ],
    "capex": [
        "capital expenditure", "purchases of property",
        "payments for property", "acquisition of property"
    ],
    "depreciation": [
        "depreciation and amortization", "depreciation",
        "depreciation expense"
    ],
    "total_debt": [
        "total debt", "long-term debt", "total borrowings"
    ],
    "total_equity": [
        "stockholders equity", "total equity", 
        "shareholders equity", "total shareholders equity"
    ],
    "cash": [
        "cash and cash equivalents", "cash", 
        "total cash"
    ],
    "tax_expense": [
        "income tax expense", "provision for income taxes",
        "income taxes"
    ],
    "pretax_income": [
        "income before income taxes", "pretax income",
        "earnings before income taxes"
    ],
}

def find_best_match(facts_df, target, matchers):
    """Find best matching label in facts."""
    for matcher in matchers:
        matches = facts_df[facts_df["label"].str.lower().str.contains(matcher, na=False)]
        if not matches.empty:
            return matches.iloc[0]["label"], matches
    return None, pd.DataFrame()
```

### Step 3 - Build economic statements
```python
economic_data = []

for period_info in periods["periods"]:
    period_end = period_info["period_end"]
    period_facts = facts_df[facts_df["period_end"] == period_end]
    
    row = {"period_end": period_end, "fiscal_period": period_info.get("fiscal_period", "FY")}
    
    for target, matchers in LABEL_MATCHERS.items():
        label, matches = find_best_match(period_facts, target, matchers)
        if not matches.empty:
            row[target] = matches.iloc[0]["value"]
            row[f"{target}_label"] = label  # For traceability
        else:
            row[target] = None

    economic_data.append(row)

economic_df = pd.DataFrame(economic_data)
```

### Step 4 - Calculate core metrics
```python
def calc_metrics(row, floor_ratio=0.8):
    metrics = {
        "period_end": row["period_end"],
        "fiscal_period": row.get("fiscal_period", "FY"),
        "revenue": row.get("revenue"),
    }
    
    # Effective tax rate
    pretax = row.get("pretax_income") or 0
    tax = row.get("tax_expense") or 0
    if pretax > 0 and tax > 0:
        eff_tax = min(max(tax / pretax, 0.15), 0.35)
    else:
        eff_tax = 0.25
    
    # NOPAT
    op_inc = row.get("operating_income") or 0
    metrics["nopat"] = op_inc * (1 - eff_tax)
    
    # Invested Capital (simplified)
    debt = row.get("total_debt") or 0
    equity = row.get("total_equity") or 0
    cash = row.get("cash") or 0
    metrics["invested_capital"] = max(debt + equity - cash, 1)
    
    # ROIC
    metrics["roic"] = metrics["nopat"] / metrics["invested_capital"]
    
    # FCF
    cfo = row.get("cfo") or 0
    capex = abs(row.get("capex") or 0)
    metrics["cfo"] = cfo
    metrics["capex"] = capex
    metrics["fcf"] = cfo - capex
    
    # Maintenance CapEx (depr_floor method)
    depr = row.get("depreciation") or 0
    metrics["maintenance_capex"] = max(depr * floor_ratio, capex * 0.5)
    
    # Owner Earnings
    metrics["owner_earnings"] = cfo - metrics["maintenance_capex"]
    
    return metrics

core_metrics = [calc_metrics(row) for _, row in economic_df.iterrows()]
core_df = pd.DataFrame(core_metrics)
```

### Step 5 - Write recast_policy for traceability
```python
recast_policy = {
    "policy_version": "v0.1",
    "created_at": str(date.today()),
    "mapping_rules": [],
    "maintenance_capex_method": {
        "name": "depr_floor",
        "floor_ratio": 0.8,
    },
    "owner_earnings_definition": "CFO - maintenance_capex",
}

# Record which labels were chosen
for target, matchers in LABEL_MATCHERS.items():
    label, _ = find_best_match(facts_df, target, matchers)
    recast_policy["mapping_rules"].append({
        "target": target,
        "matchers": matchers,
        "chosen_label": label,
        "fallback_used": label is None,
    })

atomic_io.atomic_write_yaml(economic_dir / "recast_policy.yaml", recast_policy)
```

## Blocked Conditions
- xbrl_atlas missing or facts.parquet empty -> blocked

## Partial Conditions
- CFO or capex not found -> partial, use fallback estimates
- Some periods missing key line items -> partial

## Definition of Done
- core_metrics.parquet has at least one row with owner_earnings
- recast_policy.yaml shows mapping decisions
```

### 5.2 scripts/run.py

```python
#!/usr/bin/env python3
"""recast-economic-statements skill runner (v0.1)."""
import sys
import argparse
from datetime import date
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[4]))

from company_research_runtime import (
    paths, atomic_io, runlog, artifacts_state, evidence
)

SKILL_NAME = "recast-economic-statements"

LABEL_MATCHERS = {
    "revenue": ["total revenue", "revenues", "net revenue", "net sales", "total net revenue"],
    "operating_income": ["operating income", "income from operations", "operating profit"],
    "cfo": ["net cash provided by operating activities", "cash flows from operating activities"],
    "capex": ["capital expenditure", "purchases of property", "payments for property"],
    "depreciation": ["depreciation and amortization", "depreciation"],
    "total_debt": ["total debt", "long-term debt", "total borrowings"],
    "total_equity": ["stockholders equity", "total equity", "shareholders equity"],
    "cash": ["cash and cash equivalents", "cash"],
    "tax_expense": ["income tax expense", "provision for income taxes"],
    "pretax_income": ["income before income taxes", "pretax income"],
}

def find_best_match(facts_df, matchers):
    for matcher in matchers:
        mask = facts_df["label"].str.lower().str.contains(matcher, na=False)
        if mask.any():
            return facts_df[mask].iloc[0]["label"]
    return None

def calc_metrics(row, floor_ratio=0.8):
    pretax = row.get("pretax_income") or 0
    tax = row.get("tax_expense") or 0
    eff_tax = min(max(tax / pretax, 0.15), 0.35) if pretax > 0 and tax > 0 else 0.25
    
    op_inc = row.get("operating_income") or 0
    nopat = op_inc * (1 - eff_tax)
    
    debt = row.get("total_debt") or 0
    equity = row.get("total_equity") or 0
    cash = row.get("cash") or 0
    ic = max(debt + equity - cash, 1)
    
    cfo = row.get("cfo") or 0
    capex = abs(row.get("capex") or 0)
    depr = row.get("depreciation") or 0
    maint_capex = max(depr * floor_ratio, capex * 0.5)
    
    return {
        "period_end": row["period_end"],
        "fiscal_period": row.get("fiscal_period", "FY"),
        "revenue": row.get("revenue"),
        "nopat": nopat,
        "invested_capital": ic,
        "roic": nopat / ic if ic else 0,
        "cfo": cfo,
        "capex": capex,
        "maintenance_capex": maint_capex,
        "fcf": cfo - capex,
        "owner_earnings": cfo - maint_capex,
    }

def run(ticker: str, policy_version: str = "v0.1", force_refresh: bool = False):
    ticker = ticker.upper()
    
    paths.ensure_dirs(ticker)
    run_id = paths.generate_run_id()
    run_dir = paths.get_run_dir(ticker, run_id)
    run_dir.mkdir(parents=True)
    
    runlog.write_meta(run_dir, ticker, SKILL_NAME, {
        "ticker": ticker, "policy_version": policy_version
    })
    
    warnings = []
    atlas_dir = paths.get_current_dir(ticker) / "xbrl_atlas"
    economic_dir = paths.get_current_dir(ticker) / "economic"
    economic_dir.mkdir(parents=True, exist_ok=True)
    
    # Check dependencies
    facts_path = atlas_dir / "facts.parquet"
    periods_path = atlas_dir / "periods.yaml"
    
    facts_df = atomic_io.load_parquet(facts_path)
    periods = atomic_io.load_yaml(periods_path)
    
    if facts_df.empty:
        runlog.write_needs(run_dir,
            blocked_by=[{
                "artifact": "xbrl_atlas/facts.parquet",
                "producer_skill": "extract-xbrl-timeseries",
                "reason": "facts.parquet is empty"
            }],
            suggested_plan=["extract-xbrl-timeseries", "recast-economic-statements"])
        
        runlog.write_result(run_dir, ticker, SKILL_NAME, "blocked",
            missing=["xbrl_atlas/facts.parquet with data"])
        print("BLOCKED: facts.parquet empty")
        return {"status": "blocked"}
    
    # Build economic statements
    economic_data = []
    for period_info in periods.get("periods", []):
        period_end = period_info["period_end"]
        period_facts = facts_df[facts_df["period_end"].astype(str) == str(period_end)]
        
        row = {"period_end": period_end, "fiscal_period": period_info.get("fiscal_period", "FY")}
        
        for target, matchers in LABEL_MATCHERS.items():
            label = find_best_match(period_facts, matchers)
            if label:
                val = period_facts[period_facts["label"] == label].iloc[0]["value"]
                row[target] = val
            else:
                row[target] = None
        
        economic_data.append(row)
    
    economic_df = pd.DataFrame(economic_data) if economic_data else pd.DataFrame()
    
    # Calculate core metrics
    if not economic_df.empty:
        core_metrics = [calc_metrics(row) for _, row in economic_df.iterrows()]
        core_df = pd.DataFrame(core_metrics)
    else:
        core_df = pd.DataFrame()
    
    # Build recast policy
    recast_policy = {
        "policy_version": policy_version,
        "created_at": str(date.today()),
        "mapping_rules": [
            {"target": t, "matchers": m, "chosen_label": find_best_match(facts_df, m)}
            for t, m in LABEL_MATCHERS.items()
        ],
        "maintenance_capex_method": {"name": "depr_floor", "floor_ratio": 0.8},
        "owner_earnings_definition": "CFO - maintenance_capex",
    }
    
    # Save outputs
    atomic_io.atomic_write_yaml(economic_dir / "recast_policy.yaml", recast_policy)
    atomic_io.atomic_write_parquet(economic_dir / "economic_statements.parquet", economic_df)
    atomic_io.atomic_write_parquet(economic_dir / "core_metrics.parquet", core_df)
    
    outputs = [
        "current/economic/recast_policy.yaml",
        "current/economic/economic_statements.parquet",
        "current/economic/core_metrics.parquet",
    ]
    
    # Check for missing critical fields
    if core_df.empty or core_df["owner_earnings"].isna().all():
        status = "partial"
        warnings.append("owner_earnings could not be calculated - CFO or capex missing")
    else:
        status = "ok"
    
    artifacts_state.update_artifacts_state(ticker, "economic", status, run_id)
    
    evidence.append_evidence(ticker, SKILL_NAME,
        f"Economic recast using policy {policy_version}", 0.7,
        sources=[{"type": "xbrl_atlas", "path": str(facts_path)}])
    
    result = runlog.write_result(run_dir, ticker, SKILL_NAME, status,
        outputs=outputs, warnings=warnings)
    
    print(f"\n=== Result: {status} ===")
    if not core_df.empty:
        print(f"Latest owner_earnings: {core_df.iloc[-1]['owner_earnings']}")
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("--policy-version", default="v0.1")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    
    run(args.ticker, args.policy_version, args.force_refresh)
```

---

## 六、Skill 5: valuation-and-margin-of-safety (Phase 1)

### 6.1 SKILL.md

```markdown
---
name: valuation-and-margin-of-safety
description: "Calculate intrinsic value via EPV DCF and margin of safety. Use when need valuation estimate or investment memo for any ticker."
version: v0.1-phase1
---

# valuation-and-margin-of-safety

## What This Skill Does (Phase 1)
1. Load market snapshot and core metrics
2. Calculate EPV and simplified DCF
3. Generate bear/base/bull valuation range
4. Output value_state.yaml and investment_memo.md

## MCP Tools
- fs - read/write files

## Inputs
- ticker (required)
- model_type (optional, epv|dcf|hybrid, default hybrid)
- force_refresh (optional)

## Hard Dependencies (Phase 1)
- current/market_snapshot.yaml
- current/economic/core_metrics.parquet

Note: Phase 2 will add quality_coefficient.yaml dependency

## Outputs
- current/valuation/valuation.yaml
- current/valuation/valuation_model.csv
- current/valuation/value_state.yaml
- current/valuation/investment_memo.md

## Phase 1 Defaults (No quality_coefficient)
```python
DEFAULT_ASSUMPTIONS = {
    "discount_rate": {"bear": 0.12, "base": 0.10, "bull": 0.085},
    "advantage_period_years": {"bear": 3, "base": 5, "bull": 8},
    "owner_earnings_growth": {"bear": 0.00, "base": 0.03, "bull": 0.06},
    "terminal_growth": 0.02,
    "quality_coefficient": 0.5,  # Conservative default
    "confidence": 0.3,  # Low confidence without full analysis
}
```

### Step 1 - Load inputs
```python
market = atomic_io.load_yaml(current_dir / "market_snapshot.yaml")
core_df = atomic_io.load_parquet(economic_dir / "core_metrics.parquet")

if not market.get("price") or core_df.empty:
    # blocked
    return

price = market["price"]
shares = market["shares_outstanding"]
latest = core_df.iloc[-1]  # Most recent period
owner_earnings = latest["owner_earnings"]
```

### Step 2 - Calculate EPV
```python
def calc_epv(owner_earnings, discount_rate):
    """EPV = Owner Earnings / Cost of Capital"""
    return owner_earnings / discount_rate

epv_scenarios = {
    scenario: calc_epv(owner_earnings, assumptions["discount_rate"][scenario])
    for scenario in ["bear", "base", "bull"]
}
```

### Step 3 - Calculate DCF
```python
def calc_dcf(owner_earnings, growth, discount, advantage_period, terminal_growth=0.02):
    """Two-stage DCF."""
    # Stage 1: Growth period
    cash_flows = []
    cumulative = 1.0
    for year in range(1, advantage_period + 1):
        yr_growth = growth - (growth - terminal_growth) * (year / advantage_period)
        cumulative *= (1 + yr_growth)
        cash_flows.append(owner_earnings * cumulative)
    
    pv_stage1 = sum(cf / (1 + discount)**i for i, cf in enumerate(cash_flows, 1))
    
    # Terminal value
    terminal_cf = cash_flows[-1] * (1 + terminal_growth)
    terminal_value = terminal_cf / (discount - terminal_growth)
    pv_terminal = terminal_value / (1 + discount)**advantage_period
    
    return pv_stage1 + pv_terminal

dcf_scenarios = {
    scenario: calc_dcf(
        owner_earnings,
        assumptions["owner_earnings_growth"][scenario],
        assumptions["discount_rate"][scenario],
        assumptions["advantage_period_years"][scenario]
    )
    for scenario in ["bear", "base", "bull"]
}
```

### Step 4 - Combine and calculate per-share
```python
# Weighted combination
weights = {"epv": 0.4, "dcf": 0.6}

intrinsic_values = {
    scenario: (epv_scenarios[scenario] * weights["epv"] + 
               dcf_scenarios[scenario] * weights["dcf"])
    for scenario in ["bear", "base", "bull"]
}

iv_per_share = {
    scenario: iv / shares
    for scenario, iv in intrinsic_values.items()
}

margin_of_safety = {
    scenario: (iv_per_share[scenario] - price) / iv_per_share[scenario]
    for scenario in ["bear", "base", "bull"]
}
```

### Step 5 - Build value_state.yaml
```python
value_state = {
    "ticker": ticker,
    "as_of": str(date.today()),
    "market": {
        "price": price,
        "shares_outstanding": shares,
        "market_cap": market.get("market_cap"),
        "enterprise_value": market.get("enterprise_value"),
    },
    "profit": {
        "base_period": "TTM",
        "owner_earnings": owner_earnings,
        "owner_earnings_per_share": owner_earnings / shares,
        "nopat": latest.get("nopat"),
        "invested_capital": latest.get("invested_capital"),
        "roic": latest.get("roic"),
        "fcf": latest.get("fcf"),
    },
    "quality": {
        "coefficient_base": 0.5,  # Phase 1 default
        "confidence": 0.3,
        "components": None,  # Phase 2 will populate
    },
    "valuation": {
        "intrinsic_value_per_share": iv_per_share,
        "margin_of_safety_base": margin_of_safety["base"],
        "method_weights": weights,
    },
    "links": {
        "memo": "current/valuation/investment_memo.md",
        "valuation_yaml": "current/valuation/valuation.yaml",
    },
}
```

### Step 6 - Generate investment memo
```python
memo = f"""# Investment Memo: {ticker}

**Date**: {date.today()} | **Price**: ${price:.2f} | **Base MOS**: {margin_of_safety['base']*100:.1f}%

## Summary
{ticker} appears {"undervalued" if margin_of_safety['base'] > 0.2 else "fairly valued"} 
with base IV of ${iv_per_share['base']:.2f}.

## Key Metrics
| Metric | Value |
|--------|-------|
| Owner Earnings | ${owner_earnings/1e6:.1f}M |
| OE/Share | ${owner_earnings/shares:.2f} |
| ROIC | {latest.get('roic', 0)*100:.1f}% |

## Valuation Range
| Scenario | IV | MOS |
|----------|-----|-----|
| Bear | ${iv_per_share['bear']:.2f} | {margin_of_safety['bear']*100:.1f}% |
| Base | ${iv_per_share['base']:.2f} | {margin_of_safety['base']*100:.1f}% |
| Bull | ${iv_per_share['bull']:.2f} | {margin_of_safety['bull']*100:.1f}% |

## ⚠️ Phase 1 Notice
Quality assessment pending. Using conservative defaults:
- Quality coefficient: 0.5
- Confidence: 0.3

## Next Steps
- [ ] Run profit-quality-and-risk
- [ ] Run moat-inferencer  
- [ ] Run cross-examination-audit
"""
```

## Blocked Conditions
- market_snapshot.yaml missing price -> blocked
- core_metrics.parquet empty or no owner_earnings -> blocked

## Definition of Done
- value_state.yaml with margin_of_safety_base calculated
- investment_memo.md readable
- valuation.yaml with assumptions documented
```

### 6.2 scripts/run.py

```python
#!/usr/bin/env python3
"""valuation-and-margin-of-safety skill runner (Phase 1)."""
import sys
import argparse
from datetime import date
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[4]))

from company_research_runtime import (
    paths, atomic_io, runlog, artifacts_state, evidence
)

SKILL_NAME = "valuation-and-margin-of-safety"

DEFAULT_ASSUMPTIONS = {
    "discount_rate": {"bear": 0.12, "base": 0.10, "bull": 0.085},
    "advantage_period_years": {"bear": 3, "base": 5, "bull": 8},
    "owner_earnings_growth": {"bear": 0.00, "base": 0.03, "bull": 0.06},
    "terminal_growth": 0.02,
}

def calc_epv(owner_earnings, discount_rate):
    return owner_earnings / discount_rate if discount_rate else 0

def calc_dcf(owner_earnings, growth, discount, advantage_period, terminal_growth=0.02):
    if discount <= terminal_growth:
        return 0
    
    cash_flows = []
    cumulative = 1.0
    for year in range(1, advantage_period + 1):
        yr_growth = growth - (growth - terminal_growth) * (year / advantage_period)
        cumulative *= (1 + yr_growth)
        cash_flows.append(owner_earnings * cumulative)
    
    pv_stage1 = sum(cf / (1 + discount)**i for i, cf in enumerate(cash_flows, 1))
    
    terminal_cf = cash_flows[-1] * (1 + terminal_growth) if cash_flows else 0
    terminal_value = terminal_cf / (discount - terminal_growth)
    pv_terminal = terminal_value / (1 + discount)**advantage_period
    
    return pv_stage1 + pv_terminal

def run(ticker: str, model_type: str = "hybrid", force_refresh: bool = False):
    ticker = ticker.upper()
    
    paths.ensure_dirs(ticker)
    run_id = paths.generate_run_id()
    run_dir = paths.get_run_dir(ticker, run_id)
    run_dir.mkdir(parents=True)
    
    runlog.write_meta(run_dir, ticker, SKILL_NAME, {
        "ticker": ticker, "model_type": model_type, "version": "v0.1-phase1"
    })
    
    warnings = []
    current_dir = paths.get_current_dir(ticker)
    valuation_dir = current_dir / "valuation"
    valuation_dir.mkdir(parents=True, exist_ok=True)
    
    # Load dependencies
    market = atomic_io.load_yaml(current_dir / "market_snapshot.yaml")
    core_df = atomic_io.load_parquet(current_dir / "economic" / "core_metrics.parquet")
    
    # Check blocked conditions
    if not market.get("price"):
        runlog.write_needs(run_dir,
            blocked_by=[{
                "artifact": "market_snapshot.yaml",
                "producer_skill": "company-foundation",
                "reason": "Missing price"
            }],
            suggested_plan=["company-foundation", "valuation-and-margin-of-safety"])
        runlog.write_result(run_dir, ticker, SKILL_NAME, "blocked",
            missing=["market_snapshot.yaml with price"])
        print("BLOCKED: Missing market price")
        return {"status": "blocked"}
    
    if core_df.empty or "owner_earnings" not in core_df.columns:
        runlog.write_needs(run_dir,
            blocked_by=[{
                "artifact": "core_metrics.parquet",
                "producer_skill": "recast-economic-statements",
                "reason": "Missing owner_earnings"
            }],
            suggested_plan=["recast-economic-statements", "valuation-and-margin-of-safety"])
        runlog.write_result(run_dir, ticker, SKILL_NAME, "blocked",
            missing=["core_metrics.parquet with owner_earnings"])
        print("BLOCKED: Missing core metrics")
        return {"status": "blocked"}
    
    price = market["price"]
    shares = market.get("shares_outstanding", 1)
    latest = core_df.iloc[-1]
    owner_earnings = latest.get("owner_earnings", 0)
    
    if not owner_earnings or owner_earnings <= 0:
        warnings.append("owner_earnings <= 0, using absolute value or minimum")
        owner_earnings = abs(owner_earnings) if owner_earnings else 1
    
    # Calculate valuations
    assumptions = DEFAULT_ASSUMPTIONS
    
    epv_scenarios = {s: calc_epv(owner_earnings, assumptions["discount_rate"][s]) 
                     for s in ["bear", "base", "bull"]}
    
    dcf_scenarios = {s: calc_dcf(
        owner_earnings,
        assumptions["owner_earnings_growth"][s],
        assumptions["discount_rate"][s],
        assumptions["advantage_period_years"][s],
        assumptions["terminal_growth"]
    ) for s in ["bear", "base", "bull"]}
    
    # Combine
    weights = {"epv": 0.4, "dcf": 0.6}
    intrinsic_values = {
        s: epv_scenarios[s] * weights["epv"] + dcf_scenarios[s] * weights["dcf"]
        for s in ["bear", "base", "bull"]
    }
    
    iv_per_share = {s: iv / shares for s, iv in intrinsic_values.items()}
    margin_of_safety = {s: (iv_per_share[s] - price) / iv_per_share[s] if iv_per_share[s] else 0
                        for s in ["bear", "base", "bull"]}
    
    # Build outputs
    valuation_yaml = {
        "as_of": str(date.today()),
        "methods_used": ["epv", "dcf"],
        "assumptions": assumptions,
        "method_weights": weights,
        "results": {
            "epv_per_share": {s: epv / shares for s, epv in epv_scenarios.items()},
            "dcf_per_share": {s: dcf / shares for s, dcf in dcf_scenarios.items()},
            "intrinsic_value_per_share": iv_per_share,
            "margin_of_safety": margin_of_safety,
        },
        "downside_protection": {
            "net_cash_per_share": market.get("net_debt", 0) / shares * -1 if market.get("net_debt") else None,
        },
    }
    
    value_state = {
        "ticker": ticker,
        "as_of": str(date.today()),
        "market": {
            "price": price,
            "shares_outstanding": shares,
            "market_cap": market.get("market_cap"),
            "enterprise_value": market.get("enterprise_value"),
        },
        "profit": {
            "base_period": "TTM",
            "owner_earnings": latest.get("owner_earnings"),
            "owner_earnings_per_share": latest.get("owner_earnings", 0) / shares,
            "nopat": latest.get("nopat"),
            "invested_capital": latest.get("invested_capital"),
            "roic": latest.get("roic"),
            "fcf": latest.get("fcf"),
        },
        "quality": {
            "coefficient_base": 0.5,
            "confidence": 0.3,
            "components": None,
        },
        "valuation": {
            "intrinsic_value_per_share": iv_per_share,
            "margin_of_safety_base": margin_of_safety["base"],
        },
        "links": {
            "memo": "current/valuation/investment_memo.md",
            "valuation_yaml": "current/valuation/valuation.yaml",
        },
    }
    
    # Investment memo
    verdict = "undervalued" if margin_of_safety["base"] > 0.2 else "fairly valued" if margin_of_safety["base"] > 0 else "overvalued"
    memo = f"""# Investment Memo: {ticker}

**Date**: {date.today()} | **Price**: ${price:.2f} | **Base MOS**: {margin_of_safety['base']*100:.1f}%

## Summary
{ticker} appears **{verdict}** with base IV of ${iv_per_share['base']:.2f}.

## Key Metrics
| Metric | Value |
|--------|-------|
| Owner Earnings | ${latest.get('owner_earnings', 0)/1e6:.1f}M |
| OE/Share | ${latest.get('owner_earnings', 0)/shares:.2f} |
| ROIC | {latest.get('roic', 0)*100:.1f}% |
| FCF | ${latest.get('fcf', 0)/1e6:.1f}M |

## Valuation Range
| Scenario | IV | MOS |
|----------|-----|-----|
| Bear | ${iv_per_share['bear']:.2f} | {margin_of_safety['bear']*100:.1f}% |
| Base | ${iv_per_share['base']:.2f} | {margin_of_safety['base']*100:.1f}% |
| Bull | ${iv_per_share['bull']:.2f} | {margin_of_safety['bull']*100:.1f}% |

## Assumptions (Phase 1 Defaults)
- Discount Rate: {assumptions['discount_rate']['base']*100:.0f}% (base)
- Advantage Period: {assumptions['advantage_period_years']['base']} years
- Growth: {assumptions['owner_earnings_growth']['base']*100:.0f}%

## ⚠️ Phase 1 Notice
Using conservative defaults. Full analysis requires:
- [ ] profit-quality-and-risk
- [ ] growth-driver-explorer
- [ ] moat-inferencer
- [ ] cross-examination-audit

---
*Generated by valuation-and-margin-of-safety v0.1-phase1*
"""
    
    # Save outputs
    atomic_io.atomic_write_yaml(valuation_dir / "valuation.yaml", valuation_yaml)
    atomic_io.atomic_write_yaml(valuation_dir / "value_state.yaml", value_state)
    with open(valuation_dir / "investment_memo.md", "w") as f:
        f.write(memo)
    
    # Valuation model CSV
    model_df = pd.DataFrame([
        {"scenario": s, "epv": epv_scenarios[s], "dcf": dcf_scenarios[s], 
         "combined": intrinsic_values[s], "per_share": iv_per_share[s], "mos": margin_of_safety[s]}
        for s in ["bear", "base", "bull"]
    ])
    model_df.to_csv(valuation_dir / "valuation_model.csv", index=False)
    
    outputs = [
        "current/valuation/valuation.yaml",
        "current/valuation/value_state.yaml", 
        "current/valuation/investment_memo.md",
        "current/valuation/valuation_model.csv",
    ]
    
    status = "ok" if not warnings else "partial"
    
    artifacts_state.update_artifacts_state(ticker, "valuation", status, run_id)
    
    evidence.append_evidence(ticker, SKILL_NAME,
        f"Valuation completed: IV_base=${iv_per_share['base']:.2f}, MOS_base={margin_of_safety['base']*100:.1f}%",
        confidence=0.5,
        sources=[{"type": "core_metrics"}, {"type": "market_snapshot"}])
    
    result = runlog.write_result(run_dir, ticker, SKILL_NAME, status,
        outputs=outputs, warnings=warnings)
    
    print(f"\n=== Result: {status} ===")
    print(f"Price: ${price:.2f}")
    print(f"IV (base): ${iv_per_share['base']:.2f}")
    print(f"MOS (base): {margin_of_safety['base']*100:.1f}%")
    print(f"Verdict: {verdict}")
    
    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("--model-type", choices=["epv", "dcf", "hybrid"], default="hybrid")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()
    
    run(args.ticker, args.model_type, args.force_refresh)
```

---

## 七、Smoke Test 脚本

### scripts/smoke_test_phase1.py

```python
#!/usr/bin/env python3
"""
Phase 1 smoke test - run all 5 skills and verify outputs.
Usage: python smoke_test_phase1.py AAPL MSFT GOOGL
"""
import sys
import subprocess
from pathlib import Path
import yaml
import pandas as pd

SKILLS_DIR = Path("/mnt/d/python_project/my-quant-project/.codex/skills/company_research")
BASE_PATH = Path("/home/help/mcp/work/company_research")

SKILLS = [
    "company-foundation",
    "collect-company-facts",
    "extract-xbrl-timeseries",
    "recast-economic-statements",
    "valuation-and-margin-of-safety",
]

EXPECTED_OUTPUTS = {
    "company-foundation": ["company.yaml", "current/market_snapshot.yaml"],
    "collect-company-facts": ["current/filings_index.yaml", "current/news_digest.yaml"],
    "extract-xbrl-timeseries": ["current/xbrl_atlas/facts.parquet", "current/xbrl_atlas/periods.yaml"],
    "recast-economic-statements": ["current/economic/core_metrics.parquet"],
    "valuation-and-margin-of-safety": ["current/valuation/value_state.yaml", "current/valuation/investment_memo.md"],
}

def check_outputs(ticker: str, skill: str) -> dict:
    """Check if expected outputs exist."""
    company_dir = BASE_PATH / "company" / ticker.upper()
    results = {"skill": skill, "ticker": ticker, "outputs": {}}
    
    for output in EXPECTED_OUTPUTS.get(skill, []):
        path = company_dir / output
        results["outputs"][output] = path.exists()
    
    results["all_present"] = all(results["outputs"].values())
    return results

def run_skill(ticker: str, skill: str) -> bool:
    """Run a skill for a ticker."""
    script = SKILLS_DIR / skill / "scripts" / "run.py"
    if not script.exists():
        print(f"  ⚠️  Script not found: {script}")
        return False
    
    try:
        result = subprocess.run(
            [sys.executable, str(script), ticker],
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode != 0:
            print(f"  ⚠️  Non-zero exit: {result.returncode}")
            print(f"     stderr: {result.stderr[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"  ⚠️  Timeout")
        return False
    except Exception as e:
        print(f"  ⚠️  Error: {e}")
        return False

def generate_summary(tickers: list):
    """Generate value_summary.csv from all tickers."""
    records = []
    
    for ticker in tickers:
        vs_path = BASE_PATH / "company" / ticker.upper() / "current" / "valuation" / "value_state.yaml"
        if not vs_path.exists():
            continue
        
        with open(vs_path) as f:
            vs = yaml.safe_load(f)
        
        records.append({
            "ticker": vs.get("ticker"),
            "as_of": vs.get("as_of"),
            "price": vs.get("market", {}).get("price"),
            "owner_earnings": vs.get("profit", {}).get("owner_earnings"),
            "roic": vs.get("profit", {}).get("roic"),
            "iv_base": vs.get("valuation", {}).get("intrinsic_value_per_share", {}).get("base"),
            "mos_base": vs.get("valuation", {}).get("margin_of_safety_base"),
        })
    
    if records:
        df = pd.DataFrame(records).sort_values("mos_base", ascending=False)
        output = BASE_PATH / "value_summary.csv"
        df.to_csv(output, index=False)
        print(f"\n✓ Saved: {output}")
        print(df.to_string(index=False))

def main(tickers: list):
    print("=" * 60)
    print("Phase 1 Smoke Test")
    print("=" * 60)
    
    all_results = []
    
    for ticker in tickers:
        print(f"\n>>> {ticker}")
        
        for skill in SKILLS:
            print(f"  Running: {skill}...")
            success = run_skill(ticker, skill)
            
            check = check_outputs(ticker, skill)
            status = "✓" if check["all_present"] else "✗"
            print(f"    {status} Outputs: {check['outputs']}")
            
            all_results.append({
                "ticker": ticker,
                "skill": skill,
                "run_success": success,
                "outputs_present": check["all_present"],
            })
    
    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    
    for ticker in tickers:
        ticker_results = [r for r in all_results if r["ticker"] == ticker]
        all_ok = all(r["outputs_present"] for r in ticker_results)
        status = "✓ PASS" if all_ok else "✗ FAIL"
        print(f"{ticker}: {status}")
    
    # Generate value_summary
    generate_summary(tickers)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python smoke_test_phase1.py AAPL MSFT GOOGL")
        sys.exit(1)
    
    main(sys.argv[1:])
```

---

## 八、实施检查清单

```
□ Step 0: 创建目录结构
  mkdir -p /mnt/d/python_project/my-quant-project/.codex/skills/company_research/{company-foundation,collect-company-facts,extract-xbrl-timeseries,recast-economic-statements,valuation-and-margin-of-safety}/{scripts,references}
  mkdir -p /mnt/d/python_project/my-quant-project/company_research_runtime
  mkdir -p /home/help/mcp/work/company_research/company

□ Step 1: 部署共享 runtime
  - company_research_runtime/__init__.py
  - company_research_runtime/paths.py
  - company_research_runtime/atomic_io.py
  - company_research_runtime/runlog.py
  - company_research_runtime/artifacts_state.py
  - company_research_runtime/evidence.py
  - company_research_runtime/hashing.py

□ Step 2: 部署 Skill 1 - company-foundation
  - SKILL.md (description 单行无冒号)
  - scripts/run.py
  - 测试: codex "Initialize AAPL research"

□ Step 3: 部署 Skill 2 - collect-company-facts
  - SKILL.md
  - scripts/run.py
  - 测试: 验证 filings_index.yaml

□ Step 4: 部署 Skill 3 - extract-xbrl-timeseries (v0.1)
  - SKILL.md
  - scripts/run.py
  - 测试: 验证 facts.parquet (可以是空但结构对)

□ Step 5: 部署 Skill 4 - recast-economic-statements
  - SKILL.md
  - scripts/run.py
  - 测试: 验证 core_metrics.parquet

□ Step 6: 部署 Skill 5 - valuation-and-margin-of-safety
  - SKILL.md
  - scripts/run.py
  - 测试: 验证 value_state.yaml 和 investment_memo.md

□ Step 7: 端到端 smoke test
  python smoke_test_phase1.py AAPL MSFT
  - 验证 value_summary.csv 生成
  - 检查每个 ticker 的 investment_memo.md
```

---

## 九、Codex 使用方式

### 显式调用

```bash
# 方式 1: 使用 $ 前缀
$company-foundation
> Initialize AAPL

# 方式 2: /skills 菜单
/skills
> 选择 company-foundation
> Initialize AAPL
```

### 隐式调用

```bash
# Codex 会根据 description 自动匹配
codex "Start coverage on AAPL and get market data"
# → 自动选中 company-foundation

codex "Get SEC filings for AAPL"
# → 自动选中 collect-company-facts

codex "Calculate intrinsic value for AAPL"
# → 自动选中 valuation-and-margin-of-safety
```

### 链式执行

```bash
codex "Run full Phase 1 analysis for AAPL"
# Codex 会识别需要按顺序执行 1→2→3→4→5
```

---

**文档版本**: v2.0 (Codex Best Practices Edition)
**创建日期**: 2026-01-06
**关键改进**:
- Description 单行、无冒号（避免 YAML 解析问题）
- 共享 runtime 减少重复代码
- v0.1 浅树策略（先跑通再完善）
- 项目级 Skills（git 可管理）
- sec_edgar_mcp 具体工具映射