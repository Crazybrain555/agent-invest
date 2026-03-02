# Phase 1 核心估值链 Codex Skills 实施指南（可直接复制版）

> **基于 Codex Skills 官方规范 + sec_edgar_mcp 实际工具 + 分阶段实现策略**
>
> **v2.1 架构升级**：raw/events/current 三层解耦 + event taxonomy + canonical buckets

---

## 〇、前置准备

### 0.1 目录结构

```bash
# Skills 放到项目级（git 管理）
/mnt/d/python_project/my-quant-project/
├── .codex/skills/company_research/
│   ├── company-foundation/
│   ├── sec-ingest-and-materialize-events/    # 原 collect-company-facts
│   ├── xbrl-parse-financial-report-events/   # 原 extract-xbrl-timeseries
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
    ├── company.yaml                       # Skill1: 公司身份信息
    ├── latest.json                        # 最新运行状态快照
    │
    ├── raw/                               # 原始证据层（不可变、可追溯）
    │   └── sec/
    │       └── accessions/
    │           └── {accession}/           # 每个 filing 一个目录
    │               ├── meta.yaml          # 我们生成的元数据（含 doc map）
    │               ├── manifest.yaml      # 下载清单 + hash + 完整性
    │               ├── index/             # SEC 目录索引文件
    │               │   ├── index.json
    │               │   └── {accession}-index.html
    │               ├── submission/        # 完整 submission 包
    │               │   └── {accession}.txt
    │               ├── documents/         # as-filed 文档（主文档在此）
    │               │   └── <original filenames...>
    │               ├── exhibits/          # as-filed exhibits（EX-*，排除 EX-101.*）
    │               │   └── <original filenames...>
    │               ├── xbrl/              # XBRL/iXBRL 文件集合
    │               │   └── <original filenames...>
    │               └── other/             # 可选：图片等非核心
    │
    ├── events/                            # 事件级数据层（下游 skills 直接消费）
    │   └── sec/
    │       ├── ingest_state.yaml          # 元数据（issuer_type/vmf/window/totals）
    │       ├── filings_index.parquet      # filing 粒度索引
    │       ├── events_index.parquet       # event 粒度索引
    │       └── events/
    │           └── {event_id}/            # 每个事件一个对象目录
    │               ├── event.yaml         # 事件元数据
    │               ├── raw_refs.json      # 指向 raw 的引用
    │               ├── bucket_manifest.json
    │               ├── event_overview/    # canonical buckets（按需创建）
    │               ├── financial_statements/
    │               ├── mdna_operating_review/
    │               ├── risk_factors/
    │               ├── business_and_strategy/
    │               ├── ...                # 其他 buckets
    │               └── structured_data/   # Skill3 写入
    │                   └── xbrl_atlas/
    │
    ├── current/                           # 当前态工作台
    │   ├── analysis_data/                 # 数据底座
    │   │   ├── market_snapshot.yaml       # Skill1
    │   │   ├── events_summary.parquet     # 从 events 汇总
    │   │   ├── xbrl_atlas/                # Skill3: 全局合并 atlas
    │   │   └── economic/                  # Skill4: 经济报表
    │   │
    │   ├── analytics/                     # 分析产物
    │   │   ├── diagnostics/               # Skill5-7,9
    │   │   ├── valuation/                 # Skill8
    │   │   └── evidence/                  # 证据账本
    │   │
    │   ├── gaps/                          # 缺口与问题
    │   │   ├── artifacts_state.yaml
    │   │   ├── questions.jsonl
    │   │   └── missing_data.yaml
    │   │
    │   └── outputs/                       # 最终输出
    │       ├── investment_memo.md
    │       ├── value_state.yaml
    │       └── valuation.yaml
    │
    └── runs/{run_id}/                     # 运行记录
        ├── meta.yaml
        ├── result.yaml
        ├── needs.yaml                     # blocked 时
        └── outputs/
```

### 0.2 创建目录

```bash
# 创建 Skills 目录
mkdir -p /mnt/d/python_project/my-quant-project/.codex/skills/company_research/{company-foundation,sec-ingest-and-materialize-events,xbrl-parse-financial-report-events,recast-economic-statements,valuation-and-margin-of-safety}/{scripts,references}

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
"""Path utilities for company research (v2.1: raw/events/current architecture)."""
from pathlib import Path
from datetime import datetime
import pytz

BASE_PATH = Path("/home/help/mcp/work/company_research")
TZ = pytz.timezone("America/New_York")

def get_company_dir(ticker: str) -> Path:
    return BASE_PATH / "company" / ticker.upper()

# --- raw layer ---
def get_raw_dir(ticker: str) -> Path:
    return get_company_dir(ticker) / "raw"

def get_raw_sec_dir(ticker: str) -> Path:
    return get_raw_dir(ticker) / "sec" / "accessions"

def get_accession_dir(ticker: str, accession: str) -> Path:
    return get_raw_sec_dir(ticker) / accession

# --- events layer ---
def get_events_dir(ticker: str) -> Path:
    return get_company_dir(ticker) / "events"

def get_events_sec_dir(ticker: str) -> Path:
    return get_events_dir(ticker) / "sec"

def get_event_dir(ticker: str, event_id: str) -> Path:
    return get_events_sec_dir(ticker) / "events" / event_id

# --- current layer ---
def get_current_dir(ticker: str) -> Path:
    return get_company_dir(ticker) / "current"

def get_analysis_data_dir(ticker: str) -> Path:
    return get_current_dir(ticker) / "analysis_data"

def get_analytics_dir(ticker: str) -> Path:
    return get_current_dir(ticker) / "analytics"

def get_gaps_dir(ticker: str) -> Path:
    return get_current_dir(ticker) / "gaps"

def get_outputs_dir(ticker: str) -> Path:
    return get_current_dir(ticker) / "outputs"

# --- runs ---
def get_runs_dir(ticker: str) -> Path:
    return get_company_dir(ticker) / "runs"

def generate_run_id() -> str:
    return datetime.now(TZ).strftime("%Y%m%d_%H%M%S")

def get_run_dir(ticker: str, run_id: str) -> Path:
    return get_runs_dir(ticker) / run_id

def ensure_dirs(ticker: str):
    """Create all required directories for a ticker."""
    dirs = [
        # raw
        get_raw_sec_dir(ticker),
        # events
        get_events_sec_dir(ticker) / "events",
        # current/analysis_data
        get_analysis_data_dir(ticker) / "xbrl_atlas",
        get_analysis_data_dir(ticker) / "economic",
        # current/analytics
        get_analytics_dir(ticker) / "diagnostics",
        get_analytics_dir(ticker) / "valuation",
        get_analytics_dir(ticker) / "evidence",
        # current/gaps
        get_gaps_dir(ticker),
        # current/outputs
        get_outputs_dir(ticker),
        # runs
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

def atomic_write_json(path: Path, data):
    """Write JSON atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                      dir=path.parent, delete=False) as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=False)
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

def atomic_write_text(path: Path, text: str):
    """Write text file atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode='w', suffix='.tmp',
                                      dir=path.parent, delete=False) as f:
        f.write(text)
        temp_path = f.name

    shutil.move(temp_path, path)

def load_yaml(path: Path) -> dict:
    """Load YAML file, return empty dict if not exists."""
    path = Path(path)
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}

def load_json(path: Path):
    """Load JSON file, return empty dict if not exists."""
    path = Path(path)
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)

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
                 missing: list = None, as_of: str = None,
                 components: dict = None):
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
        "components": components or {},
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
from .paths import get_gaps_dir, TZ

def update_artifacts_state(ticker: str, artifact_name: str, status: str,
                           run_id: str = None, extra: dict = None):
    """Update artifacts_state.yaml with new artifact status."""
    state_path = get_gaps_dir(ticker) / "artifacts_state.yaml"
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
    state_path = get_gaps_dir(ticker) / "artifacts_state.yaml"
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
from .paths import get_analytics_dir, get_gaps_dir, TZ

def generate_evidence_id(prefix: str = "E") -> str:
    return f"{prefix}_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S_%f')}"

def append_evidence(ticker: str, skill: str, claim: str, confidence: float,
                    sources: list = None, notes: str = None):
    """Append evidence to evidence.jsonl."""
    evidence_path = get_analytics_dir(ticker) / "evidence" / "evidence.jsonl"
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
    questions_path = get_gaps_dir(ticker) / "questions.jsonl"
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
revision: "<YYYY-MM-DD>"
---

# company-foundation

## What This Skill Does
1. Create folder tree under /home/help/mcp/work/company_research/company/{TICKER}/
2. Resolve identity via SEC EDGAR (ticker to CIK, company name, exchange, FY end)
3. Fetch market snapshot via multi-source chain (Alpaca price; shares/marketCap/EV from trading_mcp/SEC/Yahoo)
4. Write to runs/{run_id}/ then atomically promote to current/

## MCP Tools
- sec_edgar_mcp.get_cik_by_ticker - resolve CIK from ticker
- sec_edgar_mcp.get_company_info - get company details
- sec_edgar_mcp.get_recent_filings - infer fiscal year end from annual filing period_of_report
- alpaca.get_stock_latest_trade / alpaca.get_stock_snapshot - price (USD)
- alpaca.get_asset - exchange fallback
- trading_mcp.get_fundamental_stock_metrics - (optional) shares / marketCap / EV
- yfinance.get_stock_info - fallback shares / marketCap / EV
- fs - write files

## Inputs
- ticker (required) - stock ticker symbol
- as_of (optional) - date for snapshot, defaults to today
- force_refresh (optional) - ignore existing data and refresh

## Hard Dependencies
None - this is the chain start

## Outputs
- company/{TICKER}/company.yaml
- company/{TICKER}/current/analysis_data/market_snapshot.yaml
- company/{TICKER}/current/gaps/artifacts_state.yaml
- company/{TICKER}/runs/{run_id}/result.yaml

## Skip Conditions
- company.yaml exists with valid cik AND NOT force_refresh -> skip identity
- market_snapshot.yaml as_of equals today AND all fields present -> skip market

## Blocked Conditions
- sec_edgar_mcp returns no CIK AND fallback fails -> status=blocked
- trading_mcp completely unavailable -> status=partial (can still have identity)

## Definition of Done
After running on any ticker (e.g., AAPL):
- company/{TICKER}/company.yaml exists with cik field populated
- company/{TICKER}/current/analysis_data/market_snapshot.yaml exists with price and shares_outstanding
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
sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

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
    market_path = paths.get_analysis_data_dir(ticker) / "market_snapshot.yaml"
    existing_market = atomic_io.load_yaml(market_path)
    market_skipped = False

    if existing_market.get("as_of") == str(as_of) and existing_market.get("price") and not force_refresh:
        market_skipped = True
        market_data = existing_market
        print(f"Market skipped - as_of={as_of} exists with price")
    else:
        market_data = {
            "as_of": str(as_of),
            "currency": "USD",
            "price": None,
            "shares_outstanding": None,
            "shares_float": None,
            "market_cap": None,
            "enterprise_value": None,
            "source": "mixed:alpaca.get_stock_latest_trade+yfinance.get_stock_info",
        }
        print("TODO: Call alpaca.get_stock_latest_trade + trading_mcp/SEC/Yahoo")

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
        outputs=["company.yaml", "current/analysis_data/market_snapshot.yaml"],
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

## 三、Skill 2: sec-ingest-and-materialize-events

> **替代旧 Skill2 `collect-company-facts`**

### 3.1 SKILL.md

```markdown
---
name: sec-ingest-and-materialize-events
description: "Ingest SEC filings to raw evidence store and materialize events with canonical buckets. Use when building or updating SEC evidence pool for any ticker."
revision: "<YYYY-MM-DD>"
---

# sec-ingest-and-materialize-events

## Purpose
**SEC Evidence Ingestion + Event Materialization Layer** supporting valuation chain:

Two core responsibilities:
1. **Raw ingest**: Download and archive SEC filings as immutable evidence (raw/)
2. **Event materialize**: Classify filings into events with canonical buckets (events/)

Two modes (auto-detected):
- **Init mode**: target files don't exist → full backfill (lookback_years)
- **Maintenance mode**: target files exist → incremental update

Data layering contract:
- `raw/` is immutable evidence (per-accession, original filenames preserved)
- `events/` is the event-level query layer (taxonomy + canonical buckets)
- `current/gaps/` tracks what's missing or incomplete

## MCP Tools
- sec_edgar_mcp.get_cik_by_ticker - resolve CIK
- sec_edgar_mcp.get_company_info - company details
- sec_edgar_mcp.get_recent_filings - list filings by form/date range
- sec_edgar_mcp.get_filing_content - get filing text
- sec_edgar_mcp.get_filing_sections - get specific sections
- fs - read/write files

## Inputs

### Core
- `ticker` (required)
- `as_of` (optional, default today)
- `force_refresh` (optional, default false)

### Window Parameters
- `lookback_years` (default 10) - Init mode backfill
- `overlap_days` (default 2) - Maintenance mode overlap

### SEC VMF Parameters
- `vmf_score_threshold` (default 8)
- `vmf_annual_budget` (default 20)

## Hard Dependencies
- `company/{TICKER}/company.yaml` with valid `cik` and `fiscal_year_end`

## Outputs

### Raw layer
- `raw/sec/accessions/{accession}/meta.yaml`
- `raw/sec/accessions/{accession}/manifest.yaml`
- `raw/sec/accessions/{accession}/index/` (index.json, {accession}-index.html)
- `raw/sec/accessions/{accession}/submission/{accession}.txt`
- `raw/sec/accessions/{accession}/documents/` (primary doc + other docs)
- `raw/sec/accessions/{accession}/exhibits/` (EX-*, excluding EX-101.*)
- `raw/sec/accessions/{accession}/xbrl/` (XBRL package if has_xbrl)

### Events layer
- `events/sec/ingest_state.yaml`
- `events/sec/filings_index.parquet`
- `events/sec/events_index.parquet`
- `events/sec/events/{event_id}/event.yaml`
- `events/sec/events/{event_id}/raw_refs.json`
- `events/sec/events/{event_id}/bucket_manifest.json`
- `events/sec/events/{event_id}/{bucket}/...`

### Gaps
- `current/gaps/artifacts_state.yaml` (updated)
- `current/gaps/missing_data.yaml` (if gaps detected)

## Mode Detection Logic
```python
filings_parquet = events_sec_dir / "filings_index.parquet"
if not filings_parquet.exists() or force_refresh:
    mode = "init"
    fetch_start = as_of - timedelta(days=lookback_years * 365)
else:
    mode = "maintenance"
    existing_df = pd.read_parquet(filings_parquet)
    last_filed_at = existing_df["filed_at"].max()
    fetch_start = last_filed_at - timedelta(days=overlap_days)
```

## Internal Steps

### Step 0 - Init + identity check
1. Ensure ticker directory structure exists
2. Load company.yaml, verify cik + fiscal_year_end
3. If cik missing → blocked
4. Determine issuer_type (domestic vs fpi)

### Step 1 - SEC raw ingest
1. Determine mode (init/maintenance)
2. Fetch periodic core filings + event stream filings
3. For each accession:
   a. Download index.json + {accession}-index.html
   b. Parse doc table → build meta.yaml (documents list with category classification)
   c. Route files to documents/ / exhibits/ / xbrl/ / other/
   d. Download submission.txt
   e. Write manifest.yaml with completeness checks
4. Apply VMF to event stream filings

### Step 2 - Event taxonomy classification
1. Classify each filing into one of 12 taxonomy categories
2. For 6-K: apply strict "period AND results" rule
3. Generate event_id per taxonomy rules
4. Group related filings into events (e.g., 10-K + its amendment)

### Step 3 - Event materialization (buckets)
1. For each event: build source document catalog from meta.yaml
2. Apply bucket mapping rules per taxonomy category
3. Financial report events: extract mdna/risk/business/notes/fs from appropriate source
4. Non-financial events: at minimum event_overview + exhibits_index
5. Write event.yaml, raw_refs.json, bucket_manifest.json, bucket contents

### Step 4 - Update indexes
1. Write events/sec/filings_index.parquet
2. Write events/sec/events_index.parquet
3. Write events/sec/ingest_state.yaml
4. Update current/gaps/artifacts_state.yaml

## Event Taxonomy (12 categories)
See stock_skills_buildplan_v2.md Section 五 for full taxonomy.

Key: financial_report | earnings_release_guidance | mna | financing_liquidity |
default_covenant | auditor_restatement | impairment_restructuring |
governance_management | capital_return_equity | legal_regulatory |
shareholder_meeting_proxy | other_material

## Canonical Buckets (16)
See stock_skills_buildplan_v2.md Section 六 for full bucket list.

## Blocked Conditions
- company.yaml missing cik → blocked
- SEC metadata unavailable AND no existing filings_index.parquet → blocked

## Partial Conditions
- Any accession raw download incomplete → partial
- Financial report event period_end unresolvable → partial + gap
- Key bucket missing for financial report → partial + gap

## Result Observability (components)
```yaml
components:
  sec_ingest:
    mode: init|maintenance
    window: {start: "...", end: "..."}
    totals: {filings_fetched: 0, accessions_new: 0, accessions_downloaded: 0}
    warnings: [...]
    errors: [...]
  events_materialize:
    totals: {events_upserted: 0, financial_report_events: 0}
    bucket_coverage: {mdna: 0.8, risk_factors: 0.9, ...}
```

## Definition of Done
- `events/sec/filings_index.parquet` with periodic + event filings
- `events/sec/events_index.parquet` with classified events
- `raw/sec/accessions/{accession}/` with meta + manifest + downloads
- Financial report events have buckets populated (mdna/risk/fs at minimum)
```

### 3.1.1 Artifact Ownership Matrix

| Artifact | Producer | Consumer | 用途 |
|---|---|---|---|
| `company/{TICKER}/company.yaml` | Skill1 | Skill2 | CIK/公司身份 |
| `current/analysis_data/market_snapshot.yaml` | Skill1 | Skill8 | 市场口径 |
| `raw/sec/accessions/{accession}/...` | Skill2 | Skill3 | 原始证据池 |
| `events/sec/filings_index.parquet` | Skill2 | Skill3 | filing 索引 |
| `events/sec/events_index.parquet` | Skill2 | Skill3/Phase2 | 事件索引 |
| `events/sec/events/{event_id}/...` | Skill2(buckets)/Skill3(structured_data) | Phase2 skills | 事件数据包 |
| `current/analysis_data/xbrl_atlas/*` | Skill3 | Skill4 | 全局 XBRL atlas |
| `current/analysis_data/economic/*` | Skill4 | Skill8 | 经济三表 |
| `current/analytics/diagnostics/*` | Skill5-7,9 | Skill8/9 | 诊断产物 |
| `current/outputs/value_state.yaml` | Skill8 | Skill9/编排器 | 估值底座总表 |

### 3.2 scripts/run.py

Implementation: see `.codex/skills/company_research/sec-ingest-and-materialize-events/scripts/run.py`

> Note: This is a complex skill requiring significant implementation. The run.py will orchestrate:
> 1. SEC filing discovery and raw download
> 2. Filing index page parsing and document classification
> 3. Event taxonomy classification
> 4. Bucket materialization (content extraction)
> 5. Index generation (filings_index.parquet, events_index.parquet)

---

## 四、Skill 3: xbrl-parse-financial-report-events

> **替代旧 Skill3 `extract-xbrl-timeseries`**

### 4.1 SKILL.md

```markdown
---
name: xbrl-parse-financial-report-events
description: "Parse XBRL from financial report events into per-event and global Statement Atlas. Use when building financial data foundation from SEC filings for recast."
revision: "<YYYY-MM-DD>"
---

# xbrl-parse-financial-report-events

## What This Skill Does
Per-event XBRL parsing + global atlas maintenance:
1. Read `events/sec/events_index.parquet`, filter `category=financial_report`
2. For each financial report event with XBRL:
   - Locate raw/xbrl files via event.yaml raw_refs
   - Deep parse XBRL/iXBRL (instance + linkbases)
   - Write per-event atlas to `events/sec/events/{event_id}/structured_data/xbrl_atlas/`
3. Merge all per-event results into global `current/analysis_data/xbrl_atlas/`
4. (Fallback) When local XBRL missing: use sec_edgar_mcp.get_financials as bootstrap

## MCP Tools
- fs - read/write files
- (fallback) sec_edgar_mcp.get_financials - get financial statements
- (fallback) sec_edgar_mcp.get_xbrl_concepts / discover_xbrl_concepts

## Inputs
- ticker (required)
- lookback_years (optional, default 10)
- force_refresh (optional)

## Hard Dependencies
- events/sec/events_index.parquet (with category=financial_report events)
- For target events: event.yaml + raw_refs pointing to raw/xbrl that exist
- company.yaml (for fiscal_period inference)

## Outputs

### Per-event
- events/sec/events/{event_id}/structured_data/xbrl_atlas/periods.yaml
- events/sec/events/{event_id}/structured_data/xbrl_atlas/facts.parquet
- events/sec/events/{event_id}/structured_data/xbrl_atlas/nodes.parquet
- events/sec/events/{event_id}/structured_data/xbrl_atlas/edges.parquet
- events/sec/events/{event_id}/structured_data/xbrl_atlas/paths.parquet

### Global (merged)
- current/analysis_data/xbrl_atlas/periods.yaml
- current/analysis_data/xbrl_atlas/facts.parquet
- current/analysis_data/xbrl_atlas/nodes.parquet
- current/analysis_data/xbrl_atlas/edges.parquet
- current/analysis_data/xbrl_atlas/paths.parquet

### Gaps
- current/gaps/missing_data.yaml (for events with missing/unparseable XBRL)

## Incremental Strategy
- Cache key: event's lineage.raw_manifest_sha256 + xbrl.instance_filename sha256
- Unchanged → skip per-event
- New/changed → parse incrementally
- Global atlas: append + deduplicate (by fact_id)

## Fallback Strategy (bootstrap)
Use SEC extracted statements (sec_edgar_mcp.get_financials) when local XBRL missing.
Must record as degraded path (facts may lack role_uri/context_id/dimensions).

## Blocked Conditions
- events_index missing → blocked, needs sec-ingest-and-materialize-events
- All target events have no parseable XBRL → blocked

## Partial Conditions
- Some events lack XBRL or instance unparseable → partial
- Linkbases incomplete (missing calculation/definition) → partial
- Fallback triggered → partial

## Definition of Done
- Per-event: at least one financial report event has facts.parquet with data
- Global: current/analysis_data/xbrl_atlas/facts.parquet exists with rows
- periods.yaml maps period_end → event_id → accession
```

### 4.2 scripts/run.py

```python
#!/usr/bin/env python3
"""xbrl-parse-financial-report-events skill runner."""
import sys
import re
import argparse
from datetime import date
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from company_research_runtime import (
    paths, atomic_io, runlog, artifacts_state, evidence
)

SKILL_NAME = "xbrl-parse-financial-report-events"

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
    })

    warnings = []
    outputs = []

    # Check dependency: events_index
    events_sec_dir = paths.get_events_sec_dir(ticker)
    events_index_path = events_sec_dir / "events_index.parquet"
    events_df = atomic_io.load_parquet(events_index_path)

    if events_df.empty:
        runlog.write_needs(run_dir,
            blocked_by=[{
                "artifact": "events/sec/events_index.parquet",
                "producer_skill": "sec-ingest-and-materialize-events",
                "reason": "events_index.parquet missing or empty"
            }],
            suggested_plan=["sec-ingest-and-materialize-events", SKILL_NAME])
        runlog.write_result(run_dir, ticker, SKILL_NAME, "blocked",
            missing=["events/sec/events_index.parquet"])
        print("BLOCKED: events_index.parquet missing")
        return {"status": "blocked"}

    # Filter financial report events
    fr_events = events_df[events_df["category"] == "financial_report"]

    if fr_events.empty:
        warnings.append("No financial_report events found in events_index")

    # Global atlas output dir
    global_atlas_dir = paths.get_analysis_data_dir(ticker) / "xbrl_atlas"
    global_atlas_dir.mkdir(parents=True, exist_ok=True)

    all_facts = []
    all_nodes = []
    all_edges = []
    all_paths = []
    all_periods = []

    # Process each financial report event
    for _, event_row in fr_events.iterrows():
        event_id = event_row.get("event_id")
        if not event_id:
            continue

        event_dir = paths.get_event_dir(ticker, event_id)
        event_yaml = atomic_io.load_yaml(event_dir / "event.yaml")

        # Check if XBRL available
        # TODO: Check raw_refs for XBRL files, parse if available
        # For now, use fallback via sec_edgar_mcp.get_financials

        print(f"  Processing event: {event_id}")
        # TODO: Implement per-event XBRL parsing or fallback

    # Build global atlas (placeholder - empty until implementation)
    facts_df = pd.DataFrame(all_facts) if all_facts else pd.DataFrame(columns=[
        "event_id", "fact_id", "period_end", "fiscal_period", "statement_type", "role_uri",
        "concept", "label", "value", "unit", "decimals", "accession", "context_id", "dimensions"
    ])

    nodes_df = pd.DataFrame(all_nodes) if all_nodes else pd.DataFrame(columns=[
        "node_id", "statement_type", "role_uri", "concept", "label", "depth", "order"
    ])

    edges_df = pd.DataFrame(all_edges) if all_edges else pd.DataFrame(columns=[
        "parent_node_id", "child_node_id", "arcrole", "weight"
    ])

    paths_df = pd.DataFrame(all_paths) if all_paths else pd.DataFrame(columns=[
        "node_id", "period_end", "statement_type", "path_str", "value", "accession", "event_id"
    ])

    # Save global atlas
    atomic_io.atomic_write_yaml(global_atlas_dir / "periods.yaml", {"periods": all_periods})
    atomic_io.atomic_write_parquet(global_atlas_dir / "nodes.parquet", nodes_df)
    atomic_io.atomic_write_parquet(global_atlas_dir / "edges.parquet", edges_df)
    atomic_io.atomic_write_parquet(global_atlas_dir / "facts.parquet", facts_df)
    atomic_io.atomic_write_parquet(global_atlas_dir / "paths.parquet", paths_df)

    outputs = [
        "current/analysis_data/xbrl_atlas/periods.yaml",
        "current/analysis_data/xbrl_atlas/nodes.parquet",
        "current/analysis_data/xbrl_atlas/edges.parquet",
        "current/analysis_data/xbrl_atlas/facts.parquet",
        "current/analysis_data/xbrl_atlas/paths.parquet",
    ]

    # Determine status
    if facts_df.empty:
        status = "partial"
        warnings.append("facts.parquet is empty - XBRL parsing needs implementation")
    else:
        status = "ok"

    artifacts_state.update_artifacts_state(ticker, "xbrl_atlas", status, run_id)

    result = runlog.write_result(run_dir, ticker, SKILL_NAME, status,
        outputs=outputs, warnings=warnings)

    print(f"\n=== Result: {status} ===")
    print(f"Atlas: {global_atlas_dir}")
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
revision: "<YYYY-MM-DD>"
---

# recast-economic-statements

## What This Skill Does
1. Map GAAP line items to economic concepts via label matching
2. Calculate core metrics: NOPAT, ROIC, FCF, Owner Earnings
3. Save recast_policy.yaml for traceability
4. Output economic_statements.parquet and core_metrics.parquet

## Hard Dependencies
- current/analysis_data/xbrl_atlas/facts.parquet
- current/analysis_data/xbrl_atlas/periods.yaml

## Outputs
- current/analysis_data/economic/recast_policy.yaml
- current/analysis_data/economic/economic_statements.parquet
- current/analysis_data/economic/core_metrics.parquet

## Blocked Conditions
- xbrl_atlas missing or facts.parquet empty -> blocked

## Partial Conditions
- CFO or capex not found -> partial, use fallback estimates

## Definition of Done
- core_metrics.parquet has at least one row with owner_earnings
- recast_policy.yaml shows mapping decisions
```

### 5.2 scripts/run.py

```python
#!/usr/bin/env python3
"""recast-economic-statements skill runner."""
import sys
import argparse
from datetime import date
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

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

def run(ticker: str, policy_version: str = "default", force_refresh: bool = False):
    ticker = ticker.upper()

    paths.ensure_dirs(ticker)
    run_id = paths.generate_run_id()
    run_dir = paths.get_run_dir(ticker, run_id)
    run_dir.mkdir(parents=True)

    runlog.write_meta(run_dir, ticker, SKILL_NAME, {
        "ticker": ticker, "policy_version": policy_version
    })

    warnings = []
    atlas_dir = paths.get_analysis_data_dir(ticker) / "xbrl_atlas"
    economic_dir = paths.get_analysis_data_dir(ticker) / "economic"
    economic_dir.mkdir(parents=True, exist_ok=True)

    # Check dependencies
    facts_path = atlas_dir / "facts.parquet"
    periods_path = atlas_dir / "periods.yaml"

    facts_df = atomic_io.load_parquet(facts_path)
    periods = atomic_io.load_yaml(periods_path)

    if facts_df.empty:
        runlog.write_needs(run_dir,
            blocked_by=[{
                "artifact": "analysis_data/xbrl_atlas/facts.parquet",
                "producer_skill": "xbrl-parse-financial-report-events",
                "reason": "facts.parquet is empty"
            }],
            suggested_plan=["xbrl-parse-financial-report-events", "recast-economic-statements"])

        runlog.write_result(run_dir, ticker, SKILL_NAME, "blocked",
            missing=["analysis_data/xbrl_atlas/facts.parquet with data"])
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
        "current/analysis_data/economic/recast_policy.yaml",
        "current/analysis_data/economic/economic_statements.parquet",
        "current/analysis_data/economic/core_metrics.parquet",
    ]

    if core_df.empty or core_df["owner_earnings"].isna().all():
        status = "partial"
        warnings.append("owner_earnings could not be calculated")
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
    parser.add_argument("--policy-version", default="default")
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
revision: "<YYYY-MM-DD>"
---

# valuation-and-margin-of-safety

## What This Skill Does (Phase 1)
1. Load market snapshot and core metrics
2. Calculate EPV and simplified DCF
3. Generate bear/base/bull valuation range
4. Output value_state.yaml and investment_memo.md

## Hard Dependencies (Phase 1)
- current/analysis_data/market_snapshot.yaml
- current/analysis_data/economic/core_metrics.parquet

Note: Phase 2 will add quality_coefficient.yaml dependency

## Outputs
- current/analytics/valuation/valuation.yaml
- current/analytics/valuation/valuation_model.csv
- current/outputs/value_state.yaml
- current/outputs/investment_memo.md

## Blocked Conditions
- market_snapshot.yaml missing price -> blocked
- core_metrics.parquet empty or no owner_earnings -> blocked

## Definition of Done
- value_state.yaml with margin_of_safety_base calculated
- investment_memo.md readable
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

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

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
        "ticker": ticker, "model_type": model_type, "implementation_id": "baseline"
    })

    warnings = []
    analysis_data_dir = paths.get_analysis_data_dir(ticker)
    valuation_dir = paths.get_analytics_dir(ticker) / "valuation"
    valuation_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir = paths.get_outputs_dir(ticker)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    # Load dependencies
    market = atomic_io.load_yaml(analysis_data_dir / "market_snapshot.yaml")
    core_df = atomic_io.load_parquet(analysis_data_dir / "economic" / "core_metrics.parquet")

    # Check blocked conditions
    if not market.get("price"):
        runlog.write_needs(run_dir,
            blocked_by=[{
                "artifact": "analysis_data/market_snapshot.yaml",
                "producer_skill": "company-foundation",
                "reason": "Missing price"
            }],
            suggested_plan=["company-foundation", "valuation-and-margin-of-safety"])
        runlog.write_result(run_dir, ticker, SKILL_NAME, "blocked",
            missing=["analysis_data/market_snapshot.yaml with price"])
        print("BLOCKED: Missing market price")
        return {"status": "blocked"}

    if core_df.empty or "owner_earnings" not in core_df.columns:
        runlog.write_needs(run_dir,
            blocked_by=[{
                "artifact": "analysis_data/economic/core_metrics.parquet",
                "producer_skill": "recast-economic-statements",
                "reason": "Missing owner_earnings"
            }],
            suggested_plan=["recast-economic-statements", "valuation-and-margin-of-safety"])
        runlog.write_result(run_dir, ticker, SKILL_NAME, "blocked",
            missing=["analysis_data/economic/core_metrics.parquet with owner_earnings"])
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
            "net_cash_per_share": None,
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
            "memo": "current/outputs/investment_memo.md",
            "valuation_yaml": "current/analytics/valuation/valuation.yaml",
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

## Phase 1 Notice
Using conservative defaults. Full analysis requires:
- profit-quality-and-risk
- growth-driver-explorer
- moat-inferencer
- cross-examination-audit

---
*Generated by valuation-and-margin-of-safety*
"""

    # Save outputs
    atomic_io.atomic_write_yaml(valuation_dir / "valuation.yaml", valuation_yaml)
    atomic_io.atomic_write_yaml(outputs_dir / "value_state.yaml", value_state)
    atomic_io.atomic_write_text(outputs_dir / "investment_memo.md", memo)

    # Valuation model CSV
    model_df = pd.DataFrame([
        {"scenario": s, "epv": epv_scenarios[s], "dcf": dcf_scenarios[s],
         "combined": intrinsic_values[s], "per_share": iv_per_share[s], "mos": margin_of_safety[s]}
        for s in ["bear", "base", "bull"]
    ])
    model_df.to_csv(valuation_dir / "valuation_model.csv", index=False)

    skill_outputs = [
        "current/analytics/valuation/valuation.yaml",
        "current/analytics/valuation/valuation_model.csv",
        "current/outputs/value_state.yaml",
        "current/outputs/investment_memo.md",
    ]

    status = "ok" if not warnings else "partial"

    artifacts_state.update_artifacts_state(ticker, "valuation", status, run_id)

    evidence.append_evidence(ticker, SKILL_NAME,
        f"Valuation completed: IV_base=${iv_per_share['base']:.2f}, MOS_base={margin_of_safety['base']*100:.1f}%",
        confidence=0.5,
        sources=[{"type": "core_metrics"}, {"type": "market_snapshot"}])

    result = runlog.write_result(run_dir, ticker, SKILL_NAME, status,
        outputs=skill_outputs, warnings=warnings)

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
    "sec-ingest-and-materialize-events",
    "xbrl-parse-financial-report-events",
    "recast-economic-statements",
    "valuation-and-margin-of-safety",
]

EXPECTED_OUTPUTS = {
    "company-foundation": [
        "company.yaml",
        "current/analysis_data/market_snapshot.yaml",
    ],
    "sec-ingest-and-materialize-events": [
        "events/sec/filings_index.parquet",
        "events/sec/events_index.parquet",
    ],
    "xbrl-parse-financial-report-events": [
        "current/analysis_data/xbrl_atlas/facts.parquet",
        "current/analysis_data/xbrl_atlas/periods.yaml",
    ],
    "recast-economic-statements": [
        "current/analysis_data/economic/core_metrics.parquet",
    ],
    "valuation-and-margin-of-safety": [
        "current/outputs/value_state.yaml",
        "current/outputs/investment_memo.md",
    ],
}

def check_outputs(ticker: str, skill: str) -> dict:
    company_dir = BASE_PATH / "company" / ticker.upper()
    results = {"skill": skill, "ticker": ticker, "outputs": {}}

    for output in EXPECTED_OUTPUTS.get(skill, []):
        path = company_dir / output
        results["outputs"][output] = path.exists()

    results["all_present"] = all(results["outputs"].values())
    return results

def run_skill(ticker: str, skill: str) -> bool:
    script = SKILLS_DIR / skill / "scripts" / "run.py"
    if not script.exists():
        print(f"  Script not found: {script}")
        return False

    try:
        result = subprocess.run(
            [sys.executable, str(script), ticker],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            print(f"  Non-zero exit: {result.returncode}")
            print(f"     stderr: {result.stderr[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print(f"  Timeout")
        return False
    except Exception as e:
        print(f"  Error: {e}")
        return False

def generate_summary(tickers: list):
    records = []
    for ticker in tickers:
        vs_path = BASE_PATH / "company" / ticker.upper() / "current" / "outputs" / "value_state.yaml"
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
        print(f"\nSaved: {output}")
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
            status = "OK" if check["all_present"] else "FAIL"
            print(f"    {status} Outputs: {check['outputs']}")

            all_results.append({
                "ticker": ticker, "skill": skill,
                "run_success": success, "outputs_present": check["all_present"],
            })

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    for ticker in tickers:
        ticker_results = [r for r in all_results if r["ticker"] == ticker]
        all_ok = all(r["outputs_present"] for r in ticker_results)
        status = "PASS" if all_ok else "FAIL"
        print(f"{ticker}: {status}")

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
Step 0: 创建目录结构
  mkdir -p .codex/skills/company_research/{company-foundation,sec-ingest-and-materialize-events,xbrl-parse-financial-report-events,recast-economic-statements,valuation-and-margin-of-safety}/{scripts,references}
  mkdir -p company_research_runtime
  mkdir -p /home/help/mcp/work/company_research/company

Step 1: 部署共享 runtime
  - company_research_runtime/__init__.py
  - company_research_runtime/paths.py (v2.1 with raw/events/current helpers)
  - company_research_runtime/atomic_io.py (added atomic_write_json, atomic_write_text)
  - company_research_runtime/runlog.py (added components param)
  - company_research_runtime/artifacts_state.py (gaps/ path)
  - company_research_runtime/evidence.py (analytics/evidence/ path)
  - company_research_runtime/hashing.py

Step 2: 部署 Skill 1 - company-foundation
  - SKILL.md
  - scripts/run.py
  - 测试: codex "Initialize AAPL research"

Step 3: 部署 Skill 2 - sec-ingest-and-materialize-events
  - SKILL.md
  - scripts/run.py (complex - SEC raw ingest + event taxonomy + buckets)
  - 测试: 验证 events/sec/filings_index.parquet + events_index.parquet

Step 4: 部署 Skill 3 - xbrl-parse-financial-report-events
  - SKILL.md
  - scripts/run.py
  - 测试: 验证 per-event + global xbrl_atlas

Step 5: 部署 Skill 4 - recast-economic-statements
  - SKILL.md
  - scripts/run.py
  - 测试: 验证 core_metrics.parquet

Step 6: 部署 Skill 5 - valuation-and-margin-of-safety
  - SKILL.md
  - scripts/run.py
  - 测试: 验证 value_state.yaml 和 investment_memo.md

Step 7: 端到端 smoke test
  python smoke_test_phase1.py AAPL MSFT
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

### 链式执行

```bash
codex "Run full Phase 1 analysis for AAPL"
# Codex 会识别需要按顺序执行 1→2→3→4→5
```

### Canonical commands

```bash
# 1) Identity + market snapshot
python .codex/skills/company_research/company-foundation/scripts/run.py AAPL

# 2) SEC raw ingest + event materialization
python .codex/skills/company_research/sec-ingest-and-materialize-events/scripts/run.py AAPL

# 3) XBRL parsing -> per-event + global atlas
python .codex/skills/company_research/xbrl-parse-financial-report-events/scripts/run.py AAPL

# 4) GAAP -> economic statements
python .codex/skills/company_research/recast-economic-statements/scripts/run.py AAPL

# 5) Valuation + margin of safety
python .codex/skills/company_research/valuation-and-margin-of-safety/scripts/run.py AAPL --model-type hybrid
```

---

**文档版本**: v2.1 (raw/events 解耦 + event taxonomy + canonical buckets)
**更新日期**: 2026-03-02
**关键改进**:
- raw/events/current 三层架构解耦
- 事件 taxonomy 12 分类 + canonical buckets 16 个
- Skill2 重命名为 sec-ingest-and-materialize-events
- Skill3 重命名为 xbrl-parse-financial-report-events（per-event + global atlas）
- 所有路径更新到新架构（analysis_data/analytics/gaps/outputs）
