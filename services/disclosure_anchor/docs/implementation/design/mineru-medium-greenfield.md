# MinerU Medium greenfield document structure

Status: accepted for a DB-free implementation slice; contract reconciliation pending
Date: 2026-08-11

## Decision

The disclosure PDF path will use **MinerU 3.4.4 Hybrid-medium** as its sole default
provider lane. MinerU 4.0.0a5 is excluded from the current implementation because the
tested alpha regressed cross-page cell continuation, omitted source content, and was
less reliable on representative financial filings. Hybrid-high is not an automatic
fallback, a second resident lane, or a source for cell-level fusion.

At runtime cutover, admission is fail-closed: a new writer or worker may Build/Publish
only an exact MinerU 3.4.4 Hybrid-medium parser identity and profile. Pipeline, pure VLM,
High, alpha, or an unknown backend is diagnostic-only and cannot publish. The existing
settings/admin overrides are not inherited as writer authority; cutover tests must close
that escape hatch explicitly.

The internal `parser -> structure -> DocumentUnit` path will be rewritten as a small,
independent seam. New code must not import the existing structure-proof, source-evidence
graph, ledger, relation, repair, legacy unit-builder, or document-audit systems. They
remain frozen only until the greenfield writer is accepted, then are removed in the same
cutover commit. Git history is the archive; no `legacy/` copy will be kept in the tree.

The stable service shell mechanics and current public-v1 surface remain the integration
boundary for later phases:

- immutable source PDF, source access, hashes, and artifact storage;
- `Document`, `ProcessingRun`, `DocumentUnit`, repositories, and unit of work;
- applied migrations, public v1 API/views, source references, asset URIs, outbox, and
  active-run publication mechanics;
- provider acquisition and worker scheduling.

The first vertical slice is DB-free, emits diagnostic DTOs rather than a new NormalizedIR,
and does not change a public contract or migration.

## Implementation and review roles

Codex owns the main implementation line, code changes, real-sample validation, and final
technical decision. ChatGPT Pro is used for major architecture and plan review and for
adversarial review before material commits. Claude Fable is a second independent view for
checking whether a plan is over-designed, locating concrete bugs, reviewing small diffs,
and, when useful, implementing one explicitly bounded subtask.

Reviewer output is evidence to investigate, not a vote or authority transfer. A finding is
adopted only when it is supported by the governing contract, exact code, or a real source
artifact. Reviewers do not edit the same file set concurrently; one writer owns each diff,
and Codex reconciles conflicting recommendations against the source evidence.

## Decision evidence

The source-first comparison covered ten real documents: short announcements, multi-page
tables and prose, similar adjacent-table negatives, complex glyphs, and long filings. In
the complete 161-page
JiangHai comparison, Medium and High each found 340 physical tables and 334 table bodies
were identical; Medium preserved the correct p49-p50 financial row while High invented
duplicated characters/digits, and Medium completed in 546 seconds versus High's 946 at
the same concurrency. Caitong, Midea, Xinan, and the Sanfu stress window supplied
independent structure/content wins for Medium. All compared high-quality lanes used the
same pinned `MinerU2.5-Pro-2605-1.2B` weights, so the choice is about inference pipeline,
not a newer learned model. The selected stable upstream is the official
[MinerU 3.4.4 release](https://github.com/opendatalab/MinerU/releases/tag/mineru-3.4.4-released).
The counts and timings above are session evidence preserved by this decision record, not a
permanent benchmark fixture; the DB-free implementation is revalidated visually against
the immutable source PDFs rather than treating those numbers as an automated quality gate.

## Greenfield pipeline

```text
MinerU 3.4.4 Hybrid-medium official artifacts
  -> page-local physical blocks
  -> heading candidates with source signals
  -> deterministic heading resolver
  -> ordered coarse units
  -> diagnostic alias candidates
  -> visual review
  -> later persistence through the stable service shell
```

The first implementation uses ordinary typed records, not a general evidence graph. A
physical block carries the locator precision the provider actually supplies: page and bbox
when present, otherwise an explicit coarse or unlocated state. It also carries order, raw
provider role, content hash, artifact reference, and source reference directly.

Artifact responsibilities are deliberately one-way:

- `content_list` is the primary visible semantic carrier and provider output order;
- `content_list_v2` contributes typed/page-grouped provider annotations, not a second text
  truth, and only when provider page/order/ID binds uniquely to the primary carrier;
- `middle_json` contributes page geometry or a lower-level locator aid only; it never
  repairs or overrides visible text;
- `model_json` is hash-bound diagnostic material, not a public payload or structure oracle;
- provider PDFs and crops support visual review.

The reader does not require content/model/middle text or table equality and does not build
a reconciliation graph among these artifacts. An annotation that cannot bind uniquely is
ignored or leaves the primary block coarse; it is never recovered by fuzzy text or bbox
matching.

## Content conservation

1. Provider text, table, image, caption, footnote, available position, and source identity
   are retained. An ancillary field without its own bbox remains explicitly coarse or
   unlocated. A structure decision may change grouping but never rewrite or delete provider
   content.
2. Uncertain content degrades to a coarse unit. It must not be silently dropped, repaired
   from a phrase dictionary, or assigned invented text.
3. The immutable PDF is always retained. A special scalar actually emitted by the provider
   is preserved with its available context and is not force-translated to another Unicode
   character. A scalar the provider did not emit or cannot map is not fabricated here.
4. The first slice may mark same-page, materially overlapping, role-compatible
   representations as diagnostic alias candidates, while keeping every payload and its
   provenance. This is not a public retrieval owner. A later L1 retrieval-primary
   projection consumed by L2 requires an explicit contract; no global string
   deduplication is used, and this service does not implement L2 claims or forecasts.

## Heading hierarchy

MinerU titles are candidates, not hierarchy truth. Heading resolution uses this fixed
signal order:

1. source-bound PDF bookmark or printed-ToC match;
2. explicit numbering among already admitted heading candidates;
3. bounded, candidate-local PDF style evidence such as available font size, alignment, or
   spacing;
4. MinerU level as a weak provider hint;
5. flatten to the nearest reliable parent or document root.

Page furniture, table-contained rows, captions, footnotes, bold subtotals, and a sentence
continuing across a page boundary are negative evidence. Numbering and style never create
a title from text that is absent from the candidate set. PDF style extraction is bound to
an existing provider candidate and cannot create a second text universe or repair source
characters. Bookmark/printed-ToC matches likewise require bounded exact source binding. A
single monotonic parent stack materializes `parent_id` and ordered `headpath`. Every
accepted heading retains its available provider locator, source occurrence, and the signal
that determined its placement; no fabricated numeric confidence is emitted.

This project's signal priority is a local product decision. It is informed by mature,
separate patterns demonstrated by other systems, not presented as their shared policy or
imported implementation:

- Docling demonstrates explicit outline and hierarchy signals;
- Marker demonstrates document-wide style statistics rather than per-page thresholds;
- Unstructured demonstrates ordered parent identifiers and title-oriented grouping.

## Optional whole-document model review

A replaceable low-cost whole-document outline reviewer is a later phase, after the
deterministic units have passed visual review; it does not participate in the first
implementation. It may only reference existing candidate IDs and propose `keep`,
`demote`, `reparent`, or `abstain`. It cannot add text, invent a heading, change reading
order or bbox, delete an occurrence, repair a table, or select the retrieval primary.

The deterministic validator accepts the proposal only if it is complete, source-bound,
single-parent, acyclic, parent-before-child, and compatible with hard negative evidence.
Any timeout, malformed response, missing candidate, invented ID, cycle, or illegal edit
rejects the whole proposal and uses the deterministic outline. No automatic JSON repair
loop is added.

## Tables and page boundaries

The DB-free slice retains each provider table representation exactly as emitted, including
its available HTML/grid, ancillary text, crop, page/bbox, order, empty state, and artifact
hash. A coarse diagnostic unit may contain several ordered page-local table parts; this
does **not** assert that they are one logical table. Adjacent pages are never merged or
deduplicated merely by visual or textual similarity. The greenfield path does not invent
cells, headers, or cross-page rows.

The existing public contract currently treats tables as page-local and rejects empty
table payloads. Therefore the first slice makes no DB, searchability, continuation, or
canonical-table claim about MinerU merged carriers and empty stubs. Their eventual public
mapping must reconcile the current service/public implementation with v0.8's cross-page
logical-unit semantics through an explicit contract decision after the real-sample visual
stop. An empty stub is not searchable content in the diagnostic DTO and is not governed
by same-page alias grouping.

Document metadata title, source-printed title occurrences, table continuation, heading
hierarchy, and the L1 retrieval-primary projection consumed by L2 are separate concerns.

## Implementation stops

The first mandatory visual stop is after provider blocks, deterministic hierarchy, and
the coarse unit assembler exist. The review must show source PDF crops, heading tree,
headpath, ordered unit parts, page/bbox locators, and diagnostic alias status for at least:

- CGN (single-page title and page furniture);
- Zhongke (main table parts, attachment, and flattened people rows);
- Caitong (four-page table and Q1-Q7 false-title negatives);
- JiangHai slices including p49-p50 and p79-p80;
- Sanfu p134-p151 and a visually similar but independent table negative.

Only after this review may the model reviewer, DB persistence, or retrieval-owner contract
be implemented. Cutover to the greenfield writer and deletion of the frozen legacy write
kernel happen in the same commit, so there is no dual-write or fallback window.

The delete manifest includes the legacy MinerU mapper/structure/source-evidence/table and
visual repair path; PDF native/printed-ToC/character/glyph/ledger path; application NIR,
document-structure, ownership, relation, and ledger contracts; legacy builder,
unit-preparation, and document audit; and their private schemas, fixtures, tests, and design
gates. The stable ParseDocument lifecycle, BuildUnits persistence envelope, PublishRun
transaction/outbox/diff, API evidence/path resolution, repositories, and unit of work are
kept but rewired. A dependency test forbids the greenfield packages from importing any
legacy family before cutover.

Existing v4 published runs, including inactive historical runs addressable through a
public run or asset reference, remain read-only after any reparse. The only allowed
compatibility code is a small v4-only evidence-manifest resolver that neither imports nor
is imported by the greenfield writer and cannot build, publish, or rebuild old units. New
Build and Publish paths reject v2-v4 inputs. The resolver is deleted only after all active
and historical public references are migrated or explicitly retired, or after an
authorized change retires the historical-evidence contract.

## Explicit non-goals

- no MinerU version monitoring or automatic future 4.x retest;
- no full-document Medium+High dual run or result fusion;
- no phrase-, issuer-, filing-, or document-ID-specific repair rules;
- no generic provenance/relationship graph;
- no exact cross-page cell reconstruction requirement;
- no new object named NormalizedIR v5 and no old NormalizedIR rebuild path for new runs;
- no public-v1 semantic change in the first vertical slice.
