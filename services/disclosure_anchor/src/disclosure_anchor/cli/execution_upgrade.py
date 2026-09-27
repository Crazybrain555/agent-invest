"""Builders for one reviewed local execution upgrade (U01).

Root runs these after code freeze, in order: ``release-manifest`` (the new
release), ``derive`` (the target runtime, process profile and activation from
Q0's M0 and the explicit Q0 P0/A0), ``legacy-scope`` (READ ONLY inventory of
every current V4 head) and ``propose``. The reviewer then writes the GO review
for the proposal file's SHA-256. Every output is a new 0600 file; nothing is
overwritten, configured, installed or started, and no business row is
written.

``propose`` writes ``worker-local-execution-upgrade.v1`` by default (its
``--parent-*`` files are Q0's P0/A0, which are also its obligations' origin).
``--contract-version v2`` writes ``worker-local-execution-upgrade.v2`` and
names every role explicitly: ``--anchor-*`` for Q0's P0/A0 and ``--origin-*``
for the archived release, runtime bundle, process profile and activation the
inventory's obligations were frozen under. ``derive --contract-version v2``
takes ``--anchor-*`` and, unlike v1, accepts a target that keeps Q0's writer.
Mixing the two roles' flags is a usage error.

The newly qualified result runtime (``worker-qualified-runtime-upgrade.v1``)
has its own two builders. ``legacy-key-lookups`` runs READ ONLY against the
origin API before it retires and writes ``worker-legacy-key-lookup.v1``: one
closed 404 per never-submitted prepared member, with the API's actual key
lifetime. ``propose-qualified`` runs once the target runtime holds its own new
qualification (Qnew) and takes the archived ``--origin-*`` files, the
inventory and the lookups.

Exit codes: 0 pass; 64 argument error; 65 identity or refusal; 70 execution
failure. Output is one JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
from disclosure_anchor.adapters.runtime.mineru_deployment_gate import MinerUDeploymentGateError
from disclosure_anchor.application.contracts.closed_document import sha256_of
from disclosure_anchor.application.contracts.worker_execution_upgrade import (
    UPGRADE_CONTRACT,
    UPGRADE_CONTRACT_V2,
    LegacyExecutionRefused,
    encode_execution_release_manifest,
    encode_legacy_scope_inventory,
    encode_legacy_key_lookup_evidence,
    encode_local_execution_upgrade,
    encode_local_execution_upgrade_v2,
    encode_qualified_runtime_upgrade,
)
from disclosure_anchor.settings import load_settings

EX_OK = 0
EX_USAGE = 64
EX_IDENTITY = 65
EX_FAILURE = 70


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError(f"path must be absolute: {value}")
    return path


def _write(path: Path, payload: bytes) -> str:
    write_new_exact(path, payload)
    return sha256_of(payload)


def cmd_release_manifest(args: argparse.Namespace) -> int:
    from disclosure_anchor.adapters.runtime.mineru_execution_upgrade import build_execution_release_manifest

    manifest = build_execution_release_manifest(source_revision=args.source_revision)
    sha = _write(args.output, encode_execution_release_manifest(manifest))
    _emit({
        "status": "pass", "output": str(args.output), "sha256": sha,
        "source_revision": manifest.source_revision, "writer_code_sha256": manifest.writer_code_sha256,
        "worker_python_version": manifest.worker_python_version,
        "worker_package_set_sha256": manifest.worker_package_set_sha256, "file_count": len(manifest.files),
    })
    return EX_OK


def cmd_derive(args: argparse.Namespace) -> int:
    from disclosure_anchor.adapters.runtime.mineru_execution_upgrade import derive_local_upgrade

    v2 = args.contract_version == "v2"
    derived = derive_local_upgrade(
        load_settings(),
        parent_process_profile=args.anchor_process_profile if v2 else args.parent_process_profile,
        parent_activation=args.anchor_activation if v2 else args.parent_activation,
        output_dir=args.output_dir,
        contract_version=UPGRADE_CONTRACT_V2 if v2 else UPGRADE_CONTRACT,
    )
    _emit({"status": "pass", **derived.to_payload()})
    return EX_OK


def cmd_legacy_scope(args: argparse.Namespace) -> int:
    import sqlalchemy
    from sqlalchemy.pool import NullPool

    from disclosure_anchor.adapters.db.postgres.connection import app_database_url
    from disclosure_anchor.adapters.db.postgres.staged_upgrade_scope_v4 import capture_legacy_scope_inventory

    engine = sqlalchemy.create_engine(app_database_url(load_settings()), poolclass=NullPool)
    try:
        inventory = capture_legacy_scope_inventory(engine)
    finally:
        engine.dispose()
    sha = _write(args.output, encode_legacy_scope_inventory(inventory))
    counts: dict[str, int] = {}
    for member in inventory.members:
        counts[member.observed_state] = counts.get(member.observed_state, 0) + 1
    _emit({
        "status": "pass", "output": str(args.output), "sha256": sha,
        "captured_at_utc": inventory.captured_at_utc, "member_count": len(inventory.members),
        "state_counts": dict(sorted(counts.items())),
    })
    return EX_OK


_V1_ROLE_FLAGS = ("parent_process_profile", "parent_activation")
_V2_ROLE_FLAGS = {
    "derive": ("anchor_process_profile", "anchor_activation"),
    "propose": (
        "anchor_process_profile", "anchor_activation", "origin_release_manifest", "origin_runtime_bundle",
        "origin_process_profile", "origin_activation",
    ),
}


def _require_propose_roles(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Each contract version of ``derive``/``propose`` takes exactly its own role flags."""

    command = getattr(args, "command", None)
    if command not in _V2_ROLE_FLAGS:
        return
    v2_flags = _V2_ROLE_FLAGS[command]
    required, refused = (
        (v2_flags, _V1_ROLE_FLAGS) if args.contract_version == "v2"
        else (_V1_ROLE_FLAGS, v2_flags)
    )
    missing = [name for name in required if getattr(args, name) is None]
    extra = [name for name in refused if getattr(args, name) is not None]
    if missing or extra:
        parser.error(
            f"{command} --contract-version {args.contract_version} requires "
            + ", ".join("--" + name.replace("_", "-") for name in required)
            + (" and refuses " + ", ".join("--" + name.replace("_", "-") for name in extra) if extra else "")
        )


def cmd_propose(args: argparse.Namespace) -> int:
    if args.contract_version == "v2":
        return _cmd_propose_v2(args)
    from disclosure_anchor.adapters.runtime.mineru_execution_upgrade import build_upgrade_proposal

    upgrade = build_upgrade_proposal(
        load_settings(),
        release_manifest=args.release_manifest,
        runtime_bundle=args.runtime_bundle,
        parent_process_profile=args.parent_process_profile,
        parent_activation=args.parent_activation,
        inventory=args.inventory,
        exact_change_manifest_sha256=args.exact_change_manifest_sha256,
        independent_test_evidence_sha256=args.test_evidence_sha256,
        independent_code_review_sha256=args.code_review_sha256,
    )
    sha = _write(args.output, encode_local_execution_upgrade(upgrade))
    _emit({
        "status": "pass", "output": str(args.output), "proposal_sha256": sha,
        "parent_runtime_identity_sha256": upgrade.parent.runtime_identity_sha256,
        "current_runtime_identity_sha256": upgrade.current.runtime_identity_sha256,
        "legacy_member_count": upgrade.legacy_scope.member_count,
        "next": "the reviewer writes worker-local-execution-upgrade-review.v1 binding proposal_sha256",
    })
    return EX_OK


def _cmd_propose_v2(args: argparse.Namespace) -> int:
    from disclosure_anchor.adapters.runtime.mineru_execution_upgrade import build_upgrade_proposal_v2

    upgrade = build_upgrade_proposal_v2(
        load_settings(),
        release_manifest=args.release_manifest,
        runtime_bundle=args.runtime_bundle,
        anchor_process_profile=args.anchor_process_profile,
        anchor_activation=args.anchor_activation,
        origin_release_manifest=args.origin_release_manifest,
        origin_runtime_bundle=args.origin_runtime_bundle,
        origin_process_profile=args.origin_process_profile,
        origin_activation=args.origin_activation,
        inventory=args.inventory,
        exact_change_manifest_sha256=args.exact_change_manifest_sha256,
        independent_test_evidence_sha256=args.test_evidence_sha256,
        independent_code_review_sha256=args.code_review_sha256,
    )
    sha = _write(args.output, encode_local_execution_upgrade_v2(upgrade))
    anchor, origin, current = upgrade.qualification_anchor, upgrade.recovery_origin, upgrade.current
    _emit({
        "status": "pass", "output": str(args.output), "proposal_sha256": sha,
        "contract_version": UPGRADE_CONTRACT_V2,
        "anchor_runtime_identity_sha256": anchor.runtime_identity_sha256,
        "anchor_qualified_at_utc": anchor.qualified_at_utc,
        "origin_release_manifest_sha256": origin.release_manifest_sha256,
        "origin_runtime_identity_sha256": origin.runtime_identity_sha256,
        "release_manifest_sha256": current.release_manifest_sha256,
        "current_runtime_identity_sha256": current.runtime_identity_sha256,
        "legacy_member_count": upgrade.legacy_scope.member_count,
        "next": "the reviewer writes worker-local-execution-upgrade-review.v1 binding proposal_sha256",
    })
    return EX_OK


def cmd_legacy_key_lookups(args: argparse.Namespace) -> int:
    from disclosure_anchor.adapters.runtime.mineru_execution_upgrade import capture_legacy_key_lookups

    evidence = capture_legacy_key_lookups(
        load_settings(), inventory=args.inventory, key_ttl_seconds=args.key_ttl_seconds,
    )
    sha = _write(args.output, encode_legacy_key_lookup_evidence(evidence))
    _emit({
        "status": "pass", "output": str(args.output), "sha256": sha,
        "api_runtime_identity_sha256": evidence.api_runtime_identity_sha256,
        "key_ttl_seconds": evidence.key_ttl_seconds, "absent_keys": len(evidence.lookups),
    })
    return EX_OK


def cmd_propose_qualified(args: argparse.Namespace) -> int:
    from disclosure_anchor.adapters.runtime.mineru_execution_upgrade import (
        build_qualified_runtime_upgrade_proposal,
    )

    upgrade = build_qualified_runtime_upgrade_proposal(
        load_settings(),
        release_manifest=args.release_manifest,
        runtime_bundle=args.runtime_bundle,
        origin_release_manifest=args.origin_release_manifest,
        origin_runtime_bundle=args.origin_runtime_bundle,
        origin_process_profile=args.origin_process_profile,
        origin_activation=args.origin_activation,
        inventory=args.inventory,
        key_lookups=args.key_lookups,
        exact_change_manifest_sha256=args.exact_change_manifest_sha256,
        independent_test_evidence_sha256=args.test_evidence_sha256,
        independent_code_review_sha256=args.code_review_sha256,
    )
    sha = _write(args.output, encode_qualified_runtime_upgrade(upgrade))
    _emit({
        "status": "pass", "output": str(args.output), "proposal_sha256": sha,
        "contract_version": upgrade.to_payload()["contract_version"],
        "qualified_at_utc": upgrade.target_qualification.qualified_at_utc,
        "origin_runtime_identity_sha256": upgrade.recovery_origin.runtime_identity_sha256,
        "current_runtime_identity_sha256": upgrade.current.runtime_identity_sha256,
        "runtime_changes": [".".join(item) for item in upgrade.runtime_changes],
        "legacy_member_count": upgrade.legacy_scope.member_count,
        "next": "the reviewer writes worker-local-execution-upgrade-review.v1 binding proposal_sha256",
    })
    return EX_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disclosure-anchor execution-upgrade", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    release = commands.add_parser("release-manifest", help="hash the exact loadable worker release (E1)")
    release.add_argument("--source-revision", required=True)
    release.add_argument("--output", type=_absolute, required=True)
    release.set_defaults(handler=cmd_release_manifest)

    derive = commands.add_parser("derive", help="derive the target M/R, P and A from the qualified Q0")
    derive.add_argument("--contract-version", choices=("v1", "v2"), default="v1")
    derive.add_argument("--parent-process-profile", type=_absolute, help="v1: Q0's P0")
    derive.add_argument("--parent-activation", type=_absolute, help="v1: Q0's A0")
    derive.add_argument("--anchor-process-profile", type=_absolute, help="v2: the qualification anchor Q0's P0")
    derive.add_argument("--anchor-activation", type=_absolute, help="v2: the qualification anchor Q0's A0")
    derive.add_argument("--output-dir", type=_absolute, required=True)
    derive.set_defaults(handler=cmd_derive)

    scope = commands.add_parser("legacy-scope", help="READ ONLY inventory of every current V4 head")
    scope.add_argument("--output", type=_absolute, required=True)
    scope.set_defaults(handler=cmd_legacy_scope)

    propose = commands.add_parser("propose", help="assemble the U01 proposal for independent review")
    propose.add_argument("--contract-version", choices=("v1", "v2"), default="v1")
    propose.add_argument("--release-manifest", type=_absolute, required=True)
    propose.add_argument("--runtime-bundle", type=_absolute, required=True)
    propose.add_argument("--parent-process-profile", type=_absolute, help="v1: Q0's P0 (also its origin)")
    propose.add_argument("--parent-activation", type=_absolute, help="v1: Q0's A0 (also its origin)")
    propose.add_argument("--anchor-process-profile", type=_absolute, help="v2: the qualification anchor Q0's P0")
    propose.add_argument("--anchor-activation", type=_absolute, help="v2: the qualification anchor Q0's A0")
    propose.add_argument("--origin-release-manifest", type=_absolute, help="v2: the archived origin release")
    propose.add_argument("--origin-runtime-bundle", type=_absolute, help="v2: the archived origin runtime bundle")
    propose.add_argument("--origin-process-profile", type=_absolute, help="v2: the archived origin process profile")
    propose.add_argument("--origin-activation", type=_absolute, help="v2: the archived origin stream activation")
    propose.add_argument("--inventory", type=_absolute, required=True)
    propose.add_argument("--exact-change-manifest-sha256", required=True)
    propose.add_argument("--test-evidence-sha256", required=True)
    propose.add_argument("--code-review-sha256", required=True)
    propose.add_argument("--output", type=_absolute, required=True)
    propose.set_defaults(handler=cmd_propose)

    lookups = commands.add_parser(
        "legacy-key-lookups", help="READ ONLY: prove each prepared member's original key absent on the origin API",
    )
    lookups.add_argument("--inventory", type=_absolute, required=True)
    lookups.add_argument("--key-ttl-seconds", type=int, required=True)
    lookups.add_argument("--output", type=_absolute, required=True)
    lookups.set_defaults(handler=cmd_legacy_key_lookups)

    qualified = commands.add_parser(
        "propose-qualified", help="assemble the newly qualified result runtime proposal for independent review",
    )
    qualified.add_argument("--release-manifest", type=_absolute, required=True)
    qualified.add_argument("--runtime-bundle", type=_absolute, required=True)
    qualified.add_argument("--origin-release-manifest", type=_absolute, required=True)
    qualified.add_argument("--origin-runtime-bundle", type=_absolute, required=True)
    qualified.add_argument("--origin-process-profile", type=_absolute, required=True)
    qualified.add_argument("--origin-activation", type=_absolute, required=True)
    qualified.add_argument("--inventory", type=_absolute, required=True)
    qualified.add_argument("--key-lookups", type=_absolute, required=True)
    qualified.add_argument("--exact-change-manifest-sha256", required=True)
    qualified.add_argument("--test-evidence-sha256", required=True)
    qualified.add_argument("--code-review-sha256", required=True)
    qualified.add_argument("--output", type=_absolute, required=True)
    qualified.set_defaults(handler=cmd_propose_qualified)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        _require_propose_roles(parser, args)
    except SystemExit as exc:
        return EX_USAGE if exc.code not in (0, None) else 0
    try:
        return int(args.handler(args))
    except (MinerUDeploymentGateError, LegacyExecutionRefused) as exc:
        _emit({"status": "fail", "code": EX_IDENTITY, "kind": "identity", "reason": str(exc)})
        return EX_IDENTITY
    except (OSError, ValueError, RuntimeError) as exc:
        _emit({"status": "fail", "code": EX_FAILURE, "kind": "execution", "reason": f"{type(exc).__name__}: {exc}"})
        return EX_FAILURE


if __name__ == "__main__":
    sys.exit(main())
