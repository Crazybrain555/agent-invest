Goal (incl. success criteria):
- Strengthen `CLAUDE.md` + `AGENTS.md` so agents stop assuming a full AIQuantLab codebase and instead follow this repo's Phase 1 skills workflow.
- Success = `CLAUDE.md` contains only real repo structure/commands; documents `/home/help/mcp/work/company_research/company/{TICKER}/...` outputs and `raw/current/runs` invariants; adds a self-evolving doc rule (propose writing corrections back); optional Claude Code project commands/hooks added.

Constraints/Assumptions:
- approval_policy=never; sandbox_mode=danger-full-access; network_access=enabled.
- Follow AGENTS.md continuity rules; keep this ledger concise.
- Prefer in-repo patterns; avoid ad-hoc dependency installs.
- UTF-8 for file I/O.
- Docs: English-first for agent compatibility; keep key path notes in Chinese where helpful.

Key decisions:
- XBRL ownership: Skill2 (or a dedicated downloader module) downloads and materializes as-filed XBRL into `raw/sec/{accession}/xbrl/`; Skill3 consumes local XBRL to build `current/xbrl_atlas/`.
- `sec_edgar_mcp.get_filing_content` is acceptable for quick/normalized text, but not canonical (can truncate); canonical persistence is via Archives artifacts.
- Use `index.json` to enumerate artifacts; do not rely on `*-xbrl.zip` alone (zip may omit files like `*_htm.xml` and `FilingSummary.xml`).
- iXBRL nuance: instance file may be `*_htm.xml` and may not share the schema stem (e.g., JD).
- Exhibits DocType must be parsed from `{accession}-index.html`; `index.json` does not include DocType.
- Claude Code: custom project slash commands can live in `.claude/commands/*.md`, and hooks can live in `.claude/settings.json` (both are optional; this repo is not versioning them yet).

State:
- Docs updated: `CLAUDE.md` now reflects the skills-only Phase 1 repo; `AGENTS.md` clarifies doc roles + runtime output invariants; optional Claude Code project commands/hooks scaffold added under `.claude/`.

Done:
- Added `SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md`.
- Updated `SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md` to the “recommended route” (Skill2/downloader owns as-filed materialization; Skill3 consumes local XBRL).
- Updated `stock_skills_buildplan_v2.md` to remove `download_xbrl` + all `v0.*` wording; codified `raw/sec/{accession}/xbrl/` as extracted as-filed file set (no zip retention).
- Updated `Phase 1 核心估值链 Codex Skills 实施指南.md` to remove `download_xbrl` + all `v0.*` wording; rewrote Skill3 guidance to primary as-filed parsing + optional fallback.
- Added `_sec_downloads/materialize_company_samples.py`; fixed JD iXBRL instance discovery (`*_htm.xml`); default no longer downloads `*-xbrl.zip`.
- Extended sample downloader to auto-resolve tickers → latest XBRL filings via `company_tickers.json` + submissions JSON; adds on-disk XBRL validation summary.
- Ran broader sampling (Canada/Europe/South America/small caps) and validated XBRL instance+schema presence for all samples.
- Updated docs to include `40-F` (MJDS/Canada) and note that some `6-K` carry iXBRL.
- Rebuilt `_sec_downloads/company/` and re-sampled multiple regions/sizes; regenerated `_sec_downloads/sections_exhibits_summary.yaml`.
- Fixed regex escaping in section extraction; switched to longest-match + min-length threshold to avoid TOC/Part-II overlaps (notably 10-Q Item 2).
- Added 6-K/other fallback headings for MD&A-like sections.
- Exhibits download now captures all `EX-*` except `EX-101.*` using DocType from `{accession}-index.html`.
- Verified sections extraction works on AAPL 10-K/10-Q, ASML/BABA/JD 20-F, and AZN 6-K; exhibits are populated when EX-* exist.
- Updated `SEC_EDGAR_FILING_XBRL_DOWNLOAD_SPEC.md`, `stock_skills_buildplan_v2.md`, and `Phase 1 核心估值链 Codex Skills 实施指南.md` to reflect DocType parsing, full EX-* capture, and improved sections logic.
- Rewrote `CLAUDE.md` to remove non-existent full-codebase layout/commands and document the Phase 1 skills workflow + output invariants.
- Updated `AGENTS.md` to add doc roles, runtime output directory invariants, and self-evolving docs rules.
- Decided to **defer** versioning `.claude/commands/` and `.claude/settings.json` until the 5 skills stabilize.

Now:
- Validate behavior in Claude Code: run `/init` and confirm it no longer assumes a full AIQuantLab codebase.

Next:
- If `/init` proposes improvements: apply minimal diffs to `CLAUDE.md`/`AGENTS.md` and keep them consistent with `Phase 1 核心估值链 Codex Skills 实施指南.md`.

Open questions (UNCONFIRMED if needed):
- None.

Working set (files/ids/commands):
- `CONTINUITY.md`
- `CLAUDE.md`
- `AGENTS.md`
- `.claude/settings.local.json`
