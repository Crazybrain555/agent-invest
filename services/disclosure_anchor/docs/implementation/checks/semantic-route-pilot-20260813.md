# Semantic route and retrieval pilot — 2026-08-13

> **Historical baseline.** This file freezes the pre-v14 815-Unit evidence and must not be read as the current
> acceptance receipt. The current v14/r60/v94 817-row applicability/route receipts are recorded in
> `docs/agent/HANDOFF.md` and the hash-bound gold/evaluator outputs.

This receipt distinguished the then-current **DB-free candidate** from the then-last
published development generation. The candidate was `provider_unit.v13` /
taxonomy r59 / router v92 / retrieval projection rp-v5 and has been replayed
over all ten current source identities and seven held-out source identities.
The development database still has the older 800-Unit r55/v89 Unit generation,
but migrations 0043/0044 are applied and all 800 existing search parents have
already been rebuilt to rp-v5; the normal Unit Build/Publish replay remains pending.
Historical r37/r43/r46 receipts remain comparison evidence only.

## Current contract

- taxonomy: `semantic-taxonomy-2026-08-r59` (financial r30 / events r44), containing 197 financial and
  146 event routes;
- router: `semantic_router.v92`;
- optional adjudicator: `codex_cli.v4.low`, `gpt-5.6-luna`, prompt
  `semantic_route_adjudication.v32`;
- `semantic_keys` contains direct Unit topics only; `semantic_key` is its
  deterministic first member;
- `section_keys` contains exact normalized positions from accepted heading
  paths. Periodic reports use explicit context containers; event filings expose
  only a small section-container allowlist gated by filing type or authoritative
  disclosure-topic scope. It is deterministic,
  never a model candidate, and ranks below a direct Unit route;
- `content_categories` remains a CNInfo Document facet. The sole current
  `document_units_v1` omits it and exposes Unit-owned `body_status`. It is not
  fabricated from Unit text and cannot
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

The current DB-free replay covers every Unit rebuilt from ten in-scope filings, including
one full 686-Unit annual report. It is a cross-shape development corpus, not a
statistical claim about 5,000 issuers.

| filing type | Units | direct route | section route | either |
|---|---:|---:|---:|---:|
| annual_report | 703 | 382 | 700 | 700 |
| quarterly_report | 40 | 31 | 37 | 37 |
| convertible_bond | 16 | 12 | 11 | 13 |
| equity_incentive | 16 | 15 | 14 | 16 |
| share_buyback | 21 | 16 | 18 | 20 |
| performance_forecast | 10 | 6 | 6 | 7 |
| operating_data | 5 | 2 | 1 | 2 |
| performance_briefing | 2 | 2 | 0 | 2 |
| investor_relations | 1 | 1 | 0 | 1 |
| correction_supplement | 1 | 1 | 0 | 1 |
| **total** | **815** | **468** | **787** | **799** |

The source-full no-model replay records 468 deterministic decisions, 41 rule
abstentions, 300 no-candidate fallbacks and six forced model abstentions, with
zero external model calls or tokens. Direct routing covers 468/655 content
Units (71.5%); section routing covers 631/655 (96.3%); either surface covers
643/655 (98.2%). The corpus carries 230 distinct direct keys and 213 distinct
section keys. The remaining lexical-only carriers are not force-filled with an
`other` key. Because the six model-needed rows were deliberately answered by
`semantic_eval_abstain.v1`, this artifact is `production_eligible=false` and
does not prove model quality.

The exact-current replay is
`/private/tmp/disclosure-semantic-route-r59-v92-v13-source-full.json`
(SHA-256 `7c343acd9082d365ffeb9148b0040c35c7af30903aa749edaa8f69fa5ccd3858`).
It passes 71/71 source-reviewed route/path gold (gold SHA-256
`f6310bbae9b4d230f8d6b7cc790b1f2052c30190e11fdf522a5104941b1edd48`).
The v13 outline candidate additionally restores source-proved financial-
statement containers, page-table labels, bracketed siblings and bounded
interstitial notices while conserving every provider block exactly once.

The exact-current held-out replay is
`/private/tmp/disclosure-heldout-20260813-r1/heldout-eval-r59-v92-v13-source-full.json`
(SHA-256 `e6347895a4a9db1f2ba9426e3586396db034bc16b933e97ceb14d31ee5ef7a76`).
It contains 851 Units over seven Documents and passes 41/41 held-out gold (gold
SHA-256 `76c8bbba2ca443e6aa3715819a9c4d2c0089f6677150e354da2096576dbfe3ae`).
Four model-needed rows are forced abstentions and the single `decision_source=model`
row is a deterministic `heldout_abstain.v1` fixture, not an external model call.
The exact recurring heading `存货可变现净值` now gives held-out u284 a deterministic
`inventory` direct and section route; the adjacent `存货可变现净值管理制度` negative
remains unrouted. This removes one model-needed row without broad body matching.

## Bounded real-model evidence

Claude Sonnet low was exercised only as an authorized, no-tools/no-web comparator;
neither result is production-eligible. On the six current ambiguity rows under prompt
v32, the model is row-exact on five: it keeps all six direct debt-structure fields,
rejects board-duty/auditor-duty/buyback-reference noise, and now accepts both lease
accounting objects. It still selects only `income_tax_expense`, omitting the independently
defined `deferred_tax`. Artifact:
`/private/tmp/disclosure-semantic-route-r59-v92-v13-sonnet-low-v32-current-bounded.json`,
SHA-256 `391855085221ff23e14690d3fd36336d238bf718eacb0bd238be80c1502d27f2`.

On the held-out comparator, v32 correctly rejects the inquiry revision notice, accepts
`other_equity_investments`, and no longer sees u284 because its exact `inventory` route is
deterministic. It remains unstable on restructuring: u10 omits `consideration_payment`,
and u129 adds `target_asset` even though the Unit is a transaction-classification test.
Artifact:
`/private/tmp/disclosure-heldout-20260813-r1/heldout-eval-r59-v92-v13-sonnet-low-v32-bounded.json`,
SHA-256 `131472324ed72eec84af37a4af38d746538296c593c81e6dd390a3c92225a2c1`.
Therefore Sonnet-low remains a bounded second opinion, not a sole semantic writer;
deterministic direct routes, section routes and lexical projection remain the production
recall surfaces. The exact current target still requires independent Fable review and a
clean live Unit/Search replay.

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
keeps the original MinerU text; the then-current `provider_unit_locator.v3` preserves the
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

## 2026-08-20 exact-current live generation

Provider_unit.v9 / taxonomy r49 / router v83 is published through the normal
Build/Publish/Search path for all ten development Documents. The live audit
`/private/tmp/disclosure-live-unit-audit-r49-v83-v9-self-verifying-v2-20260820.json`
(SHA-256 `1bd8f2cf2b109678d63489a880acaef110a1cdac2bcdf6fff865f92babf89666`)
is generated by repo-resident `scripts/audit_live_unit_replay.py` inside one read-only,
repeatable-read transaction. It binds replay SHA-256 `50487e76...74cd`, the ten
active processing-run IDs and the compared field list. All 800 source identities,
titles, heading paths, direct/section keys, content hashes, query hashes and body
statuses match the live public view. Both independently canonicalized row sets
hash to `sha256:ffbedc2c2dde705cfd9ad77e2c0b7ee4711062252a81d8ba4cefecdc1bbe63b4`;
missing, unexpected and mismatched rows are all zero. The only declared
normalization is the public absence representation `semantic_keys/section_keys
NULL -> []`; replay rows themselves must contain explicit arrays. Replay bytes are
read once and the receipt hash is computed from the exact buffer parsed for comparison.
All compared fields are mandatory and hashes require lowercase hexadecimal SHA-256.
There are 648 content-bearing and 152 heading-only Units. Public
payloads contain no
`provider_type`, `semantic_type`, nested `kind`, publisher/market or Document
category fields, and there is no content-bearing Unit with an empty body.

Exactly 11 content-bearing Units have neither a direct nor a section route.
Row-level review classifies them as cover/logo, securities metadata,
legal-responsibility boilerplate, literal other-information guidance or contact
material. They remain searchable by exact title/path/body rather than receiving
a fabricated fallback key. Five additional route-free rows are pure heading
anchors. Eight repeated title/path/content groups are distinct heading-only
`-续` source occurrences with different page/locator evidence, not duplicate
parses.

The live search projection has 800 parents, eight windows and 25,955 atoms. The
expanded 22-query v4 gold binds 204 manually judged Units to both the exact
source/live `query_projection_hash` and answer-bearing `content_hash`. Receipt
`/private/tmp/disclosure-semantic-retrieval-query-r49-v83-expanded.json`
(SHA-256 `578a740ffbcd3db03267ee786cc468ef15d33027930fa1736f9221dc3349279c`)
passes with Success@5 1.00, grade-3 Recall@20 1.00, grade>=2 Recall@10/20
0.901353/0.962753, nDCG@10 0.887914, narrow returned-precision@5 1.00,
broad returned-precision@10 0.895833, no grade-0 top-five result and no
mechanical top-ten carrier. The exact graded-gold SHA-256 is
`d56894961fb1529c78b40c9d2bacf3a74b2cfa358338a9aa6279101b6fc21227`.

The ablation is independently material: removing direct routes lowers nDCG by
0.247257 and grade>=2 Recall@20 by 0.287374 across nine affected cases;
removing lexical retrieval lowers nDCG by 0.198842 across twelve cases;
removing section routes lowers nDCG by 0.053407 and grade>=2 Recall@20 by
0.039773 across four cases. The section cases include consolidation-scope
changes, other-receivable details and a synonym-only future-strategy query;
the latter loses all grade-3 Recall@20 without `section_keys`. This proves the
three retrieval surfaces are complementary rather than interchangeable NULL
fillers.

After this rebuild, the user-authorized development cleanup removed exactly 800
inactive v79 Unit rows, ten superseded rebuild runs, 74 related outbox events
and ten obsolete snapshot directories. It preserved all ten raw PDFs, ten root
parse runs, ProviderDocument records and parser bundles, plus the ten active
v83 rebuild runs and 800 active Units. Receipt
`/private/tmp/disclosure-v83-unit-history-cleanup-receipt.json` (SHA-256
`09928669ce865d9c639fc585987d63dc1f5077038e3ea95541d9890acaaaab50`).
`make doctor-full`, the 111-test scratch integration gate and `make agent-check`
(868 tests, 100 skipped) all pass on the exact-current bytes and generation.

## 2026-08-19 historical live generation

Provider_unit.v9 / taxonomy r46 / router v79 is published through the actual
Build/Publish/Search path for all ten development Documents. The DB-free replay
`/private/tmp/disclosure-semantic-route-r46-v79-direct-content-current.json`
(SHA-256 `385bc547881bb15bd6d19f174a38a3bff6afe261731a5b6c0386ff302eac47bf`)
contains 800 Units, makes zero model calls, and passes the 30/30 direct-route
gold in exact canonical order (gold SHA-256
`7e362f22ebf0530c17f65c0df1c836561a32feb5f7392bf3ac80faaa82e15fc4`).
The structural change does not manufacture direct topics: it recovers exact
`section_keys` from each Unit's own accepted heading path, including a heading-only
anchor that is itself the source heading, while every `heading_only` Unit keeps
direct `semantic_key(s)` NULL. Event filings add only
the closed overview, investor-protection, scheme-adjustment, and regulatory-
approval section containers evidenced by the held-out restructuring document.

The live audit
`/private/tmp/disclosure-live-unit-audit-r46-v79-v9-20260819.json`
(SHA-256 `228c8a45130cb31e24baebee5ad026151f1b76ea81c74fc94f7989aa22341eb3`)
matches all 800 source identities and every title, heading path, direct key,
section key, content hash and query hash with zero differences. The live
distribution is direct/section/either 322/705/780. There are 648 content-bearing
and 152 heading-only Units; 143 heading-only anchors have an exact section route,
and none has a direct route. Exactly 11 content-bearing and nine structural
Units have neither direct nor section routes; exhaustive row review classifies
them as covers/front matter, legal templates, contact/signature, explicit
other/risk-tail text, or similar lexical-only carriers. They retain exact
title/path/body search instead of receiving a placeholder key. The database has
no Unit whose title, heading path, payload text and content artifact are all
empty, and no artifact-only Unit without a title or heading path. Payloads
contain zero deprecated `provider_type`, `semantic_type`, nested `kind`,
publisher/market or Document-category fields.

The active search projection has 800 parents, eight body windows and 25,955
atoms. The tracked 18-query v4 gold (SHA-256
`c2ea4639e669aa09be49bcf954b358e4be34ded4f3635704825e326440502c53`)
binds 139 reviewed Unit identities to both content and query hashes. Live
receipt `/private/tmp/disclosure-semantic-retrieval-query-r46-v79-v9.json`
(SHA-256 `e78dc8e78f1a75f6e449b66d95934c9816fa38e5c0b97f73fb73a5f4f5f80273`)
passes with Success@5 1.00, grade-3 Recall@20 1.00, grade>=2 Recall@10/20
0.905247/0.961420, nDCG@10 0.885799, narrow returned-precision@5 1.00,
broad returned-precision@10 0.816667, no grade-0 top-five result and no
mechanical top-ten carrier. This is an L2 retrieval-substrate proxy, not a
finished natural-language L2 planner.

After the r46/v79 rebuild, the user-authorized development cleanup removed
exactly 800 inactive v78 Unit rows, 10 superseded rebuild runs, 163 related
outbox events, and 10 obsolete snapshot directories. It preserved all ten raw
PDFs, ten root parse runs, ProviderDocument records, parser bundles, ten active
rebuild runs and 800 current Units. Post-cleanup state is 20 runs and 800/800
active Units with zero inactive Units; `make doctor-full` passes every database,
source, artifact, search, queue, MinerU canary and orphan check. Receipt:
`/private/tmp/disclosure-v79-unit-history-cleanup-receipt.json` (SHA-256
`ebcc2b8d52bf97f41157f1c8a4de9d2a9539f6738b93063ceb7df78ad57ae34a`).

All seven held-out source PDFs were regenerated after the Windows GPU
multimodal canary passed; the 221-page report completed 221/221 pages without
the former MM-cache 500. Final held-out replay
`/private/tmp/disclosure-heldout-20260813-r1/heldout-eval-r46-v79-direct-content.json`
(SHA-256 `1a904d0a3a9261c9cffcac4653f4154681022d227ae5f0cf633bbf909cec6809`)
has 821 Units, direct/section/either 320/762/788, zero model calls and gold
28/28 (gold SHA-256
`296a3b473de631ad0c44eaab3ed57a4be69d9b694757c8158fce13269df4b37e`).
All 107 heading-only Units have NULL direct keys; 93 retain an exact section route.
The 33 residual no-route rows are covers, contacts, checkbox/boilerplate,
long-form principles/opinion text, or lexical-only detail sections; no source-
unsupported catch-all was added merely to increase coverage.

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
across 23 Unit rows while the other 782 Unit rows were NULL. 0038 temporarily
restored that deprecated v1 join and introduced a clean v2; 0039 converges the
clean shape onto the sole `document_units_v1` and removes v2. Content-bearing
Units must not be used to fabricate a missing provider category; their
retrieval support comes from the direct/section/lexical surfaces above.

The current audit finds 152 `{"text": ""}` payloads. All 152 are heading-only
Units with a non-NULL title, nonempty hash-bound heading chain and nonempty
title search tokens; none is a body block silently replaced by an empty value.
Of those, nine remain lexical-only because neither a direct nor a section
route is supported by the closed, source-bound taxonomy.

## Remaining production gates

- The exact-current candidate makes one bounded model batch call. Its seven
  reviewed outcomes are acceptable and the receipt is source/input-bound, but
  cancellation, retry/backoff and exact deployed model identity still require
  an explicit resident-worker canary before production scheduling.
- The 22-query graded gold proves the reviewed cases, not full query-language recall.
  Held-out process classes and atypical PDF layouts remain required.
- The three visually confirmed local MinerU omissions above are retained as an
  explicit upstream quality limit. Broadening native-PDF repair to nonnumeric
  drift requires a new source-bound contract and cross-document evidence; it
  is not authorized by these samples.
- The r52/v86 source/held-out replays and expanded route gold are complete, but
  the development database still contains the older r49/v83 generation. Final
  independent Fable review, full repository gates, normal-path
  Build/Publish/Search replay, query-gold refresh, exact live audit and user
  visual acceptance remain before any production-readiness claim.
