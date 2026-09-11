# M6 native component fixtures

`component-vectors.v1.json` contains synthetic Python M6 contract values for the
Windows wire/journal checks. Its SHA256 is
`b341ab55284809eb82fff16a4852bfbb99257c7dd8551f7eb682f90e051b4a9d`.
The source hashes, run IDs, QPC frequency and document facts are test identities;
they do not describe a physical host, real PDF or publication qualification.

The vectors cover complete requests and stamped records, anchor/resource values,
Unicode and Int64 producer values, and an original-deadline owner resume chain.
The native checks also inject journal/guard flush failures, preserve truncated
input, enforce separate record/log bounds, and compare pre/post-write bytes.

For the explicit Windows .NET Framework 4.8 gate, precompile
`scripts/windows/mineru_resident_wire.cs`, `mineru_m6_owner_wire.cs`,
`mineru_m6_owner_journal.cs`, `mineru_m6_writer_guard.cs` and
`mineru_m6_owner_platform.cs` into one library using the qualified compiler,
`/noconfig`, `/target:library`, `/warnaserror+` and explicit Framework references
to `System.dll` and `System.Net.Http.dll`. Build before starting a measured run.
Keep exact source/compiler/library hashes in the run evidence.

Invoke `scripts/windows/test_mineru_m6_owner_components.ps1` with absolute
`-AssemblyPath`, its hex `-ExpectedAssemblySha256`, this file's absolute
`-VectorsPath`, the hex `-ExpectedVectorsSha256` above, and a new
`-OutputDirectory`. The separate
`scripts/windows/test_mineru_m6_self_job_parent.ps1` takes the same assembly
arguments, absolute `-ProbePath` pointing to `test_mineru_m6_self_job.ps1`, its
hex `-ExpectedProbeSha256`, and another new `-OutputDirectory`. It starts only
its own finite test children and verifies their exact handles, PID/birth and exits.
These are opt-in Windows checks, outside deterministic no-DB unit discovery.

The production bootstrap must independently qualify private exclusive streams,
real device/boot/QPC identity, actual process closure and an event/attempt envelope
that fits its Job memory bound. A clean marker after a failed marker flush may
replay only when the journal itself was already flushed and hashes agree. A dirty
or torn marker fails closed; these components never repair it automatically.
Passing these fixtures does not qualify the complete owner, PDF runner, public
consumer, quality evaluation or hour/stability measurements.


`binding-vectors.v1.json` contains 20 synthetic RunSpec/anchor pairs, including
service/E2E mode authority, supplementary Unicode ordering, carry-in bounds,
unknown fields, physical identity mismatch and noncanonical input. SHA256:
`a892d4d4ea512933127773e6f56e475afb5d246b8e75e19992b1b3dd57d51dc7`.
Expected acceptance was independently computed with the Python M6RunSpec JSON
validator, M6OwnerAnchor.assert_spec and canonical-byte comparison.

Compile `mineru_m6_owner_binding.cs`, `mineru_m6_private_store.cs` and
`test_mineru_m6_owner_binding.cs` against the above qualified component library,
with explicit System.dll/System.Core.dll references. Pin compiler, sources,
library and vector bytes before calling `MineruM6OwnerBindingChecks.Run` with
vector UTF-8 text and a new private test-directory path. Its actual Windows checks
also prove 10,000 unauthenticated errors allocate no diagnostic artifact, distinct
authenticated bytes survive, one summary binds the counts, and storage errors
propagate. It creates only its new test artifacts; it does not operate business
services or claim complete-owner qualification.

The separate `test_mineru_m6_owner_identity.cs` checks typed BootId decoding and
identity separation. It is compiled alongside the identity source for mechanism
checks. Production compilation must omit test sources and record its own output
hash. Physical capture and its negative cases each need a fresh finite owner
process. Reboot, sleep and clock-adjustment behavior cannot be inferred from these
component tests.

The binding check also replays 1,000 `verifier_drain_pending` responses with one
unchanged command and distinct request IDs, within an eight-artifact store. It
checks original-byte retention, the ordered request-hash chain, separate changed
business bytes, conflict retention and a permanent failure latch after uncertain
storage or sealing. This is protocol flow-control evidence, not PDF throughput.

For the complete native executable, compile the following production sources in
one invocation: `mineru_m6_owner_host.cs`, `mineru_m6_private_store.cs`,
`mineru_m6_owner_binding.cs`, `mineru_m6_owner_identity.cs`,
`mineru_m6_run_control.cs`, `mineru_m6_owner_endpoint.cs`,
`mineru_m6_owner_journal.cs`, `mineru_m6_writer_guard.cs`,
`mineru_m6_owner_platform.cs`, `mineru_m6_owner_wire.cs`,
`mineru_resident_wire.cs` and `mineru_nvml_backend.cs`. Use `/noconfig`,
`/target:exe`, `/main:MineruM6OwnerHost`, `/warnaserror+` and explicit references to
`System.dll`, `System.Core.dll`, `System.Net.Http.dll` and `System.Management.dll`.
Record the complete source manifest, compiler hash and executable hash before
starting. Mechanism test classes belong in a separate DLL referencing this EXE.

`test_mineru_m6_owner_host_parent.ps1` starts only the pinned owner executable.
Supply its absolute binary/configuration paths and prefixed SHA256 identities,
a new private output directory, the expected native hostname, planned seconds
and close grace. The sum must not exceed 90 seconds. Pair it with a concurrently
running control fixture; running the parent alone intentionally reaches the
owner's hard lifetime and cannot pass normal closure. If the host policy permits
it, invoke the reviewed script with process-scoped `RemoteSigned`; do not modify
machine/user execution policy for this gate. Invocation scheduling is outside
T0; wait for READY separately from the original native interval.

The parent records the actual started PID/birth and keeps the process handle
through exit. `-InjectCrashAfterSignal` is an opt-in zero-PDF failure fixture:
after a durable protocol observation, publish `crash-request.json` into that
parent's output directory containing exactly the PID/birth from its
`process-start.json`. It kills only that newly started process through its held
Process object. A subsequent explicit invocation supplies the original anchor
SHA and `ResumeDeadlineTicks`, original configuration and binary. Verify the
independently read journal prefix before authorizing the recovered client.
The damaged-tail case injects bytes only into its newly created test run after
the first child has actually exited, then checks rejection and unchanged journal
and guard bytes. It never corrupts a business run or repairs uncertain evidence.
