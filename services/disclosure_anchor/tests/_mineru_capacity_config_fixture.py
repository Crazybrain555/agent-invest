"""Handwritten A1 structural configuration, not a measured runtime capability."""

import json

CAPACITY_BYTES = (
    b'{"api_event_loop_limit":1,"api_process_limit":1,'
    b'"contract_version":"mineru.capacity-config.v1",'
    b'"final_http_limit_per_loop":7,"finalizer_active_limit":1,'
    b'"hybrid_batch_ratio_requested":2,"max_unacked_result_bytes":73,'
    b'"mkl_num_threads":2,"omp_num_threads":4,"openblas_num_threads":1,'
    b'"parse_active_limit":2,"pdf_render_processes_requested":3,'
    b'"pipeline_inference_locks":true,"processing_window_size":16,'
    b'"result_reservation_bytes":31,"total_nonterminal_limit":3}'
)


def capacity_payload(**changes):
    """Return a fresh literal payload, never a projection of a tested DTO."""
    value = json.loads(CAPACITY_BYTES)
    value.update(changes)
    return value


def canonical_payload(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
