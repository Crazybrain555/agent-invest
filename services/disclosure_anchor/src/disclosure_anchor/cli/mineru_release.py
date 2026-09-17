"""Source-only MinerU release: build, verify, qualify, bind and install.

Exit codes: 0 pass; 64 argument or input format; 65 identity or semantic
inconsistency; 70 execution failure or an undeterminable outcome. Output is one
JSON object on stdout. ``build`` and ``verify`` are offline. ``qualify``,
``bind`` and ``install`` touch the live runtime read-only or under the explicit
private binding; none of them admits business work.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from disclosure_anchor.adapters.runtime.mineru_release_package import (
    ReleaseIdentityError,
    ReleaseInputError,
    build_release_package,
    release_inventory,
    verify_release_package,
    write_new_json,
)

EX_OK = 0
EX_USAGE = 64
EX_IDENTITY = 65
EX_FAILURE = 70


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _fail(code: int, kind: str, reason: str, **references: Any) -> int:
    _emit({"status": "fail", "code": code, "kind": kind, "reason": reason, "references": references})
    return code


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError(f"path must be absolute: {value}")
    return path


def _verify_summary(report: Any) -> dict[str, Any]:
    manifest = report.manifest
    return {
        "source_head": manifest.source["head"],
        "source_manifest_sha256": manifest.source["source_manifest_sha256"],
        "capacity_config_sha256": manifest.inputs["capacity_config_sha256"],
        "deployment_profile_sha256": manifest.inputs["deployment_profile_sha256"],
        "local_worker_profile_sha256": manifest.inputs["local_worker_profile_sha256"],
        "release_manifest_sha256": manifest.sha256,
        "compose_sha256": manifest.projection["compose_sha256"],
        "capacity_sources_sha256": manifest.api_build["capacity_sources_sha256"],
        "local_profile_admits_full_pending": manifest.projection["local_profile_admits_full_pending"],
        "problems": list(report.problems),
        "projection_mismatches": list(report.projection_mismatches),
        "implicit_external_reads": list(report.implicit_external_reads),
    }


def cmd_build(args: argparse.Namespace) -> int:
    report = build_release_package(
        source_root=args.source_root, source_head=args.source_head, capacity_path=args.capacity,
        deployment_profile_path=args.deployment_profile, local_profile_path=args.local_profile, out=args.out,
    )
    verification = verify_release_package(args.out, check_active_dependencies=True)
    summary = _verify_summary(verification)
    summary.update({
        "status": "pass" if verification.passed else "fail",
        "package": str(report.package),
        "built_at_utc": report.manifest.built_at_utc,
        "out_of_scope_dirty": list(report.out_of_scope_dirty),
        "untracked_in_release_directories": list(report.untracked_in_release_directories),
    })
    _emit(summary)
    return EX_OK if verification.passed else EX_IDENTITY


def cmd_verify(args: argparse.Namespace) -> int:
    report = verify_release_package(args.package, check_active_dependencies=args.check_active_dependencies)
    summary = _verify_summary(report)
    summary["status"] = "pass" if report.passed else "fail"
    if args.inventory_out is not None:
        write_new_json(args.inventory_out, release_inventory(report))
        summary["inventory_out"] = str(args.inventory_out)
    _emit(summary)
    return EX_OK if report.passed else EX_IDENTITY


def cmd_qualify(args: argparse.Namespace) -> int:
    from disclosure_anchor.adapters.runtime.mineru_release_private_binding import load_release_private_binding
    from disclosure_anchor.adapters.runtime.mineru_release_qualification import load_canary_manifest, qualify_release

    report = verify_release_package(args.package, check_active_dependencies=True)
    if not report.passed:
        return _fail(EX_IDENTITY, "package", "release package did not verify", **_verify_summary(report))
    binding = load_release_private_binding(args.private_binding)
    canary = load_canary_manifest(args.canary_manifest)
    summary = qualify_release(
        report=report, runtime_bundle=args.runtime_bundle, canary=canary, binding=binding, package=args.package,
        output=args.output,
    )
    _emit({"status": "pass", **summary, "output": str(args.output)})
    return EX_OK


def cmd_bind(args: argparse.Namespace) -> int:
    from disclosure_anchor.adapters.runtime.mineru_release_binding import bind_release

    report = verify_release_package(args.package, check_active_dependencies=True)
    if not report.passed:
        return _fail(EX_IDENTITY, "package", "release package did not verify", **_verify_summary(report))
    result = bind_release(
        report=report, runtime_bundle=args.runtime_bundle, observation=args.observation,
        deployment_qualification=args.deployment_qualification, private_inputs=args.private_inputs,
        output=args.output, base_env=args.base_env,
    )
    _emit({"status": "pass", "output": str(result.output), **result.binding})
    return EX_OK


def cmd_install(args: argparse.Namespace) -> int:
    from disclosure_anchor.adapters.runtime.mineru_release_install import install_release
    from disclosure_anchor.adapters.runtime.mineru_release_private_binding import load_release_private_binding

    report = verify_release_package(args.package, check_active_dependencies=True)
    if not report.passed:
        return _fail(EX_IDENTITY, "package", "release package did not verify", **_verify_summary(report))
    binding = load_release_private_binding(args.private_binding)
    summary = install_release(report=report, package=args.package, binding=binding, output=args.output)
    _emit({**summary, "output": str(args.output)})
    return EX_OK if summary["status"] == "pass" else EX_FAILURE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="disclosure-anchor mineru-release", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build", help="build a new immutable package from an exact tracked commit")
    build.add_argument("--source-root", type=_absolute, required=True, help="repository top level")
    build.add_argument("--source-head", required=True, help="40-hex commit whose tracked bytes are released")
    build.add_argument("--capacity", type=_absolute, required=True)
    build.add_argument("--deployment-profile", type=_absolute, required=True)
    build.add_argument("--local-profile", type=_absolute, required=True)
    build.add_argument("--out", type=_absolute, required=True, help="new package directory")
    build.set_defaults(handler=cmd_build)

    verify = commands.add_parser("verify", help="re-derive and check a package offline")
    verify.add_argument("--package", type=_absolute, required=True)
    verify.add_argument("--check-active-dependencies", action="store_true")
    verify.add_argument("--inventory-out", type=_absolute, default=None)
    verify.set_defaults(handler=cmd_verify)

    qualify = commands.add_parser("qualify", help="run the deployment canaries against the installed release (live)")
    qualify.add_argument("--package", type=_absolute, required=True)
    qualify.add_argument("--runtime-bundle", type=_absolute, required=True)
    qualify.add_argument("--canary-manifest", type=_absolute, required=True)
    qualify.add_argument("--private-binding", type=_absolute, required=True)
    qualify.add_argument("--output", type=_absolute, required=True)
    qualify.set_defaults(handler=cmd_qualify)

    bind = commands.add_parser("bind", help="derive profile, activation and overlay from attested evidence")
    bind.add_argument("--package", type=_absolute, required=True)
    bind.add_argument("--observation", type=_absolute, required=True)
    bind.add_argument("--runtime-bundle", type=_absolute, required=True)
    bind.add_argument("--deployment-qualification", type=_absolute, required=True)
    bind.add_argument("--private-inputs", type=_absolute, required=True)
    bind.add_argument("--base-env", type=_absolute, default=None)
    bind.add_argument("--output", type=_absolute, required=True)
    bind.set_defaults(handler=cmd_bind)

    install = commands.add_parser("install", help="stage the package and run the Windows installation owner (live)")
    install.add_argument("--package", type=_absolute, required=True)
    install.add_argument("--private-binding", type=_absolute, required=True)
    install.add_argument("--output", type=_absolute, required=True)
    install.set_defaults(handler=cmd_install)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EX_USAGE if exc.code not in (0, None) else 0
    try:
        return args.handler(args)
    except ReleaseInputError as exc:
        return _fail(EX_USAGE, "input", str(exc))
    except ReleaseIdentityError as exc:
        return _fail(EX_IDENTITY, "identity", str(exc))
    except (OSError, ValueError, RuntimeError) as exc:
        return _fail(EX_FAILURE, "execution", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
