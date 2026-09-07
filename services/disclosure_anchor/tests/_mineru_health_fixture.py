"""Explicit synthetic wire-v2 serving-runtime fields; never runtime defaults."""


def protocol_health_fields() -> dict[str, object]:
    return {
        "task_protocol_schema": "mineru-task-protocol.v2",
        "task_protocol_runtime": {
            "schema": "mineru-task-runtime.v1",
            "enabled": True,
            "task_registry_max_records": 128,
            "task_result_reservation_bytes": 268435456,
            "max_unacked_result_bytes": 2147483648,
        },
    }
