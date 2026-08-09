# Simple95 Round 1 final-diff acceptance check

Scope: deterministic `simple95-run-receipt.v1` and
`simple95-diff-receipt.v1` only. Do not run blind24, GPU inference, visual QA,
runtime writes, publication or cleanup in this round.

## Contract and mutation gate

Run with the main-checkout virtualenv and the worktree source:

```bash
PYTHONPATH=src \
/Users/zhang/dev/agent-invest/services/disclosure_anchor/.venv/bin/python \
-m unittest -v \
tests.unit.test_simple95_acceptance_receipts \
tests.contract.test_simple95_acceptance_receipt_contracts
```

The table-driven mutations must prove:

| Mutation | Required classification |
|---|---|
| payload only | content |
| owner occurrence or physical order | structure/order/owner |
| canonical contents swapped across fixed physical positions | structure/order/owner |
| NFKC-equivalent contents swapped across fixed positions | structure/order/owner |
| heading path | structure and, when routed, query |
| target transform/grouping | query/search plan |
| PublicationGate pass to block | publication outcome |
| all asset IDs replaced | no receipt byte change |
| provider changed with otherwise identical identity | receipt bytes change; diff rejects |
| duplicate multiplicity | content change |
| stored hash/root tamper | reject |
| rules/version label only | never content |
| legacy target or absent parse receipt | reject |
| repeated same input | byte-identical run and diff receipts |

The schema contract test fresh-exports both schemas and compares them byte for
byte with `contracts/acceptance/`. It also proves closed-field and semantic
replay rejection. The observer test checks that receipt construction does not
mutate inputs and that no production source imports the receipt script.

## Test-composition and repository gate

After auditing the new tests, update the ratchet once:

```bash
PYTHONPATH=src \
/Users/zhang/dev/agent-invest/services/disclosure_anchor/.venv/bin/python \
scripts/audit_test_composition.py --update

make agent-check \
PYTHON=/Users/zhang/dev/agent-invest/services/disclosure_anchor/.venv/bin/python
```

## Existing all15 semantic replay

The existing frozen all15 is intentionally not a valid input for current run
receipts: its NIRs carry `parser-target.v1` and no parse-receipt artifact. First
run `build-run --document-id ...` against one member and record the expected
fail-closed error. Do not synthesize a receipt.

Then replay the complete frozen corpus solely to prove the legacy audit outputs
did not change:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
/Users/zhang/dev/agent-invest/services/disclosure_anchor/.venv/bin/python \
scripts/audit_unit_corpus.py \
  --manifest /private/tmp/disclosure-anchor-simple95-all15-fresh-windows-wired-20260807-r3-manifest.jsonl \
  --data-root /private/tmp/disclosure-anchor-simple95-all15-fresh-windows-wired-20260807-r3-workdir/services/disclosure_anchor \
  --out /private/tmp/disclosure-anchor-simple95-receipt-round1-all15-source-replay-20260809-r1 \
  --workers 1 --source-replay
```

The recorded Round 1 run uses one worker because this sandbox forbids the
process-pool semaphore query; the single-process path is the same deterministic
audit composition and avoids treating an environment permission as a semantic
failure.

Compare `summary.json`, `per_document.jsonl` and `findings.jsonl` byte for byte
with `/private/tmp/disclosure-anchor-queryhash-v2-all15-source-replay-20260809-r3`.
Pinned baseline SHA-256 values are:

```text
summary.json      21652e2fec371d622af079a00d2fe2dc6b8fa77f991139407919b85f6c4de6d0
per_document.jsonl 8c4212f147587b22997b5d162969a37a914af803a1162c9292d859b22a101775
findings.jsonl    e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

Expected summary remains 15 documents, 1501 units, zero failed documents and
zero errors. A later current-v2 fresh Windows run, outside this round, is the
first corpus eligible for real run receipts.

## Independent review evidence

Give the read-only reviewer the user goal, round boundary, full diff, both
schemas, mutation output, `agent-check` log and all15 byte comparison. Only
reproducible P0/P1/P2 findings may change this round. After P0/P1 are zero,
commit/push the immutable checkpoint, submit its full SHA to GPT Pro, and stop
after GO without starting blind24.
