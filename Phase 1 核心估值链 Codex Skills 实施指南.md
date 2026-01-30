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
    ├── company.yaml                       # Skill1: 公司身份信息
    ├── latest.json                        # 最新运行状态快照
    │
    ├── current/                           # 当前状态层（可查询）
    │   ├── artifacts_state.yaml           # 产物状态追踪
    │   ├── evidence.jsonl                 # 证据账本
    │   ├── questions.jsonl                # 待解问题
    │   ├── market_snapshot.yaml           # Skill1: 市场数据快照
    │   │
    │   │   # --- SEC 证据池 ---
    │   ├── filings_index.yaml             # 契约文件（含VMF筛选字段 + FPI 6-K 归类结果）
    │   ├── filings_index.parquet          # 分析层（同schema）
    │   │
    │   │   # --- 下游 Skills ---
    │   ├── xbrl_atlas/                    # Skill3: 报表图谱
    │   ├── economic/                      # Skill4: 经济报表
    │   ├── diagnostics/                   # 诊断信息
    │   └── valuation/                     # Skill5: 估值结果
    │
    ├── raw/                               # 原始材料层（不可变、可追溯）
    │   ├── sec/                           # SEC filings
    │   │   └── {accession}/               # 每个 filing 一个目录
    │   │       ├── meta.yaml              # 元数据（含VMF信息）
    │   │       ├── manifest.yaml          # 下载清单 + hash + 完整性
    │   │       ├── primary_document.html  # 主文档
    │   │       ├── primary_document.txt   # 纯文本版
    │   │       ├── sections/              # 关键段落（从主文档抽取）
    │   │       │   ├── mdna.md
    │   │       │   ├── risk_factors.md
    │   │       │   └── business.md
    │   │       ├── xbrl/                  # XBRL 包（周期性filing）
    │   │       │   ├── *.xml
    │   │       │   └── *.xsd
    │   │       └── exhibits/              # 高价值附件（VMF筛选）
    │   │           ├── exhibit_99_1.html  # 新闻稿/业绩公告
    │   │           ├── exhibit_10_1.html  # 重大合同
    │   │           └── exhibit_2_1.html   # 并购协议
    │
    └── runs/{run_id}/                     # 运行记录
        ├── meta.yaml                      # 输入参数
        ├── result.yaml                    # 运行结果
        ├── needs.yaml                     # blocked时的依赖说明
        └── outputs/                       # 本次产物快照
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
revision: "<YYYY-MM-DD>"   # optional
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
- yfinance.get_stock_info - fallback shares / marketCap / EV (ADRs may need FX conversion)
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
annual = sec_edgar_mcp.get_recent_filings(identifier=ticker, form_type="10-K", days=3650, limit=1)

company_data = {
    "ticker": ticker.upper(),
    "company_name": company_info.get("name"),
    "cik": cik_result.get("cik"),
    # Prefer Alpaca asset exchange; SEC company_info.exchange is often null
    "exchange": normalize_exchange(alpaca.get_asset(symbol=ticker).get("exchange")) if use_alpaca else None,
    "sic": company_info.get("sic"),
    # Prefer annual filing period_of_report (10-K / 20-F / 40-F) to infer fiscal year end MM-DD
    "fiscal_year_end": extract_mm_dd(annual["filings"][0]["period_of_report"]) if annual.get("filings") else None,
    "currency": "USD",
}
```

### Step 4 - Fetch market snapshot (USD) via multi-source chain
```python
trade = alpaca.get_stock_latest_trade(symbol_or_symbols=ticker)
yahoo = yfinance.get_stock_info(ticker=ticker)

market_snapshot = {
    "as_of": str(as_of),
    "currency": "USD",
    "price": trade.get("price") or yahoo.get("regularMarketPrice"),
    "shares_outstanding": yahoo.get("sharesOutstanding"),
    "shares_float": yahoo.get("floatShares"),  # may be null (ADRs might be inconsistent)
    # market_cap: keep source value by default; cross-check vs price*shares_outstanding and only switch on large divergence
    "market_cap": yahoo.get("marketCap"),
    # enterprise_value: for ADRs, enterpriseValue may be in financialCurrency (e.g., CNY); require FX payload to normalize
    "enterprise_value": yahoo.get("enterpriseValue"),
    "source": "mixed:alpaca.get_stock_latest_trade+yfinance.get_stock_info",
}
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
            "currency": "USD",
            "price": None,
            "shares_outstanding": None,
            "shares_float": None,
            "market_cap": None,
            "enterprise_value": None,
            "source": "mixed:alpaca.get_stock_latest_trade+yfinance.get_stock_info",
        }
        print("TODO: Call alpaca.get_stock_latest_trade + trading_mcp/SEC/Yahoo for shares/marketCap/EV")
    
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

## 三、Skill 2: collect-company-facts（SEC 证据池 + VMF）

### 3.1 SKILL.md

```markdown
---
name: collect-company-facts
description: "Ingest and maintain SEC evidence pool for a ticker: filings + raw/sec snapshots + filings_index under current/. Supports init and maintenance modes."
revision: "<YYYY-MM-DD>"   # optional
---

# collect-company-facts

## Purpose
**SEC Evidence Ingestion + Maintenance Layer** supporting valuation chain:
- Periodic core filings (Domestic: 10-K/10-Q/DEF14A; FPI/MJDS: 20-F/40-F + 6-K interim results) + XBRL for reconstruction
- Event filings (Domestic: 8-K; FPI: 6-K other) filtered by VMF, for quality/uncertainty signals

Two modes (auto-detected):
- **Init mode**: target files don't exist → full backfill (lookback_years)
- **Maintenance mode**: target files exist → incremental update anchored to last indexed filed_at with overlap_days backfill

Data layering contract (raw vs index):
- `raw/` is immutable replay store (per-accession directories)
- `current/*` (index) is query/analysis layer: keys must exist; values may be null; Parquet timestamps are enforced as UTC typed columns
- Write path follows “runs → promote”: write `runs/{run_id}/outputs/current/*` first, then atomically replace `current/*`

## Inputs

### Core
- `ticker` (required)
- `as_of` (optional, default today)
- `force_refresh` (optional, default false)

### Window Parameters
- `lookback_years` (default 10) - Init mode: SEC backfill years
- `overlap_days` (default **2**) - Maintenance mode: anchor backfill overlap days (anchor = max filed_at)

### SEC VMF Parameters
- `vmf_score_threshold` (default 8) - Score threshold for event download
- `vmf_annual_budget` (default 20) - Max events per year (hard triggers exempt)
- `download_sections` (default true) - Prefer local parse from persisted `primary_document.html`; do not rely on `get_filing_sections` for 10-Q/20-F/40-F

**Section extraction（本地解析）**：
- 数据源：`raw/sec/{accession}/primary_document.html`（按 SEC Archives `index.json` 落盘的主文档；必要时可用 `primary_document.txt` 辅助）。
- 规则（best-effort）：
  - `10-K`：优先抽取 `Item 7 (MD&A)`、`Item 1A (Risk Factors)`、`Item 1 (Business)`
  - `10-Q`：优先抽取 `Part I Item 2 (MD&A)`、`Part II Item 1A (Risk Factors)`
  - `20-F/40-F`：优先抽取 `Item 5 (Operating and Financial Review and Prospects)`、`Item 3.D (Risk Factors)`（40-F 往往以年报/附表形式存在，best-effort）
- 输出：写入 `raw/sec/{accession}/sections/{mdna.md,risk_factors.md,business.md}`；未命中则允许缺失并在 manifest/warnings 记录原因。

**XBRL 落盘（不做成开关）**：
- 对 Periodic Core（10-K/10-Q/20-F/40-F/6-K-Periodic）若 `has_xbrl=true`：下载并“解包式”落盘 as-filed XBRL 文件集到 `raw/sec/{accession}/xbrl/`（instance + `.xsd` + linkbases），优先不保留 `*-xbrl.zip`。

## Hard Dependencies
- `company/{TICKER}/company.yaml` with valid `cik`

## Outputs

### SEC
- `current/filings_index.yaml` - contract file (issuer_type + sixk_classifier_version + vmf_version)
- `current/filings_index.parquet` - analysis layer
- `raw/sec/{accession}/...` - meta + manifest + primary doc (+ sections) (+ `xbrl/` for periodic filings) (+ exhibits per VMF)
- `current/events_index.parquet` - candidate SEC events pointers (`sec:{accession}`; not evidence claims)

## Mode Detection Logic

```python
# SEC
if not filings_index.yaml exists or force_refresh:
    mode = "init"
    fetch_start = as_of - timedelta(days=lookback_years * 365)
else:
    mode = "maintenance"
    last_filed_at = max_date(filings_index.filings[].filed_at)  # latest filed_at in current index
    fetch_start = last_filed_at - timedelta(days=overlap_days)

fetch_end = as_of
sec_days = (fetch_end - fetch_start).days + 1
```

## Blocked Conditions
- company.yaml missing cik → blocked
- SEC metadata unavailable AND no existing filings_index → blocked

## Definition of Done
- `filings_index.yaml/parquet` with periodic filings + VMF-indexed events
- `raw/sec/{accession}/` with meta + manifest (+ downloads per VMF)
- `events_index.parquet` (SEC event candidates pointers; not evidence claims)

## Result Observability (components)
`runs/{run_id}/result.yaml` SHOULD include `components` for orchestrator/debug:

```yaml
components:
  sec: {status, mode, window, totals, warnings, errors}
```

Rollup (orchestrator-friendly):
1) `sec.status in {blocked, error}` → skill `status = blocked/error`
2) `sec.status=partial` → skill `status = partial`
3) `sec.status=skipped` → skill `status = skipped`
4) `sec.status=ok` → skill `status = ok`

```

### 3.1.1 Artifact Ownership Matrix（产物归属与依赖）

| Artifact | Producer | Consumer（典型） | 用途 |
|---|---|---|---|
| `company/{TICKER}/company.yaml` | Skill1 `company-foundation` | Skill2 `collect-company-facts` | CIK/公司身份（SEC 抓取前置条件） |
| `company/{TICKER}/current/market_snapshot.yaml` | Skill1 `company-foundation` | Skill5 `valuation-and-margin-of-safety` | 市场口径（price/shares/EV 等） |
| `company/{TICKER}/current/filings_index.yaml` + `.parquet` | Skill2 `collect-company-facts` | Skill3 `extract-xbrl-timeseries` / Skill5 `valuation-and-margin-of-safety` | SEC 索引（含 bucket、6-K 分类、VMF、download 状态） |
| `company/{TICKER}/raw/sec/{accession}/...` | Skill2 `collect-company-facts` | Skill3 `extract-xbrl-timeseries` | 原始证据池（可回放/可追溯） |
| `company/{TICKER}/current/events_index.parquet` | Skill2 `collect-company-facts` | Phase2 分析类 skills（growth/audit/moat 等） | 事件候选池（可追溯指针 + 初筛标签；用于后续生成 evidence claims） |
| `company/{TICKER}/current/xbrl_atlas/*` | Skill3 `extract-xbrl-timeseries` | Skill4 `recast-economic-statements` | XBRL 报表图谱与 facts 底座 |
| `company/{TICKER}/current/economic/*` | Skill4 `recast-economic-statements` | Skill5 `valuation-and-margin-of-safety` | 经济三表与核心指标（ROIC/FCF 等） |
| `company/{TICKER}/current/valuation/*` | Skill5 `valuation-and-margin-of-safety` | 下游决策/报告 | 估值输出（value_state 等） |

### 3.2 scripts/run.py

Implementation: see `.codex/skills/company_research/collect-company-facts/scripts/run.py` (SEC-only for Phase 1; News/Papers deferred to a future evidence DB/MCP).


## 四、Skill 3: extract-xbrl-timeseries

### 4.1 SKILL.md

```markdown
---
name: extract-xbrl-timeseries
description: "Extract XBRL data into Statement Atlas with facts.parquet nodes edges paths. Use when building financial data foundation from SEC filings for recast."
revision: "<YYYY-MM-DD>"   # optional
---

# extract-xbrl-timeseries

## What This Skill Does
Build Statement Atlas（树 + facts + 溯源）：
1. 从 `current/filings_index.yaml` 选取 `has_xbrl=true` 的周期性 filings（10-K/10-Q/20-F/40-F/6-K-Periodic）
2. 解析 `raw/sec/{accession}/xbrl/` 的 as-filed XBRL（instance + `.xsd` + linkbases）
3. 产出完整的 `current/xbrl_atlas/*`（facts/nodes/edges/paths/periods）
4. （可选降级）当本地 XBRL 缺失或解析失败时，可用 SEC “已抽取”XBRL / `sec_edgar_mcp.get_financials` 做 bootstrap，但必须在 result/manifest 中记录降级原因

## MCP Tools
- fs - read/write files
- (fallback) sec_edgar_mcp.get_financials - get financial statements
- (fallback) sec_edgar_mcp.get_xbrl_concepts / discover_xbrl_concepts - extracted facts/concepts

## Inputs
- ticker (required)
- lookback_years (optional, default 10)
- force_refresh (optional)

## Hard Dependencies
- current/filings_index.yaml with `has_xbrl=true` periodic filings
- raw/sec/{accession}/xbrl/ materialized by Skill2/downloader (preferred)

## Outputs
- current/xbrl_atlas/periods.yaml
- current/xbrl_atlas/nodes.parquet
- current/xbrl_atlas/edges.parquet
- current/xbrl_atlas/facts.parquet
- current/xbrl_atlas/paths.parquet

## Fallback Strategy（bootstrap via extracted statements）
Use SEC “已抽取”的结构化报表（例如 `sec_edgar_mcp.get_financials`）替代本地 as-filed XBRL 解析。
仅用于兜底/快速跑通；必须记录为降级路径（facts 的 `role_uri/context_id/dimensions` 可能缺失）。

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
        "role_uri": None,  # Not available in fallback path
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

## Primary Strategy（as-filed XBRL parsing）
Use本地 as-filed XBRL（由 Skill2/downloader 通过 SEC Archives `index.json` 落盘）：
1. 逐个 accession 读取 `raw/sec/{accession}/xbrl/`
2. 识别 instance：
   - iXBRL 常见 `*_htm.xml`（文件名不一定与 `.xsd` 同名）
   - 传统 XBRL 常见 `{stem}.xml`
3. 解析 instance facts：`concept/name` + `contextRef` + `unitRef` + `decimals` + `value`
4. 解析 schema/linkbases：
   - `*_pre.xml`（presentation）→ 报表树（nodes/edges + `role_uri`）
   - `*_cal.xml`（calculation）→ 加总关系（用于校验/一致性）
   - `*_def.xml`（definition）→ 维度/成员（写入 facts 的 `dimensions`）
   - `*_lab.xml`（label）→ 标签（facts.label 与 nodes.label）
5. 产出 Statement Atlas：`facts/nodes/edges/paths/periods`，保留 `accession` 溯源

## Blocked Conditions
- filings_index.yaml missing -> blocked, needs collect-company-facts
- 对所有候选 periods 都找不到可解析的本地 XBRL instance -> blocked（除非显式启用 fallback）

## Partial Conditions
- 部分 accession 缺 XBRL 或 instance 不可解析 -> partial（其余 periods 继续产出）
- linkbases 不完整（例如缺 calculation/definition）-> partial（树/维度信息不完整）
- 触发 fallback -> partial，并在 result/manifest 记录降级原因
```

### 4.2 scripts/run.py

```python
#!/usr/bin/env python3
"""extract-xbrl-timeseries skill runner."""
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
        "implementation_id": "bootstrap_get_financials",
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
revision: "<YYYY-MM-DD>"   # optional
---

# recast-economic-statements

## What This Skill Does
1. Map GAAP line items to economic concepts via label matching
2. Calculate core metrics: NOPAT, ROIC, FCF, Owner Earnings
3. Save recast_policy.yaml for traceability
4. Output economic_statements.parquet and core_metrics.parquet

## MCP Tools
- fs - read/write files

## Inputs
- ticker (required)
- policy_version (optional, default "default")
- force_refresh (optional)

## Hard Dependencies
- current/xbrl_atlas/facts.parquet
- current/xbrl_atlas/periods.yaml

## Outputs
- current/economic/recast_policy.yaml
- current/economic/economic_statements.parquet
- current/economic/core_metrics.parquet

## Strategy（best-effort label matching）
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
    "policy_version": "default",
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
revision: "<YYYY-MM-DD>"   # optional
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
            # net_debt is intentionally not part of market_snapshot.yaml (derive later from filings/economic layer).
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
*Generated by valuation-and-margin-of-safety*
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
    "collect-company-facts": ["current/filings_index.yaml"],
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

□ Step 4: 部署 Skill 3 - extract-xbrl-timeseries
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
- 优先 as-filed XBRL 解析；必要时允许 fallback（需记录降级原因）
- 项目级 Skills（git 可管理）
- sec_edgar_mcp 具体工具映射
