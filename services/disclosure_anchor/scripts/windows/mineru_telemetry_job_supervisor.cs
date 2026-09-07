// Default-off PS5.1-compatible, finite Job owner. No polling subprocesses.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class MineruTelemetryJobSupervisor
{
    const uint SUSPENDED = 4, EXTENDED_STARTUPINFO = 0x80000, KILL_ON_CLOSE = 0x2000, WAIT_TIMEOUT = 258;
    [StructLayout(LayoutKind.Sequential)] struct SI {
        public uint cb; public string reserved, desktop, title;
        public uint x, y, xSize, ySize, xCount, yCount, fill, flags;
        public ushort show, reservedSize; public IntPtr reservedBytes, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)] struct PI {
        public IntPtr process, thread; public uint processId, threadId;
    }
    [StructLayout(LayoutKind.Sequential)] struct SIEX { public SI basic; public IntPtr attributes; }
    [StructLayout(LayoutKind.Sequential)] struct IO { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct BASIC {
        public long processTime, jobTime; public uint flags;
        public UIntPtr minWorkingSet, maxWorkingSet; public uint activeLimit;
        public UIntPtr affinity; public uint priority, scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct EXTENDED {
        public BASIC basic; public IO io; public UIntPtr a,b,c,d;
    }
    [StructLayout(LayoutKind.Sequential)] struct ACCOUNTING {
        public long totalUser, totalKernel, periodUser, periodKernel;
        public uint pageFaults, totalProcesses, activeProcesses, terminatedProcesses;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern bool CreateProcessW(string app, StringBuilder command, IntPtr pa, IntPtr ta,
        bool inherit, uint flags, IntPtr env, string cwd, ref SIEX si, out PI pi);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, int kind, ref EXTENDED value, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool QueryInformationJobObject(IntPtr job, int kind, out ACCOUNTING value, uint size, IntPtr returned);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool InitializeProcThreadAttributeList(IntPtr list, int count, uint flags, ref UIntPtr size);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool UpdateProcThreadAttribute(IntPtr list, uint flags, UIntPtr kind, IntPtr value, UIntPtr size, IntPtr previous, IntPtr returned);
    [DllImport("kernel32.dll")] static extern void DeleteProcThreadAttributeList(IntPtr list);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool member);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetProcessTimes(IntPtr process, out long creation, out long exit, out long kernel, out long user);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);

    static string QuoteArgument(string value) {
        if (value == null || value.IndexOf('\0') >= 0) throw new ArgumentException("invalid argument");
        StringBuilder b = new StringBuilder("\""); int slashes = 0;
        foreach (char ch in value) {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') { b.Append('\\', slashes * 2 + 1).Append(ch); slashes = 0; continue; }
            b.Append('\\', slashes).Append(ch); slashes = 0;
        }
        return b.Append('\\', slashes * 2).Append('"').ToString();
    }
    static string JsonString(string value) {
        StringBuilder b = new StringBuilder("\"");
        foreach (char ch in value) {
            if (ch == '"' || ch == '\\') b.Append('\\').Append(ch);
            else if (ch < 32 || ch > 126) b.Append("\\u").Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
            else b.Append(ch);
        }
        return b.Append('"').ToString();
    }
    static string Number(long value) { return value.ToString(CultureInfo.InvariantCulture); }
    static ACCOUNTING Accounting(IntPtr job) {
        ACCOUNTING value;
        if (!QueryInformationJobObject(job, 1, out value, (uint)Marshal.SizeOf(typeof(ACCOUNTING)), IntPtr.Zero))
            throw new Win32Exception();
        if (value.totalUser < 0 || value.totalKernel < 0 || value.totalProcesses < 1)
            throw new InvalidOperationException("invalid actual Job accounting");
        return value;
    }
    static ACCOUNTING Quiesce(IntPtr job, Stopwatch clock, long deadline) {
        while (true) {
            ACCOUNTING value = Accounting(job);
            if (value.activeProcesses == 0) return value;
            long remaining = deadline - clock.ElapsedMilliseconds;
            if (remaining <= 0) throw new TimeoutException("Job members did not quiesce");
            Thread.Sleep((int)Math.Min(25, remaining));
        }
    }
    static void Close(IntPtr value, List<Exception> failures) {
        if (value != IntPtr.Zero && !CloseHandle(value)) failures.Add(new Win32Exception());
    }

    public static string Run(string executable, string[] arguments, int lifetimeMilliseconds,
                             int cleanupMilliseconds, string sourceSha256) {
        if (lifetimeMilliseconds < 1000 || lifetimeMilliseconds > 7200000 ||
            cleanupMilliseconds < 1000 || cleanupMilliseconds > 10000)
            throw new ArgumentException("finite lifetime/cleanup bounds required");
        if (!System.Text.RegularExpressions.Regex.IsMatch(sourceSha256, @"\Asha256:[0-9a-f]{64}\z"))
            throw new ArgumentException("canonical supervisor source SHA required");
        IntPtr job = IntPtr.Zero; PI pi = new PI(); bool assigned = false, quiescent = false;
        IntPtr attributes = IntPtr.Zero, jobList = IntPtr.Zero; bool attributesInitialized = false;
        List<Exception> failures = new List<Exception>(); string result = null;
        Stopwatch clock = Stopwatch.StartNew(); string jobInstance = Guid.NewGuid().ToString("D");
        try {
            // Unnamed, non-inheritable handle cannot accidentally open a shared Job.
            job = CreateJobObjectW(IntPtr.Zero, null); if (job == IntPtr.Zero) throw new Win32Exception();
            EXTENDED limit = new EXTENDED(); limit.basic.flags = KILL_ON_CLOSE;
            if (!SetInformationJobObject(job, 9, ref limit, (uint)Marshal.SizeOf(typeof(EXTENDED)))) throw new Win32Exception();
            StringBuilder command = new StringBuilder(QuoteArgument(executable));
            foreach (string arg in arguments) command.Append(' ').Append(QuoteArgument(arg));
            if (command.Length > 32766) throw new ArgumentException("Windows command line exceeded bound");
            UIntPtr attributeSize = UIntPtr.Zero;
            if (InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref attributeSize) ||
                Marshal.GetLastWin32Error() != 122 || attributeSize.ToUInt64() == 0 || attributeSize.ToUInt64() > 65536)
                throw new InvalidOperationException("unexpected Job attribute size query");
            attributes = Marshal.AllocHGlobal(checked((int)attributeSize.ToUInt64()));
            if (!InitializeProcThreadAttributeList(attributes, 1, 0, ref attributeSize)) throw new Win32Exception();
            attributesInitialized = true;
            jobList = Marshal.AllocHGlobal(IntPtr.Size); Marshal.WriteIntPtr(jobList, job);
            // PROC_THREAD_ATTRIBUTE_JOB_LIST: process/input attribute 13.
            // Windows 10+ required; no post-create assignment fallback.
            if (!UpdateProcThreadAttribute(attributes, 0, new UIntPtr(0x2000d), jobList,
                                           new UIntPtr((uint)IntPtr.Size), IntPtr.Zero, IntPtr.Zero)) throw new Win32Exception();
            SIEX si = new SIEX(); si.basic.cb = (uint)Marshal.SizeOf(typeof(SIEX)); si.attributes = attributes;
            if (!CreateProcessW(executable, command, IntPtr.Zero, IntPtr.Zero, false, SUSPENDED | EXTENDED_STARTUPINFO,
                                IntPtr.Zero, null, ref si, out pi)) throw new Win32Exception();
            assigned = true;
            bool member;
            if (!IsProcessInJob(pi.process, job, out member)) throw new Win32Exception();
            if (!member) throw new InvalidOperationException("creation-time Job membership absent");
            long childCreation, ignoredExit, ignoredKernel, ignoredUser;
            if (!GetProcessTimes(pi.process, out childCreation, out ignoredExit, out ignoredKernel, out ignoredUser)) throw new Win32Exception();
            if (ResumeThread(pi.thread) == 0xffffffff) throw new Win32Exception();
            long remaining = lifetimeMilliseconds - clock.ElapsedMilliseconds;
            uint wait = remaining <= 0 ? WAIT_TIMEOUT : WaitForSingleObject(pi.process, (uint)remaining);
            bool forced = wait == WAIT_TIMEOUT;
            if (wait != 0 && !forced) throw new Win32Exception();
            if (forced && !TerminateJobObject(job, 124)) throw new Win32Exception();
            ACCOUNTING accounting;
            try { accounting = Quiesce(job, clock, clock.ElapsedMilliseconds + cleanupMilliseconds); }
            catch (TimeoutException) {
                forced = true;
                if (!TerminateJobObject(job, 124)) throw new Win32Exception();
                accounting = Quiesce(job, clock, clock.ElapsedMilliseconds + cleanupMilliseconds);
            }
            quiescent = true;
            uint exitCode; if (!GetExitCodeProcess(pi.process, out exitCode)) throw new Win32Exception();
            using (Process own = Process.GetCurrentProcess()) {
                long ownCreation, ownExit, ownUserTicks, ownKernelTicks;
                if (!GetProcessTimes(GetCurrentProcess(), out ownCreation, out ownExit, out ownKernelTicks, out ownUserTicks)) throw new Win32Exception();
                long ownUser = checked(ownUserTicks * 100L), ownKernel = checked(ownKernelTicks * 100L);
                // All Job descendants are now dead; totals include exited members.
                // Own CPU is explicitly pre-attestation, not this serialization/exit.
                result = "{\"child_creation_filetime_100ns\":" + Number(childCreation) +
                    ",\"child_exit_code\":" + Number(exitCode) + ",\"child_pid\":" + Number(pi.processId) +
                    ",\"contract_version\":\"mineru.windows-job-accounting.v1\",\"forced_termination\":" + (forced ? "true" : "false") +
                    ",\"job_active_processes\":" + Number(accounting.activeProcesses) +
                    ",\"job_instance\":" + JsonString(jobInstance) +
                    ",\"job_system_ns_total\":" + Number(checked(accounting.totalKernel * 100L)) +
                    ",\"job_total_processes\":" + Number(accounting.totalProcesses) +
                    ",\"job_user_ns_total\":" + Number(checked(accounting.totalUser * 100L)) +
                    ",\"supervisor_creation_filetime_100ns\":" + Number(ownCreation) +
                    ",\"supervisor_pid\":" + Number(own.Id) +
                    ",\"supervisor_pre_attestation_system_ns_total\":" + Number(ownKernel) +
                    ",\"supervisor_pre_attestation_user_ns_total\":" + Number(ownUser) +
                    ",\"supervisor_source_sha256\":" + JsonString(sourceSha256) + "}";
            }
        } catch (Exception ex) { failures.Add(ex); }
        finally {
            if (!quiescent && pi.process != IntPtr.Zero) {
                try {
                    if (assigned) {
                        if (!TerminateJobObject(job, 125)) throw new Win32Exception();
                        Quiesce(job, clock, clock.ElapsedMilliseconds + cleanupMilliseconds);
                    } else {
                        if (!TerminateProcess(pi.process, 125)) throw new Win32Exception();
                        if (WaitForSingleObject(pi.process, (uint)cleanupMilliseconds) != 0)
                            throw new TimeoutException("unassigned child did not exit");
                    }
                } catch (Exception ex) { failures.Add(ex); }
            }
            Close(pi.thread, failures); Close(pi.process, failures); Close(job, failures);
            if (attributesInitialized) DeleteProcThreadAttributeList(attributes);
            if (attributes != IntPtr.Zero) Marshal.FreeHGlobal(attributes);
            if (jobList != IntPtr.Zero) Marshal.FreeHGlobal(jobList);
        }
        if (failures.Count > 0) throw new AggregateException("Windows Job lifecycle failed", failures);
        return result;
    }
}
