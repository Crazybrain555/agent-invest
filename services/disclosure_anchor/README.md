# disclosure-anchor

Local L1 disclosure-file service for the investment-research prediction engine: it watches a
hand-maintained tracked-company list, syncs CNINFO disclosure indexes, archives immutable raw PDFs,
parses them with MinerU, slices them into L2-ready `document_unit` rows, and publishes the active
processing run behind stable public read contracts.

Pipeline: sync (CNINFO index, API/web channels) → download (immutable raw archive) → parse
(pinned MinerU 3.4.4 Hybrid-medium → `provider_document.v1`) → build source-bound coarse
units → publish active run (+ change events). A worker loop drives provider-native queues;
the Filing API exposes the unchanged `disclosure_public.*_v1` views. Historical NormalizedIR
v4 runs remain readable evidence but cannot re-enter Build or Publish.

## Setup

```bash
/opt/miniconda3/bin/python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Set service runtime paths in your shell or a private env file outside this checkout. `.env.template` and
`.env.example` show the expected disclosure_anchor variables and keep all credentials as placeholders.

## Common commands

```bash
make db-create migrate    # bootstrap roles/database, apply the append-only migration head
make track FILE=...       # upsert the tracked-company watchlist (offline, idempotent)
make sync COMPANY=<scode> [WINDOW=n]   # sync one company's disclosure index
make worker-once          # one worker round: sync -> download -> parse -> build -> publish
make worker-loop          # continuous worker (singleton advisory lock)
make api                  # Filing API on 127.0.0.1:8711 (fails closed without env/mount sentinel)
make doctor               # environment + data integrity checks
make agent-check          # lint + strict mypy + no-DB tests + diff check
make test                 # live-DB gates
make archive              # clean source archive from tracked files (git archive)
```

## Pointers

- Canonical contract: `docs/architecture/service-purpose.md`
- Milestone specs and acceptance: `docs/implementation/milestones/`, `docs/implementation/checks/`
- Code map: `src/disclosure_anchor/AGENTS.md` (per-directory AGENTS.md, nearest wins)
- Cross-agent operating contract: `AGENTS.md`
