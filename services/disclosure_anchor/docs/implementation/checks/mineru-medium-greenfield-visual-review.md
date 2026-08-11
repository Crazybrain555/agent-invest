# MinerU Medium greenfield visual review

Date: 2026-08-11

Scope: DB-free provider record, deterministic outline, coarse units, provider table relation,
and retrieval-primary projection

Provider lane: MinerU 3.4.4 Hybrid-medium

## Verdict

The greenfield visual stop is **passed for the DB-free foundation**. The reader, outline,
coarse-unit partition, page locators, provider-declared table owner/continuation relation,
and explicit retrieval targets preserve the reviewed source content and geometry without
invoking the legacy proof system. This is not a publication or public-contract approval.

The stop intentionally leaves three visible limitations for later, separate stages:

- CGN's two-line subject remains split into adjacent H1/H2 candidates;
- Zhongke's visually obvious document title is not a provider title candidate, and the four
  attendee lines remain flattened inside the provider table HTML;
- same-page alias evaluation remains unimplemented because the reviewed corpus contains no
  positive duplicate occurrence that justifies a general rule.

Those limitations must not be repaired with document-specific phrases, fuzzy block
matching, or table-cell invention. Source-bound style/bookmark hints and the optional
whole-outline reviewer may adjust only existing heading candidates. The table projection
accepts only MinerU's retained/deleted carrier relation; it does not repair cells or infer
continuation from visual or textual similarity.

## Reproducible review surface

The tracked scratch tool is `scripts/review_mineru_medium_outline.py`. It reads an exact
MinerU `hybrid_auto/` leaf and immutable source PDF, builds the existing greenfield DTOs,
renders source and provider layout pages at 144 DPI, and writes a create-only report under
`/private/tmp`.

Representative invocation:

```bash
PYTHONPATH=src .venv/bin/python scripts/review_mineru_medium_outline.py \
  --source-pdf SOURCE.pdf \
  --provider-bundle PARSER_OUTPUT/HASH/hybrid_auto \
  --source-page-offset 0 \
  --provider-page 0 \
  --run-evidence RUN_EVIDENCE.json \
  --out /private/tmp/UNIQUE_REVIEW_ROOT
```

The run evidence and zero-based source-page offset are mandatory. The tool binds matching
before/after source hashes and page counts, the provider output root, and the exact `-s`/`-e`
window before creating the report, so a window bundle cannot silently pretend to start on
another source page. The report projects physical table segments to content-list table
carriers only by the provider's page/table order and retained/deleted state, and it records
the resulting owner/continuation/unbound status. It still records same-page alias state as
`not_implemented` / `not_evaluated` rather than deriving a string or geometry heuristic.

## Frozen review reports

All reports were generated beneath
`/private/tmp/disclosure-anchor-medium-visual-review-20260811-r6`. The full SHA-256 values
below bind the JSON records; rendered PNG hashes are contained inside each record.

| Review | Pages rendered | Blocks / headings / units / segments / logical tables | Primary / evidence blocks | Search targets | `report.json` SHA-256 |
|---|---:|---:|---:|---:|---|
| CGN | 1 | 13 / 3 / 4 / 0 / 0 | 13 / 0 | 13 | `e9211d11782c06540eb36cbe8e68e8bdf7c72459f49a5f06aea84b5e0f09778c` |
| Zhongke | 3 | 7 / 0 / 1 / 3 / 2 | 4 / 3 | 5 | `afc5aaffa45fbe76ab1a955db4f14217d0aac61b4718a0b8c042b5af02026af9` |
| Caitong | 4 | 8 / 1 / 2 / 4 / 1 | 5 / 3 | 5 | `fb5cae55a631e014e035b0cfc9a5960f32b24cf32d705f6a7dc91b1ef3991da4` |
| JiangHai p49-p50 window | 2 of 8 | 34 / 2 / 3 / 12 / 5 | 11 / 23 | 15 | `6e5f448cec9b35c38ee7034320524d27b0e2422981650186aba1297f21bb6b47` |
| JiangHai p79-p80 full bundle | 2 of 161 | 2020 / 531 / 531 / 340 / 284 | 1637 / 383 | 1863 | `379ef554a394f19fa5c171b922688e493f152c051c899b992c8489a36e16fedc` |
| Sanfu p143-p144, p147-p148 | 4 of 18 | 178 / 30 / 31 / 13 / 7 | 133 / 45 | 146 | `af20dc453a8566752f1f954ca17daeaa22bdcde4caa4e4a6cede8da48a636dbd` |

CGN is artifact-only diagnostic evidence: its run record is permanently `failed` because
of the frozen OpenMP diagnostic gate. The other five records are
`succeeded_raw_observation`. None is described as publication evidence.

## Source-first findings

### CGN

The layout covers the three page-furniture fields, company title, two-line subject,
guarantee paragraph, body, signature, and date in physical order, with no false table. The
deterministic outline conserves every block. The adjacent H1/H2 subject split remains a
known candidate-level issue rather than being joined by a phrase rule. MinerU types the
three visual page-header fields as ordinary paragraphs in this sample, so the first
retrieval projection conservatively keeps them primary; a future source-bound furniture
hint may demote them, but no company-specific phrase rule is justified.

### Zhongke

The three rendered pages show the main table on pages 1-2 and the independent attachment
table on page 3 with accurate boxes and no invented block. The table projection records two
logical owners and one continuation stub; only the two owners contribute table search
targets. The provider emits no accepted heading candidate, so the document remains one
coarse root unit. Four visually separate attendee lines are flattened in the provider
HTML; their page-local table segment and crop remain available for a human or downstream
agent.

### Caitong

The source is one table spanning four pages. One logical owner plus three evidence-only
continuation stubs preserve Q1-Q7, answers, checkbox, and page geometry while contributing
one table search target. The continuation questions remain table content rather than
polluting the heading path. Each of the four page-local segments is independently
inventoried once.

### JiangHai

On p49-p50, Medium preserves the visually correct narrow financial-table continuation and
all page boxes. The relation is replayed from MinerU's retained/deleted carrier sequence,
not inferred from text, HTML, or visual similarity. On p79-p80, the source table
continuation and subsequent independent tables are all visible; the projection preserves
MinerU's owner boundary and all physical locators without claiming cell-level correctness.

### Sanfu

The p143-p144 and p147-p148 boundaries are decisive negative cases: each following page
starts a different financial statement despite the similar grid. The projection closes the
old owner and opens a new one at both boundaries, retains page-local geometry, and creates
no false cross-page association. Table footer responsibility lines and the next statement
titles remain visible.

## Mechanical closure

For every generated record:

- block assignments equal the provider block count and cover every source index once;
- physical-table inventory count equals the provider segment count and preserves order;
- every table block and physical segment appears in exactly one logical-table part or
  explicit unbound record; all six frozen reports have zero unbound parts;
- heading decisions bind the exact source block and accepted headings open exactly one
  coarse unit;
- every explicit provider payload target is selected exactly once by one coarse unit, while
  typed page furniture and empty continuation carriers remain evidence-only;
- source and layout PDFs are hashed before/after rendering;
- every rendered PNG has path, size, and SHA-256 in `report.json`;
- same-page alias evaluation remains explicitly unasserted.

This closes the DB-free visual-review prerequisite for the provider-native table relation
and retrieval-primary projection. Persistence is already represented by the isolated
provider-document envelope; public table/evidence mapping, sole-writer cutover, and legacy
deletion retain their own independent stops.
