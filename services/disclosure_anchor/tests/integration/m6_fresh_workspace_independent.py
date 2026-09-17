"""Opt-in, zero-PDF acceptance through the real campaign CLI; never prepares its workspace.

The independent oracle reads the native spec/journal and process receipts after
the entry closes. It never publishes a spec, fixes ACLs, or synthesizes closure.
Run explicitly with --execute and the same frozen inputs as bootstrap-check.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import signal
import subprocess
import sys


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verify_identity_pair(run, start, spec, after):
    """Independent native formula and remote byte readback, not product validation."""
    selected = []
    for metadata_path in (run / "native").glob("diagnostic-*.json"):
        require(metadata_path.stat().st_size <= 1024, "metadata byte limit")
        metadata = load(metadata_path)
        if metadata.get("code") == "owner_identity":
            selected.append((metadata_path, metadata))
    require(len(selected) == 1, "expected exactly one retained native identity")
    path, metadata = selected[0]
    raw = path.with_suffix(".bin").read_bytes()
    require(len(raw) <= 65536, "physical identity byte limit")
    require(metadata["contract_version"] == "m6.transport-diagnostic.v1", "identity metadata contract")
    require(metadata["body_sha256"] == digest(raw), "identity body bytes changed")
    body = json.loads(raw)
    require(canonical(body) == raw, "native identity is not exact canonical bytes")
    require(body["contract_version"] == "m6.physical-owner-identity.v2", "physical identity contract")
    require(body["boot_identity_version"] == "m6.windows-boot-counter.v1", "native boot version")
    native_boot = digest(canonical({"contract_version": "m6.windows-boot-counter.v1",
                                   "windows_node_identity_sha256": body["windows_node_identity_sha256"],
                                   "boot_counter": body["boot_counter"]}))
    require(body["clock"] == spec["clock"], "identity clock differs from actual spec")
    require(native_boot == spec["clock"]["boot_identity_sha256"], "native boot derivation mismatch")
    domain = digest(canonical({"boot_identity_sha256": native_boot, "clock_source": "QueryPerformanceCounter",
                               "frequency_hz": spec["clock"]["qpc_frequency_hz"]}))
    require(domain == spec["clock"]["clock_domain_identity_sha256"], "clock-domain derivation mismatch")
    for key in ("pid", "creation_filetime_100ns"):
        require(type(body[key]) is int and body[key] == start[key], "identity/start mismatch: " + key)
    require(body["gpu_device_identity_sha256"] == spec["runtime"]["gpu_device_identity_sha256"], "identity GPU")
    epoch = digest(canonical({"run_id": spec["run_id"], "owner_source_sha256": spec["runtime"]["owner_source_sha256"],
                              "boot_identity_sha256": native_boot, "pid": start["pid"],
                              "creation_filetime_100ns": start["creation_filetime_100ns"]}))
    anchor = load(run / "run" / "anchor.json")
    require(anchor["owner_process_epoch_sha256"] == epoch, "physical process epoch mismatch")
    resident_boot = digest(canonical({"windows_node_identity_sha256": body["windows_node_identity_sha256"],
                                      "boot_utc": body["windows_boot_utc"]}))
    require(after["owner_identities"] == [{"metadata_name": path.name,
                                            "metadata_sha256": digest(path.read_bytes()), "body_sha256": digest(raw)}],
            "local retained identity differs from independently read remote original")
    return {"native_boot_sha256": native_boot, "resident_boot_sha256": resident_boot,
            "physical_owner_epoch_sha256": epoch, "identity_body_sha256": digest(raw)}


def load(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result
    return json.loads(path.read_bytes(), object_pairs_hook=unique)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def save(path: Path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def remote_read(binding, script, output, label):
    ssh = binding["ssh"]
    require(digest(Path(ssh["executable_path"]).read_bytes()) == ssh["executable_sha256"], "SSH executable drift")
    encoded = base64.b64encode(("$ErrorActionPreference='Stop'; " + script).encode("utf-16le")).decode("ascii")
    argv = [ssh["executable_path"], "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
            "-o", "UserKnownHostsFile=" + ssh["known_hosts_path"], "-i", ssh["private_key_path"],
            "-p", str(ssh["port"]), ssh["username"] + "@" + ssh["address"],
            "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    with (output / (label + ".stdout")).open("xb") as out, (output / (label + ".stderr")).open("xb") as err:
        completed = subprocess.run(argv, stdout=out, stderr=err, timeout=60, check=False)
    require(completed.returncode == 0, f"{label}: SSH exit {completed.returncode}")
    return load(output / (label + ".stdout"))


def verify(output, binding, intent, intent_sha, before, after):
    run = output / "campaign"
    summary = load(run / "campaign-summary.json")
    require(intent["contract_version"] == "m6.campaign-intent.v2", "new G0 must use a prebound evaluation plan")
    require(digest((run / "evaluation-plan.json").read_bytes()) == intent["evaluation_plan_sha256"],
            "entry did not preserve the exact prebound evaluation plan")
    inputs = load(run / "campaign-inputs.json")
    require(digest((run / "campaign-intent.json").read_bytes()) == intent_sha, "entry did not retain exact intent")
    require(inputs["evaluation_plan_sha256"] == intent["evaluation_plan_sha256"] == summary["evaluation_plan_sha256"],
            "evaluation identity changed across entry inputs and result")
    for key, value in {"mode": "bootstrap-check", "status": "complete", "admitted_count": 0,
                       "database_access": "none", "hidden_setup": False, "spec_hash_equal": True,
                       "owner_external_exit_verified": True, "local_children_reaped": True,
                       "first_error": None, "cleanup_failures": {}, "intent_sha256": intent_sha}.items():
        require(type(summary.get(key)) is type(value) and summary[key] == value, f"summary {key} differs")
    require(before["workspace_exists"] is False, "workspace already existed before the real entry")
    require(after["matching_owner_alive"] is False, "the exact native owner remains alive")
    require(after["listening_count"] == 0, "native port remains listening")
    require(after["spec_count"] == 1, "expected one native spec, including after idempotent bind")
    windows = binding["windows"]
    records = run / "launcher-records"
    start, end = load(records / "process-start.json"), load(records / "process-exit.json")
    require(start["contract_version"] == "m6.owner-external-start.v2", "start contract")
    require(end["contract_version"] == "m6.owner-external-exit.v2", "exit contract")
    for key in ("pid", "creation_filetime_100ns", "run_id", "attempt_id", "binary_sha256", "launcher_sha256", "configuration_sha256"):
        require(start[key] == end[key], f"exit/start instance mismatch: {key}")
    require(start["run_id"] == intent["run"]["run_id"], "wrong run")
    require(start["binary_sha256"] == windows["owner_executable_sha256"], "wrong native binary")
    require(start["launcher_sha256"] == windows["launcher_sha256"], "wrong launcher")
    launch = load(run / "launcher-command.json")
    require(launch["intent_sha256"] == intent_sha, "launch projection intent differs")
    for key, value in launch["expected_start"].items():
        require(type(start[key]) is type(value) and start[key] == value, "launch/start differs: " + key)
    require(start["attempt_id"] == inputs["attempt_id"], "native launch attempt differs")
    require(start["resume"] is False and start["original_anchor_sha256"] == "none", "G0 reused original owner")
    require((records / "ready.json").read_bytes().rstrip(b"\r\n") ==
            (run / "ready.json").read_bytes().rstrip(b"\r\n"), "transport/fetched READY differs")
    for key, value in {"exit_code": 0, "process_handle_signaled": True, "forced_termination": False,
                       "ready_timeout": False, "ready_received": True, "parent_failure": None, "cancel": None,
                       "exact_process_handle_opened": True, "stdout_eof": True, "stderr_eof": True}.items():
        require(type(end.get(key)) is type(value) and end[key] == value, f"external exit {key} differs")
    spec_raw = (run / "run" / "run-spec.json").read_bytes()
    require(digest(spec_raw) == after["spec_sha256"] == summary["spec_sha256"], "cross-host spec mismatch")
    spec = json.loads(spec_raw)
    identity = verify_identity_pair(run, start, spec, after)
    events_raw = base64.b64decode(after["journal_base64"], validate=True)
    (output / "native-events.jsonl").write_bytes(events_raw)
    events = [json.loads(line) for line in events_raw.splitlines()]
    require(bool(events), "native journal absent")
    kinds = [entry["event"]["payload"]["kind"] for entry in events]
    require(kinds[0] == "run_started" and kinds[-1] == "run_closed", "native run is not closed")
    require(not {"attempt_admitted", "remote_accepted", "publication_committed"}.intersection(kinds), "business admission in G0")
    require(events[-1]["event"]["payload"]["reason"] == "stop_requested", "native failure closure")
    require(events_raw.endswith(b"\n"), "truncated native journal")
    previous_tick = spec["t0_ticks"]
    for index, entry in enumerate(events, 1):
        require(entry["stamp"]["sequence"] == index, "native journal sequence gap")
        require(entry["stamp"]["owner_process_epoch_sha256"] == summary["owner_epoch_sha256"], "native owner epoch changed")
        require(entry["event"]["run_id"] == spec["run_id"] and entry["event"]["spec_sha256"] == digest(spec_raw), "native event from another run/spec")
        require(entry["stamp"]["boot_identity_sha256"] == spec["clock"]["boot_identity_sha256"], "native boot changed")
        require(entry["stamp"]["received_qpc_ticks"] >= previous_tick, "native owner time moved backwards")
        previous_tick = entry["stamp"]["received_qpc_ticks"]
    close_tick = events[-1]["stamp"]["received_qpc_ticks"]
    require(close_tick <= spec["max_close_ticks"], "closure exceeded original max_close")
    require(summary["bind"]["idempotent_replay"] is True, "same-byte bind replay was not verified")
    for entry in after["directories"]:
        require(entry["protected"] is True and entry["reparse"] is False, "native private directory not protected")
        require(entry["owner"] == after["current_sid"], "private directory owner differs")
        require(set(entry["allow_sids"]) <= {after["current_sid"], "S-1-5-18"}, "private ACL grants another principal")
    require(summary["children"]["runner"] is None and summary["children"]["verifier"] is None, "G0 spawned business children")
    return {"status": "pass", "hidden_setup": False, "admitted": 0, "database_access": "none",
            "physical_identity": identity,
            "spec_hash_equal": True, "owner_external_exit_verified": True, "local_children_reaped": True,
            "native_event_count": len(events), "journal_sha256": digest(events_raw), "spec_sha256": digest(spec_raw)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True, help="explicit real-machine opt-in")
    for key in ("intent", "private-binding", "binding", "package", "manifest", "scope", "quality-plan", "evaluation-plan", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--intent-sha256", required=True)
    args = parser.parse_args(argv)
    output = args.output.absolute()
    require(not output.exists(), "test output must be new")
    output.mkdir(mode=0o700)
    evidence = {"status": "incomplete", "started_utc": datetime.now(timezone.utc).isoformat(), "failure": None}
    try:
        intent, binding = load(args.intent), load(args.private_binding)
        require(digest(args.intent.read_bytes()) == args.intent_sha256, "intent changed")
        files = [args.intent, args.private_binding, args.binding, args.package / "release-manifest.json",
                 args.manifest, args.scope, args.quality_plan, args.evaluation_plan]
        pins = {str(path): digest(path.read_bytes()) for path in files}
        save(output / "input-hashes.json", pins)
        workspace = PureWindowsPath(binding["windows"]["workspace_root"]) / ("m6-" + intent["run"]["run_id"])
        before = remote_read(binding, "@{workspace_exists=(Test-Path -LiteralPath " + ps_string(workspace) + ")} | ConvertTo-Json -Compress", output, "before")
        require(before["workspace_exists"] is False, "fresh workspace required; no test repair allowed")
        command = [binding["python_executable"], "-m", "disclosure_anchor.cli.m6_campaign", "bootstrap-check"]
        for key in ("intent", "intent-sha256", "private-binding", "binding", "manifest", "scope", "quality-plan", "evaluation-plan"):
            command.extend(["--" + key, str(getattr(args, key.replace("-", "_")))])
        command += ["--release-manifest", str(args.package / "release-manifest.json"), "--output", str(output / "campaign")]
        env = {key: value for key, value in os.environ.items() if key not in {"DATABASE_URL", "DISCLOSURE_MIGRATION_DATABASE_URL"}}
        env["PYTHONPATH"] = str(Path(binding["service_root"]) / "src")
        save(output / "command.json", command)
        # Transport cleanup is separate from the native business max_close.
        deadline = intent["run"]["planned_seconds"] + intent["close_grace_seconds"] + 360
        with (output / "entry.stdout").open("xb") as out, (output / "entry.stderr").open("xb") as err:
            child = subprocess.Popen(command, cwd=binding["service_root"], env=env, stdout=out, stderr=err, start_new_session=True)
            evidence["entry_pid"] = child.pid
            save(output / "entry-start.json", {"pid": child.pid, "maximum_wait_seconds": deadline})
            try:
                evidence["entry_exit_code"] = child.wait(timeout=deadline)
            except BaseException:
                child.send_signal(signal.SIGINT)
                try:
                    child.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                evidence["entry_exit_code"] = child.returncode
                evidence["owner_outcome"] = "unknown: reconcile original native receipts before any new attempt"
                raise
        require(evidence["entry_exit_code"] == 0, "official entry failed; retain original campaign evidence")
        start = load(output / "campaign" / "launcher-records" / "process-start.json")
        run_hash = hashlib.sha256(intent["run"]["run_id"].encode()).hexdigest()
        private = workspace / "private"
        native_run = private / "runs" / run_hash
        # Read-only observation after actual exit: no Prepare, uploads or directory changes.
        script = f"""
$private={ps_string(private)}; $run={ps_string(native_run)}
$spec=Join-Path $run 'spec.json'; $journal=Join-Path $run 'events.jsonl'
if ((Get-Item -LiteralPath $journal).Length -gt 8388608) {{ throw 'G0 journal over readback bound' }}
$p=Get-Process -Id {int(start['pid'])} -ErrorAction SilentlyContinue
$same=$false; if ($null -ne $p) {{ $same=($p.StartTime.ToUniversalTime().ToFileTimeUtc() -eq {int(start['creation_filetime_100ns'])}) }}
$identity=@(); $diagnosticCount=0
foreach ($meta in [IO.Directory]::EnumerateFiles($run,'diagnostic-*.json')) {{
 $diagnosticCount++; if ($diagnosticCount -gt 4096) {{ throw 'diagnostic count bound' }}
 $item=Get-Item -LiteralPath $meta
 if ($item.Length -gt 1024 -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {{ throw 'unsafe metadata' }}
 $value=[IO.File]::ReadAllText($meta) | ConvertFrom-Json
 if ($value.code -ceq 'owner_identity') {{
  $body=[IO.Path]::ChangeExtension($meta,'.bin'); $item=Get-Item -LiteralPath $body
  if ($item.Length -gt 65536 -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {{ throw 'unsafe identity' }}
  $identity+=@{{metadata_name=[IO.Path]::GetFileName($meta);
   metadata_sha256='sha256:'+(Get-FileHash -LiteralPath $meta -Algorithm SHA256).Hash.ToLowerInvariant();
   body_sha256='sha256:'+(Get-FileHash -LiteralPath $body -Algorithm SHA256).Hash.ToLowerInvariant()}}
 }}
}}
$directories=@($private,(Join-Path $private 'runs'),(Join-Path $private 'staging'),(Join-Path $private 'attempts'),$run) | ForEach-Object {{
 $a=Get-Acl -LiteralPath $_; @{{path=$_; protected=$a.AreAccessRulesProtected; owner=$a.GetOwner([Security.Principal.SecurityIdentifier]).Value;
 reparse=(([IO.File]::GetAttributes($_) -band [IO.FileAttributes]::ReparsePoint) -ne 0);
 allow_sids=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | Where-Object {{$_.AccessControlType -eq 'Allow'}} | ForEach-Object {{$_.IdentityReference.Value}})}}
}}
@{{matching_owner_alive=$same; listening_count=@(Get-NetTCPConnection -State Listen -LocalPort {int(binding['windows']['port'])} -ErrorAction SilentlyContinue).Count;
 current_sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; directories=@($directories); owner_identities=@($identity);
 spec_count=@(Get-ChildItem -LiteralPath (Join-Path $private 'runs') -Filter spec.json -Recurse).Count;
 spec_sha256='sha256:'+(Get-FileHash -LiteralPath $spec -Algorithm SHA256).Hash.ToLowerInvariant();
 journal_base64=[Convert]::ToBase64String([IO.File]::ReadAllBytes($journal))}} | ConvertTo-Json -Depth 8 -Compress
"""
        after = remote_read(binding, script, output, "after")
        require(pins == {str(path): digest(path.read_bytes()) for path in files}, "frozen input changed during execution")
        evidence.update(verify(output, binding, intent, args.intent_sha256, before, after))
    except (Exception, KeyboardInterrupt) as exc:
        evidence["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
        save(output / "independent-evidence.json", evidence)
    print(json.dumps(evidence, ensure_ascii=False))
    return 0 if evidence["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
