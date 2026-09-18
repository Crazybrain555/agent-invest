# Explicit MinerU capacity for a deployment epoch

The optional `mineru.capacity-config.v1` input enables bounded native document
concurrency in one API process and serving event loop. It preserves the original
MinerU 3.4.4 Hybrid-medium parsing and output semantics. This path is selected
explicitly; ordinary installation remains `legacy-runtime`, N=1/P=1/H=7.

The canonical codec is `application/contracts/mineru_capacity_config.py`. It
accepts one closed, canonical UTF-8 JSON object with all sixteen fields supplied.
Its software bounds describe supported input, not qualified machine capacity.

| Field | Meaning |
| --- | --- |
| `contract_version` | Exactly `mineru.capacity-config.v1` |
| `parse_active_limit` (N) | Active complete-document parse work, including CPU work and waits within that stage |
| `total_nonterminal_limit` (P) | All durable nonterminal task responsibility, including ingress, pending, parsing and finalizing |
| `finalizer_active_limit` (F) | Simultaneous retained-result ZIP finalizers; excludes Mac validation/publication/ACK |
| `final_http_limit_per_loop` (H) | Shared final asynchronous POST credits across the serving loop; not N multiplied by H |
| `api_process_limit`, `api_event_loop_limit` | Both one; does not create multiple GPU engines |
| `processing_window_size` | Native page window within each complete PDF; no split-document acceptance |
| `omp_num_threads`, `mkl_num_threads`, `openblas_num_threads` | Independent requested native thread settings |
| `pdf_render_processes_requested` | Requested shared render pool capacity; native per-job resolution may use fewer workers |
| `hybrid_batch_ratio_requested` | Native Hybrid batch ratio, in its supported set |
| `pipeline_inference_locks` | True: retain the original model locking behavior |
| `result_reservation_bytes` (B) | Reservation acquired before a task takes a parse slot |
| `max_unacked_result_bytes` (L) | Aggregate reserved and retained result capacity; not a RAM/VRAM budget |

P includes N and F responsibility. A task waiting for B remains durable pending
work and holds neither N nor F. The config requires N<=P, F<=P and B<=L; it does
not require P*B<=L. Whole-document source, native image/crop, host memory and
temporary disk budgets still need separate measured admission bounds.

## Installation and observation

`install_mineru_fixed_api.ps1` selects the explicit Docker target only when both
`-CapacityConfigSource` and `-ExpectedCapacityConfigSha256` are present. The file
must be the selected build context's `capacity-config.json`. Four fixed helper
sources in that context are individually hashed and checked during image build.
The config is baked root-owned 0444 at `/usr/local/etc/mineru/capacity.json`.
Its path and SHA anchors belong to the image; compose cannot override them.

Compose supplies the twelve exact original ENV projections and the configured H
in the API command; both are projected by the codec's `capacity_environment()` and
`capacity_http_arguments()` (see `mineru-release.md`), never by a hand-maintained mapping. An API-only capacity upgrade allows changes to those fields
only, while comparing the remainder of resolved compose and retaining the actual
proxy/inference service epochs. It uses the existing API-only recreation and
fresh output/registry rollback witness. A failed or changed witness still blocks
rollback over newly acquired task responsibility.

The explicit-capacity projection also pins `services.mineru-api.stop_grace_period`
to `10s`. An API-only upgrade on either axis may introduce that key from an unset
live value or keep it at `10s`; any other live value is drift and is rejected
before mutation. After installation, and on every collector run, the API
container's actual `Config.StopTimeout` must equal 10. The installer asserts it only
in its post-deployment validation; rollback and the pre-mutation published-image
reuse check validate the previous compose and do not require the key. The budget
bounds only the forced teardown after the stop signal; it does not replace business
soft-drain before signalling, the 900 ms freshness rule or the M6 `max_close`.

The optional `-ApiDeviceProfile cpu|cuda0` selects a separate device-change axis
and requires an explicit-capacity API-only upgrade. The supplied candidate
Compose must select that profile: CPU has no GPU reservation; `cuda0` uses
`MINERU_DEVICE_MODE=cuda:0` and the exact NVIDIA device `0` reservation. The
installer compares all non-device configuration, including capacity, unchanged.
To change H or another capacity setting while retaining CUDA, omit
`-ApiDeviceProfile` and use the capacity-upgrade path above; existing device
configuration remains part of its strict comparison. Neither option grants
permission to operate a shared runtime. Collector checks the actual API device
requests, environment and unprivileged container boundary; rollback restores and
verifies the previous profile while preserving inference/proxy epochs.

With phase tracing enabled, `MINERU_MODEL_DEVICE` records parameters and buffers
from the actual serving Hybrid and orientation instances, including device,
dtype, model path, PID/start ticks and capacity identity. Configuration alone is
not proof that model tensors use CUDA. Unavailable observations remain unknown;
recoverable observation IO failures warn and permit a later collection attempt.
Capacity-validation and unexpected errors still propagate. Device selection may
also change MinerU's native floating-point dtype, so throughput qualification
requires retained-output comparison before claiming equivalent parsing quality.

The v11 runtime manifest binds the externally supplied canonical config and four
helper sources. Collector v6 preserves a complete seventeen-field serving health
sample, including runtime v3, admission and capacity observation. Volatile owner,
clock, stage, HTTP and framework observations remain in raw evidence, outside the
stable identity hash. Uninitialized H and unavailable framework getters remain
explicit unknowns. Healthy idle deployment requires zero real responsibility,
open task admission and no foreign-loop/drain flags.

The selected no-DB smoke and epoch commands accept paired `--capacity-config`
and `--capacity-config-sha256`. Explicit epoch freezing additionally requires
`--mineru-bin` and `--runtime-bundle-identity` to verify current client/writer and
the external manifest identity before observing the host. A legacy consumer must
reject v11 unless its external capacity authority has been threaded through.
Full Settings/staged-worker/PG qualification remains a separate activation step.

## Continuous supply and qualification

Native N/P/H and framework settings remain fixed for an epoch. A later online
`C_target` controls new document grants within measured capacity; shrinking a
target drains accepted work normally. Normal P-full backpressure is valid load
evidence, not a telemetry failure. Count actual parse owners from `parse_active`:
the wire `processing_tasks` aggregate also includes finalizers and may exceed N.

The diagnostic v2 lifecycle's optional `on_terminal` hook runs after a trusted
remote terminal has been journaled and before local result retrieval. It permits
immediate refill while retaining each previous task's result, disk and ACK
responsibility. Resume can notify again; callers make notifications idempotent.
This hook grants no publication or ACK authority.

First qualify the actual deployment path and compare matched complete-PDF native
concurrency with continuous replenishment and GPU/CPU/RAM observations. N=2 is an
initial experiment, not a permanent ceiling. Offline ownership tests, functional
smoke and no-PG throughput are distinct from the final same-run publication and
complete GPU-host-hour M6 acceptance.
