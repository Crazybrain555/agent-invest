// Independent M6 identity / host CLI / self-Job tests. Part of
// test_mineru_m6_native_suite (C# 5, warnaserror).
//
// The positive self-Job entry is deliberately NOT executed inside the suite
// process: entering the kill-on-close Job installs a watchdog that terminates
// the whole process at its deadline and forbids child processes. Positive
// scenarios run as separate child processes ("selfjob-child" mode) that the
// PowerShell runner starts, holds by exact PID/creation time and reaps.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Reflection;
using System.Threading;
using Microsoft.Win32;

public static class MineruM6IdentityTests {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr GetModuleHandleW(string name);

    // The production assembly remains unmodified and separately compiled.
    // Reflection reaches only the two pure internal codec methods; exceptions
    // retain their actual contract type for the negative assertions.
    static object InvokeCodec(string name, params object[] arguments) {
        try { return typeof(MineruM6OwnerIdentity).GetMethod(name, BindingFlags.Static | BindingFlags.NonPublic).Invoke(null, arguments); }
        catch (TargetInvocationException error) { throw error.InnerException ?? error; }
    }
    static uint DecodeBootCounter(object value, RegistryValueKind kind) { return (uint)InvokeCodec("DecodeBootCounter", value, kind); }
    static string BootIdentity(string node, uint counter) { return (string)InvokeCodec("BootIdentity", node, counter); }

    public static void Test01_BootCounterMustBeTypedDword() {
        MineruM6NativeSuite.Equal(68, DecodeBootCounter(0x44, RegistryValueKind.DWord), "DWORD decodes");
        MineruM6NativeSuite.Equal(4294967295L, DecodeBootCounter(-1, RegistryValueKind.DWord), "negative Int32 is an unsigned counter, never a failure or wall-clock fallback");
        MineruM6NativeSuite.Throws<IOException>(delegate { DecodeBootCounter("68", RegistryValueKind.String); }, "REG_SZ refused");
        MineruM6NativeSuite.Throws<IOException>(delegate { DecodeBootCounter(68L, RegistryValueKind.QWord); }, "QWORD refused");
        MineruM6NativeSuite.Throws<IOException>(delegate { DecodeBootCounter(null, RegistryValueKind.DWord); }, "null refused");
        MineruM6NativeSuite.Throws<IOException>(delegate { DecodeBootCounter(68L, RegistryValueKind.DWord); }, "Int64 boxed under DWORD kind refused");
    }

    public static void Test02_BootIdentityHashRule() {
        string node = MineruM6NativeSuite.LabelHash("node");
        string expected = MineruM6NativeSuite.Sha("{\"boot_counter\":68,\"contract_version\":\"m6.windows-boot-counter.v1\",\"windows_node_identity_sha256\":\"" + node + "\"}");
        MineruM6NativeSuite.Equal(expected, BootIdentity(node, 68), "boot identity v1 binds version, node and unsigned counter");
        MineruM6NativeSuite.Check(BootIdentity(node, 69) != expected, "counter change changes the boot identity");
        MineruM6NativeSuite.Check(BootIdentity(MineruM6NativeSuite.LabelHash("other-node"), 68) != expected, "node change changes the boot identity");
        string large = BootIdentity(node, 4294967295u);
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.Sha("{\"boot_counter\":4294967295,\"contract_version\":\"m6.windows-boot-counter.v1\",\"windows_node_identity_sha256\":\"" + node + "\"}"), large, "unsigned formatting of the maximum counter");
    }

    public static void Test03_OneCaptureAttemptPerProcessIncludingFailure() {
        MineruM6NativeSuite.Check(GetModuleHandleW("nvml.dll") == IntPtr.Zero, "NVML is not loaded before the identity probe");
        string wrongNode = MineruM6NativeSuite.LabelHash("not-this-node");
        string uuid = "GPU-00000000-0000-0000-0000-000000000000";
        IOException first = MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6OwnerIdentity(wrongNode, uuid, MineruM6NativeSuite.LabelHash("nvml")); },
            "identity capture against a foreign node hash");
        MineruM6NativeSuite.Log("first identity attempt: " + first.Message);
        MineruM6NativeSuite.Check(GetModuleHandleW("nvml.dll") == IntPtr.Zero, "node refusal happens before any NVML load");
        IOException second = MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6OwnerIdentity(wrongNode, uuid, MineruM6NativeSuite.LabelHash("nvml")); },
            "second capture in the same process");
        MineruM6NativeSuite.Log("second identity attempt: " + second.Message);
        MineruM6NativeSuite.Check(second.Message != first.Message, "second attempt is refused as a repeat, not re-evaluated");
        IOException third = MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6OwnerIdentity(wrongNode, uuid, MineruM6NativeSuite.LabelHash("nvml")); }, "third capture");
        MineruM6NativeSuite.Equal(second.Message, third.Message, "repeat refusal is stable");
        MineruM6NativeSuite.Check(first.Message.IndexOf(wrongNode, StringComparison.Ordinal) < 0, "refusal message does not echo the supplied node hash");
    }
}

public static class MineruM6HostCliTests {
    static int Main(string[] args, out string stderr) {
        TextWriter original = Console.Error;
        StringWriter capture = new StringWriter(CultureInfo.InvariantCulture);
        Console.SetError(capture);
        try { return MineruM6OwnerHost.Main(args); }
        finally { Console.SetError(original); stderr = capture.ToString(); }
    }
    static string[] Args(string seconds, string grace, string resumeDeadline, string anchor) {
        return new string[] { Path.Combine(MineruM6NativeSuite.TempRoot, "no-such-config.json"), MineruM6NativeSuite.LabelHash("cfg"),
            MineruM6NativeSuite.LabelHash("bin"), seconds, grace, "536870912", resumeDeadline, anchor };
    }

    public static void Test01_ArgumentShapeRefusedBeforeAnySideEffect() {
        string stderr;
        MineruM6NativeSuite.Equal(1, Main(new string[0], out stderr), "no arguments");
        MineruM6NativeSuite.Check(stderr.Length > 0, "controlled error is reported");
        MineruM6NativeSuite.Equal(1, Main(new string[7], out stderr), "seven arguments");
        MineruM6NativeSuite.Equal(1, Main(Args("1.5", "1", "0", "none"), out stderr), "non-integer seconds");
        MineruM6NativeSuite.Equal(1, Main(Args("-1", "1", "0", "none"), out stderr), "negative seconds");
        MineruM6NativeSuite.Equal(1, Main(Args("0", "1", "0", "none"), out stderr), "zero seconds");
        MineruM6NativeSuite.Equal(1, Main(Args("7200", "1", "0", "none"), out stderr), "lifetime over 7200 seconds");
        MineruM6NativeSuite.Equal(1, Main(Args("10", "0", "0", "none"), out stderr), "zero grace");
        MineruM6NativeSuite.Equal(1, Main(Args("10", "5", "5", "none"), out stderr), "resume deadline without anchor");
        MineruM6NativeSuite.Equal(1, Main(Args("10", "5", "0", MineruM6NativeSuite.LabelHash("anchor")), out stderr), "anchor without resume deadline");
        MineruM6NativeSuite.Check(stderr.IndexOf(MineruM6NativeSuite.LabelHash("cfg"), StringComparison.Ordinal) < 0, "error output does not echo the configuration hash argument");
        // None of the refusals above may have entered the finite self-Job: a later
        // invalid Enter must still be an argument refusal, not "already entered".
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { MineruM6SelfJob.Enter(Stopwatch.GetTimestamp() - 1, 268435456); }, "Job not entered by refused CLI invocations");
    }
}

public static class MineruM6SelfJobNegativeTests {
    public static void Test01_FiniteLifetimeAndMemoryBounds() {
        long now = Stopwatch.GetTimestamp(), frequency = Stopwatch.Frequency;
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { MineruM6SelfJob.Enter(now - 1, 268435456); }, "deadline in the past");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { MineruM6SelfJob.Enter(now, 268435456); }, "deadline now");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { MineruM6SelfJob.Enter(checked(now + frequency * 7300L), 268435456); }, "deadline beyond 7210 seconds");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { MineruM6SelfJob.Enter(now + frequency * 60L, 134217727); }, "memory below 128 MiB");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { MineruM6SelfJob.Enter(now + frequency * 60L, 1073741825); }, "memory above 1 GiB");
        // Still not entered: the next refusal is again an argument refusal.
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { MineruM6SelfJob.Enter(now - 1, 268435456); }, "no lifetime was installed by refused entries");
    }
}

// Child-process scenarios driven by test_mineru_m6_native_suite.ps1.
// Usage: <suite.exe> selfjob-child <expire SECONDS | normal | second-enter | invalid>
public static class MineruM6SelfJobChild {
    static void Announce(string scenario, string detail) {
        using (Process me = Process.GetCurrentProcess()) {
            Console.WriteLine("M6-CHILD {\"scenario\":" + MineruM6NativeSuite.JsonQuote(scenario) + ",\"pid\":" + me.Id.ToString(CultureInfo.InvariantCulture) +
                ",\"creation_filetime_100ns\":" + me.StartTime.ToUniversalTime().ToFileTimeUtc().ToString(CultureInfo.InvariantCulture) +
                ",\"qpc_ticks\":" + Stopwatch.GetTimestamp().ToString(CultureInfo.InvariantCulture) +
                ",\"qpc_frequency_hz\":" + Stopwatch.Frequency.ToString(CultureInfo.InvariantCulture) +
                ",\"detail\":" + MineruM6NativeSuite.JsonQuote(detail) + "}");
            Console.Out.Flush();
        }
    }
    public static int Run(string[] args) {
        string scenario = args[1];
        try {
            switch (scenario) {
                case "expire": {
                    long seconds = Int64.Parse(args[2], CultureInfo.InvariantCulture);
                    long deadline = checked(Stopwatch.GetTimestamp() + seconds * Stopwatch.Frequency);
                    MineruM6SelfJob job = MineruM6SelfJob.Enter(deadline, 268435456);
                    job.AssertNoChildren();
                    Announce(scenario, "entered; deadline_ticks=" + deadline.ToString(CultureInfo.InvariantCulture));
                    // Block forever: only the finite Job may end this process (exit 124).
                    while (true) Thread.Sleep(100);
                }
                case "normal": {
                    MineruM6SelfJob job = MineruM6SelfJob.Enter(Stopwatch.GetTimestamp() + 120L * Stopwatch.Frequency, 268435456);
                    job.AssertNoChildren();
                    string outcome;
                    bool started = false; int nativeError = 0;
                    try {
                        using (Process child = new Process()) {
                            child.StartInfo = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe"), "/c exit 0") { UseShellExecute = false, CreateNoWindow = true };
                            started = child.Start();
                            child.WaitForExit(10000);
                        }
                    } catch (Win32Exception error) { nativeError = error.NativeErrorCode; }
                    if (!started) outcome = "CHILD_START_REFUSED:" + nativeError.ToString(CultureInfo.InvariantCulture);
                    else {
                        // If the OS let a child exist, the no-child invariant must now report it.
                        try { job.AssertNoChildren(); outcome = "CHILD_UNACCOUNTED"; }
                        catch (InvalidOperationException) { outcome = "CHILD_ACCOUNTED"; }
                    }
                    Announce(scenario, outcome);
                    return outcome == "CHILD_UNACCOUNTED" ? 3 : 0;
                }
                case "second-enter": {
                    MineruM6SelfJob.Enter(Stopwatch.GetTimestamp() + 120L * Stopwatch.Frequency, 268435456);
                    try { MineruM6SelfJob.Enter(Stopwatch.GetTimestamp() + 120L * Stopwatch.Frequency, 268435456); Announce(scenario, "SECOND_ENTER_ALLOWED"); return 3; }
                    catch (InvalidOperationException) { Announce(scenario, "SECOND_ENTER_REFUSED"); return 0; }
                }
                case "invalid": {
                    try { MineruM6SelfJob.Enter(Stopwatch.GetTimestamp() - 1, 268435456); Announce(scenario, "PAST_DEADLINE_ACCEPTED"); return 3; }
                    catch (ArgumentException) { Announce(scenario, "PAST_DEADLINE_REFUSED"); return 0; }
                }
                default:
                    Console.Error.WriteLine("unknown selfjob-child scenario: " + scenario);
                    return 2;
            }
        } catch (Exception error) {
            Console.Error.WriteLine(error.ToString());
            return 4;
        }
    }
}
