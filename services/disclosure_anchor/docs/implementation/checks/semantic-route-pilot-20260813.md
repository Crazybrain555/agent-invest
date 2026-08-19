# Semantic route and retrieval pilot — 2026-08-13

This receipt distinguishes the current source-identity generation from the
historical development baselines retained below. The development database is
at migration 0038 and all ten active Documents publish provider_unit.v9 /
taxonomy r45 / router v77. The same current bytes were replayed DB-free over the
ten immutable Provider documents and seven held-out documents, then exercised
through the live Build/Publish/Search path without resetting parse or Provider
artifacts. Historical r37/r43 receipts remain only as comparison evidence.

## Current contract

- taxonomy: `semantic-taxonomy-2026-08-r45`, containing 184 financial and
  123 event routes;
- router: `semantic_router.v77`;
- optional adjudicator: `codex_cli.v4.low`, `gpt-5.6-luna`, prompt
  `semantic_route_adjudication.v31`;
- `semantic_keys` contains direct Unit topics only; `semantic_key` is its
  deterministic first member;
- `section_keys` contains exact normalized positions from accepted heading
  paths. Periodic reports use explicit context containers; event filings expose
  only a small section-container allowlist gated by filing type or authoritative
  disclosure-topic scope. It is deterministic,
  never a model candidate, and ranks below a direct Unit route;
- `content_categories` remains a CNInfo Document facet. Deprecated
  `document_units_v1` joins it only for compatibility; current
  `document_units_v2` omits it. It is not fabricated from Unit text and cannot
  prove a Unit route;
- no direct or section evidence leaves the two route fields NULL. The exact
  title/path/body search projection remains available, so NULL does not mean
  unfindable;
- Build freezes source/input-bound receipts. Publish verifies and replays them
  without invoking a model.

The model is a low-frequency ambiguity fallback, not a parser or primary
classifier. Exact headings, labeled fields, bounded Unit-local quantitative
topics and section paths are handled deterministically. The adapter is configuration-
driven and serialized by one bounded semaphore; cache identity includes the
model, effort, prompt, taxonomy, router, sources, candidates, and fixed batch
group.

## Full-corpus replay

The current replay covers every active Unit in ten in-scope filings, including
one full 686-Unit annual report. It is a cross-shape development corpus, not a
statistical claim about 5,000 issuers.

| filing type | Units | direct route | section route | either |
|---|---:|---:|---:|---:|
| annual_report | 686 | 277 | 540 | 591 |
| quarterly_report | 46 | 40 | 21 | 41 |
| convertible_bond | 16 | 13 | 0 | 13 |
| equity_incentive | 14 | 14 | 1 | 14 |
| share_buyback | 19 | 18 | 0 | 18 |
| performance_forecast | 10 | 7 | 0 | 7 |
| operating_data | 5 | 3 | 0 | 3 |
| performance_briefing | 2 | 2 | 0 | 2 |
| investor_relations | 1 | 1 | 0 | 1 |
| correction_supplement | 1 | 1 | 0 | 1 |
| **total** | **800** | **376** | **562** | **691** |

Decision sources are 376 deterministic, 137 deterministic rule abstentions,
and 287 no-candidate fallbacks. Direct route cardinality is: 424 zero, 348 one,
21 two, three three, one four, one five, and two seven. No Unit exceeds the
eight-route cap. Section cardinality is 238 zero, 207 one, 350 two, and five
three.

The current DB-free replay completed with zero calls and zero model tokens.
The final reviewed replay is
`/private/tmp/disclosure-semantic-route-r45-v77-post-pro-p2-current.json`
(SHA-256 `0d5df1f520120314d3e4d8009ba670e839454805a0be8c410ba73c8aa2969098`).
It is session evidence, not a tracked production artifact. Zero calls prove
that this corpus does not depend on the optional adapter; they do not by
themselves qualify that adapter for a future truly ambiguous Unit.
Because calls and tokens are zero, all 376 direct routes are deterministic and
the configured dormant adapter identity changes no Unit row. All 800 rows carry
their source-rebuilt `query_projection_hash` and match the current live
generation exactly.

## Historical r43/r37 comparison evidence

Relative to the published r37 baseline, r43/v73 treats direct routes as
bounded Unit-local topics rather than realized-fact assertions. History, risk,
forecast, plan, condition, causality, negation and not-applicable values do not
erase an otherwise valid coarse-topic witness. Exact forecast
period/range/comparison/basis/risk roles remain source-role anchors: numbers inside
one role cannot manufacture another role. Exact TOC/whole-statement carriers
remain mechanical exclusives, strict typed table fields/headers are equivalent
to the same allowlisted body topic, and ordinary table cells remain lexical.
When controlled labels overlap, one source occurrence locks only the longest
field; an independently present shorter field remains eligible. The
v72 role arbiter likewise preserves an independently typed forecast range under
an exact period heading; only a role whose evidence is merely the shared number
is suppressed. v73 retains that boundary and adds `business_risk` only for a
periodic/operating Unit whose own title explicitly contains risk and whose own
payload has content; heading-only anchors and accounting-policy measurement
sections remain excluded. It changes 19 current and 14 held-out query hashes,
but no Unit boundary or content hash. The adjacent synthetic
period-plus-typed-range regression still keeps both source roles. The
source-reviewed direct-route gold is 25/25 (gold SHA-256
`13db857efc81ab94127cd98d69f0b3991acc68669646848370cc34df5f88bf4d`).

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
keeps the original MinerU text; current `provider_unit_locator.v3` preserves the
v2 raw-block/provider/source hashes and also binds the exact heading payload
ordinal. Publish replays the PDF. No table, nonnumeric
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

An independent DB-free replay covers 824 Units across performance flash,
inquiry notice, major contract, delisting risk, rights issue, restructuring,
and a full 221-page semiannual filing. It has 376 direct-routed Units, 608
section-routed Units, 716 with either route surface, 108 lexical-only Units,
and zero model calls. Final report:
`/private/tmp/disclosure-heldout-20260813-r1/heldout-eval-r43-v73-chash.json`
(SHA-256 `4dd628d198229580cd021327d3aa59638eab3b258c927e0f9b87244ba2ae05ff`).
The event slice adds exact, scoped section recall for rights-issue issuance/
subscription/outcome sections and restructuring impact/risk/rationale/
classification/supporting-finance sections. A 21-case source-reviewed direct,
section and exact-heading-path gold passes 21/21 (gold SHA-256
`2c6c25edcb6fe10704a33d6e6560fa3a7b8335e1ec8174905297277b7a502519`).
Risk headings such as “标的资产评估值风险” retain both `transaction_risk` and a
corroborated object topic such as `target_asset`; the risk frame cannot erase
an independent Unit-local topic. The semiannual management-team and
raw-material-risk Units likewise retain `revenue_and_cost` when their own body
contains a controlled local topic witness; L2, not L1, interprets causal or
conditional modality.

The 221-page semiannual source also constrained the outline boundary. A provider
table by itself is evidence content, never proof of a missing section boundary.
When a table block has exactly one nonempty caption that is itself a strong root
numbered heading, that exact caption payload occurrence may be the source-bound
heading while the same block retains its table body. This recovers the missing
`四、纳入环境信息...` parent without inventing text, moves its descendants out
of the incentive subtree, and gives content descendants the standardized
`governance` + `environment_social` structural contexts. Ordinary captions,
table labels and incidental numbered text remain body evidence. An unnumbered
weak label exits a completed parenthesized subgroup only
when the immediately following provider block explicitly restarts the same
numbering family at one. This fixes the source-visible receivables subgroup
labels while distant or lower-ordinal lookahead remains nested.

The earlier spanning-title/header-row rule changes exactly one of 805 current
Units—the actual Q&A table whose second row is “序号/提问内容/回复内容”—and zero
of these 824 held-out Units. The earlier typed-field slice changes only six
current rows, all source-reviewed form/header or canonical bond-heading
positives.

## Source and regression review

The tracked direct-route gold contains 25 source-reviewed positive/negative
cases (SHA-256
`13db857efc81ab94127cd98d69f0b3991acc68669646848370cc34df5f88bf4d`).
All 25 pass on the current no-model replay.

The taxonomy adopts only stable regulatory/report headings, not
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

Route population is not the product objective by itself. The historical binary
`semantic-retrieval-query-gold.v1.json` is retained for comparison; current
acceptance uses graded `semantic-retrieval-query-gold.v4.json` to evaluate the
planned L2/L3 union:

1. explicit direct route keys selected from the public semantic route catalog;
2. exact structural section keys;
3. current source-bound lexical title/path/body/atom projection as fallback and
   tie-breaker.

The check does not infer keys from natural language and is not a production
ranking endpoint. It verifies whether a downstream planner that knows the
catalog can retrieve answer-bearing Units, directly relevant evidence and
bounded adjacent context without embeddings. It reports Success@5, graded
Recall@10/20, nDCG@10, narrow/broad returned precision, mechanical-carrier
leakage and direct/section/lexical/neighbour ablations. A v4 review refuses to
run unless every source-identity row has the same `query_projection_hash` and
answer-bearing `content_hash` as the live Build/Publish/Search generation.
Every Unit entering any evaluated full or ablation top-20 pool has an explicit
reviewed grade 0--3 (mechanical exclusions are explicit grade 0), and every
judgment is bound to both hashes. Unjudged results are unknown and fail the
review rather than silently becoming negatives; changed body/table content
therefore cannot inherit an old human grade even when its query projection is
unchanged. Returned precision measures purity among results actually emitted
within K; it is not fixed-denominator P@K. Recall and nDCG independently
penalize omissions and ordering.

All 17 source-reviewed queries passed their must-find, precision-at-reviewed-K,
and forbidden-result gates against the published r37/v54 live rows. They cover
buyback funding/account, incentive
recipients, forecast ranges, capital adequacy, bond-interest calculation,
quarterly metrics, customers/suppliers, audit opinion, sales volume/price,
investor Q&A, corrected data, and a risk-management section. The audit query
does not return annual-report Unit `u292` (“五、其他信息”) in its top five. A
separate literal query for `其他信息` does return that Unit at rank one through
lexical search even though it has no fabricated narrow semantic key. This is a
historical live baseline, not proof that the then-uncommitted v8 Unit identities
match the old search projection. After the exact v8 Build/Publish/Search replay,
the evaluator must bind each semantic row to the live `query_projection_hash`
before the same 17-query result can be claimed for the current candidate.

The pre-r35 live query baseline is
`/private/tmp/disclosure-semantic-retrieval-query-v53-tax34.json`
(SHA-256 `2309510216e08ffbe677738dbc1e43d612e2d83b101bf55abdac686eb3774fe3`).
The historical v1 query gold SHA-256 is
`6df04c728f2feffac308edf910e5124661ed4348fb38365c94bc2352665607fc`.
The v4 thresholds are provisional acceptance budgets derived from the third
Pro review; they become passed evidence only after the exact v8 live replay and
manual review of every returned top-20 row. They require grade-3 Success@5 at
least 98%, grade-3 Recall@20 100%, grade>=2 macro Recall@10/20 at least 90%/95%,
nDCG@10 at least 0.85, narrow returned-precision@5 at least 0.80 and broad
returned-precision@10 at least 0.60 (both measures count reviewed grades 1--3
as relevant among emitted results), at
most one grade-0 Unit in any top five, and zero reviewed TOC/furniture carrier
in any top ten.

The superseding source-wide live r43/v73 review is
`/private/tmp/disclosure-semantic-retrieval-query-r43-v73-v8-20260817.json`
(SHA-256 `708b7119cc858598aebc055a8a3e10021d9a45fb0917d9e0e4d6fd86c7f5609f`).
The tracked v4 gold SHA-256 is
`62f9e17199cfd5da7ab2324b047c97e201bd55cf9ab28ba9e965f47281a105d0`.
It binds all 805 source rows and 138 explicitly judged identities to exact live
query and content hashes.  Positives are not limited to full/ablation result
pools: a separate ranker-independent source-wide sweep checked taxonomy/query
lexical witnesses, oversized Units and bounded neighbours, and all verified
positives remain in the Recall denominator even when every ranker misses them.
The evaluated retrieval union keeps lower-ranked lexical results when a direct
route exists, supports semantic-key any/all filters, source-bound phrase ANY and
AND-token lexical plans, and a filing-type ranking preference that cannot create
a result without direct/section/lexical evidence.
The r43 correction keeps the display intent separate from its explicit fielded
lexical plan: “销量价格” uses `销售量 AND 价格变动`, bond-interest recall unions
terms/method/date roles plus the source-bound lexical alternatives `年利息计算` /
`单利按年计息`, the quarterly equity query unions the independent phrases
`未分配利润` / `其他综合收益` and prefers quarterly filings, and the broad risk
query unions the coarse risk topic, specific warning and two structural risk
sections with lexical `风险`. This recovers independently reviewed statement,
Q&A, bond-method and cross-section risk Units without copying document IDs or
body phrases into the router.

The final independent denominator sweep also found annual-report Unit
`1225067794/u305`, titled `2、持续经营`, which directly describes the Company's
loss, short-term liabilities, liquidity pressure, twelve-month cash-flow
forecast and mitigation measures. It is therefore retained as a grade-2
`business_risk_section` judgment beside its continuation Unit. A later exact
review found the parallel market-review Unit `1225067794/u25`, titled
`2、物业服务`, which directly describes slower project growth, intensified
competition, lower collection rates and prices, operating pressure and
strategic contraction. It is retained as grade 2 beside the already-judged
`5、物流仓储` sibling. These corrections change only the hash-bound relevance
denominator; they do not change the Unit, router, query plan or ranking
implementation.

The final v9 independent sweep then found two further grade-2 annual-interest
support Units, `1225067794/u257` and `/u265`, whose tables explicitly state coupon
rates and annual simple-interest / annual-payment / maturity-repayment terms. Adding
only those judgments made the old plan fail honestly at macro grade>=2 Recall@10/20
`0.892901/0.949074` and annual-interest Recall@10/20 `0.777778/0.777778`.
The general lexical alternative `单利按年计息` recovers both as lexical-only rows
at ranks 9 and 10; the no-lexical ablation excludes them and the no-direct ablation
retains them. No Unit, taxonomy route, provider identity or ranker branch was added.

All 18 graded queries pass: Success@5 1.00, grade-3 Recall@20 1.00,
grade>=2 Recall@10/20 0.905247/0.961420, nDCG@10 0.886143, narrow
returned-precision@5 1.00, broad returned-precision@10 0.816667, maximum
grade-0 in any top five 0, and mechanical carriers in any top ten 0. The lower
but honest Recall/nDCG values include the final Pro-identified source-wide
positives and supersede the incomplete-denominator result. This
validates the current L1 retrieval substrate and an explicit fielded query plan,
not a future natural-language L2 planner or a claim of 5,000-company coverage.

The corresponding live row audit is
`/private/tmp/disclosure-live-unit-audit-r43-v73-20260816.json` (SHA-256
`fdba6b457664fed344e2ed272a73a13e3f0779b5b0fd13170f95cb74ebc43cf1`).
All 805 identities and the title, heading path, filing type, direct keys,
section keys and query hash match the source replay with zero differences. All
ten active receipt sidecars are taxonomy r43/router v73 and match their DB
hashes. Search has 805 parents, eight windows and 25,955 atoms; queues are
empty. The 156 empty text payloads are all `heading_only`; payloads contain no
deprecated or redundant type/facet keys. Eighteen content-bearing Units have
no direct or section route and remain honest lexical candidates rather than
receiving fabricated fallback keys. `make doctor-full` passes migration,
views/ACLs, raw/Provider/Unit hashes, search coverage, queues, remote MinerU
canary and orphan checks.

## 2026-08-19 current live generation

The reviewed provider_unit.v9 / taxonomy r45 / router v77 bytes are committed
as `276be7e` and published through the actual Build/Publish/Search path for all
ten development Documents. DB-free replay
`/private/tmp/disclosure-semantic-route-r45-v77-post-pro-p2-current.json`
(SHA-256 `0d5df1f520120314d3e4d8009ba670e839454805a0be8c410ba73c8aa2969098`)
contains 800 Units, makes zero model calls, and passes the 30/30 direct-route
gold. Live audit
`/private/tmp/disclosure-live-unit-audit-r45-v77-v9-20260819.json`
(SHA-256 `9aac1ef4287b3bd9dca4c0c976fed1bc2097384d7cb4439941d655840f534037`)
matches all 800 source identities and every title, heading path, direct key,
section key, content hash and query hash with zero differences.

The live distribution is direct/section/either 376/562/691. There are 648
content-bearing and 152 heading-only Units. Exactly 11 content-bearing Units
have neither direct nor section routes; manual row review classifies them as
company/report covers, legal-responsibility templates, contact/signature or
explicit other/risk-tail text. They retain exact title/path/body lexical
retrieval instead of receiving a placeholder key. All 98 remaining no-route
Units are source-bound structural anchors. The database has no Unit whose
title, heading path, payload text and content artifact are all empty, and no
artifact-only Unit without a title or heading path. Payloads contain zero
deprecated `provider_type`, `semantic_type`, nested `kind`, publisher/market
or Document-category fields.

The active search projection has 800 parents, eight body windows and 25,955
atoms. The tracked 18-query v4 gold (SHA-256
`52a379bec3a5c05b8e3a44e0f64e63fd31b86b8b20abc033e9120e396aad6151`)
binds 138 reviewed Unit identities to both content and query hashes. Live
receipt `/private/tmp/disclosure-semantic-retrieval-query-r45-v77-v9-post-cleanup.json`
(SHA-256 `828c27ea3bf65bc3f9802c06ac33de210f1c7002d809b7b275e2fdaaf7a8416b`)
passes with Success@5 1.00, grade-3 Recall@20 1.00, grade>=2 Recall@10/20
0.905247/0.961420, nDCG@10 0.886143, narrow returned-precision@5 1.00,
broad returned-precision@10 0.816667, no grade-0 top-five result and no
mechanical top-ten carrier.

The user-authorized development cleanup removed 4,830 inactive Unit rows,
50 inactive rebuild runs, 60 obsolete Unit snapshot directories and their
Unit/publish outbox history. It preserved all ten raw PDFs, ten root parse runs,
ProviderDocument records, parser bundles, ten active rebuild runs and 800
current Units. Post-cleanup state is 20 runs and 800/800 active Units with zero
inactive Units; `make doctor-full` passes every database, source, artifact,
search, queue, canary and orphan check. Receipt:
`/private/tmp/disclosure-v9-unit-history-cleanup-receipt.json`.

All seven held-out source PDFs were also regenerated after the Windows GPU
multimodal canary passed; the 221-page report completed 221/221 pages without
the former MM-cache 500. Final held-out replay
`/private/tmp/disclosure-heldout-20260813-r1/heldout-eval-r45-v77-post-pro-p2.json`
(SHA-256 `e75e1c618a9f545d0a9971b2c62ef36e02937d338b27a3774e246f69c3059e92`)
has 821 Units, direct/section/either 380/634/733, zero model calls and gold
23/23. Visual PDF review retains two disclosed P2 hierarchy nuances instead of
adding risky paragraph-title heuristics: a restructuring list whose numbered
subsections lost provider title typing, and repeated not-applicable financial-
note selectors under weak unnumbered parents. Their body/table/search content
is intact and source occurrences remain distinct.

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
across 23 Unit rows while the other 782 Unit rows were NULL. 0038 restores that
deprecated join only on `document_units_v1` to honor the published contract and
introduces `document_units_v2` without the field. Content-bearing Units must not
be used to fabricate a missing provider category; their retrieval support comes
from the direct/section/lexical surfaces above.

The current audit finds 152 `{"text": ""}` payloads. All 152 are heading-only
Units with a non-NULL title, nonempty hash-bound heading chain and nonempty
title search tokens; none is a body block silently replaced by an empty value.
Of those, 98 are intentionally lexical-only because neither a direct nor a
section route is honestly supported.

## Remaining production gates

- The current corpus makes no model call. Before a future ambiguous Unit may
  use the optional CLI chooser, its closed no-tool boundary, cancellation,
  retry/backoff and exact deployed model identity still require an explicit
  production canary; do not infer adapter eligibility from the zero-call run.
- The 18-query graded gold proves the reviewed cases, not full query-language recall.
  Held-out process classes and atypical PDF layouts remain required.
- The three visually confirmed local MinerU omissions above are retained as an
  explicit upstream quality limit. Broadening native-PDF repair to nonnumeric
  drift requires a new source-bound contract and cross-document evidence; it
  is not authorized by these samples.
- The r45/v77 source replays, live Build/Publish/Search generation, graded
  query review, row audit, development-history cleanup and doctor are complete.
  A final repository gate, post-documentation independent review and user
  visual acceptance remain before any production-readiness claim.
