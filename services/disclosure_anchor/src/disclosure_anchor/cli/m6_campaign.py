"""Campaign entry: run one owner-bound M6 campaign, or a zero-admission bootstrap-check, from frozen inputs.

`run` composes the whole lifecycle on this Mac: pinned inputs, fresh roles,
Windows Prepare/stage/Run through the release launcher, READY, bind by value,
open, the existing runner and verifier as sourced children, closure through
the runner's receipts, external exit verification and one summary.
`bootstrap-check` stops at zero admission: it proves the fresh-workspace
bootstrap, the authenticated bind and the real closure protocol without any
corpus admission, database access or hidden setup.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

from disclosure_anchor.adapters.runtime.m6_campaign_assembly import (
    CampaignIdentityError, CampaignInputError, M6CampaignAssembly, load_campaign_inputs,
)


def _summary(args: argparse.Namespace) -> int:
    """Derive the delivery report from immutable evidence only; nothing is fetched or mutated."""
    from disclosure_anchor.adapters.runtime.exact_file_write import write_new_exact
    from disclosure_anchor.adapters.runtime.m6_campaign_evidence import load_campaign_evidence
    from disclosure_anchor.application.contracts.closed_document import canonical_bytes
    from disclosure_anchor.application.contracts.m6_evaluation_plan import M6_EVALUATION_PLAN_MAX_BYTES, M6EvaluationPlan
    from disclosure_anchor.application.services.m6_delivery_report import build_delivery_report

    output = args.output.absolute()
    try:
        with args.evaluation_plan.absolute().open("rb") as source:
            plan_raw = source.read(M6_EVALUATION_PLAN_MAX_BYTES + 1)
        plan = M6EvaluationPlan.from_canonical_bytes(plan_raw, maximum_bytes=M6_EVALUATION_PLAN_MAX_BYTES)
        if output.exists() or output.is_symlink() or not output.parent.is_dir():
            raise ValueError("summary output must be a new directory under an existing parent")
        evidence = load_campaign_evidence(
            args.run_dir.absolute(), evaluation_plan=plan,
            native_journal=None if args.native_journal is None else args.native_journal.absolute(),
            telemetry_artifact_root=None if args.telemetry_artifact_root is None else args.telemetry_artifact_root.absolute(),
            telemetry_run_id=args.telemetry_run_id,
            manifest=None if args.manifest is None else args.manifest.absolute(),
            quality_plan=None if args.quality_plan is None else args.quality_plan.absolute(),
        )
        report = build_delivery_report(
            plan=plan, intent=evidence.intent, spec=evidence.spec, receipt=evidence.receipt,
            events=evidence.events, manifest=evidence.manifest, closure=evidence.closure,
            external=evidence.external, telemetry=evidence.telemetry, inputs=evidence.inputs,
            loader_unknowns=evidence.unknowns, stage_timing=evidence.stage_timing,
        )
    except CampaignIdentityError as exc:
        print(json.dumps({"m6_campaign_error": "identity", "message": str(exc)}), file=sys.stderr, flush=True)
        return 65
    except (CampaignInputError, ValueError, OSError) as exc:
        print(json.dumps({"m6_campaign_error": type(exc).__name__, "message": str(exc)}), file=sys.stderr, flush=True)
        return 64
    output.mkdir(mode=0o700)
    write_new_exact(output / "delivery-report.json", report.canonical_bytes() + b"\n")
    write_new_exact(output / "delivery-report-inputs.json", canonical_bytes({
        "inputs": [{"path": path, "sha256": digest} for path, digest in evidence.inputs], "unknowns": list(evidence.unknowns),
    }) + b"\n")
    print(json.dumps({"delivery_pass": report.delivery_pass, "run_validity": report.run_validity.status,
                      "report": str(output / "delivery-report.json")}), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "bootstrap-check"):
        item = sub.add_parser(name)
        item.add_argument("--intent", type=Path, required=True, help="canonical m6.campaign-intent.v2 bytes (evaluation plan frozen by hash)")
        item.add_argument("--intent-sha256", required=True)
        item.add_argument("--private-binding", type=Path, required=True, help="0600 m6.campaign-private-binding.v1")
        item.add_argument("--binding", type=Path, required=True, help="WP1 release binding.json (pinned by the intent)")
        item.add_argument("--release-manifest", type=Path, required=True, help="m6.release.v1 manifest (pinned by the intent)")
        item.add_argument("--manifest", type=Path, required=True)
        item.add_argument("--scope", type=Path, required=True)
        item.add_argument("--quality-plan", type=Path, required=True)
        item.add_argument("--evaluation-plan", type=Path, required=True,
                          help="canonical m6.evaluation-plan.v1 bytes whose sha256 the intent pins; frozen before Prepare")
        item.add_argument("--output", type=Path, required=True, help="new directory under the runtime root")
        item.add_argument("--attempt-id", default=None, help="owner attempt identifier; default: a fresh UUID")
    summary_parser = sub.add_parser("summary", help="read-only delivery report from an existing campaign output directory")
    summary_parser.add_argument("--run-dir", type=Path, required=True, help="campaign output directory of a finished run")
    summary_parser.add_argument("--evaluation-plan", type=Path, required=True)
    summary_parser.add_argument("--output", type=Path, required=True, help="new directory for delivery-report.json")
    summary_parser.add_argument("--manifest", type=Path, default=None, help="corpus manifest when campaign-inputs.json does not record its path")
    summary_parser.add_argument("--quality-plan", type=Path, default=None)
    summary_parser.add_argument("--native-journal", type=Path, default=None, help="events.jsonl when not under <run-dir>/native")
    summary_parser.add_argument("--telemetry-artifact-root", type=Path, default=None,
                                help="private synchronized telemetry observer root holding <run-id>/{frames.jsonl,receipt.v3.json,seal.v3.json}")
    summary_parser.add_argument("--telemetry-run-id", default=None, help="observer run id under --telemetry-artifact-root")
    args = parser.parse_args(argv)
    if args.command == "summary":
        return _summary(args)
    try:
        inputs = load_campaign_inputs(
            intent_path=args.intent.absolute(), intent_sha256=args.intent_sha256,
            private_binding_path=args.private_binding.absolute(), binding_path=args.binding.absolute(),
            release_manifest_path=args.release_manifest.absolute(), manifest_path=args.manifest.absolute(),
            scope_path=args.scope.absolute(), quality_plan_path=args.quality_plan.absolute(),
            evaluation_plan_path=args.evaluation_plan.absolute(),
        )
        assembly = M6CampaignAssembly(
            inputs, output=args.output.absolute(), mode=args.command,
            attempt_id=args.attempt_id or ("attempt-" + uuid.uuid4().hex[:16]),
        )
    except CampaignIdentityError as exc:
        print(json.dumps({"m6_campaign_error": "identity", "message": str(exc)}), file=sys.stderr, flush=True)
        return 65
    except (CampaignInputError, ValueError, OSError) as exc:
        print(json.dumps({"m6_campaign_error": type(exc).__name__, "message": str(exc)}), file=sys.stderr, flush=True)
        return 64
    summary = assembly.execute()
    print(json.dumps({"status": summary["status"], "summary": str(args.output.absolute() / "campaign-summary.json"),
                      "first_error": summary["first_error"]}), flush=True)
    if summary["status"] == "complete":
        return 0
    return 1 if summary["status"] == "failed" else 70


if __name__ == "__main__":
    raise SystemExit(main())
