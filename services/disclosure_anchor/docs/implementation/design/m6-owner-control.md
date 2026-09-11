# M6 owner control

This private WP-D boundary carries the already accepted M6 run evidence. It does
not schedule business work or write publication state. The Python contract and
client are separate from qualification of the native Windows owner, its journal,
actual process lifetime and storage. A fixture response does not qualify a host.

## Bootstrap and identity

The native bootstrap captures T0 before run-specific input freezing, copying,
startup and warmup. Its immutable `m6.owner-anchor.v1` has the physical boot/QPC
clock, owner incarnation, owner source/device identities, original interval and
finite resource envelope. It deliberately has no corpus/spec hash. The controller
freezes those inputs after the anchor exists, constructs the complete `M6RunSpec`,
compares every shared anchor field and binds the exact spec hash. This resolves
the T0/spec hash cycle without moving input preparation outside measured cost.

The owner must report its stored spec and anchor, never echo caller identity as
an attestation. Before binding it cannot issue a normal status reply; authenticated
non-bind requests close without a success response and retain a bounded refusal
diagnostic. Rebinding different bytes is an error. `run_started` is sequence 1
with stamp T0; all subsequent real observations are received at current QPC.
Same-boot recovery verifies the original journal and deadline, uses an explicit
new incarnation and `owner_resumed`, and keeps admission stopped. No client silently
accepts another owner/anchor, and no cross-host UTC subtraction is used.

The client accepts a changed owner only with an explicit bounded chain of
owner-resumed records obtained from the independently read original journal.
Each record binds the same run/spec/boot/clock/T0/deadline and the preceding
incarnation. This authorizes drain and exact retries of known predecessor stamps;
it permanently disables new admission. A failed status may advance to closed
after cleanup, which confirms closure without making the run eligible for credit.

## Authentication, transport and retry

One pinned SSH session opens one persistent `direct-tcpip` channel to a configured
Windows loopback port. Use an explicitly qualified native management account;
the existing business forwarding-only account intentionally forbids exec. Preserve
the existing GPU exporter port. No new SSH/firewall permissions are implied.

Each request sends `M6-AUTH/1 <64 lowercase hex characters>` plus LF, then complete
canonical `m6.owner-request.v1` bytes plus LF. A response is one canonical
`m6.owner-reply.v1` line. The native protocol cap is 65,536 bytes per JSON body;
its configured event cap must leave room for the reply envelope. The token comes
from an owned private file and never appears in durable models or diagnostic
exceptions. The owner must compare tokens without content-dependent early return,
bind each token to its caller role/incarnation, and close unauthenticated requests
without exposing status. Authentication remains a server boundary even though the
client rejects incompatible roles and admission acknowledgements locally.

The controller may bind/open/close. The selected runner may request a lease,
submit its observations, stop and acknowledge actual admission closure. Independent
verifiers submit only their own observations. All callers may read status.
The server additionally validates event kinds against authenticated roles.

Every control exchange has a fresh request ID; the reply binds its exact hash.
Producer retry identity is separate: identical producer incarnation/sequence/bytes
returns its original durable stamp, including an explicitly recovered predecessor
owner's stamp. Changed bytes under the same producer key must be durably retained
as a conflict. A rejected response cannot conceal an attached accepted record.
EOF, malformed/oversized data, identity drift and cleanup failures propagate;
there are no automatic retries. Lost responses require reconciliation of the exact
pending producer envelope. An exception permanently closes this client's admission
guard while leaving drain/control possible. Failed channel closure retains the
handle for cleanup but prohibits reuse. Closing a local SSH channel proves nothing
about remote owner/Job exit.

## Admission and stop propagation

The local guard requires a sleep-inclusive injected clock. On macOS this is
`mach_continuous_time` converted with `mach_timebase_info`; uptime-only clocks are
not a fallback. Apple documents that this clock continues while the system sleeps.
See [Apple's clock contract](https://developer.apple.com/documentation/kernel/1646199-mach_continuous_time).

A lease is anchored at request **send**, not response receipt. The client subtracts
its uncertainty/drift allowance and caps the grant at the original Windows
deadline. Successful non-lease exchanges may preserve the original unexpired
grant; they cannot extend it. The guard is supplemental, not a hard realtime or
physical-clock qualification claim.

The caller must explicitly supply `stop_propagation_reserve_ns`, derived from the
selected runner's bounded in-flight claim completion, observation append, control
poll and admission-closed ACK. The accepted grant must fit both the configured
lease maximum and `stop_admission_budget - propagation_reserve`. If that leaves
no usable lease, construction fails. A nominal one-second grant with a one-second
total stop budget is insufficient. Real runner integration must establish those
bounds; a synthetic test reserve is not operational qualification.

Windows reaching its deadline closes the owner grant gate and records
`stop_admission_requested`. The Mac runner closes new admission, waits for any
in-flight claim to return, retains every durable claim and stamps its observation,
then sends `M6AdmissionClosedAck` for its exact incarnation. Its immutable
reconciliation receipt identifies all claims and unresolved H0 responsibility.
Only after validating this acknowledgement may the owner record
`stop_admission_effective`. A nonzero unresolved count is a permanent measurement
incident. The acknowledgement and closure sidecars must be archived with the
journal; their hashes alone do not establish that reconciliation occurred.

The propagation reserve includes its clock-drift allowance. A bound owner that
has not opened admission answers lease requests with `ok` and a null lease. A
conflict reply contains the stamp of the submitted conflicting bytes, not the
predecessor observation. A lost SSH session requires a new explicitly composed
transport after closing the old one; there is no silent reconnect.

Late admissions still obey WP-B: responsibility/cost remains, eligible pages are
zero, and exceeding the stop budget makes the entire measurement incomplete.
No adapter drops late evidence to manufacture a complete receipt.

A newly refused, authenticated business observation with the correct role binds
one permanent observation_refused measurement incident to its producer-byte hash.
The owner stores at most one such incident per run, stops admission, and reports
failed; the host retains the refused raw requests in its bounded private
diagnostics. A typo or unexpected late observation cannot be silently discarded to
produce a complete run. Ordinary verifier_drain_pending is flow control and does
not create an incident. Before open and after actual effective stop, new admission
is rejected; a distinct sequence cannot admit the same attempt twice.

The journal reserves eight owner-event slots and their worst-case record bytes
before accepting another producer record. Bootstrap must make its journal limits
match the anchor, account for indexes within the actual process memory limit, and
bound recovery attempts explicitly. This reserve does not promise unlimited crash
recovery. Exhaustion, orphaned/torn sidecars and uncertain IO remain visible and
may require offline reconciliation. Native resource closure precedes immutable
closure-receipt publication; changed orphaned ACK/closure sidecars raise an IO
failure and close the connection. They are never rewritten into clean evidence.

## Native implementation qualification

The Windows implementation must independently prove strict wire parity, durable
append before ACK, conflict retention, damaged-tail preservation, original-deadline
recovery, finite storage/process bounds and actual child/resource closure. An
accepted client package does not establish these gates or automatic PDF/E2E/hour
acceptance. Public contracts and legacy telemetry clocks remain unchanged.

## Native startup prerequisites

The physical identity probe uses a typed Registry64 `BootId` DWORD, the actual
MachineGuid-derived node hash, and the pinned System32 NVML device UUID. Boot
identity v1 hashes the contract version, node hash and unsigned BootId; the outer
physical identity evidence is v2. Missing or mistyped values fail without a UTC
fallback. Microsoft documents this counter as incrementing on a successful boot
in [its Windows boot-counter documentation](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/oem-hvci-enablement).
This is a local identity label, not a tamper-proof attestation. WMI LastBootUpTime
remains a diagnostic field and its availability is still a startup prerequisite;
it does not enter the QPC clock-domain hash. Exactly one capture attempt is allowed
per process, including a failed attempt. Resumption requires a new process.

Native bootstrap validates the complete closed run-spec projection against the
Python contract and compares every shared anchor field before constructing the
already-bound control. Its bounded JSON reader accepts at most 65,536 bytes and
8,192 parsed values; a Python-valid larger spec is not qualified for this native
owner. Unicode carry-in ordering follows scalar order, not UTF-16 ordinal order.
The controller must reject unsupported native bounds before sending a bind.

The endpoint diagnostic sink aggregates no-body transport noise into a fixed set
of saturating counters. Sealing stores one bounded summary; post-seal no-body
noise is a bounded count for the exit-intent metadata. Authenticated raw bytes and
non-success control replies are retained in the private store. A storage budget or
IO failure propagates; it cannot become a success reply or a clean measurement.
These records exclude authentication headers. The fixed host wires this sink
into both endpoint refusals and non-success control replies. A tested standalone
sink does not prove that the host composition is installed.

Repeated `verifier_drain_pending` replies are a narrowly defined no-effect flow
control case: the same canonical command/producer bytes, run and spec retain the
first complete raw request, aggregate a saturating count, and bind all request
hashes in arrival order. Changing only the request nonce does not allocate a new
artifact pair. Changed business bytes get a separate original record; conflicts
and all other rejections remain fully retained. At most 32 distinct pending-drain
commands can be tracked, with no eviction. Summary v2 contains these groups.
A diagnostic storage failure poisons the sink; even summary sealing cannot be
retried after uncertain IO. The host must reserve fixed sidecar/summary capacity
before assigning the remaining diagnostic budget.

The fixed `mineru_m6_owner_host.cs` executable captures T0 at its first controlled
entry and enters its finite self-Job before reading the private deployment. Its
eight arguments bind configuration path/hash, executable hash, planned seconds,
close grace, memory bound, and the explicit original-deadline/anchor resume pair.
Build all production dependencies into that executable before a measured run;
test libraries are not production dependencies. The immutable deployment binding
contains only configuration and executable hashes. It does not contain tokens.

The private deployment file must explicitly belong to the current Windows user,
with access limited to that user and SYSTEM. A protected directory DACL alone
does not guarantee the file owner: an elevated SSH token can create a file owned
by Administrators. Provision the file owner and DACL together, then independently
verify them and the content hash. Use the system machine identity, not optional
environment variables, for the deployment host check. These requirements do not
change SSH account policy or machine-wide PowerShell execution policy.

For a fresh run, the controller publishes the canonical spec only after observing
the original anchor; the host accepts only a matching controller bind before
constructing control. A resumed process reads the existing spec and constructs
recovered control before READY. READY advertises a flushed journal prefix length
and hash. The controller independently reads exactly that prefix, verifies its
records and original owner chain, and only then constructs a recovered client.
The journal writer permits read sharing; a concurrent read-only handle must allow
ReadWrite sharing to coexist with it. Other writers and deletion remain denied.

The host reserves 64 artifact entries and 4 MiB for fixed closure/recovery records
when persisting ordinary diagnostics. Exhaustion propagates, including on resume.
The final closure callback seals diagnostics, releases receipt/binary read pins
and checks the native no-child invariant before the finite metadata tail. A
successful run then closes transport, journal, guard and credentials and writes an
exit-intent explicitly marked as not externally verified. The controller must
still verify the exact process handle and PID/birth. A watchdog exit, failed
startup, socket EOF or missing external proof cannot establish successful closure.
