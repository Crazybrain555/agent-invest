# MinerU Medium greenfield visual review

Date: 2026-08-11

Scope: DB-free provider record, deterministic outline, coarse units, and review projection

Provider lane: MinerU 3.4.4 Hybrid-medium

## Verdict

The first greenfield visual stop is **passed for the DB-free foundation**. The reader,
outline, coarse-unit partition, page locators, and physical-table inventory preserve the
reviewed source content and geometry without invoking the legacy proof system. This is not
a publication or retrieval-owner approval.

The stop intentionally leaves three visible limitations for later, separate stages:

- CGN's two-line subject remains split into adjacent H1/H2 candidates;
- Zhongke's visually obvious document title is not a provider title candidate, and the four
  attendee lines remain flattened inside the provider table HTML;
- alias evaluation and the L1 retrieval-primary projection are not implemented.

Those limitations must not be repaired with document-specific phrases, fuzzy block
matching, or table-cell invention. Source-bound style/bookmark hints and the optional
whole-outline reviewer may adjust only existing heading candidates. Table content remains
coarse when its logical continuation is uncertain.

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
another source page. The report never associates a physical table segment with a flat
block, unit, or adjacent-page table. It records alias state as `not_implemented` /
`not_evaluated` rather than deriving a string or geometry heuristic.

## Frozen review reports

All reports were generated beneath
`/private/tmp/disclosure-anchor-medium-visual-review-20260811-r4`. The full SHA-256 values
below bind the JSON records; rendered PNG hashes are contained inside each record.

| Review | Pages rendered | Blocks / headings / units / segments | `report.json` SHA-256 |
|---|---:|---:|---|
| CGN | 1 | 13 / 3 / 4 / 0 | `26ff7682f67080cddfd55686f011aa92de3fabe44b6cce58eb0492308ea1f55e` |
| Zhongke | 3 | 7 / 0 / 1 / 3 | `d73c420d11f7e50eb5ad423a506ce429467b806575f0e7c0adbfaf579cbf7a5c` |
| Caitong | 4 | 8 / 1 / 2 / 4 | `ee22ded42180837af306953fb7cb91b700781c2b7764e22d28f636cf5f129297` |
| JiangHai p49-p50 window | 2 of 8 | 34 / 2 / 3 / 12 | `067855cf8a74bf144bea81c622fac5427aa7ff96d17dc31d4b35df869e0f3193` |
| JiangHai p79-p80 full bundle | 2 of 161 | 2020 / 531 / 531 / 340 | `3217c1d7e855f3899cd6af4d55904708fbef90f92ab2b7b05c85e89aacf4dd79` |
| Sanfu p143-p144, p147-p148 | 4 of 18 | 178 / 30 / 31 / 13 | `1fae92321d5ba07f537bcc98199a03c524bfbdf8512a0f77c10d0f8ea5f1d0aa` |

CGN is artifact-only diagnostic evidence: its run record is permanently `failed` because
of the frozen OpenMP diagnostic gate. The other five records are
`succeeded_raw_observation`. None is described as publication evidence.

## Source-first findings

### CGN

The layout covers the three page-furniture fields, company title, two-line subject,
guarantee paragraph, body, signature, and date in physical order, with no false table. The
deterministic outline conserves every block. The adjacent H1/H2 subject split remains a
known candidate-level issue rather than being joined by a phrase rule.

### Zhongke

The three rendered pages show the main table on pages 1-2 and the independent attachment
table on page 3 with accurate boxes and no invented block. The provider emits no accepted
heading candidate, so the document remains one coarse root unit. Four visually separate
attendee lines are flattened in the provider HTML; their page-local table segment and crop
remain available for a human or downstream agent.

### Caitong

The source is one table spanning four pages. The owner payload plus three continuation
segments preserve Q1-Q7, answers, checkbox, and page geometry. The continuation questions
remain table content rather than polluting the heading path. Each of the four page-local
segments is independently inventoried once.

### JiangHai

On p49-p50, Medium preserves the visually correct narrow financial-table continuation and
all page boxes; the review does not turn that observation into a guessed segment identity.
On p79-p80, the source table continuation and subsequent independent tables are all visible.
Medium's conservative logical split at this boundary retains more physical locator truth
than an eager merge and is acceptable for a coarse unit.

### Sanfu

The p143-p144 and p147-p148 boundaries are decisive negative cases: each following page
starts a different financial statement despite the similar grid. Medium keeps the tables
separate, retains page-local geometry, and does not create a cross-page association. Table
footer responsibility lines and the next statement titles remain visible.

## Mechanical closure

For every generated record:

- block assignments equal the provider block count and cover every source index once;
- physical-table inventory count equals the provider segment count and preserves order;
- heading decisions bind the exact source block and accepted headings open exactly one
  coarse unit;
- source and layout PDFs are hashed before/after rendering;
- every rendered PNG has path, size, and SHA-256 in `report.json`;
- alias and cross-page table association are explicitly unasserted.

This closes the visual-review prerequisite for continuing with the optional outline
reviewer and provider-document persistence. Persistence, public table mapping, retrieval
ownership, sole-writer cutover, and legacy deletion retain their own independent stops.
