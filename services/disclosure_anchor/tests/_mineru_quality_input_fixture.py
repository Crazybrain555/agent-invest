"""Literal data-only input/request graphs; no original IO authority is claimed."""

from tests._mineru_quality_config_fixture import canonical, config_payload, sha


REGULAR = 0o100600
DIRECTORY = 0o40700
I63 = 2**63 - 1
LIFETIME_NS = 7200 * 1_000_000_000
E1_RECORD_BYTES = 2 * 1024 * 1024 + 8192
SOURCE_BYTES = b"literal source data; no physical PDF observation"
PAGE_BYTES = b'{"literal":"page"}\n'
OUTSIDE_BYTES = b"outside parser subroot\n"


def source_payload():
    return {"identity": [17, 101, REGULAR, 501], "bytes": len(SOURCE_BYTES), "sha256": sha(SOURCE_BYTES)}


def output_payload():
    return {"path": "output/parser/page.json", "identity": [17, 201, REGULAR, 501],
            "bytes": len(PAGE_BYTES), "sha256": sha(PAGE_BYTES)}


def inventory_payload():
    # Deliberately unsorted and child-before-parent/root. These exact array
    # positions, including the outside file and empty directory, define its hash.
    return [
        output_payload(),
        {"path": "output/empty", "identity": [17, 202, DIRECTORY, 501], "bytes": None, "sha256": None},
        {"path": "output/outside.bin", "identity": [17, 203, REGULAR, 501],
         "bytes": len(OUTSIDE_BYTES), "sha256": sha(OUTSIDE_BYTES)},
        {"path": "output/parser", "identity": [17, 204, DIRECTORY, 501], "bytes": None, "sha256": None},
        {"path": "output/zero.bin", "identity": [17, 205, REGULAR, 501], "bytes": 0, "sha256": sha(b"")},
        {"path": "output", "identity": [17, 206, DIRECTORY, 501], "bytes": None, "sha256": None},
    ]


def target_payload():
    return {"name": "MinerU", "package_version": "3.4.4", "backend": "hybrid-http-client", "method": "auto",
            "language": "ch", "formula": True, "table": True, "effort": "medium", "image_analysis": False,
            "full_pdf": True, "start_page": None, "end_page": None,
            "runtime_bundle_identity_sha256": sha(b"literal runtime bundle identity"),
            "inline_equation_left": "$", "inline_equation_right": "$", "target_contract_version": "parser-target.v1"}


def manifest_payload():
    inventory = inventory_payload()
    return {"contract_version": "mineru-owned-quality.input.v1", "attempt_id": "独立-original-attempt",
            "configuration_sha256": sha(b"original complete E1 binding; not nested quality config"),
            "clock_identity_sha256": sha(b"original clock identity"), "started_ns": 100, "deadline_ns": 9_000_000_000,
            "journal_root_identity": [17, 301, DIRECTORY, 501], "journal_header_sha256": sha(b"original header"),
            "binding_record_sha256": sha(b"original binding record"), "resources_identity": [17, 302, DIRECTORY, 501],
            "snapshot_seal": source_payload(), "snapshot_record_sha256": sha(b"original snapshot record"),
            "source_observed_record_sha256": sha(b"original source observed record"), "source_page_count": 2,
            "output_inventory": inventory, "output_inventory_sha256": sha(canonical(inventory)),
            "output_record_sha256": sha(b"original output record"), "target_identity": target_payload(),
            "configuration": config_payload()}


def request_payload(*, role="producer"):
    raw = canonical(manifest_payload())
    return {"contract_version": "mineru-owned-quality.request.v1", "role": role,
            "work_id": "0123456789abcdef0123456789abcdef",
            "input_file": {"slot": "input.json", "identity": [17, 401, REGULAR, 501],
                           "byte_count": len(raw), "sha256": sha(raw), "evidence_kind": "complete"},
            "retained_root_identity": [17, 402, DIRECTORY, 501], "resources_identity": [17, 302, DIRECTORY, 501],
            "source_path": "/literal/run/resources/source.pdf", "output_path": "/literal/run/resources/output",
            "input_path": "/literal/run.quality/input.json"}


def rehash_inventory(payload):
    payload["output_inventory_sha256"] = sha(canonical(payload["output_inventory"]))
    return payload


def inventory_at_bytes(byte_count):
    """Valid unique direct children with one exact hand-computed JSON byte total.

    No production DTO/codec is called. Files carry synthetic hashes and are not
    created. Keep each path within 4096 bytes so the aggregate bound is isolated.
    """
    root = inventory_payload()[-1]

    def entry(index, padding):
        return {"path": f"output/f{index:04d}-" + "x" * padding,
                "identity": [17, 1000 + index, REGULAR, 501], "bytes": 0, "sha256": sha(b"")}

    maximum_padding = 4096 - len(entry(0, 0)["path"])
    full_cost = len(canonical(entry(0, maximum_padding))) + 1
    complete = (byte_count - len(canonical([root]))) // full_cost - 1
    assert 0 <= complete < 998
    values = [root] + [entry(index, maximum_padding) for index in range(complete)]
    final = [entry(complete, 0), entry(complete + 1, 0)]
    padding = byte_count - len(canonical(values)) - sum(len(canonical(item)) + 1 for item in final)
    assert 0 <= padding <= 2 * maximum_padding
    values.extend((entry(complete, min(padding, maximum_padding)),
                   entry(complete + 1, max(0, padding - maximum_padding))))
    assert len(canonical(values)) == byte_count
    return values
