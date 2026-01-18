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
    │   │   # --- News 证据池 ---
    │   ├── news_digest.yaml               # 摘要 + 高影响事件
    │   ├── news_index.parquet             # 标准化索引
    │   │
    │   │   # --- Papers 证据池 ---
    │   ├── papers_digest.yaml             # 摘要
    │   ├── papers_index.parquet           # 标准化索引
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
    │   │       ├── sections/              # 关键段落
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
    │   │
    │   ├── news/                          # 新闻（按日分区，gzip压缩）
    │   │   └── YYYY/MM/news_YYYY-MM-DD.jsonl.gz
    │   │
    │   └── papers/                        # 论文（按月分区，gzip压缩）
    │       └── YYYY/MM/papers_YYYY-MM.jsonl.gz
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
    # Prefer annual filing period_of_report (10-K / 20-F) to infer fiscal year end MM-DD
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

## 三、Skill 2: collect-company-facts (v0.3 VMF版)

### 3.1 SKILL.md

```markdown
---
name: collect-company-facts
description: "Ingest and maintain evidence pool for a ticker: SEC filings with XBRL and VMF-filtered events, news, papers. Produces partitioned raw stores plus searchable indices and digests. Supports init and maintenance modes."
version: v0.3
---

# collect-company-facts

## Purpose
**Evidence Ingestion + Maintenance Layer** supporting valuation chain:
- **Profit side**: periodic filings (Domestic: 10-K/10-Q; FPI: 20-F + 6-K interim results) + XBRL for financial reconstruction
- **Quality/uncertainty side**: event filings (Domestic: 8-K; FPI: 6-K other) filtered by VMF, governance (DEF14A), news and papers

Two modes (auto-detected):
- **Init mode**: target files don't exist → full backfill
- **Maintenance mode**: target files exist → incremental update anchored to last indexed date (SEC: max filed_at; News: max published_at) with overlap_days backfill; Papers uses staleness/refresh_days

Data layering contract (raw vs index/digest):
- `raw/` is immutable replay store: **source + envelope only** (no regenerable fields like tags/score/classification)
- `current/*` (index/digest) is query/summary layer: **keys must exist; values may be null**; Parquet timestamps are enforced as **UTC typed** columns
- Index/Digest write path follows “runs → promote”: write `runs/{run_id}/outputs/current/*` first, then atomically replace `current/*` (Raw is append-only evidence store).
- Raw `.jsonl.gz` append uses `gzip.open(..., "at")` which creates multi-member gzip; most readers handle it, but some strict readers may only read the first member. Phase1 ok; for strict compatibility prefer “per-day/per-run new files → later compaction”.
- Phase1 News relevance: produce deterministic, explainable, versioned `relevance_score` in index (aboutness, not impact), with `relevance_version` + `relevance_reasons` (later skills may override/recompute).

## Inputs

### Core
- `ticker` (required)
- `as_of` (optional, default today)
- `force_refresh` (optional, default false)

### Window Parameters
- `lookback_years` (default 10) - Init mode: SEC backfill years
- `lookback_days_news` (default 5) - Init mode: news backfill days
- `lookback_days_papers` (default 180) - Init mode: papers backfill days
- `overlap_days` (default **2**) - Maintenance mode: 以 anchor 回抓的 overlap 天数（SEC: max filed_at; News: max published_at，用于补齐间隔天数 + 防止边界缺失）
- `papers_refresh_days` (default 30) - Papers staleness threshold

### SEC VMF Parameters
- `vmf_score_threshold` (default 8) - Score threshold for event download
- `vmf_annual_budget` (default 20) - Max events per year (hard triggers exempt)
- `download_xbrl` (default true)
- `download_sections` (default true)

### News Parameters
- `news_max_per_day` (default 100) - **每天**最大抓取数，防止长窗口漏数据
- `news_langs` (default ["en", "zh"]) - 语言过滤列表（支持多语言）

### Papers Parameters
- `papers_mode` (default auto) - auto|on|off
- `papers_max_results` (default 200)

## Hard Dependencies
- `company/{TICKER}/company.yaml` with valid `cik`

## Outputs

### SEC
- `current/filings_index.yaml` - 契约文件（含 issuer_type + sixk_classifier_version + vmf_version）
- `current/filings_index.parquet` - 分析层
- `raw/sec/{accession}/`
  - `meta.yaml` - filing元数据
  - `manifest.yaml` - 下载清单+hash
  - `primary_document.html`
  - `sections/` - MD&A/Risk Factors
  - `xbrl/` - XBRL包
  - `exhibits/` - 高价值附件（99.*/10.1/2.1）

### News
- `raw/news/YYYY/MM/news_YYYY-MM-DD.jsonl.gz` - 按日分区
- `current/news_digest.yaml` - 摘要
- `current/news_index.parquet` - 索引（含 `raw_path` 指针 + `relevance_score`/`relevance_version`/`relevance_reasons` + `impact_score`(Phase2；Phase1 默认 NaN)）

### Papers
- `raw/papers/YYYY/MM/papers_YYYY-MM.jsonl.gz` - 按月分区
- `current/papers_digest.yaml` - 摘要
- `current/papers_index.parquet` - 索引（含 `raw_path` 指针）

### Events Candidates（推荐）

> 候选事件指针池（不是 evidence claim/结论）。下游 Phase2 分析类 skills 从这里挑选事件并写入 `current/evidence.jsonl`。

- `current/events_index.parquet` - `news:{article_id}` / `sec:{accession}` 的候选事件索引（含 raw_path/local_dir 等可追溯指针）

## SEC Download Strategy: VMF (Valuation Materiality Filter)

### Periodic Core (10年全量下载)

| 发行人类型 | Forms |
|-----------|-------|
| Domestic | 10-K, 10-K/A, 10-Q, 10-Q/A, DEF14A |
| FPI | 20-F, 20-F/A, 6-K (Interim Financials/Results subset) |

下载内容：primary_document + xbrl + sections + meta + manifest
FPI 6-K（Interim Financials/Results）：必须下载 exhibits/99.*（结果公告/演示材料/摘要财务报表常在此承载）

### Event Stream (全量索引 + VMF筛选下载)

Forms: 8-K, 8-K/A (Domestic) | 6-K (FPI, excluding Interim Financials/Results subset)

#### FPI 6-K Split (Periodic vs Event)

- 6-K (Interim Financials/Results) → Periodic Core（10年全量下载）
- 6-K (Other events) → Event Stream（全量索引 + VMF 选择性下载）

v0.2 启发式（更严格：period AND results，避免把非财报 6-K 误判为 periodic）：
- Period 信号：three months ended / six months ended / quarter ended / quarter / half-year / interim report / interim financial statements / unaudited interim / q1-q4
- Results 信号：results / earnings / financial results / financial statements / interim results / unaudited / condensed consolidated
- 判定规则：(title/description 命中 period AND results) OR (exhibits 99.* 描述命中 period AND results)
- 明确禁止：guidance/outlook/presentation-only 不能判为 6-K-Periodic（它们只作为“已判为 periodic 后的结果包附带材料”下载）

Sanity checks（6-K 分类验收）：
- 6-K 标题/附件描述同时含“期间口径”与“财务结果/报表”，且常见 `exhibits 99.*` → 必须归入 6-K-Periodic（Periodic Core，全量下载）
- 6-K 仅为 monthly return/股本变动/治理等事项 → 必须归入 6-K-Event（Event Stream，走 VMF）
- 仅出现 presentation/guidance 但没有期间口径/中期报表信号 → 必须归入 6-K-Event（不能当 periodic）

VMF 仅作用于 Event Stream（Domestic: 8-K/8-K/A；FPI: 6-K-Event）。6-K-Periodic 不进入 VMF（已归入 Periodic Core）。

VMF三层筛选：

#### 层1 硬触发（不受预算限制）

**8-K Items**: 2.02(Earnings), 4.01/4.02(Auditor), 2.04(Default), 2.06(Impairment), 2.01(M&A)

**附件模式**: exhibit 99.*, "earnings release", "guidance", "investor presentation"

**关键词**: restatement, material weakness, default, covenant, bankruptcy, impairment, guidance

#### 层2 打分筛选

| 维度 | 关键词 | 权重 |
|------|--------|------|
| 现金流/融资 | liquidity, refinancing, default, covenant | 5 |
| 盈利/指引 | earnings, results, guidance, outlook | 4 |
| 财务质量 | restatement, auditor, material weakness | 3 |
| 资产质量 | impairment, restructuring | 3 |
| 并购 | acquisition, merger, disposition | 2 |

条件: score >= vmf_score_threshold (default 8)

#### 层3 年度预算

每年最多 vmf_annual_budget (default 20) 个事件；硬触发不受限

### 事件Filing下载内容

- primary_document.html - 永远下载
- exhibits/99.* - 永远下载
- exhibits/10.1 - 命中融资/M&A关键词时
- exhibits/2.1 - 命中2.01或M&A关键词时

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

# News
if not news_index.parquet exists or force_refresh:
    mode = "init"
    fetch_start = as_of - timedelta(days=lookback_days_news)
else:
    mode = "maintenance"
    last_published_at = max_date(news_index.published_at)        # timestamp in UTC
    fetch_start = last_published_at.date() - timedelta(days=overlap_days)

fetch_end = as_of
news_window_days = (fetch_end - fetch_start).days + 1

# Papers
if not papers_index.parquet exists or force_refresh:
    mode = "init"  # lookback_days_papers
else:
    if days_since_last >= papers_refresh_days:
        mode = "maintenance"
    else:
        mode = "skip"
```

## Blocked Conditions
- company.yaml missing cik → blocked
- SEC metadata unavailable AND no existing filings_index → blocked

## Definition of Done
- `filings_index.yaml/parquet` with periodic filings + VMF-indexed events
- `raw/sec/{accession}/` with meta + manifest (+ downloads per VMF)
- `news_index.parquet` + `news_digest.yaml`
- `papers_index.parquet` + `papers_digest.yaml`
- `events_index.parquet` (event candidates pointers; not evidence claims)

## Result Observability (components)
`runs/{run_id}/result.yaml` SHOULD include `components` for orchestrator/debug (without increasing skill count):

```yaml
components:
  sec: {status, mode, window, totals, warnings, errors}
  news: {status, mode, window, totals, warnings, errors}
  papers: {status, mode, reason, window, totals, warnings, errors}
```

Rollup (orchestrator-friendly):
1) `sec.status in {blocked, error}` → skill `status = blocked/error`
2) any other `*.status=error` → skill `status = partial`
3) any `*.status=partial` → skill `status = partial`
4) `sec ok` + `news ok` + `papers ok/skipped` → skill `status = ok`
5) all components skipped + input fingerprint unchanged → skill `status = skipped` (optional)
```

### 3.1.1 Artifact Ownership Matrix（产物归属与依赖）

| Artifact | Producer | Consumer（典型） | 用途 |
|---|---|---|---|
| `company/{TICKER}/company.yaml` | Skill1 `company-foundation` | Skill2 `collect-company-facts` | CIK/公司身份（SEC 抓取前置条件） |
| `company/{TICKER}/current/market_snapshot.yaml` | Skill1 `company-foundation` | Skill5 `valuation-and-margin-of-safety` | 市场口径（price/shares/EV 等） |
| `company/{TICKER}/current/filings_index.yaml` + `.parquet` | Skill2 `collect-company-facts` | Skill3 `extract-xbrl-timeseries` / Skill5 `valuation-and-margin-of-safety` | SEC 索引（含 bucket、6-K 分类、VMF、download 状态） |
| `company/{TICKER}/raw/sec/{accession}/...` | Skill2 `collect-company-facts` | Skill3 `extract-xbrl-timeseries` | 原始证据池（可回放/可追溯） |
| `company/{TICKER}/current/news_index.parquet` + `news_digest.yaml` | Skill2 `collect-company-facts` | 下游 memo/事件时间线 | 新闻证据池（去重、标签、摘要） |
| `company/{TICKER}/current/papers_index.parquet` + `papers_digest.yaml` | Skill2 `collect-company-facts` | 下游 memo/技术评估 | 论文证据池（去重、标签、摘要） |
| `company/{TICKER}/current/events_index.parquet` | Skill2 `collect-company-facts` | Phase2 分析类 skills（growth/audit/moat 等） | 事件候选池（可追溯指针 + 初筛标签；用于后续生成 evidence claims） |
| `company/{TICKER}/current/xbrl_atlas/*` | Skill3 `extract-xbrl-timeseries` | Skill4 `recast-economic-statements` | XBRL 报表图谱与 facts 底座 |
| `company/{TICKER}/current/economic/*` | Skill4 `recast-economic-statements` | Skill5 `valuation-and-margin-of-safety` | 经济三表与核心指标（ROIC/FCF 等） |
| `company/{TICKER}/current/valuation/*` | Skill5 `valuation-and-margin-of-safety` | 下游决策/报告 | 估值输出（value_state 等） |

### 3.2 scripts/run.py (v0.3)

```python
#!/usr/bin/env python3
"""
collect-company-facts skill runner (v0.3 VMF edition).
Usage: python run.py TICKER [--as-of DATE] [--force-refresh]
"""
import sys
import argparse
import gzip
import json
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from collections import Counter
from dataclasses import dataclass
from typing import List, Set, Optional, Tuple
import pandas as pd

# Add runtime to path
sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from company_research_runtime import (
    paths, atomic_io, runlog, artifacts_state, evidence
)
from company_research_runtime.paths import TZ

SKILL_NAME = "collect-company-facts"

# ===== VMF Configuration =====

@dataclass
class VMFConfig:
    score_threshold: int = 8
    annual_budget: int = 20

    # Hard trigger items (8-K)
    hard_trigger_items: Set[str] = None

    # Hard trigger keywords
    hard_trigger_keywords: List[str] = None

    # Scoring weights
    score_weights: dict = None

    def __post_init__(self):
        if self.hard_trigger_items is None:
            self.hard_trigger_items = {"2.01", "2.02", "2.04", "2.06", "4.01", "4.02"}

        if self.hard_trigger_keywords is None:
            self.hard_trigger_keywords = [
                "restatement", "material weakness", "auditor", "going concern",
                "default", "covenant", "bankruptcy", "restructuring",
                "impairment", "write-down", "guidance", "outlook", "earnings", "results"
            ]

        if self.score_weights is None:
            self.score_weights = {
                "liquidity": 5, "refinancing": 5, "credit facility": 5,
                "default": 5, "covenant": 5,
                "earnings": 4, "results": 4, "guidance": 4, "outlook": 4, "margin": 4,
                "restatement": 3, "auditor": 3, "material weakness": 3,
                "impairment": 3, "restructuring": 3,
                "acquisition": 2, "merger": 2, "disposition": 2
            }

@dataclass
class VMFResult:
    triggered: bool
    hard_triggered: bool
    reasons: List[str]
    score: int

def apply_vmf(filing: dict, config: VMFConfig) -> VMFResult:
    """Apply Valuation Materiality Filter to an event filing."""
    reasons = []
    score = 0
    hard_triggered = False

    form = filing.get("form", "")
    items = filing.get("items", [])
    title = (filing.get("title") or filing.get("description") or "").lower()
    exhibits = filing.get("exhibits", [])

    # Layer 1: Hard triggers

    # A) 8-K Item hard triggers
    for item in items:
        item_clean = item.replace("Item ", "").strip()
        if item_clean in config.hard_trigger_items:
            hard_triggered = True
            reasons.append(f"item_{item_clean}")

    # B) Exhibit type hard triggers (99.*)
    for exhibit in exhibits:
        exhibit_num = str(exhibit.get("number", ""))
        exhibit_desc = (exhibit.get("description") or "").lower()

        if exhibit_num.startswith("99"):
            hard_triggered = True
            reasons.append(f"exhibit_{exhibit_num}")

        if any(kw in exhibit_desc for kw in ["earnings release", "results", "guidance", "investor presentation"]):
            hard_triggered = True
            reasons.append(f"exhibit_desc:{exhibit_num}")

    # C) Keyword hard triggers
    for kw in config.hard_trigger_keywords:
        if kw in title:
            hard_triggered = True
            reasons.append(f"keyword:{kw}")
            break  # One is enough for hard trigger

    # Layer 2: Scoring (if not hard triggered)
    if not hard_triggered:
        for keyword, weight in config.score_weights.items():
            if keyword in title:
                score += weight
                if len(reasons) < 5:  # Limit reason list
                    reasons.append(f"score:{keyword}")

    triggered = hard_triggered or (score >= config.score_threshold)

    return VMFResult(
        triggered=triggered,
        hard_triggered=hard_triggered,
        reasons=reasons,
        score=score
    )

def is_6k_interim_results(filing: dict) -> Tuple[bool, List[str]]:
    """Return (is_interim_results, reasons) for FPI 6-K.

    STRICT rule: require (period AND results) signals; guidance/outlook/presentation-only must NOT classify as interim.
    """
    if (filing.get("form") or "").upper() != "6-K":
        return (False, [])

    title_desc = " ".join([
        filing.get("title") or "",
        filing.get("description") or "",
    ]).lower()

    period_signals = [
        "three months ended", "six months ended", "quarter ended",
        "quarter", "half-year", "interim report", "interim financial statements",
        "unaudited interim", "q1", "q2", "q3", "q4",
    ]
    results_signals = [
        "financial results", "results", "earnings",
        "financial statements", "interim results",
        "unaudited", "condensed consolidated",
    ]

    def _hits(signals: List[str], text: str) -> List[str]:
        return [s for s in signals if s in (text or "")]

    title_period = _hits(period_signals, title_desc)
    title_results = _hits(results_signals, title_desc)

    ex99_text = " ".join([
        (ex.get("description") or "").lower()
        for ex in (filing.get("exhibits", []) or [])
        if str(ex.get("number", "")).startswith("99")
    ])
    ex_period = _hits(period_signals, ex99_text)
    ex_results = _hits(results_signals, ex99_text)

    is_interim = (bool(title_period) and bool(title_results)) or (bool(ex_period) and bool(ex_results))
    if not is_interim:
        return (False, ["no_periodic_signals"])

    reasons: List[str] = []
    if title_period:
        reasons.append(f"title_period:{title_period[0]}")
    if title_results:
        reasons.append(f"title_results:{title_results[0]}")
    if ex_period:
        reasons.append(f"ex99_period:{ex_period[0]}")
    if ex_results:
        reasons.append(f"ex99_results:{ex_results[0]}")

    return (True, reasons[:10])

# ===== Utility Functions =====

def canonical_url(url: str) -> str:
    """Canonicalize URL for deduplication."""
    if not url:
        return ""

    parsed = urlparse(url.lower())

    # Remove tracking parameters
    if parsed.query:
        params = parse_qs(parsed.query)
        tracking_params = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_content',
                          'utm_term', 'fbclid', 'gclid', '_ga', 'ref'}
        clean_params = {k: v for k, v in params.items() if k not in tracking_params}

        if clean_params:
            query = urlencode(sorted(clean_params.items()), doseq=True)
        else:
            query = ""
    else:
        query = ""

    return urlunparse((
        parsed.scheme or "https",
        parsed.netloc,
        parsed.path.rstrip('/'),
        parsed.params,
        query,
        ""
    ))

def generate_article_id(article: dict) -> str:
    """Generate stable article ID."""
    url = article.get("url", "")
    if url:
        canon = canonical_url(url)
        return hashlib.sha1(canon.encode()).hexdigest()[:16]
    else:
        fallback = f"{article.get('title', '')}|{article.get('published_at', '')}|{article.get('source', '')}"
        return hashlib.sha1(fallback.encode()).hexdigest()[:16]

def extract_event_tags(article: dict) -> List[str]:
    """Extract event tags from article."""
    tags = []
    title = (article.get("title") or "").lower()

    if any(kw in title for kw in ["earning", "revenue", "profit", "quarter", "fiscal"]):
        tags.append("earnings")
    if any(kw in title for kw in ["acquisition", "merger", "deal", "acquire"]):
        tags.append("m_and_a")
    if any(kw in title for kw in ["lawsuit", "sec", "investigation", "fraud"]):
        tags.append("legal")
    if any(kw in title for kw in ["ceo", "executive", "appoint", "resign"]):
        tags.append("management")
    if any(kw in title for kw in ["guidance", "forecast", "outlook", "expect"]):
        tags.append("guidance")
    if any(kw in title for kw in ["dividend", "buyback", "repurchase"]):
        tags.append("capital_return")

    return tags

TRUSTED_NEWS_DOMAINS = {
    "reuters.com",
    "bloomberg.com",
    "wsj.com",
    "ft.com",
    "sec.gov",
}

def score_news_relevance(article: dict, *, ticker: str, company_name: str) -> tuple[float, list[str], dict]:
    """
    Phase1: rule-based, explainable aboutness score (relevance ≠ impact/materiality).
    Returns: (score_0_1, reasons, features)
    """
    title = (article.get("title") or "")
    snippet = (article.get("snippet") or "")
    source = (article.get("source") or article.get("domain") or "")
    text = f"{title} {snippet}".lower()

    reasons: list[str] = []
    features: dict = {}

    score = 0.05

    ticker_lower = (ticker or "").lower()
    ticker_hit = bool(re.search(rf"\\b{re.escape(ticker_lower)}\\b", text)) if ticker_lower else False
    features["ticker_hit"] = ticker_hit
    if ticker_hit:
        score += 0.35
        reasons.append("ticker_hit")

    company_lower = (company_name or "").lower()
    company_tokens = [t for t in re.findall(r"[a-z0-9]+", company_lower) if t not in {"inc", "corp", "ltd", "plc", "co", "sa", "ag"}]
    core_token = company_tokens[0] if company_tokens else ""
    name_hit = (company_lower in text) or (core_token and core_token in text)
    features["name_hit"] = bool(name_hit)
    if name_hit:
        score += 0.25
        reasons.append(f"name_hit:{core_token or company_lower[:20]}")

    source_domain = source.lower()
    trusted_source = any(d in source_domain for d in TRUSTED_NEWS_DOMAINS)
    features["trusted_source"] = trusted_source
    if trusted_source:
        score += 0.10
        reasons.append(f"trusted_source:{source_domain}")

    title_lower = title.lower()
    strong_context_terms = ["shares", "stock", "ceo", "cfo", "quarter", "earnings", "results", "guidance", "outlook"]
    strong_context = any(t in title_lower for t in strong_context_terms) and (ticker_hit or name_hit)
    features["strong_context"] = strong_context
    if strong_context:
        score += 0.10
        reasons.append("strong_context:title")

    market_narrative_terms = ["sector", "market", "index", "futures"]
    market_narrative = any(t in text for t in market_narrative_terms) and (not ticker_hit) and (not name_hit)
    features["market_narrative"] = market_narrative
    if market_narrative:
        score -= 0.15
        reasons.append("penalty:market_narrative")

    listicle_terms = ["top stocks", "morning news", "5 things", "what to watch"]
    listicle = any(t in title_lower for t in listicle_terms)
    features["listicle"] = listicle
    if listicle and ("earnings" not in text) and ("guidance" not in text) and ("acquisition" not in text):
        score -= 0.10
        reasons.append("penalty:listicle")

    score = max(0.0, min(1.0, score))
    features["score"] = score
    return (score, reasons, features)

def generate_news_digest(
    new_articles: list,
    *,
    fetched: int,
    deduped_new: int,
    stored_total: int,
    window_start: date,
    window_end: date,
    as_of: date,
    mode: str,
) -> dict:
    """Generate news digest for this run window."""
    themes = Counter()
    high_impact = []

    for a in new_articles:
        tags = a.get("event_tags", [])
        for tag in tags:
            themes[tag] += 1

        # High impact: earnings + guidance
        if "earnings" in tags and "guidance" in tags:
            high_impact.append({
                "article_id": a.get("article_id"),
                "published_at": a.get("published_at"),
                "title": a.get("title"),
                "url": a.get("url"),
                "tags": tags,
                "impact_hint": "可能影响未来利润预期/折现率",
                "confidence": 0.8
            })
        elif "m_and_a" in tags:
            high_impact.append({
                "article_id": a.get("article_id"),
                "published_at": a.get("published_at"),
                "title": a.get("title"),
                "url": a.get("url"),
                "tags": tags,
                "impact_hint": "可能改变未来现金流路径",
                "confidence": 0.7
            })

    window_days = (window_end - window_start).days + 1
    return {
        "as_of": str(as_of),
        "window": {
            "mode": mode,
            "lookback_days": window_days,
            "start": str(window_start),
            "end": str(window_end),
        },
        "totals": {
            "fetched": fetched,
            "deduped_new": deduped_new,
            "stored_total": stored_total,
        },
        "top_themes": [{"theme": t, "count": c} for t, c in themes.most_common(5)],
        "high_impact_events": high_impact[:10],
    }

def generate_papers_digest(
    new_papers: list,
    *,
    papers_mode: str,
    window_mode: str,
    lookback_days: int,
    fetched: int,
    deduped_new: int,
    stored_total: int,
    as_of: date,
) -> dict:
    """Generate papers digest for this run window."""

    top_relevant = []
    for p in new_papers[:5]:
        top_relevant.append({
            "paper_id": p.get("paper_id"),
            "title": p.get("title"),
            "year": p.get("year"),
            "why_relevant": "Keyword match with company/industry",
            "confidence": 0.5
        })

    return {
        "as_of": str(as_of),
        "mode": papers_mode,
        "status": "ok",
        "reason": None,
        "window": {
            "mode": window_mode,
            "lookback_days": lookback_days,
        },
        "totals": {
            "fetched": fetched,
            "deduped_new": deduped_new,
            "stored_total": stored_total,
        },
        "top_relevant": top_relevant
    }

def write_skipped_digest(
    path: Path,
    reason: str,
    as_of: date,
    *,
    papers_mode: str,
    stored_total: int | None = None,
):
    """Write a skipped papers digest."""
    digest = {
        "as_of": str(as_of),
        "mode": papers_mode,
        "status": "skipped",
        "reason": reason,
        "window": {"mode": None, "lookback_days": None},
        "totals": {"fetched": 0, "deduped_new": 0, "stored_total": stored_total},
        "top_relevant": []
    }
    atomic_io.atomic_write_yaml(path, digest)

def is_relevant_industry(sic: str) -> bool:
    """Check if SIC indicates tech/pharma/relevant industry for papers."""
    tech_sics = ["3571", "3572", "3674", "3825", "2834", "2836", "7370", "7371", "7372", "7373"]
    return sic[:4] in tech_sics if sic else False

def probe_issuer_type(cik: str) -> str:
    """Init probe to avoid misclassifying FPI as domestic when filings index is empty."""
    # Keep it lightweight: only probe a short recent window and minimal hits.
    if sec_edgar_mcp.get_recent_filings(identifier=cik, form_type="20-F", days=365, limit=5).get("filings"):
        return "fpi"
    if sec_edgar_mcp.get_recent_filings(identifier=cik, form_type="10-Q", days=365, limit=5).get("filings"):
        return "domestic"
    if sec_edgar_mcp.get_recent_filings(identifier=cik, form_type="6-K", days=365, limit=5).get("filings"):
        return "fpi"
    return "domestic"

def determine_issuer_type(company: dict, filings: list = None, *, cik: str | None = None) -> str:
    """Determine issuer_type via observed forms (preferred over unreliable flags)."""
    forms = {(f.get("form") or "").upper() for f in (filings or [])}

    # Init edge case: empty forms → probe via small queries to avoid missing FPI logic
    if (not forms) and cik:
        return probe_issuer_type(cik)

    # FPI: 20-F is definitive; 6-K without 10-Q is strong fallback
    if any(form.startswith("20-F") for form in forms):
        return "fpi"
    if any(form.startswith("10-Q") for form in forms):
        return "domestic"
    if any(form.startswith("6-K") for form in forms) and not any(form.startswith("10-Q") for form in forms):
        return "fpi"

    return "domestic"

def stable_paper_id(paper: dict) -> str:
    """Stable prefixed paper_id (supports multi-source fusion without collisions)."""
    doi = (paper.get("doi") or "").strip()
    openalex_id = (paper.get("openalex_id") or "").strip()
    arxiv_id = (paper.get("arxiv_id") or "").strip()
    pubmed_id = (paper.get("pubmed_id") or "").strip()
    title = (paper.get("title") or "").strip()

    if doi:
        return f"doi:{doi}"
    if openalex_id:
        return f"openalex:{openalex_id}"
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    if pubmed_id:
        return f"pubmed:{pubmed_id}"
    return f"sha1:{hashlib.sha1(title.encode('utf-8')).hexdigest()[:16]}"

def append_to_gzip_jsonl(path: Path, record: dict):
    """Append a record to gzip JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "at", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')

# ===== Main Run Function =====

def run(ticker: str,
        as_of: date = None,
        force_refresh: bool = False,
        # Window params
        lookback_years: int = 10,
        lookback_days_news: int = 5,
        lookback_days_papers: int = 180,
        overlap_days: int = 2,
        papers_refresh_days: int = 30,
        # VMF params
        vmf_score_threshold: int = 8,
        vmf_annual_budget: int = 20,
        download_xbrl: bool = True,
        download_sections: bool = True,
        # News params
        news_max_per_day: int = 100,
        news_langs: List[str] = None,  # default ["en", "zh"]
        # Papers params
        papers_mode: str = "auto",
        papers_max_results: int = 200):

    ticker = ticker.upper()
    as_of = as_of or date.today()
    news_langs = news_langs or ["en", "zh"]

    vmf_config = VMFConfig(
        score_threshold=vmf_score_threshold,
        annual_budget=vmf_annual_budget
    )

    # ===== Step 0: Initialize =====
    print(f"=== collect-company-facts v0.3 for {ticker} ===")
    print(f"as_of: {as_of}, force_refresh: {force_refresh}")

    paths.ensure_dirs(ticker)
    run_id = paths.generate_run_id()
    run_dir = paths.get_run_dir(ticker, run_id)
    run_dir.mkdir(parents=True)
    run_outputs_current = run_dir / "outputs" / "current"
    run_outputs_current.mkdir(parents=True, exist_ok=True)

    runlog.write_meta(run_dir, ticker, SKILL_NAME, {
        "ticker": ticker,
        "as_of": str(as_of),
        "force_refresh": force_refresh,
        "lookback_years": lookback_years,
        "overlap_days": overlap_days,
        "vmf_score_threshold": vmf_score_threshold,
        "vmf_annual_budget": vmf_annual_budget,
        "lookback_days_news": lookback_days_news,
        "papers_mode": papers_mode,
        "version": "v0.3"
    })

    warnings = []
    outputs = []
    sec_status = "ok"
    news_status = "ok"
    papers_status = "ok"
    sec_warnings, sec_errors = [], []
    news_warnings, news_errors = [], []
    papers_warnings, papers_errors = [], []
    current_dir = paths.get_current_dir(ticker)
    raw_dir = paths.get_raw_dir(ticker)

    # Skill2 is ingestion: do not emit evidence claims here; only ensure ledgers exist (empty is OK).
    evidence.ensure_jsonl(current_dir / "evidence.jsonl")
    evidence.ensure_jsonl(current_dir / "questions.jsonl")

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
    sic = company.get("sic", "")
    print(f"CIK={cik}, company_name={company_name}, sic={sic}")

    # ===== Step 1: SEC Pipeline =====
    print("\n=== SEC Pipeline ===")

    filings_index_path = current_dir / "filings_index.yaml"
    filings_parquet_path = current_dir / "filings_index.parquet"
    existing_index = atomic_io.load_yaml(filings_index_path)

    # Determine mode + fetch window (Maintenance must fill gaps; not just overlap_days)
    if not existing_index.get("filings") or force_refresh:
        sec_mode = "init"
        fetch_start = as_of - timedelta(days=lookback_years * 365)
        print(f"Mode: INIT ({lookback_years} years)")
    else:
        sec_mode = "maintenance"
        last_filed_at = max(
            (date.fromisoformat(f.get("filed_at")) for f in existing_index.get("filings", []) if f.get("filed_at")),
            default=as_of,
        )
        fetch_start = last_filed_at - timedelta(days=overlap_days)
        print(f"Mode: MAINTENANCE (anchor last_filed_at={last_filed_at}, overlap_days={overlap_days})")

    fetch_end = as_of
    sec_window_days = (fetch_end - fetch_start).days + 1

    # Determine issuer type
    issuer_type = determine_issuer_type(company, existing_index.get("filings", []), cik=cik)
    print(f"Issuer type: {issuer_type}")

    # Periodic core + Event stream forms by issuer type
    if issuer_type == "domestic":
        periodic_forms = ["10-K", "10-K/A", "10-Q", "10-Q/A", "DEF14A"]
        event_forms = ["8-K", "8-K/A"]
    else:
        periodic_forms = ["20-F", "20-F/A"]
        event_forms = ["6-K"]  # FPI: will be split into 6-K-Periodic vs 6-K-Event

    # TODO: Call sec_edgar_mcp.get_recent_filings
    # Placeholder for periodic filings
    periodic_filings = []
    print(f"TODO: Fetch periodic filings {periodic_forms} for {sec_window_days} days ({fetch_start}→{fetch_end})")

    # TODO: Fetch event stream filings (and split FPI 6-K first)
    event_filings = []
    if issuer_type == "domestic":
        print(f"TODO: Fetch event filings {event_forms} for {sec_window_days} days ({fetch_start}→{fetch_end})")
    else:
        sixk_filings = []
        print(f"TODO: Fetch 6-K filings for {sec_window_days} days ({fetch_start}→{fetch_end}), then split 6-K-Periodic vs 6-K-Event")

        sixk_periodic = []
        sixk_event = []
        for filing in sixk_filings:
            is_interim, reasons = is_6k_interim_results(filing)
            filing["sixk_class"] = "interim_results" if is_interim else "other_event"
            filing["sixk_reasons"] = reasons

            if is_interim:
                filing["bucket"] = "periodic_core"
                filing["is_event_stream"] = False
                sixk_periodic.append(filing)  # Interim Financials/Results → Periodic Core
            else:
                filing["bucket"] = "event_stream"
                filing["is_event_stream"] = True
                sixk_event.append(filing)     # Other events → Event Stream

        periodic_filings.extend(sixk_periodic)
        event_filings = sixk_event

    # Apply VMF to event filings
    vmf_triggered_count = 0
    vmf_hard_triggered_count = 0
    year_budgets = {}

    for filing in event_filings:
        vmf_result = apply_vmf(filing, vmf_config)
        filing["bucket"] = "event_stream"
        filing["is_event_stream"] = True
        filing.setdefault("sixk_class", None)
        filing.setdefault("sixk_reasons", [])

        filing["source"] = filing.get("source") or "sec_edgar_mcp.get_recent_filings"
        filing["primary_doc"] = filing.get("primary_doc") or "primary_document.html"
        filing["filing_url"] = filing.get("filing_url") or filing.get("url")
        filing["items"] = filing.get("items")

        filing["vmf_triggered"] = vmf_result.triggered
        filing["vmf_hard_triggered"] = vmf_result.hard_triggered
        filing["vmf_reasons"] = vmf_result.reasons
        filing["vmf_score"] = vmf_result.score

        if vmf_result.triggered:
            vmf_triggered_count += 1
            if vmf_result.hard_triggered:
                vmf_hard_triggered_count += 1
                # Hard triggered: always download
                filing["downloaded"] = True
                filing["download_level"] = "primary_plus_exhibits"
            else:
                # Check budget
                year = filing.get("filed_at", "2020")[:4]
                if year_budgets.get(year, 0) < vmf_config.annual_budget:
                    filing["downloaded"] = True
                    filing["download_level"] = "primary_plus_exhibits"
                    year_budgets[year] = year_budgets.get(year, 0) + 1
                else:
                    filing["downloaded"] = False
                    filing["download_level"] = "metadata_only"
        else:
            filing["downloaded"] = False
            filing["download_level"] = "metadata_only"

    print(f"VMF: {vmf_triggered_count} triggered ({vmf_hard_triggered_count} hard) out of {len(event_filings)} events")

    # TODO: Download periodic filings
    for filing in periodic_filings:
        filing["bucket"] = "periodic_core"
        filing["is_event_stream"] = False
        filing.setdefault("sixk_class", None)
        filing.setdefault("sixk_reasons", [])

        filing["vmf_triggered"] = None
        filing["vmf_hard_triggered"] = None
        filing["vmf_reasons"] = []
        filing["vmf_score"] = None

        filing["source"] = filing.get("source") or "sec_edgar_mcp.get_recent_filings"
        filing["primary_doc"] = filing.get("primary_doc") or "primary_document.html"
        filing["filing_url"] = filing.get("filing_url") or filing.get("url")
        filing["items"] = filing.get("items")

        filing["downloaded"] = True
        filing["download_level"] = "primary_plus_exhibits"

        # Create accession directory
        accession = filing.get("accession", "PLACEHOLDER")
        accession_dir = raw_dir / "sec" / accession
        accession_dir.mkdir(parents=True, exist_ok=True)
        (accession_dir / "xbrl").mkdir(exist_ok=True)
        (accession_dir / "sections").mkdir(exist_ok=True)
        (accession_dir / "exhibits").mkdir(exist_ok=True)

        # Write meta.yaml
        meta = {
            "form": filing.get("form"),
            "filed_at": filing.get("filed_at"),
            "period_end": filing.get("period_end"),
            "accession": accession,
            "cik": cik,
            "company_name": company_name,
            "has_xbrl": filing.get("has_xbrl", False),
            "is_amendment": "/A" in filing.get("form", ""),
            "vmf": None
        }
        atomic_io.atomic_write_yaml(accession_dir / "meta.yaml", meta)

        # Write manifest.yaml
        manifest = {
            "downloaded_at": datetime.now(TZ).isoformat(),
            "download_level": "primary_plus_exhibits",
            "files": {},
            "completeness": {
                "has_primary_doc": False,  # TODO: set after download
                "has_sections": False,
                "has_exhibits": False,
                "has_xbrl": False
            }
        }
        atomic_io.atomic_write_yaml(accession_dir / "manifest.yaml", manifest)

    # TODO: Download VMF-triggered event filings
    for filing in event_filings:
        if filing.get("downloaded"):
            accession = filing.get("accession", "PLACEHOLDER")
            accession_dir = raw_dir / "sec" / accession
            accession_dir.mkdir(parents=True, exist_ok=True)
            (accession_dir / "exhibits").mkdir(exist_ok=True)

            # Write meta.yaml with VMF info
            meta = {
                "form": filing.get("form"),
                "filed_at": filing.get("filed_at"),
                "period_end": filing.get("period_end"),
                "accession": accession,
                "cik": cik,
                "company_name": company_name,
                "has_xbrl": False,
                "items": filing.get("items", []),
                "vmf": {
                    "triggered": filing["vmf_triggered"],
                    "hard_triggered": filing["vmf_hard_triggered"],
                    "reasons": filing["vmf_reasons"],
                    "score": filing["vmf_score"]
                }
            }
            atomic_io.atomic_write_yaml(accession_dir / "meta.yaml", meta)

            # Write manifest.yaml
            manifest = {
                "downloaded_at": datetime.now(TZ).isoformat(),
                "download_level": filing["download_level"],
                "files": {},
                "completeness": {}
            }
            atomic_io.atomic_write_yaml(accession_dir / "manifest.yaml", manifest)

    # Merge existing and new filings (upsert by accession to allow updating historical fields)
    existing_filings = existing_index.get("filings", []) or []
    by_accession = {f.get("accession"): f for f in existing_filings if f.get("accession")}
    existing_accessions = set(by_accession.keys())

    fetched = len(periodic_filings) + len(event_filings)
    deduped_new = 0

    for f in periodic_filings + event_filings:
        record = {
            "form": f.get("form"),
            "filed_at": f.get("filed_at"),
            "period_end": f.get("period_end"),  # SEC period_of_report; for 6-K often null unless parsed
            "accession": f.get("accession"),
            "has_xbrl": f.get("has_xbrl", False),
            "local_dir": f"raw/sec/{f.get('accession')}/",
            "is_amendment": "/A" in (f.get("form") or ""),
            "primary_doc": f.get("primary_doc") or "primary_document.html",
            "filing_url": f.get("filing_url") or f.get("url"),

            # Bucketing / 6-K classification
            "bucket": f.get("bucket") or ("event_stream" if f.get("is_event_stream") else "periodic_core"),
            "sixk_class": f.get("sixk_class"),             # interim_results | other_event | None
            "sixk_reasons": f.get("sixk_reasons", []),     # list[str]

            # Event Stream / VMF (nullable for periodic core)
            "is_event_stream": f.get("is_event_stream", False),
            "vmf_triggered": f.get("vmf_triggered"),
            "vmf_hard_triggered": f.get("vmf_hard_triggered"),
            "vmf_reasons": f.get("vmf_reasons", []),
            "vmf_score": f.get("vmf_score"),

            # 8-K only (nullable)
            "items": f.get("items"),

            "downloaded": f.get("downloaded", False),
            "download_level": f.get("download_level", "metadata_only"),

            "source": f.get("source") or "sec_edgar_mcp.get_recent_filings",
        }

        accession = record.get("accession")
        if not accession:
            continue
        if accession not in existing_accessions:
            deduped_new += 1
        by_accession[accession] = record  # upsert（允许修正历史字段：downloaded/vmf/sixk_class/...）

    all_filings = sorted(by_accession.values(), key=lambda x: x.get("filed_at", ""), reverse=True)

    filings_index = {
        "as_of": str(as_of),
        "issuer_type": issuer_type,
        "sixk_classifier_version": "v0.2",
        "vmf_version": "v0.1",
        "window": {
            "mode": sec_mode,
            "start": str(fetch_start),
            "end": str(fetch_end),
            "overlap_days": overlap_days,
            "lookback_years": lookback_years,
        },
        "totals": {
            "fetched": fetched,
            "deduped_new": deduped_new,
            "stored_total": len(all_filings),
        },
        "filings": all_filings,
    }
    atomic_io.atomic_write_yaml(run_outputs_current / "filings_index.yaml", filings_index)
    atomic_io.atomic_write_yaml(filings_index_path, filings_index)
    outputs.append("current/filings_index.yaml")
    print(f"Updated filings_index.yaml: {len(all_filings)} total filings")

    # Write parquet version
    if all_filings:
        filings_df = pd.DataFrame(all_filings)
        atomic_io.atomic_write_parquet(run_outputs_current / "filings_index.parquet", filings_df)
        atomic_io.atomic_write_parquet(filings_parquet_path, filings_df)
        outputs.append("current/filings_index.parquet")

    sec_status = "ok" if all_filings else "partial"
    if sec_status == "partial":
        warnings.append("SEC: no filings returned (check MCP calls / window)")

    # ===== Step 2: News Pipeline =====
    print("\n=== News Pipeline ===")

    news_index_path = current_dir / "news_index.parquet"
    news_digest_path = current_dir / "news_digest.yaml"

    # Determine mode + fetch window
    existing_df = None
    if not news_index_path.exists() or force_refresh:
        news_mode = "init"
        fetch_start = as_of - timedelta(days=lookback_days_news)
        print(f"Mode: INIT (lookback_days_news={lookback_days_news})")
    else:
        news_mode = "maintenance"
        existing_df = pd.read_parquet(news_index_path)
        if existing_df.empty or "published_at" not in existing_df.columns:
            fetch_start = as_of - timedelta(days=overlap_days)
        else:
            last_published = pd.to_datetime(existing_df["published_at"], utc=True, errors="coerce").max()
            last_covered_day = last_published.date() if pd.notnull(last_published) else as_of
            fetch_start = last_covered_day - timedelta(days=overlap_days)
        print(f"Mode: MAINTENANCE (overlap_days={overlap_days})")

    fetch_end = as_of
    news_window_days = (fetch_end - fetch_start).days + 1
    print(f"Fetching news from {fetch_start} to {fetch_end}")

    # TODO: Call gdelt.gdelt_search_articles
    # 按天循环，每天最多 news_max_per_day 篇
    articles = []  # Placeholder
    total_cap = news_max_per_day * news_window_days
    print(f"TODO: Query GDELT for '{ticker}' OR '{company_name}'")
    print(f"  - langs: {news_langs}, max_per_day: {news_max_per_day}, total_cap: {total_cap}")

    # Normalize and dedupe
    new_raw_articles = []
    new_index_articles = []
    if existing_df is not None and (not existing_df.empty) and "article_id" in existing_df.columns:
        existing_ids = set(existing_df["article_id"].tolist())
    else:
        existing_ids = set()

    for article in articles:
        article_id = generate_article_id(article)
        if article_id not in existing_ids:
            raw_record = dict(article)
            raw_record["article_id"] = article_id
            raw_record["canonical_url"] = canonical_url(raw_record.get("url", ""))
            raw_record["ticker"] = ticker
            raw_record["query"] = f'"{ticker}" OR "{company_name}"'
            raw_record["retrieved_at"] = datetime.now(timezone.utc).isoformat()

            pub_date_str = raw_record.get("published_at", str(as_of))
            try:
                pub_date = datetime.fromisoformat(pub_date_str.replace('Z', '+00:00')).date()
            except Exception:
                pub_date = as_of
            raw_path = f"raw/news/{pub_date.strftime('%Y/%m')}/news_{pub_date.strftime('%Y-%m-%d')}.jsonl.gz"

            index_record = dict(raw_record)
            index_record["raw_path"] = raw_path
            index_record["event_tags"] = extract_event_tags(index_record)
            score, reasons, features = score_news_relevance(index_record, ticker=ticker, company_name=company_name)
            index_record["relevance_score"] = score
            index_record["relevance_version"] = "rule_v0.1"
            index_record["relevance_reasons"] = reasons
            index_record["relevance_features_json"] = json.dumps(features, ensure_ascii=False, sort_keys=True)
            index_record["impact_score"] = None  # Phase2: placeholder (default NaN)

            new_raw_articles.append(raw_record)
            new_index_articles.append(index_record)
            existing_ids.add(article_id)

    print(f"Deduped to {len(new_index_articles)} new articles")

    # Append to partitioned raw store
    for article in new_raw_articles:
        pub_date_str = article.get("published_at", str(as_of))
        try:
            pub_date = datetime.fromisoformat(pub_date_str.replace('Z', '+00:00')).date()
        except:
            pub_date = as_of

        file_path = raw_dir / "news" / pub_date.strftime("%Y/%m") / f"news_{pub_date.strftime('%Y-%m-%d')}.jsonl.gz"
        append_to_gzip_jsonl(file_path, article)

    # Update news_index.parquet
    if news_mode == "init":
        if new_index_articles:
            news_index_df = pd.DataFrame(new_index_articles)
        else:
            news_index_df = pd.DataFrame(columns=[
                "article_id", "published_at", "retrieved_at", "title", "source",
                "url", "canonical_url", "lang", "snippet", "query", "ticker",
                "raw_path",
                "relevance_score", "relevance_version", "relevance_reasons", "relevance_features_json", "impact_score",
                "event_tags"
            ])
    else:
        if new_index_articles:
            new_df = pd.DataFrame(new_index_articles)
            if news_index_path.exists():
                existing_df = pd.read_parquet(news_index_path)
                news_index_df = pd.concat([existing_df, new_df], ignore_index=True)
            else:
                news_index_df = new_df
        else:
            news_index_df = pd.read_parquet(news_index_path) if news_index_path.exists() else pd.DataFrame()

    # Parquet：强制 UTC timestamp 类型（避免 object/string 漂移）
    if "published_at" in news_index_df.columns:
        news_index_df["published_at"] = pd.to_datetime(news_index_df["published_at"], utc=True, errors="coerce")
    if "retrieved_at" in news_index_df.columns:
        news_index_df["retrieved_at"] = pd.to_datetime(news_index_df["retrieved_at"], utc=True, errors="coerce")

    atomic_io.atomic_write_parquet(run_outputs_current / "news_index.parquet", news_index_df)
    atomic_io.atomic_write_parquet(news_index_path, news_index_df)
    outputs.append("current/news_index.parquet")

    # Generate digest
    news_digest = generate_news_digest(
        new_articles=new_index_articles,
        fetched=len(articles),
        deduped_new=len(new_index_articles),
        stored_total=len(news_index_df),
        window_start=fetch_start,
        window_end=fetch_end,
        as_of=as_of,
        mode=news_mode,
    )
    atomic_io.atomic_write_yaml(run_outputs_current / "news_digest.yaml", news_digest)
    atomic_io.atomic_write_yaml(news_digest_path, news_digest)
    outputs.append("current/news_digest.yaml")
    print(f"Generated news_digest.yaml")

    # ===== Step 3: Papers Pipeline =====
    print("\n=== Papers Pipeline ===")

    papers_index_path = current_dir / "papers_index.parquet"
    papers_digest_path = current_dir / "papers_digest.yaml"

    if papers_mode == "off":
        stored_total = None
        if papers_index_path.exists():
            try:
                stored_total = len(pd.read_parquet(papers_index_path))
            except Exception:
                stored_total = None
        write_skipped_digest(
            run_outputs_current / "papers_digest.yaml",
            "user_disabled",
            as_of,
            papers_mode=papers_mode,
            stored_total=stored_total,
        )
        write_skipped_digest(
            papers_digest_path,
            "user_disabled",
            as_of,
            papers_mode=papers_mode,
            stored_total=stored_total,
        )
        outputs.append("current/papers_digest.yaml")
        papers_status = "skipped"
        print("Mode: OFF (user disabled)")
    elif papers_mode == "auto" and not is_relevant_industry(sic):
        stored_total = None
        if papers_index_path.exists():
            try:
                stored_total = len(pd.read_parquet(papers_index_path))
            except Exception:
                stored_total = None
        write_skipped_digest(
            run_outputs_current / "papers_digest.yaml",
            "not_relevant_industry",
            as_of,
            papers_mode=papers_mode,
            stored_total=stored_total,
        )
        write_skipped_digest(
            papers_digest_path,
            "not_relevant_industry",
            as_of,
            papers_mode=papers_mode,
            stored_total=stored_total,
        )
        outputs.append("current/papers_digest.yaml")
        papers_status = "skipped"
        print(f"Mode: AUTO → SKIPPED (SIC {sic} not relevant)")
    else:
        # Determine mode
        if not papers_index_path.exists() or force_refresh:
            papers_mode_actual = "init"
            papers_window_days = lookback_days_papers
            print(f"Mode: INIT ({lookback_days_papers} days)")
        else:
            # Staleness check
            existing_digest = atomic_io.load_yaml(papers_digest_path)
            last_fetch_str = existing_digest.get("as_of")

            if last_fetch_str:
                try:
                    last_fetch = datetime.fromisoformat(last_fetch_str).date()
                    days_since = (as_of - last_fetch).days
                    if days_since < papers_refresh_days:
                        print(f"Mode: SKIP (last fetch {days_since} days ago < {papers_refresh_days}d threshold)")
                        stored_total = None
                        if papers_index_path.exists():
                            try:
                                stored_total = len(pd.read_parquet(papers_index_path))
                            except Exception:
                                stored_total = None
                        write_skipped_digest(
                            run_outputs_current / "papers_digest.yaml",
                            "too_fresh",
                            as_of,
                            papers_mode=papers_mode,
                            stored_total=stored_total,
                        )
                        write_skipped_digest(
                            papers_digest_path,
                            "too_fresh",
                            as_of,
                            papers_mode=papers_mode,
                            stored_total=stored_total,
                        )
                        outputs.append("current/papers_digest.yaml")
                        papers_status = "skipped"
                        # Skip to finalization
                        papers_mode_actual = "skip"
                    else:
                        papers_mode_actual = "maintenance"
                        papers_window_days = papers_refresh_days
                        print(f"Mode: MAINTENANCE (refresh after {days_since} days)")
                except:
                    papers_mode_actual = "maintenance"
                    papers_window_days = papers_refresh_days
            else:
                papers_mode_actual = "maintenance"
                papers_window_days = papers_refresh_days

        if papers_mode_actual != "skip":
            # TODO: Call openalex.search_works
            papers = []  # Placeholder
            print(f"TODO: Query OpenAlex for '{company_name}'")

            # Dedupe
            new_raw_papers = []
            new_index_papers = []
            if papers_mode_actual == "maintenance" and papers_index_path.exists():
                existing_df = pd.read_parquet(papers_index_path)
                existing_ids = set(existing_df["paper_id"].tolist())
            else:
                existing_ids = set()

            for paper in papers:
                paper_id = stable_paper_id(paper)  # doi:/openalex:/arxiv:/pubmed:/sha1:
                if paper_id not in existing_ids:
                    raw_record = dict(paper)
                    raw_record["paper_id"] = paper_id
                    raw_record["ticker"] = ticker
                    raw_record["retrieved_at"] = datetime.now(timezone.utc).isoformat()

                    raw_path = f"raw/papers/{as_of.year}/{as_of.month:02d}/papers_{as_of.year}-{as_of.month:02d}.jsonl.gz"

                    index_record = dict(raw_record)
                    index_record["raw_path"] = raw_path
                    index_record["tags"] = []  # TODO: extract tags
                    index_record["relevance_score"] = 0.5  # TODO: implement

                    new_raw_papers.append(raw_record)
                    new_index_papers.append(index_record)
                    existing_ids.add(paper_id)

            print(f"Deduped to {len(new_index_papers)} new papers")

            # Append to partitioned raw store
            year = as_of.year
            month = as_of.month
            for paper in new_raw_papers:
                file_path = raw_dir / "papers" / f"{year}/{month:02d}" / f"papers_{year}-{month:02d}.jsonl.gz"
                append_to_gzip_jsonl(file_path, paper)

            # Update papers_index.parquet
            if papers_mode_actual == "init":
                if new_index_papers:
                    papers_index_df = pd.DataFrame(new_index_papers)
                else:
                    papers_index_df = pd.DataFrame(columns=[
                        "paper_id", "doi", "openalex_id", "title", "year",
                        "authors", "venue", "url", "abstract", "retrieved_at",
                        "ticker", "raw_path", "relevance_score", "tags"
                    ])
            else:
                if new_index_papers:
                    new_df = pd.DataFrame(new_index_papers)
                    if papers_index_path.exists():
                        existing_df = pd.read_parquet(papers_index_path)
                        papers_index_df = pd.concat([existing_df, new_df], ignore_index=True)
                    else:
                        papers_index_df = new_df
                else:
                    papers_index_df = pd.read_parquet(papers_index_path) if papers_index_path.exists() else pd.DataFrame()

            # Parquet：强制 UTC timestamp 类型（避免 object/string 漂移）
            if "retrieved_at" in papers_index_df.columns:
                papers_index_df["retrieved_at"] = pd.to_datetime(papers_index_df["retrieved_at"], utc=True, errors="coerce")

            atomic_io.atomic_write_parquet(run_outputs_current / "papers_index.parquet", papers_index_df)
            atomic_io.atomic_write_parquet(papers_index_path, papers_index_df)
            outputs.append("current/papers_index.parquet")

            # Generate digest
            papers_digest = generate_papers_digest(
                new_papers=new_index_papers,
                papers_mode=papers_mode,
                window_mode=papers_mode_actual,
                lookback_days=papers_window_days,
                fetched=len(papers),
                deduped_new=len(new_index_papers),
                stored_total=len(papers_index_df),
                as_of=as_of,
            )
            atomic_io.atomic_write_yaml(run_outputs_current / "papers_digest.yaml", papers_digest)
            atomic_io.atomic_write_yaml(papers_digest_path, papers_digest)
            outputs.append("current/papers_digest.yaml")
            papers_status = "ok"
            print(f"Generated papers_digest.yaml")

    # ===== Step 4: Finalize =====
    print("\n=== Finalizing ===")

    # Load digests for components (skipped branches also write digest files)
    papers_digest = atomic_io.load_yaml(papers_digest_path) if papers_digest_path.exists() else {}

    # Build events candidates index (events_index.parquet) instead of writing evidence claims here
    events = []
    for e in (news_digest.get("high_impact_events") or []):
        published_at = e.get("published_at")
        try:
            pub_dt = datetime.fromisoformat((published_at or "").replace('Z', '+00:00'))
            pub_date = pub_dt.date()
        except Exception:
            pub_date = as_of
        raw_path = f"raw/news/{pub_date.strftime('%Y/%m')}/news_{pub_date.strftime('%Y-%m-%d')}.jsonl.gz"
        events.append({
            "event_id": f"news:{e.get('article_id')}",
            "event_type": "news",
            "occurred_at": published_at,
            "ticker": ticker,
            "headline": e.get("title"),
            "tags": e.get("tags", []),
            "materiality_hint": e.get("impact_hint"),
            "score_hint": None,  # Phase1: aboutness score != materiality/impact score
            "impact_score": None,  # Phase2: placeholder (default NaN)
            "source_ref_json": json.dumps({"url": e.get("url"), "raw_path": raw_path}, ensure_ascii=False, sort_keys=True),
            "anchors_json": json.dumps({"snippet": e.get("snippet")}, ensure_ascii=False, sort_keys=True),
        })

    for f in all_filings:
        if f.get("bucket") != "event_stream":
            continue
        if not f.get("vmf_triggered"):
            continue
        events.append({
            "event_id": f"sec:{f.get('accession')}",
            "event_type": "sec",
            "occurred_at": f.get("filed_at"),
            "ticker": ticker,
            "headline": f"{f.get('form')} {f.get('accession')}",
            "tags": (f.get("vmf_reasons") or []) + (f.get("items") or []),
            "materiality_hint": "vmf_triggered_event",
            "score_hint": f.get("vmf_score"),
            "impact_score": None,  # Phase2: placeholder (default NaN)
            "source_ref_json": json.dumps({"local_dir": f.get("local_dir"), "filing_url": f.get("filing_url")}, ensure_ascii=False, sort_keys=True),
            "anchors_json": json.dumps({"items": f.get("items")}, ensure_ascii=False, sort_keys=True),
        })

    events_df = pd.DataFrame(events)
    if events_df.empty:
        events_df = pd.DataFrame(columns=[
            "event_id", "event_type", "occurred_at", "ticker", "headline",
            "tags", "materiality_hint", "score_hint", "impact_score", "source_ref_json", "anchors_json"
        ])
    else:
        events_df["occurred_at"] = pd.to_datetime(events_df["occurred_at"], utc=True, errors="coerce")

    atomic_io.atomic_write_parquet(run_outputs_current / "events_index.parquet", events_df)
    atomic_io.atomic_write_parquet(current_dir / "events_index.parquet", events_df)
    outputs.append("current/events_index.parquet")
    print("Generated events_index.parquet")

    # Update artifacts_state (align with component status; avoid blindly writing ok)
    if sec_status in ["ok", "partial", "skipped"]:
        artifacts_state.update_artifacts_state(ticker, "filings_index.yaml", sec_status, run_id)
        if "current/filings_index.parquet" in outputs:
            artifacts_state.update_artifacts_state(ticker, "filings_index.parquet", sec_status, run_id)

    if news_status in ["ok", "partial", "skipped"]:
        artifacts_state.update_artifacts_state(ticker, "news_digest.yaml", news_status, run_id)
        if "current/news_index.parquet" in outputs:
            artifacts_state.update_artifacts_state(ticker, "news_index.parquet", news_status, run_id)

    if papers_status in ["ok", "partial", "skipped"]:
        artifacts_state.update_artifacts_state(ticker, "papers_digest.yaml", papers_status, run_id)
        if "current/papers_index.parquet" in outputs:
            artifacts_state.update_artifacts_state(ticker, "papers_index.parquet", papers_status, run_id)

    if "current/events_index.parquet" in outputs:
        artifacts_state.update_artifacts_state(ticker, "events_index.parquet", "ok", run_id)

    components = {
        "sec": {
            "status": sec_status,
            "mode": filings_index.get("window", {}).get("mode"),
            "window": filings_index.get("window", {}),
            "totals": filings_index.get("totals", {}),
            "warnings": sec_warnings,
            "errors": sec_errors,
        },
        "news": {
            "status": news_status,
            "mode": news_digest.get("window", {}).get("mode"),
            "window": news_digest.get("window", {}),
            "totals": news_digest.get("totals", {}),
            "warnings": news_warnings,
            "errors": news_errors,
        },
        "papers": {
            "status": papers_status,
            "mode": papers_digest.get("window", {}).get("mode"),
            "reason": papers_digest.get("reason"),
            "window": papers_digest.get("window", {}),
            "totals": papers_digest.get("totals", {}),
            "warnings": papers_warnings,
            "errors": papers_errors,
        },
    }

    # Rollup status (orchestrator-friendly)
    # - sec blocked/error -> skill blocked/error
    # - any other error -> partial
    # - any partial -> partial
    # - sec ok + news ok + papers ok/skipped -> ok
    # - all skipped + input fingerprint unchanged -> skipped (optional)
    if sec_status in ["blocked", "error"]:
        status = sec_status
    elif news_status == "error" or papers_status == "error":
        status = "partial"
    elif sec_status == "partial" or news_status == "partial" or papers_status == "partial":
        status = "partial"
    elif sec_status == "skipped" and news_status == "skipped" and papers_status == "skipped":
        status = "skipped"
    else:
        status = "ok"

    result = runlog.write_result(run_dir, ticker, SKILL_NAME, status,
        outputs=outputs, warnings=warnings, as_of=str(as_of),
        components=components)

    print(f"\n=== Result: {status} ===")
    print(f"Run: {run_dir}")
    print(f"Outputs: {outputs}")

    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect company facts v0.3 (VMF)")
    parser.add_argument("ticker", help="Stock ticker")
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--force-refresh", action="store_true")

    # Window params
    parser.add_argument("--lookback-years", type=int, default=10, help="Init: SEC lookback years")
    parser.add_argument("--lookback-days-news", type=int, default=5, help="Init: news lookback days")
    parser.add_argument("--lookback-days-papers", type=int, default=180, help="Init: papers lookback days")
    parser.add_argument("--overlap-days", type=int, default=2, help="Maintenance: overlap window days")
    parser.add_argument("--papers-refresh-days", type=int, default=30, help="Papers staleness threshold")

    # VMF params
    parser.add_argument("--vmf-score-threshold", type=int, default=8)
    parser.add_argument("--vmf-annual-budget", type=int, default=20)

    # News params
    parser.add_argument("--news-max-per-day", type=int, default=100,
                        help="Max articles per day (prevents data loss on long windows)")
    parser.add_argument("--news-langs", nargs="+", default=["en", "zh"],
                        help="Language filter list")

    # Papers params
    parser.add_argument("--papers-mode", default="auto", choices=["auto", "on", "off"])
    parser.add_argument("--papers-max-results", type=int, default=200)

    args = parser.parse_args()

    run(args.ticker,
        as_of=args.as_of,
        force_refresh=args.force_refresh,
        lookback_years=args.lookback_years,
        lookback_days_news=args.lookback_days_news,
        lookback_days_papers=args.lookback_days_papers,
        overlap_days=args.overlap_days,
        papers_refresh_days=args.papers_refresh_days,
        vmf_score_threshold=args.vmf_score_threshold,
        vmf_annual_budget=args.vmf_annual_budget,
        news_max_per_day=args.news_max_per_day,
        news_langs=args.news_langs,
        papers_mode=args.papers_mode,
        papers_max_results=args.papers_max_results)
```


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
            # Phase 1 v0.1: net_debt is intentionally not part of market_snapshot.yaml (derive later from filings/economic layer).
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
