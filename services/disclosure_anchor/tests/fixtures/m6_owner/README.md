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
