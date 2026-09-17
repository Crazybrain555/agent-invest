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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "bootstrap-check"):
        item = sub.add_parser(name)
        item.add_argument("--intent", type=Path, required=True, help="canonical m6.campaign-intent.v1 bytes")
        item.add_argument("--intent-sha256", required=True)
        item.add_argument("--private-binding", type=Path, required=True, help="0600 m6.campaign-private-binding.v1")
        item.add_argument("--binding", type=Path, required=True, help="WP1 release binding.json (pinned by the intent)")
        item.add_argument("--release-manifest", type=Path, required=True, help="m6.release.v1 manifest (pinned by the intent)")
        item.add_argument("--manifest", type=Path, required=True)
        item.add_argument("--scope", type=Path, required=True)
        item.add_argument("--quality-plan", type=Path, required=True)
        item.add_argument("--output", type=Path, required=True, help="new directory under the runtime root")
        item.add_argument("--attempt-id", default=None, help="owner attempt identifier; default: a fresh UUID")
    args = parser.parse_args(argv)
    try:
        inputs = load_campaign_inputs(
            intent_path=args.intent.absolute(), intent_sha256=args.intent_sha256,
            private_binding_path=args.private_binding.absolute(), binding_path=args.binding.absolute(),
            release_manifest_path=args.release_manifest.absolute(), manifest_path=args.manifest.absolute(),
            scope_path=args.scope.absolute(), quality_plan_path=args.quality_plan.absolute(),
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
