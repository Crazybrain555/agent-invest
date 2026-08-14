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
| annual_report | 571286, 573256, 1213105553, 1219768304, 1222856328, 1225374514 | provider-classified; old, current, Chinese, and English formats |
| semiannual_report | 1217616113, 1221054498, 1225468066 | provider-classified; table-dense and multilingual |
| quarterly_report | 1221570547 | provider-classified |
| performance_forecast | 1225450112, 1225455464 | provider-classified; correction and numeric ranges |
| performance_flash | 1225457637 | provider-classified; compact financial table |
| operating_data | 1217574006, 1225452785 | provider-classified; short and wide operating tables |
| investor_relations | 100001036802 | provider-classified; Q&A without reliable headings |
| performance_briefing | 100001053504, 1225346988 | provider-classified; Q&A |
| inquiry_regulatory | 1222355452 | provider-classified; multi-line document title |
| restructuring_assets | 1225469289, 1225086656 | provider-classified; deep outline, inquiry reply, and long mixed content |
| additional_issuance | 1225469289, 1225412520 | provider topic plus a standalone issuance report |
| convertible_bond | 1220510536 | official CNInfo source re-query derives this class; local baseline registration omitted raw category |
| rights_issue | 1225464994 | provider-classified; source numbering typo retained |
| share_buyback | 1223032208, 1225441582 | exact business forms; result notice and adjacent-scenario table coverage |
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
| 688981 / 1225086656 | 130 | asset-restructuring inquiry reply; long Q&A, mixed text, and 112 physical tables | `0de075d9d50de77aae5ccb14a5d35609fc64982932740d1187ab71c2c6b0ec9c` |
| 600150 / 1225346988 | 9 | performance briefing with compact investor Q&A | `b7bdf8a1b4bf728f8b979f2b55e2bf85502ba30cc09a8fd82473b4aff1724abb` |
| 000651 / 1225374514 | 265 | English annual report; `Section` plus Roman-ordinal hierarchy and 357 physical tables | `b8e0a1d49f5928866e2581283ad90b0ccacc8683d7e605ea0642fce115a9a4f7` |
| 688012 / 1225412520 | 47 | standalone issuance report with deep numbered hierarchy | `f871f55919bcb2a2b1d167ea180e3cf2b1d00cd2c5b41b99e4113a83ef09a22f` |
| 300750 / 1225441582 | 9 | buyback result notice and share-change table | `3ddd79599606517fe80de61e10f722a1cacc114665390feb3e0c1abf0ddc80fb` |
| 002594 / 1225452785 | 2 | wide monthly production/sales table | `85b4caf3c6380f51a1c9a8acd7d051c9bb80455a715fc7af6175147023c1a934` |
| 688012 / 1225455464 | 4 | short performance forecast with numeric ranges | `020a8e984e8dbe30397b167cdbb468bd7bdec0667923490c674b18ed70f425fc` |
| 601138 / 1225468066 | 221 | current semiannual report, dense financial tables, and yes/no front matter | `34d02741f6651eba443de0b3ec16562a43d22b31c12d548e4f17004ed618ba2d` |

On 2026-08-13 this 221-page source produced the same remote failure twice in
its first processing window. The MinerU client terminal recorded the exact
marker `Unexpected status code: [500]`; the paired vLLM 0.21 container log was
`AssertionError: Expected a cached item for mm_hash=...` in the multimodal IPC
cache. After the pinned container command added `--mm-processor-cache-gb 0`, a
fixed image-completion canary, the formerly failing page window, and the full
14-window / 221-page run all succeeded. The strict Provider reader accepted
221 pages, 3,062 blocks, 290 physical table segments, and 297 artifacts with
bundle digest
`sha256:c9d1afe81c6cafd7ee5d59dbc5845d435d2c681bb3c70166a1bf98a289b6ef3b`.
This is backend-availability evidence, not a PDF-specific repair rule.

## Latest clean replay receipt (2026-08-13)

The final Unit-schema QA replay keeps ten diverse documents in the development
database and deliberately replaces the preceding replay generation. It contains
805 active Units: 446 mixed, 12 table, and 347 text. The `provider_unit.v3`
search projection contains 805 parent rows and 25,954 source-bound atoms under
`rp-2026.08-provider-unit-v3`. Mechanical and source checks found:

- all ten documents have exactly one parse run; all runs are succeeded,
  published, and have zero unassigned table parts;
- the Provider records contain 360 pages, 4,150 blocks, 337 physical table
  segments, 269 logical tables, and 3,209 retrieval targets with complete Unit
  ownership;
- no persisted Unit payload contains `provider_type`, `kind`, or
  `semantic_type`; the payload shapes are only shallow text, table, visual, and
  ordered mixed parts;
- all 156 `{"text":""}` payloads are accepted source headings with non-empty
  `title` and `heading_path`; there is no unexplained empty carrier, empty mixed
  text part, or empty table body;
- at the time of this clean replay, `semantic_key` and `semantic_keys` were both
  NULL on all 805 Units because the then-current writer had no trusted route
  classifier. Consequently that replay's `key_tokens` were empty rather than
  carrying a fake `document_content` placeholder. The subsequent controlled-
  taxonomy router is evaluated in a separate offline receipt before any new
  replay; this paragraph remains a historical DB observation;
- the pre-0037 audit found actual CNInfo `content_categories` on two Documents
  (then repeated across 23 Unit rows). 0037 keeps that fact Document-only; local
  test registrations without provider category metadata remain NULL, and no
  provider facet is repeated on the Unit view;
- 804 Units are `ok`. One quarterly-report Unit is `needs_review` because a
  physical table continuation cannot be bound at a provider-declared page
  boundary (`continuation_not_page_boundary`). On the same source page MinerU
  also omitted `2026年1-3月`, `1.77%`, `1.83%`, `5`, and `8` from a narrative
  sentence; the adjacent table and hash-bound visual artifact preserve those
  values. The service does not synthesize the missing sentence values;
- all 340 Unit evidence descriptors resolve to matching bytes, media type, size,
  and SHA-256 (57,677,277 bytes total); all ten raw PDFs, Provider records, Unit
  snapshots, active-run uniqueness, and search coverage pass `doctor --full`.

The bullets above describe the earlier published replay. The 2026-08-14
`provider_unit.v5` candidate now restores that sentence and seven other
numeric-only omissions from two PDFs through the exact-bbox native-text rule in
`provider_unit_locator.v2`. A full read-only pass observed 2,860 MinerU text
rectangles: eight repairs and 2,852 unchanged. The source pages were rendered
and checked for the restored heading number, dates, periods, percentages,
footnote ordinals, basis points and share-lock figures. ProviderDocument remains
byte-for-byte MinerU; only the Unit projection changes, and the candidate is not
described as published until the final manifest-bound Unit replay succeeds.

The first short-document build exposed and closed one real integration bug:
Python `None` for `semantic_keys` was initially bound as JSON `null`, which the
SQL scalar/array pairing CHECK correctly rejected. `JSONB(none_as_null=True)`
now persists the intended SQL NULL and a real-PostgreSQL regression test pins
that boundary. The same parse run was retried at Build and published; no second
parse was created.

## Earlier full-scope acceptance receipt (2026-08-12)

The earlier 30-document source matrix above produced 4,955 active Units: 2,804
mixed, 188 table, and 1,963 text, plus 86,407 search atoms. Those development
rows were intentionally deleted before the latest clean replay; this paragraph
is historical acceptance evidence, not a claim about current database state.
After the independent row review, 4,951 Units were `ok` and four source-bound
Beigene titles were explicitly `needs_review`: MinerU retained only `<sup>®</sup>`
plus BAT1706/BTK/PD-1/PARP1-PARP2 while the PDF and body text retained the
adjacent Chinese drug names. No text was synthesized or overwritten. Across
those Provider records:

- 24,252 provider blocks, 2,364 physical table segments, 1,838 logical tables,
  and 20,934 retrieval targets have complete, exactly-once ownership;
- zero logical-table relations are unbound and every active Unit uses the
  closed `provider_unit_locator.v1` contract;
- all 4,955 active Unit search projections replay exactly from the current
  payload and locator; no title/path target is duplicated into body search;
  word-token projection uses visible inline-HTML text while exact body atoms
  retain the provider scalar, and all rows use `rp-2026.08-provider-unit-v2`;
- all 69 evidence references on the six newly added provider-classified
  documents resolved to hash- and size-matching JPEG bytes (11,590,058 bytes);
- rebuild replay for every active document matched by the newest numbering
  families completed with zero unassigned table parts. This includes English
  `Section` plus uppercase Roman siblings, the older uppercase Roman auditor
  notes, and lowercase Roman/lettered deep lists; uppercase Roman is rank 2,
  while lowercase Roman and lettered lists share the conservative deep rank 6.

Visual source checks confirmed every newly admitted numbered heading on its
rendered PDF page. The checks included long Chinese headings, compact risk
sections, English `Section`/Roman-ordinal transitions, the JiangHai p49-50
narrow table, and the hard-negative adjacent tables in Sanfu and Midea. The
final eight-document round completed Parse -> Build -> Publish with 1,442 Units
and zero unassigned table parts. It also verified that title-only payloads are
represented minimally as `{"text":""}` and are not evidence of loss by
themselves: sampled cases were printed headings whose content belongs to a
following child or sibling Unit.

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
6. In source 1225468066 page 2, MinerU omits the printed `否` beneath item seven
   and merges the item-nine `否` into the title. The latter scalar survives and
   the former remains recoverable only from the immutable PDF. The service does
   not invent a second PDF-text universe or move an answer by layout guess;
   source page/bbox provenance remains available for L2 or human review.
7. In source 100020384132 page 1, MinerU keeps only `编号：2026-` and drops the
   separately printed trailing `003`, so no semantic provider artifact contains
   the complete small form number `2026-003`. The registered document title does
   carry the complete value and the immutable PDF is authoritative; this is a
   provider form-header omission, not a Unit projection loss. A future
   source-coverage metric may flag this family, but no document-ID repair is
   permitted.
8. One annual-report cover Unit (`du_01KZVBVAK4E4ABGZ3NPM2QER7E`) is a
   source-bound logo image with no title or provider text. It is reachable by
   document/order/locator and its JPEG digest participates in content identity,
   but it intentionally has no full-text search atom. Do not fabricate OCR text
   merely to make the cover keyword-searchable.

## Acceptance rule

Every behavior change must name the failure family, source-bound invariant,
and fail-closed boundary, then pass a representative positive and adjacent
negative from this in-scope corpus. No rule may branch on a provider document
ID, issuer, or literal filing phrase. Provider payloads are preserved rather
than repaired; uncertainty is represented by conservative structure or
`needs_review`, never by fabricated text, cells, or confidence.
