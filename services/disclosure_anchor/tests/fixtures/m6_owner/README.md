# M6 owner cross-language wire vectors (independently authored)

`wire-vectors.v2.json` is generated, never hand-edited. It is a cross-language parity oracle
shared between the Python closed contract models and the native Windows owner
suite (`scripts/windows/test_mineru_m6_native_suite.cs`). The native side must
reproduce every canonical byte string and hash exactly; agreement is asserted
byte-for-byte, so a drift in either serializer fails a vector rather than being
absorbed by a looser comparison.

## Regenerate / verify

```bash
PYTHONPATH=src .venv/bin/python tests/fixtures/m6_owner/generate_m6_owner_fixtures.py
PYTHONPATH=src .venv/bin/python tests/fixtures/m6_owner/generate_m6_owner_fixtures.py --check
.venv/bin/python -m unittest tests.unit.test_m6_owner_fixture_generator
```

The generator imports only production contract modules
(`disclosure_anchor.application.contracts.m6_*`). It does not import any test
package, previous `m6_support` helper, or earlier fixture file.

## Contents

| Key | Purpose |
|---|---|
| `service`, `e2e` | Canonical `M6RunSpec` + `M6OwnerAnchor` pairs, one per mode. Service carry-in IDs exercise Unicode scalar ordering (U+FF5E before U+1F600). |
| `binding_negatives` | Spec/anchor variants the Python contract rejects; the native `MineruM6OwnerBinding.Validate` must reject each. Includes the UTF-16-ordinal-sorted carry-in list. |
| `journal.records` | Five canonical `M6RunEvent` lines in physical order plus an `owner_resumed` record from a second incarnation; `record_hashes`/`producer_hashes` bind exact bytes. |
| `journal.negatives` | Records to append after a given prefix that a native reader must refuse, with the expected damage code family. |
| `requests`, `reply` | Canonical `M6OwnerRequest`/`M6OwnerReply` bytes and hashes, including a cross-run append the request validator must reject. |
| `pending_drain` | The command-identity hashing rule and two-request hash chain for repeated `verifier_drain_pending` aggregation. |
| `attempt_set` | Sorted per-ID UTF-8 hash rule for reconciliation/closure receipts. |
| `quote_cases` | JSON string quoting parity between Python `json.dumps(ensure_ascii=False)` and the native quoter. |

## Boundaries

Identity placeholders use synthetic labels (`sha256("m6-independent-fixture|<label>")`). Canonical-content, producer, record, command and chain hashes are computed from their exact test bytes. They are not arbitrary labels.

This parity oracle imports production closed Python models. Independent semantic expectations and actual file/process observations are authored separately in the C# suites; agreement between serializers alone is not an independent semantic proof.
No production PDF, PostgreSQL row, real Windows MachineGuid/BootId, GPU UUID,
or credential appears here. Passing these vectors proves wire parity only; it
is not M6, G2 or hour qualification.
