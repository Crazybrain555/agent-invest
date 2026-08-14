# Semantic route and retrieval pilot — 2026-08-13

This is the current data-quality receipt for Unit semantic routing. The clean
development database publishes r37/v54 for all 805 Units from commit `0f4eefb`.
The manifest-bound Unit-only reset and replay re-read the same immutable PDFs,
Provider records, parser bundles and native PDF text while preserving every
source/parse artifact. The public rows, search projection and replay evidence
below are the resulting live development state.

## Current contract

- taxonomy: `semantic-taxonomy-2026-08-r37`, containing 182 financial and
  120 event routes;
- router: `semantic_router.v54`;
- optional adjudicator: `codex_cli.v4.low`, `gpt-5.6-luna`, prompt
  `semantic_route_adjudication.v31`;
- `semantic_keys` contains direct Unit topics only; `semantic_key` is its
  deterministic first member;
- `section_keys` contains exact normalized positions from accepted heading
  paths. Periodic reports use explicit context containers; event filings expose
  only a small section-container allowlist gated by filing type or authoritative
  disclosure-topic scope. It is deterministic,
  never a model candidate, and ranks below a direct Unit route;
- `content_categories` remains a CNInfo Document facet exposed on Document
  reads only. It is not fabricated from Unit text and cannot prove a Unit route;
- no direct or section evidence leaves the two route fields NULL. The exact
  title/path/body search projection remains available, so NULL does not mean
  unfindable;
- Build freezes source/input-bound receipts. Publish verifies and replays them
  without invoking a model.

The model is a low-frequency ambiguity fallback, not a parser or primary
classifier. Exact headings, labeled fields, standardized numeric facts, and
section paths are handled deterministically. The adapter is configuration-
driven and serialized by one bounded semaphore; cache identity includes the
model, effort, prompt, taxonomy, router, sources, candidates, and fixed batch
group.

## Full-corpus replay

The current replay covers every active Unit in ten in-scope filings, including
one full 687-Unit annual report. It is a cross-shape development corpus, not a
statistical claim about 5,000 issuers.

| filing type | Units | direct route | section route | either |
|---|---:|---:|---:|---:|
| annual_report | 687 | 261 | 518 | 585 |
| quarterly_report | 47 | 39 | 21 | 41 |
| convertible_bond | 17 | 12 | 0 | 12 |
| equity_incentive | 14 | 14 | 1 | 14 |
| share_buyback | 19 | 18 | 0 | 18 |
| performance_forecast | 10 | 6 | 0 | 6 |
| operating_data | 6 | 3 | 0 | 3 |
| performance_briefing | 3 | 2 | 0 | 2 |
| investor_relations | 1 | 1 | 0 | 1 |
| correction_supplement | 1 | 1 | 0 | 1 |
| **total** | **805** | **357** | **540** | **683** |

Decision sources are 357 deterministic, 150 deterministic rule abstentions,
and 298 no-candidate fallbacks. Direct route cardinality is: 448 zero, 331 one,
19 two, three three, two five, and two seven.
No Unit exceeds the eight-route cap. Section cardinality is 265 zero, 242 one,
and 298 two.

The full replay completed with zero calls and zero model tokens.
Convertible-bond `1225466824/u6`, “（2）年利息计算”, is now routed by a canonical
regulatory subheading rather than a model. The final reviewed replay is
`/private/tmp/disclosure-semantic-route-r37-source-native-v54-final-fable-fix.json`
(SHA-256 `89d7b3918885c427beb990fea3ecf2ffa20a82fbe157a2d2e5565982f19f780a`).
It is session evidence, not a tracked production artifact. Zero calls prove
that this corpus does not depend on the optional adapter; they do not by
themselves qualify that adapter for a future truly ambiguous Unit.
The DB-free report header records its configured but unused evaluation adapter
as `codex_cli.v4.high` / `gpt-5.3-codex-spark`; the live Build receipts record
the current configured but unused default `codex_cli.v4.low` /
`gpt-5.6-luna`. Because calls and tokens are zero, all 357 direct routes are
deterministic and the differing dormant adapter identity changes no Unit row.

The reviewed candidate was then committed and replayed through the actual
Build/Publish/Search path. The exact ten-run target manifest has SHA-256
`9eeb6b77f630e4b97c88f167bb7271aa969b1ca9312af136a319977d7e52d577`.
The reset removed only the prior 805 Units, 805 search parents, eight body
windows, 25,955 search atoms, 815 Unit/publish outbox rows and 30 Unit sidecars;
all ten raw PDFs, ProviderDocument records, parser trees and processing runs
remained hash-bound and present. Reset receipt:
`/private/tmp/disclosure-final-replay-20260814-r37.5xyg2x/unit_rebuild_reset_receipt_r37.json`
(SHA-256 `cafb633234fe8c2b9a54f89529cd18a253e13b0353ba182903cf8d917cb4d95e`).
Build and Publish succeeded for all ten exact runs, and Search rebuilt all 805
parents with no failure. Replay receipt:
`/private/tmp/disclosure-final-replay-20260814-r37.5xyg2x/unit_rebuild_replay_receipt_r37.json`
(SHA-256 `7cf5d7c86b1a85a9b8ae9081ab760017d6a3313497798ce7958dffae239692cc`).

The live public-view audit reports 805 distinct assets and 805 distinct
Document/order identities, all active. Title, heading path, direct keys,
section keys and filing type match the source replay 805/805. Payloads contain
zero `provider_type` or `semantic_type` residue. The search projection contains
805 parents, eight windows and 25,955 atoms. All ten Documents have exactly one
active and one total run in this clean development database. Audit receipt:
`/private/tmp/disclosure-final-replay-20260814-r37.5xyg2x/unit_data_quality_audit_r37.json`
(SHA-256 `12acf55a42866ba5cf76c78f79f8a8dd51e5b3beb77edbbaa825b83475dfb6f4`).
The 17-query L2/L3 gold passes 17/17 against these live rows, and the full
doctor passes raw/Provider/Unit hashes, PostgreSQL/migrations/views/roles,
search cardinality, remote MinerU canary, queues and orphan checks. Resident
worker and GC jobs remain deliberately unloaded.

The previous source-identity candidate was
`/private/tmp/disclosure-semantic-route-r36-source-native-final.json`
(SHA-256 `3ca6a451e21c2686b73aa6d55b2eb7416ffb887f1af84e66c4066acbe8622e6d`).
The r36→r37/v54 exact row diff changes no title, heading path, direct route,
primary route or decision source. Only equity-incentive `1225339310/u11`, whose
accepted source heading is exactly “本次归属后对公司财务指标的影响”, additionally
gains the scope-authorized `dilution_impact` structural key.

The published r35 baseline is
`/private/tmp/disclosure-semantic-route-v53-tax35-full-current-no-model.json`
(SHA-256 `8f2a859b6bf8c8c1f7fdb060caa97bc1103a628f0b31f5239c3dbe55e87093dc`).
The r35→r36 source replay changes no direct-route cardinality. Six annual Units
beneath the exact accepted heading `公司投资情况` gain `investment_analysis`;
the quarterly `3/4/5/6` numbered roots and their following paths are corrected;
units 11–12 newly inherit the `share_changes` section route from `3 股东信息`;
generic `利息收入`、`利息支出`、`资本管理` routes lose the misleading `bank_`
prefix while genuinely bank-specific routes retain it. A continuation-marked
stale audit ancestor is removed only after a long page gap plus the same
unnumbered provider title at near-identical bbox positions on two intervening
pages; non-continuation, different-position and adjacent one-page negatives
remain nested.

The same replay calibrated native-PDF numeric reconciliation over all ten source
documents: 2,860 exact MinerU text rectangles yielded eight repairs in two PDFs
and 2,852 unchanged observations. The repairs restore only omitted numbers such
as dates, periods, percentages and short numeric headings. Admission requires
the same immutable PDF hash/page count, the exact provider block/bbox, and a
deletion-only proof: native text may add complete numeric cores (with a trailing
`%/‰` removed or retained), while every other character and existing number
remains in source order; ASCII space/tab immediately adjacent to a deleted
numeric token may disappear with that token or remain as the MinerU placeholder.
Malformed grouping that lexes into adjacent unsigned numeric atoms is rejected
rather than treated as another omitted number. Isolated PDFium-generated `CRLF`
line breaks preserve an ASCII word boundary, so
a repaired wrapped English phrase cannot be fused; one calibrated terminal
rectangle space is removed only when that same bounded-text observation contains
a PDFium-generated line break, while NUL, bare `CR/LF`, blank lines and all other
boundary/inner spacing (including whitespace adjacent to `CRLF`) remain exact. ProviderDocument
keeps the original MinerU text; `provider_unit_locator.v2` stores raw-block plus
provider/source text hashes and Publish replays the PDF. No table, nonnumeric
difference, alternate reading order, or second parser structure is admitted.

A post-replay rejection audit examined all 18 rectangles where native PDF text
contained more numeric tokens but the deletion-only proof correctly abstained.
For 15/18, the complete native text (ignoring provider spacing) already exists
in the final Unit: these are cross-page or split-block empty carriers, not lost
search content. Three rectangles contain real, visually confirmed MinerU
omissions that cannot be repaired without also tolerating nonnumeric drift:
`1225067794/source_index=1476` omits the note reference/year,
`1225067794/source_index=3424` omits dates and part of the bond name, and
`1225231394/source_index=121` omits `(2)`, `2024` and `=`. Their surrounding
body/table content, heading context and direct/section retrieval routes remain
present. The service preserves the conservative Provider text rather than
weakening the source proof from three examples. Audit receipt:
`/private/tmp/disclosure-final-replay-20260814-r37.5xyg2x/native_rejected_numeric_audit_r37.json`
(SHA-256 `3f80a5c33ad42f53e70bee92fd840ec4b1e4982ec9c06a8a145c2d16357eecd8`).

An independent DB-free replay covers 822 Units across performance flash,
inquiry notice, major contract, delisting risk, rights issue, restructuring,
and a full 221-page semiannual filing. It has 364 direct-routed Units, 580
section-routed Units, 688 with either route surface, 134 lexical-only Units,
and zero model calls. Final report:
`/private/tmp/disclosure-heldout-20260813-r1/heldout-eval-semantic_router.v54.json`
(SHA-256 `3674b57444257d1434cfdab6ea2a465af0553bce6a71d6e42fd9e5d39e1beaec`).
The event slice adds exact, scoped section recall for rights-issue issuance/
subscription/outcome sections and restructuring impact/risk/rationale/
classification/supporting-finance sections. A 12-case source-reviewed direct-
and-section gold passes 12/12 (gold SHA-256
`f23106e2960e1471465a2abac96426f5f5610e94517005cca528cf3d9ffc5ead`;
review SHA-256 `201137c13931ae932169bf0af81a5f9de7a3ba4390747162461bd4d75a9dc736`).
Risk headings such as “标的资产评估值风险” are centred on `transaction_risk`
instead of treating the embedded noun as the Unit's primary topic.

The 221-page semiannual source also constrained two outline rules. A provider
table by itself is evidence content, never proof of a missing section boundary;
the environmental subsection therefore remains conservatively under the last
source-bound parent rather than inventing a heading that MinerU merged into a
caption. An unnumbered weak label exits a completed parenthesized subgroup only
when the immediately following provider block explicitly restarts the same
numbering family at one. This fixes the source-visible receivables subgroup
labels while distant or lower-ordinal lookahead remains nested.

The earlier spanning-title/header-row rule changes exactly one of 805 current
Units—the actual Q&A table whose second row is “序号/提问内容/回复内容”—and zero
of these 822 held-out Units. The earlier typed-field slice changes only six
current rows, all source-reviewed form/header or canonical bond-heading
positives.

## Source and regression review

The tracked direct-route gold contains 20 source-reviewed positive/negative
cases (SHA-256
`11c7db254b3b9738ae714607ed9016920cbcbcd6bfea27addf81f627df8ba480`).
All 20 pass on the current no-model replay.

The last taxonomy change adopted only stable regulatory/report headings, not
document phrases: quarterly metrics, operating and financial condition,
customer/supplier concentration, profit distribution, investor relations,
board committees, management remuneration, and risk management. Full-corpus
diff review found and removed one important false-positive family:
“审计委员会” is a structural board-committee section, not a direct Unit route.
That prevents a quarterly-report important notice from acquiring a committee
topic merely because its body says the report was reviewed by the audit
committee.

The same diff adds directly useful routes for annual-report Units such as
“分季度主要财务指标”, “主要客户”, and “主要供应商”, while `其他信息` remains outside
the narrow taxonomy. No rule branches on issuer, provider document ID, or an
observed full sentence.

Two adversarial table families remain explicit stop regressions: an exclusive
whole-statement container locks only on an exact source heading, and ordinary
table data cells never become labeled-field evidence. Definitions/glossary
tables likewise remain lexical only. Raw table HTML and all visible search
segments are byte/source-preserving; typed roles are an additional routing
view, not a payload rewrite.

## L2/L3 retrieval acceptance

Route population is not the product objective by itself. The tracked
`semantic-retrieval-query-gold.v1.json` evaluates the planned L2/L3 union:

1. explicit direct route keys selected from the public semantic route catalog;
2. exact structural section keys;
3. current source-bound lexical title/path/body/atom projection as fallback and
   tie-breaker.

The check does not infer keys from natural language and is not a production
ranking endpoint. It verifies whether a downstream planner that knows the
catalog can retrieve the right Units without embeddings.

All 17 source-reviewed queries pass their must-find, precision-at-reviewed-K,
and forbidden-result gates. They cover buyback funding/account, incentive
recipients, forecast ranges, capital adequacy, bond-interest calculation,
quarterly metrics, customers/suppliers, audit opinion, sales volume/price,
investor Q&A, corrected data, and a risk-management section. The audit query
does not return annual-report Unit `u292` (“五、其他信息”) in its top five. A
separate literal query for `其他信息` does return that Unit at rank one through
lexical search even though it has no fabricated narrow semantic key.

The pre-r35 live query baseline is
`/private/tmp/disclosure-semantic-retrieval-query-v53-tax34.json`
(SHA-256 `2309510216e08ffbe677738dbc1e43d612e2d83b101bf55abdac686eb3774fe3`).
The tracked query gold SHA-256 is
`6df04c728f2feffac308edf910e5124661ed4348fb38365c94bc2352665607fc`.

## Accepted NULL semantics

`semantic_keys = NULL` is acceptable only when it remains honest and
retrievable. It may mean the Unit is a heading-only carrier, signature/contact
boilerplate, a negative/applicability section, or real content not yet covered
by the closed vocabulary. It never means “other” unless the source itself says
that. L2/L3 can still find it by Document facets, section route, or lexical
title/path/body search.

`content_categories = NULL` has a different meaning: the source Document did
not carry a CNInfo category facet. The router must not fill that provider field
from Unit topics. Filing type, disclosure topics, Unit routes, section routes,
and lexical search are separate retrieval surfaces.

The pre-0037 read-only public-view audit makes that distinction concrete: the
r37/v54 clean replay has 357 direct-routed Units and 540 section-routed Units.
Only two Documents carried a non-NULL CNInfo content facet; it was then repeated
across 23 Unit rows while the other 782 Unit rows were NULL. 0037 removes that
repetition from Unit reads. Content-bearing Units must not be used to fabricate
a missing provider category; their retrieval support comes from the
direct/section/lexical surfaces above.

The same audit finds 156 `{"text": ""}` payloads. All 156 are heading-only
Units with a non-NULL title, nonempty hash-bound heading chain and nonempty
title search tokens; none is a body block silently replaced by an empty value.
Of those, 102 are intentionally lexical-only because neither a direct nor a
section route is honestly supported.

## Remaining production gates

- The current corpus makes no model call. Before a future ambiguous Unit may
  use the optional CLI chooser, its closed no-tool boundary, cancellation,
  retry/backoff and exact deployed model identity still require an explicit
  production canary; do not infer adapter eligibility from the zero-call run.
- The 17-query gold proves the reviewed cases, not full query-language recall.
  Held-out process classes and atypical PDF layouts remain required.
- The three visually confirmed local MinerU omissions above are retained as an
  explicit upstream quality limit. Broadening native-PDF repair to nonnumeric
  drift requires a new source-bound contract and cross-document evidence; it
  is not authorized by these samples.
- The manifest-bound replay, live row inspection, held-out source inspection
  and full doctor/canary pass are complete. Independent live-data review is
  also complete; user visual acceptance remains required before a production-
  readiness claim or worker enablement.
