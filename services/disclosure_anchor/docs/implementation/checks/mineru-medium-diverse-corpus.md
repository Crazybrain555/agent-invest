# MinerU Medium production-scope acceptance corpus

## Scope and purpose

This corpus validates the exact MinerU 3.4.4 Hybrid-medium/full-PDF/merge-on
writer against the document classes that this service is configured to
process. It is not a general PDF benchmark and it is not an annual-report-only
benchmark. The normative scope is the 20 values in `config/processing_policy.json`
under `process`:

`annual_report`, `semiannual_report`, `quarterly_report`,
`performance_forecast`, `performance_flash`, `operating_data`,
`investor_relations`, `performance_briefing`, `inquiry_regulatory`,
`restructuring_assets`, `additional_issuance`, `convertible_bond`,
`rights_issue`, `share_buyback`, `major_contract`, `risk_alert`,
`delisting_risk`, `equity_incentive`, `equity_share_change`, and
`correction_supplement`.

The 11 `register_only` classes, arbitrary external PDFs, and image-only scans
are not completion criteria for this writer. A future policy revision that
moves one of those classes into `process` must add a representative source
case before rollout. Every rule here is a source-bound failure-family rule;
provider document IDs, company names, and literal document phrases are never
runtime branches.

## Process-class coverage

| Process class | Representative source evidence | Coverage kind |
|---|---|---|
| annual_report | 571286, 573256, 1213105553, 1219768304, 1222856328 | provider-classified; old and current formats |
| semiannual_report | 1217616113, 1221054498 | provider-classified; table-dense and multilingual |
| quarterly_report | 1221570547 | provider-classified |
| performance_forecast | 1225450112 | provider-classified; correction and numeric ranges |
| performance_flash | 1225457637 | provider-classified; compact financial table |
| operating_data | 1217574006 | provider-classified; multiple short tables |
| investor_relations | 100001036802 | provider-classified; Q&A without reliable headings |
| performance_briefing | 100001053504 | provider-classified; Q&A |
| inquiry_regulatory | 1222355452 | provider-classified; multi-line document title |
| restructuring_assets | 1225469289 | provider-classified; deep outline and long mixed content |
| additional_issuance | 1225469289 | provider topic on the same source filing |
| convertible_bond | 1220510536 | official CNInfo source re-query derives this class; local baseline registration omitted raw category |
| rights_issue | 1225464994 | provider-classified; source numbering typo retained |
| share_buyback | 1223032208 | exact business form covered; CNInfo parent code classifies this source as `equity_share_change`, which is also processed |
| major_contract | 1225437561 | provider-classified; one-page announcement |
| risk_alert | 1223236784 and 1225448705 | provider-classified/topic; signature and delisting variants |
| delisting_risk | 1225448705 | provider-classified; multi-line printed title |
| equity_incentive | 1221246012 | official CNInfo source re-query derives this class; local baseline registration omitted raw category |
| equity_share_change | 1223032208 | official CNInfo source re-query derives this class |
| correction_supplement | 1225450112 | provider topic on the same correction filing |

The three rows that appear as `other` in the development database were
registered from local PDFs with `provider_metadata={}` before the clean
provider replay. Read-only re-query of the official CNInfo index returned:

- 1220510536: `01010503||010112||010115||010915||010999` ->
  `convertible_bond`;
- 1221246012: `01010503||010112||010115||012325` ->
  `equity_incentive`;
- 1223032208: `01010503||010112||011513` ->
  `equity_share_change`.

This is a test-registration metadata limitation, not a production admission
gap: all three derived classes are in `process`. The Midea document is also a
share-buyback form, but the provider emits only the broader `011513` code; no
parser behavior depends on that class label.

## Frozen source matrix

All rows below completed Parse -> Build -> Publish under the current Provider
writer. Source SHA-256 identifies the immutable PDF.

| Security / provider document | Pages | Primary acceptance surface | Source SHA-256 |
|---|---:|---|---|
| 600519 / 571286 | 55 | old annual-report fonts and small image marks | `433fceec0b5b93e27ab51bd9b93bda967946a1c572e58e2c91dea49671131830` |
| 000001 / 573256 | 116 | old stencil-glyph annual report | `638410c29eb9e6c6a56ad96b1dec18cd02417ffdc8f597c4e77bf888ff228fbc` |
| 603725 / 1213105553 | 231 | complex annual outline and repeated page frames | `8d1319b34015bb546dd07b627e0d40cd50f5b976c4ba38513b29c8db1e60e177` |
| 603078 / 1217574006 | 2 | operating-data tables and unique typed footer | `9af8736f1888eae54767ec403341b354d319cd3f8836fc9ce22dcd9d2e7bda5b` |
| 002484 / 1217616113 | 161 | narrow cross-page cells, PUA checkboxes, missed headings | `06630ba9c28f9f0f7b7e66357a0ed2447d547eedc4f0b0ec1447fd0a23d07b98` |
| 688359 / 1219768304 | 296 | long annual report, diagrams, look-alike adjacent tables | `914450b9b4832340552da428cc1668e8ebe0f33c8a5e1aa58fde6ae2e6cb3e7f` |
| 601108 / 100001053504 | 4 | performance Q&A without stable headings | `f3a972945d62e4a4ffdccd3dd6cc02e591e109a80b86dcfa150b2fee56983000` |
| 688361 / 100001036802 | 3 | investor Q&A and multiple logical tables | `a9e806911a1410fe80262eb821abdba375ae3d2b8d1089983cc762ac1473a303` |
| 301046 / 1220510536 | 3 | convertible-bond share table and repeated headers | `2f4896c6977572d7384462407a445fa4c8e7fb0f1ce053457c61b4d479fbbab4` |
| 688235 / 1221054498 | 232 | multilingual labels, 222 tables, split trademark glyphs | `5ffb8c9225efa6e84c7c901c7a2eb6b1c80c5aedc893c53428def3abba587ac3` |
| 300750 / 1221246012 | 1 | short equity-incentive notice and signature | `c55420dea3436d7aaf59b352b699a2ea7f5ac0a575c7da0f1ce10b45c7027568` |
| 600208 / 1221570547 | 17 | quarterly financial tables and repeated frames | `30b4810923ff65a77e4880427bb9336cc3edbb0c3089aa6b62de2a0c7edb21ee` |
| 003816 / 1222355452 | 1 | inquiry notice and multi-line title | `89af4fadffc6f34eaf6a04f873761dbf3529fb50afc42ee83bd0a93d66c02e99` |
| 600941 / 1222856328 | 29 | colored hierarchy, images, stale-numbering hard negative | `b2b9b9c3f440f81bd0bc97861bd13a0a9aaadfe030493ea03feaed1f5c1299c1` |
| 000333 / 1223032208 | 7 | buyback scenarios; adjacent similar tables must stay separate | `df24f79882f88529b9881fa8ec9df7c9c33d20f1be0c5e77675daf4e67218d76` |
| 002601 / 1223236784 | 10 | risk notice; terminal board/date signature | `994234c36d78dfc41ffec60d8098c5738623c1887fc415ba409e05726058010b` |
| 601669 / 1225437561 | 1 | one-page major contract with material numbers | `c22904c7f339a76e6fa2be3246b7b5d740a52c3beaa32f41817fa41daa686bc0` |
| 688496 / 1225448705 | 4 | delisting risk, bullet PUA, multi-line printed title | `0a46316ac0a033feff9890aab2fd567630045b2d316d10c17567f08c882ba5bb` |
| 600889 / 1225450112 | 3 | forecast correction and before/after numeric ranges | `42c67f2261fc1474a563408ed109d5b6538bfde2192e894f1b5870a71e4c0028` |
| 600919 / 1225457637 | 3 | performance-flash financial table | `e0aa91c79217a662e0fab2814566e2b32d647f36df49aa1962e9327974298b78` |
| 300176 / 1225464994 | 11 | rights issue; source-authored numbering inconsistency | `40da12f5339258328393cf0714c383c04223c2a2e92f1f712e54a58de85a9b94` |
| 000670 / 1225469289 | 73 | restructuring, issuance, deep outline, mixed tables/text | `05bf588c073517d13b05752d65841f1db140dfb7efd79b78df508391288a7542` |

## Current mechanical receipt

The clean development database contains 22 current documents and 3,513 active
Units: 2,005 mixed, 101 table, and 1,407 text. The current search projection
contains 57,376 atoms. After the independent row review, 3,509 Units are `ok`
and four source-bound Beigene titles are explicitly `needs_review`: MinerU
retained only `<sup>®</sup>` plus BAT1706/BTK/PD-1/PARP1-PARP2 while the PDF and
body text retain the adjacent Chinese drug names. No text was synthesized or
overwritten. Across the active Provider records:

- 16,849 provider blocks, 1,577 physical table segments, 1,261 logical tables,
  and 14,628 retrieval targets have complete, exactly-once ownership;
- zero logical-table relations are unbound and every active Unit uses the
  closed `provider_unit_locator.v1` contract;
- all 3,513 active Unit search projections replay exactly from the current
  payload and locator; no title/path target is duplicated into body search;
  word-token projection uses visible inline-HTML text while exact body atoms
  retain the provider scalar, and all rows use `rp-2026.08-provider-unit-v2`;
- all 69 evidence references on the six newly added provider-classified
  documents resolved to hash- and size-matching JPEG bytes (11,590,058 bytes);
- rebuild replay for the four documents affected by the latest outline rules
  completed with zero unassigned table parts.

Visual source checks confirmed every newly admitted numbered heading on its
rendered PDF page. The checks included long Chinese headings, compact risk
sections, annual-report chapter transitions, the JiangHai p49-50 narrow table,
and the hard-negative adjacent tables in Sanfu and Midea.

## Residuals and limits

These are explicit residuals, not silently repaired data:

1. A printed document title split over multiple provider title blocks may
   still become adjacent root Units. Text, locators, and search content survive,
   but the title is not yet one source occurrence. A future fix needs bounded
   multi-block source geometry, not a company/title phrase list.
2. A unique typed page header can remain inside body order when MinerU emits it
   between sentence fragments. This is the conservative no-loss choice; only
   text repeated in the same frame role across pages is removable furniture.
3. Provider-emitted PUA bullets and checkbox glyphs remain source scalars. They
   are not translated through an unverified Unicode dictionary.
4. This is failure-surface coverage across all configured process classes, not
   a statistical sample of every issuer/template. A later rollout rehearsal
   should stratify by these classes and layout risks, then add only newly
   observed general failure families.
5. Pure image-only scans are outside the current processing-policy acceptance
   boundary. The immutable PDF is still retained if such a document is ever
   registered, but scan-specific reconstruction is not a release gate.

## Acceptance rule

Every behavior change must name the failure family, source-bound invariant,
and fail-closed boundary, then pass a representative positive and adjacent
negative from this in-scope corpus. No rule may branch on a provider document
ID, issuer, or literal filing phrase. Provider payloads are preserved rather
than repaired; uncertainty is represented by conservative structure or
`needs_review`, never by fabricated text, cells, or confidence.
