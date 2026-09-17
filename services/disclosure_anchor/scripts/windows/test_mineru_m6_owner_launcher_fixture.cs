// Independent test-only controlled child for the production owner launcher.
//
// It is compiled together with the twelve production native sources (entry point
// selected with /main:LauncherFixture) so that the launcher's pinned-assembly load
// resolves the REAL MineruM6PrivateStore ACL helper and the REAL canonical wire
// instead of a stub that would hide both. The native owner host is never started
// from this entry point: no journal, no endpoint, no private store instance, no
// GPU, NVML, network, database, Docker or PDF operation ever runs here.
//
// Every physical identity in the emitted anchor is an explicitly declared literal.
// The anchor is nevertheless built and proved canonical through the product's own
// MineruM6OwnerWire.Anchor validator, and anchor_sha256 is the real hash of those
// exact bytes, so a launcher that later verifies the anchor properly still accepts
// the success case.
//
// The behaviour to exhibit is derived from the declared test run id (m6t.<mode>.<id>)
// so that the staged deployment document stays the closed m6.owner-deployment.v2
// contract with no test-only field added to it.
using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

public static class LauncherFixture {
    const int StdOutputHandle = -11;
    const int StdErrorHandle = -12;
    const int FloodChunkBytes = 8192;
    const int FloodChunkCount = 512;
    const int HeldMilliseconds = 300000;
    const int ShortHeldMilliseconds = 15000;

    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr GetStdHandle(int id);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetStdHandle(int id, IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr handle);

    static string Sha(char digit) { return "sha256:" + new string(digit, 64); }
    static string Q(string text) { return MineruResidentWire.Quote(text); }
    static string N(long value) { return MineruResidentWire.Integer(value); }

    // Detach the CLR writers first so process shutdown cannot touch a closed handle,
    // then drop this process's only copies of the pipe write ends. The parent then
    // observes EOF on both pipes while this process is still very much alive.
    static bool ClosePipes() {
        Console.Out.Flush();
        Console.Error.Flush();
        Console.SetOut(TextWriter.Null);
        Console.SetError(TextWriter.Null);
        bool closed = true;
        foreach (int id in new int[] { StdOutputHandle, StdErrorHandle }) {
            IntPtr handle = GetStdHandle(id);
            if (!SetStdHandle(id, IntPtr.Zero)) closed = false;
            if (handle != IntPtr.Zero && handle != new IntPtr(-1) && !CloseHandle(handle)) closed = false;
        }
        return closed;
    }

    // Product-independent evidence that the EOF path was really exercised: written into this
    // attempt's own working directory after both pipe write ends are gone.
    static void MarkPipesClosed() {
        File.WriteAllText("fixture-pipes-closed.json",
            "{\"contract_version\":\"mineru.test-fixture-pipes-closed.v1\"}", MineruResidentWire.Utf8);
    }

    static string BuildClock(long frequencyHz) {
        string boot = Q(Sha('2'));
        string frequency = N(frequencyHz);
        string domain = MineruResidentWire.Object("boot_identity_sha256", boot,
            "clock_source", Q("QueryPerformanceCounter"), "frequency_hz", frequency);
        return MineruResidentWire.Object(
            "host_assignment_identity_sha256", Q(Sha('1')),
            "boot_identity_sha256", boot,
            "qpc_frequency_hz", frequency,
            "clock_domain_identity_sha256", Q(MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(domain))));
    }

    static string BuildAnchor(MineruJsonValue configuration, string runId, long plannedSeconds, long closeGraceSeconds) {
        long frequency = Stopwatch.Frequency;
        long t0 = Stopwatch.GetTimestamp();
        string anchor = MineruResidentWire.Object(
            "contract_version", Q("m6.owner-anchor.v1"),
            "run_id", Q(runId),
            "clock", BuildClock(frequency),
            "owner_process_epoch_sha256", Q(Sha('4')),
            "owner_source_sha256", configuration.Get("owner_source_sha256").Raw,
            "gpu_device_identity_sha256", Q(Sha('3')),
            "t0_ticks", N(t0),
            "planned_seconds", N(plannedSeconds),
            "deadline_ticks", N(checked(t0 + plannedSeconds * frequency)),
            "max_close_ticks", N(checked(t0 + (plannedSeconds + closeGraceSeconds) * frequency)),
            "resources", MineruM6OwnerWire.Resources(configuration.Get("resources")));
        // The product's own validator decides whether this is canonical; the fixture
        // never asserts a shape the launcher is supposed to police.
        if (MineruM6OwnerWire.Anchor(MineruResidentWire.Parse(anchor, 65536)) != anchor)
            throw new FormatException("fixture anchor is not canonical");
        return anchor;
    }

    static string BuildReady(string status, string anchorSha256, string anchorRaw, string specSha256,
                             long prefixBytes, string prefixSha256) {
        return MineruResidentWire.Object(
            "status", Q(status),
            "anchor_sha256", Q(anchorSha256),
            "owner_epoch_sha256", Q(Sha('4')),
            "anchor", anchorRaw,
            "spec_sha256", specSha256 == null ? "null" : Q(specSha256),
            "journal_prefix_bytes", N(prefixBytes),
            "journal_prefix_sha256", Q(prefixSha256));
    }

    static void Emit(string line) { Console.Out.Write(line); Console.Out.Write("\n"); Console.Out.Flush(); }

    static void Flood() {
        string chunk = new string('x', FloodChunkBytes);
        for (int i = 0; i < FloodChunkCount; i++) { Console.Out.Write(chunk); Console.Error.Write(chunk); }
        Console.Out.Flush();
        Console.Error.Flush();
    }

    public static int Main(string[] arguments) {
        if (arguments.Length != 8) {
            Console.Error.Write("fixture requires the eight owner-host arguments; received " +
                arguments.Length.ToString(CultureInfo.InvariantCulture));
            return 90;
        }
        string runId, mode, anchorRaw, anchorSha, emptySha = MineruResidentWire.Hash(new byte[0]);
        long plannedSeconds, closeGraceSeconds;
        try {
            plannedSeconds = long.Parse(arguments[3], CultureInfo.InvariantCulture);
            closeGraceSeconds = long.Parse(arguments[4], CultureInfo.InvariantCulture);
            MineruJsonValue configuration = MineruResidentWire.Parse(
                File.ReadAllText(arguments[0], MineruResidentWire.Utf8), 65536);
            runId = configuration.Get("run_id").String();
            string[] parts = runId.Split('.');
            if (parts.Length != 3 || parts[0] != "m6t") throw new FormatException("declared test run id must be m6t.<mode>.<id>");
            mode = parts[1];
            anchorRaw = BuildAnchor(configuration, runId, plannedSeconds, closeGraceSeconds);
            anchorSha = MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(anchorRaw));
        } catch (Exception failure) {
            Console.Error.Write("fixture preparation failed: " + failure.ToString());
            return 90;
        }

        string ready = BuildReady("ready_unbound", anchorSha, anchorRaw, null, 0, emptySha);
        switch (mode) {
            case "normal": Emit(ready); return 0;
            case "nonzero": Emit(ready); return 7;
            case "no_ready": return 0;
            case "ready_not_json": Emit("not a json object at all"); return 0;
            case "ready_extra_field":
                Emit(MineruResidentWire.Object(
                    "status", Q("ready_unbound"), "anchor_sha256", Q(anchorSha), "owner_epoch_sha256", Q(Sha('4')),
                    "anchor", anchorRaw, "spec_sha256", "null", "journal_prefix_bytes", N(0),
                    "journal_prefix_sha256", Q(emptySha), "unexpected_extra_field", Q("present")));
                return 0;
            case "ready_anchor_mismatch": {
                // Both the named run and the declared anchor hash are wrong, so a launcher that only
                // compares the run id and one that also verifies the anchor bytes both refuse it.
                string other = BuildAnchor(MineruResidentWire.Parse(
                    File.ReadAllText(arguments[0], MineruResidentWire.Utf8), 65536), runId + ".other",
                    plannedSeconds, closeGraceSeconds);
                Emit(BuildReady("ready_unbound", Sha('5'), other, null, 0, emptySha));
                return 0;
            }
            case "ready_fresh_state_violation":
                Emit(BuildReady("ready_unbound", anchorSha, anchorRaw, Sha('6'), 4096, Sha('7')));
                return 0;
            case "ready_oversize":
                Console.Out.Write(new string('x', 262144));
                Console.Out.Flush();
                return 0;
            case "ready_write_failure": Directory.CreateDirectory("ready.json"); Emit(ready); return 0;
            case "exit_write_failure": Directory.CreateDirectory("process-exit.json"); Emit(ready); return 0;
            case "flood": Emit(ready); Flood(); return 0;
            case "timeout": Emit(ready); Thread.Sleep(HeldMilliseconds); return 0;
            case "eof_before_ready":
                if (!ClosePipes()) return 91;
                MarkPipesClosed();
                Thread.Sleep(HeldMilliseconds);
                return 0;
            case "eof_after_ready":
                Emit(ready);
                Thread.Sleep(250);
                if (!ClosePipes()) return 91;
                MarkPipesClosed();
                Thread.Sleep(HeldMilliseconds);
                return 0;
            case "cancel_wait": Emit(ready); Thread.Sleep(HeldMilliseconds); return 0;
            case "cancel_ignored": Emit(ready); Thread.Sleep(ShortHeldMilliseconds); return 0;
            default:
                Console.Error.Write("fixture has no declared behaviour for mode " + mode);
                return 92;
        }
    }
}
