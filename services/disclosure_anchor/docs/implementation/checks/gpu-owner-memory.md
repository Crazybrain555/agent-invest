---
id: disclosure_anchor_gpu_owner_memory
project: disclosure_anchor
title: GPU owner-memory diagnostic (MinerU vLLM inference worker)
status: candidate
created_at: 2026-10-12
---

# GPU owner-memory diagnostic

A read-only measurement inside the long-lived vLLM inference worker. It splits
retained warm VRAM into these parts:

- cached-but-free allocator memory
- fragmentation
- encoder-cache backing storage
- other live tensors

It does not change MinerU behavior, concurrency, memory floors, allocator
settings or admission, and it is not a repair. Code:
`scripts/windows/mineru_inference_memory/`. Tests:
`tests/unit/test_mineru_owner_memory.py`.

## Placement and preconditions

- vLLM selects it with `--worker-cls mineru_inference_memory.worker.OwnerMemoryWorker`.
  MinerU 3.4.4 `mineru-openai-server` (`mineru.cli.vlm_server:openai_server`) passes
  unknown arguments through to `vllm serve`. `OwnerMemoryWorker` subclasses
  `vllm.v1.worker.gpu_worker.Worker` and adds none of the following: general plugin,
  worker extension, thread, timer, RPC/HTTP route or dev mode.
- Before upstream `Worker.__init__`, construction is refused unless all of these hold:
  - vLLM reports exactly `0.21.0` and torch reports release `2.11.0`.
  - `v1/worker/{worker_base,gpu_worker,gpu_model_runner}.py` are byte-identical to
    v0.21.0 (`ad7125a431e176d4161099480a66f0169609a690`) and are the files actually
    loaded. The serving image's installed bytes were read and match these pins
    (`recurrence-20261012-r1/diagnostic-r1/installed-source-manifest.json`).
  - The class bases are unchanged; `--worker-extension-cls` appends to `__bases__`.
- After upstream `init_device` and before model load, startup aborts unless all of
  these hold:
  - the runner is exactly the V1 `GPUModelRunner` and its `encoder_cache` is a `dict`;
  - the device is one CUDA device, torch CUDA is initialized, and that device is current;
  - records can reach vLLM's INFO log.

  A failure here is a visible engine startup failure, never a silent fallback.
- The image installs only `__init__.py`, `core.py` and `worker.py` into
  `dist-packages/mineru_inference_memory/` on the exact raw MinerU digest.
  `core.verify_installed_distribution()` is the content-only build check: it reads
  package metadata and source bytes without importing torch or vLLM.
- After successful upstream `init_device`, runtime preconditions and owner binding,
  the initialized worker publishes `/tmp/mineru-owner-memory/owner.json`. This is a
  single container-local startup fact, separate from business state. Its closed
  `mineru.owner-memory-identity.v1` fields are `worker_class`, `module_sha256`
  (`sha256:` plus actual `worker.py` bytes), `pid`, `proc_start_ticks` (real Linux
  `/proc/self/stat` field 22), `device` (`cuda:0`) and `instance_id`, plus
  `contract_version`. `instance_id` equals the logs' `instance` value.
- Publication is bounded to 16 KiB and uses an owner-only temporary regular file,
  fsync and atomic replacement in an owned directory. Symlinks, writable-by-other
  directories/files, invalid process birth or another device refuse startup.
  Serving hooks never write this file. The collector reads it without importing
  worker/torch/CUDA, verifies the live PID/start ticks and current container epoch,
  and independently hashes every installed diagnostic source. It does not depend
  on startup logs remaining in a bounded log tail. Memory samples remain log-only.

## Hooks, threads and phases

On a single GPU, `UniProcExecutor` calls every worker method on the EngineCore main
thread, and `AsyncOutputFuture.result()` also runs `get_output()` on that thread.
Hooks bind to the PID and thread that ran `init_device`. Calls from any other thread or
process make no CUDA call; they produce one `error` record and are counted in
`counters.foreign_calls`.

| phase | hook (vLLM 0.21.0) | what the sample is |
|---|---|---|
| `post_warmup` | after `Worker.compile_or_warm_up_model` returns | Cold baseline after graph capture. `profile_run` cleared `encoder_cache` (`gpu_model_runner.py:5960`). Once. |
| `first_use` | after the first token-scheduling step returns its output (normally `sample_tokens`) | First-use growth. `context.output` is `async`, `final`, `none` or `other`. Once. |
| `step_entry` | `Worker.execute_model` entry, before upstream | Between steps, before this step's `_update_states` pops `free_encoder_mm_hashes` (`gpu_model_runner.py:1111`). At least 30 s apart. |

Every sample reads `torch.cuda.memory_stats`, the backend from
`get_allocator_backend`, device-wide `mem_get_info`, and the metadata of the cache
entries. The metadata read is `nbytes`, `untyped_storage().data_ptr()/nbytes()` and
the device of each entry.

Nothing synchronizes the device, copies tensors, collects garbage, empties caches,
resets peaks or changes allocator settings.

## Truthful limits

- **No hook runs while the engine is idle.** `EngineCore.step` returns without calling
  the worker when the scheduler has no requests (`core.py:415`). The idle state is
  read at the next `step_entry`, and `context.quiet_ns` gives the preceding gap. Every
  gap of 30 s or more is followed by a sample, because the last sample is never newer
  than the last hook return.
- **`step_entry` precedes the runner's state update.** Entries named in that step's
  free list are still counted. `encoder_cache.scheduled_free` projects which storages
  lose their last cache reference, but the post-eviction state appears only at a later
  sample. Observing between `_update_states` and the forward pass would need a runner
  monkeypatch, which is deliberately not done.
- **Eviction is capacity-driven.** Freeable entries stay cached through idle until new
  encoder inputs need space (`encoder_cache_manager.py:171-177`). Qwen2-VL caches
  `split()` views of one batch output (`qwen2_vl.py:1330`), so one surviving view
  keeps the whole parent allocation alive.
- **`device_work: not_synchronized` on every sample.**
  - Allocator counters are CPU-side bookkeeping, and kernels may still be running.
  - Blocks freed with pending stream events stay `active` until the allocator next
    processes events. A quiet gap therefore does not prove the GPU is idle.
- **Storage bytes are a lower bound.** A storage's `nbytes` is at most its allocator
  block size, because of rounding and splitting. `physical_bytes` therefore
  under-counts the allocator memory those storages hold.
- **Two blind spots.**
  - `device_memory` covers the whole device, including the API process.
  - Non-torch memory (CUDA context, NCCL, library handles) is not in allocator stats.
- **Peaks are startup-relative.** `.peak` values date from vLLM's last reset during
  startup profiling. Samples at least 30 s apart see transients only through peaks.
- **Missing never means zero.**
  - A missing or malformed metric is `null` and listed in `allocator.missing`.
  - A non-native allocator backend reports no allocator numbers, because torch
    documents constant zeros there.
  - A failed section reports `state: error` with its exception type.
  - No record at all means no observation.

## Reading a quiet-gap sample against the same instance's baseline

Compare samples that share `instance` and `pid`. Below, the abbreviated names refer
to these stats:

| name | stat |
|---|---|
| A | `allocated_bytes.all.current` |
| R | `reserved_bytes.all.current` |
| C | `active_bytes.all.current` |
| I | `inactive_split_bytes.all.current` |

| quantity | from one `step_entry` with `quiet_ns` ≥ 30 s |
|---|---|
| live-tensor growth | A − A(post_warmup) |
| encoder-held storage | `encoder_cache.physical_bytes`; much larger than `logical_bytes` means views pin parents |
| other live growth | live-tensor growth − (encoder physical − encoder physical at baseline) |
| cached free blocks | R − C |
| fragmented, non-releasable | I |
| free whole segments | R − C − I (upper bound of what `empty_cache` could return) |
| pending frees | C − A |
| allocation pressure | increase of `num_alloc_retries` and `num_ooms` |

Compare `R` growth with the about 3.1 GiB warm difference measured against an
inference-only recreate. Use `scheduled_free.bytes_losing_all_cache_refs` with
`present_entries` to see whether entry-granular eviction can release whole parents.

## Record format

Each record is one log line, `MINERU_GPU_OWNER_MEMORY <json>`, written through logger
`vllm.mineru_inference_memory` (vLLM's handler).

- The JSON has sorted keys and is at most 8192 bytes; the measured worst case is
  about 3.4 KB.
- `schema` is `mineru.gpu-owner-memory.v1`.
- `kind` is `startup`, `sample` or `error`. If even an error record cannot be
  produced, one static `{"kind":"record_failure","schema":...}` line is logged once
  per process.
- Every record carries `seq`, `instance` (random per worker), `pid`, `device_index`,
  `observed_at` (`CLOCK_MONOTONIC` start/completion and emission wall time) and
  `counters`.
- `startup` adds:
  - versions and source digests (`sha256:<hex>`);
  - the module's own file digests (`sha256:<hex>`);
  - rank, backend and async-scheduling flag;
  - allocator backend and configuration;
  - process start ticks and boot id;
  - the sampling policy.
- A `sample` adds `phase`, `device_work`, `allocator`, `device_memory`,
  `encoder_cache` and `context`. Context fields:
  - `quiet_ns` and `previous_output` (on `step_entry`);
  - `scheduled`: this step's request, encoder, finished and free-list counts;
  - `output` (on `first_use`);
  - `since_previous_attempt`: owner steps since the previous sample attempt.
- Each section has a `state` of `available`, `incomplete`, `unavailable` or `error`,
  plus a `reason` or an `error_type`.
- Records contain no addresses, cache keys, request IDs or document content.

Logging is bounded:

- one startup record and the two one-shot samples;
- otherwise at most one sample or sample-time error per 30 s;
- owner-mismatch and clock-regression errors are reported once;
- internal failures are reported at most once per 30 s.

## Local tests and their limits

The tests run without CUDA, torch or vLLM, using fake storages, tensors, owners and a
fake vLLM/torch module tree. They exercise the real binding. They cover:

- one shared parent versus many pinned parents, and alias/overlap deduplication;
- the free projection, and excluded or unreadable entries;
- null-not-zero metrics and the non-native backend;
- refusal before binding and for foreign threads and PIDs;
- the uninitialized-CUDA check order;
- throttling across busy and quiet periods, clock regression, failing sections and a
  raising log sink;
- `BaseException` passthrough and exact upstream return and exception identity;
- version, source and base refusal before upstream construction, and runtime refusal
  before model load;
- imports with no side effects.

They cannot prove:

- that real torch tensors, storages and `memory_stats` behave like the fakes;
- that vLLM resolves the worker in this image and that MinerU passes `--worker-cls`
  through;
- that hook overhead and log volume are acceptable in production;
- the thread model under another executor backend;
- any root-cause attribution.

These need the parent's image qualification and natural-workload observation.

## Integration and remaining qualification

The existing release path binds one four-file inference build context (the release
owner's Dockerfile plus `__init__.py`, `core.py`, `worker.py`), derived image reference,
actual image ID, worker argv, observed source hashes and current loaded-owner proof.
The proxy keeps its raw image. Stable runtime identity includes configuration and
source only; it excludes process IDs, instance IDs, start times, epochs and counters.

First installation or changed diagnostic source uses the existing full installer
only after all business duties close under producer exclusion and a fresh zero-duty
inventory. API/proxy epochs may change. Installed identical diagnostic source/image/
compose/collector can use the existing same-config inference recreation. Fresh
qualification, epoch bracket, G5 and binding remain required before producer resume.

Root still owns composition-ledger audit, native PowerShell checks, real image
qualification and production controls. After deployment, correlate the persistent
identity `instance_id` with startup source facts, the `post_warmup` baseline and
natural `step_entry` quiet-gap samples. No real GPU behavior or root-cause claim is
established by these deterministic unit tests.
