// M6-only .NET Framework 4.8 boundary. Compile before starting a measured run.
// One owner process, no subprocess executor. No shared/named Job is opened.
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;

public sealed class MineruM6SelfJob {
    const uint ACTIVE_PROCESS = 8, KILL_ON_CLOSE = 0x2000, JOB_MEMORY = 0x200;
    [StructLayout(LayoutKind.Sequential)] struct IO { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct BASIC {
        public long processTime, jobTime; public uint flags;
        public UIntPtr minWorkingSet, maxWorkingSet; public uint activeLimit;
        public UIntPtr affinity; public uint priority, scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct EXTENDED {
        public BASIC basic; public IO io;
        public UIntPtr processMemory, jobMemory, peakProcessMemory, peakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential)] struct ACCOUNTING {
        public long totalUser, totalKernel, periodUser, periodKernel;
        public uint pageFaults, totalProcesses, activeProcesses, terminatedProcesses;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, int kind, ref EXTENDED value, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool QueryInformationJobObject(IntPtr job, int kind, out ACCOUNTING value, uint size, IntPtr returned);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);

    readonly IntPtr job;
    readonly long hardDeadline;
    readonly Thread watchdog;
    static MineruM6SelfJob lifetime;

    MineruM6SelfJob(long deadline, long maximumMemoryBytes) {
        long now = Stopwatch.GetTimestamp();
        if (!Stopwatch.IsHighResolution || deadline <= now ||
            deadline - now > checked(Stopwatch.Frequency * 7210L) ||
            maximumMemoryBytes < 134217728 || maximumMemoryBytes > 1073741824)
            throw new ArgumentException("M6 finite physical lifetime/memory required");
        hardDeadline = deadline;
        job = CreateJobObjectW(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new Win32Exception();
        bool assigned = false;
        try {
            EXTENDED limits = new EXTENDED();
            limits.basic.flags = ACTIVE_PROCESS | KILL_ON_CLOSE | JOB_MEMORY;
            limits.basic.activeLimit = 1;
            limits.jobMemory = new UIntPtr(checked((ulong)maximumMemoryBytes));
            if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf(typeof(EXTENDED))))
                throw new Win32Exception();
            if (!AssignProcessToJobObject(job, GetCurrentProcess())) throw new Win32Exception();
            assigned = true;
            // Independent from a blocked socket or filesystem write. Expiry
            // deliberately leaves incomplete/partial evidence; it cannot forge
            // a clean close. No runtime/credential/service subprocess is killed.
            watchdog = new Thread(WatchLifetime);
            watchdog.IsBackground = true;
            watchdog.Name = "M6 owner finite lifetime";
            watchdog.Start();
        } finally {
            if (!assigned && !CloseHandle(job)) throw new Win32Exception();
        }
        AssertNoChildren();
    }

    public static MineruM6SelfJob Enter(long hardDeadlineTicks, long maximumMemoryBytes) {
        if (lifetime != null) throw new InvalidOperationException("M6 owner Job already entered");
        lifetime = new MineruM6SelfJob(hardDeadlineTicks, maximumMemoryBytes);
        return lifetime;
    }

    void WatchLifetime() {
        // A dedicated thread is independent of a saturated .NET worker pool.
        while (Stopwatch.GetTimestamp() < hardDeadline) Thread.Sleep(50);
        if (!TerminateJobObject(job, 124)) {
            // If the Job syscall fails, the only remaining owned process is
            // this process. Never expand termination to an external PID/tree.
            if (!TerminateProcess(GetCurrentProcess(), 125))
                Environment.FailFast("M6 owner hard-lifetime termination failed");
        }
    }

    public void AssertNoChildren() {
        ACCOUNTING value;
        if (!QueryInformationJobObject(job, 1, out value, (uint)Marshal.SizeOf(typeof(ACCOUNTING)), IntPtr.Zero))
            throw new Win32Exception();
        if (value.activeProcesses != 1 || value.totalProcesses != 1 || value.terminatedProcesses != 0)
            throw new InvalidOperationException("M6 owner Job has residual children");
        GC.KeepAlive(watchdog);
    }

    // There is intentionally no Dispose/CloseHandle after self assignment:
    // closing the final KILL_ON_CLOSE handle would terminate this owner before
    // its final evidence is flushed. The OS closes it on actual process exit.
    // The independent controller must verify that exact PID/birth has exited.
}
