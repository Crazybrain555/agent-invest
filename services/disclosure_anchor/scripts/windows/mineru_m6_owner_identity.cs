// One startup attestation, never a per-sample CIM/CLI process. Call only after
// entering the finite self-Job: WMI and vendor native calls may block.
using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Management;
using System.Threading;
using Microsoft.Win32;

public sealed class MineruM6OwnerIdentity {
    public readonly string NodeSha,BootUtc,BootSha,HostSha,GpuDeviceSha,ClockRaw;
    public readonly int ProcessId;
    public readonly long CreationFiletime;
    public readonly uint BootCounter;
    static int captureAttempted;
    static string Q(string x) { return MineruResidentWire.Quote(x); }
    static string H(string x) { return MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(x)); }
    static string Obj(params string[] values) { return MineruResidentWire.Object(values); }

    // Microsoft documents BootId as incrementing on each successful boot:
    // https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/oem-hvci-enablement
    // It is a host-local identity label, not a tamper-proof attestation. Never
    // write this value or fall back to a wall-clock date when it is unavailable.
    internal static uint DecodeBootCounter(object value,RegistryValueKind kind) {
        if(kind!=RegistryValueKind.DWord || !(value is int))
            throw new IOException("M6 physical boot counter is not a DWORD");
        return unchecked((uint)(int)value);
    }
    static uint ReadBootCounter() {
        using(RegistryKey machine=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64))
        using(RegistryKey key=machine.OpenSubKey(@"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters",false)) {
            if(key==null) throw new IOException("M6 physical boot counter unavailable");
            object value=key.GetValue("BootId",null,RegistryValueOptions.DoNotExpandEnvironmentNames);
            if(value==null) throw new IOException("M6 physical boot counter missing");
            return DecodeBootCounter(value,key.GetValueKind("BootId"));
        }
    }
    internal static string BootIdentity(string nodeSha,uint counter) {
        return H(Obj("contract_version",Q("m6.windows-boot-counter.v1"),
            "windows_node_identity_sha256",Q(nodeSha),"boot_counter",MineruResidentWire.Integer(counter)));
    }

    static string NodeIdentity() {
        using(RegistryKey machine=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64))
        using(RegistryKey key=machine.OpenSubKey(@"SOFTWARE\Microsoft\Cryptography",false)) {
            if(key==null) throw new IOException("M6 physical node identity unavailable");
            string text=key.GetValue("MachineGuid",null,RegistryValueOptions.DoNotExpandEnvironmentNames) as string;
            Guid value;
            if(text==null || !Guid.TryParse(text.Trim(),out value)) throw new IOException("M6 physical node identity malformed");
            // Same rule as the existing Windows runtime collector. Do not emit
            // the registry value or use a caller-supplied node hash as evidence.
            return H(text.Trim().ToLowerInvariant());
        }
    }
    static string BootTime() {
        EnumerationOptions options=new EnumerationOptions();
        options.Timeout=TimeSpan.FromSeconds(5);options.ReturnImmediately=true;options.Rewindable=false;options.BlockSize=1;
        using(ManagementObjectSearcher searcher=new ManagementObjectSearcher(
            new ManagementScope(@"\\.\root\cimv2"),new ObjectQuery("SELECT LastBootUpTime FROM Win32_OperatingSystem"),options))
        using(ManagementObjectCollection rows=searcher.Get()) {
            string result=null;
            foreach(ManagementBaseObject row in rows) using(row) {
                if(result!=null) throw new IOException("M6 OS identity is not singleton");
                string raw=row["LastBootUpTime"] as string;
                if(raw==null) throw new IOException("M6 physical boot identity unavailable");
                result=ManagementDateTimeConverter.ToDateTime(raw).ToUniversalTime().ToString("o",CultureInfo.InvariantCulture);
            }
            if(result==null) throw new IOException("M6 physical boot identity missing");
            return result;
        }
    }
    public MineruM6OwnerIdentity(string expectedNodeSha,string gpuUuid,string expectedNvmlDllSha) {
        // Native module residency is not a reliable disposal signal. One startup
        // attempt per process, including a failed attempt; recovery is a new owner.
        if(Interlocked.CompareExchange(ref captureAttempted,1,0)!=0)
            throw new IOException("M6 physical identity capture already attempted in this process");
        if(!Environment.Is64BitProcess || !Stopwatch.IsHighResolution || Stopwatch.Frequency<=0)
            throw new IOException("M6 physical Windows QPC requires a 64-bit high-resolution owner");
        NodeSha=NodeIdentity();
        if(NodeSha!=expectedNodeSha) throw new IOException("M6 physical node differs from authorized deployment");
        BootCounter=ReadBootCounter();
        BootUtc=BootTime(); // Diagnostic only; never part of QPC/boot identity.
        using(MineruNvmlBackend backend=new MineruNvmlBackend(expectedNvmlDllSha,gpuUuid)) {
            // The existing backend pins the System32 DLL, loads only that exact
            // file, looks up by UUID and verifies the actual device UUID.
            GpuDeviceSha=backend.DeviceIdentitySha256;
        }
        HostSha=H(Obj("windows_node_identity_sha256",Q(NodeSha),"gpu_uuid",Q(gpuUuid)));
        BootSha=BootIdentity(NodeSha,BootCounter);
        string domain=Obj("boot_identity_sha256",Q(BootSha),"clock_source",Q("QueryPerformanceCounter"),
                          "frequency_hz",MineruResidentWire.Integer(Stopwatch.Frequency));
        ClockRaw=Obj("host_assignment_identity_sha256",Q(HostSha),"boot_identity_sha256",Q(BootSha),
                     "qpc_frequency_hz",MineruResidentWire.Integer(Stopwatch.Frequency),"clock_domain_identity_sha256",Q(H(domain)));
        MineruM6OwnerWire.Clock(MineruResidentWire.Parse(ClockRaw,65536));
        using(Process process=Process.GetCurrentProcess()) {
            ProcessId=process.Id;CreationFiletime=process.StartTime.ToUniversalTime().ToFileTimeUtc();
        }
    }
    public string ProcessEpoch(string runId,string sourceSha) {
        return H(Obj("run_id",Q(runId),"owner_source_sha256",Q(sourceSha),"boot_identity_sha256",Q(BootSha),
            "pid",MineruResidentWire.Integer(ProcessId),"creation_filetime_100ns",MineruResidentWire.Integer(CreationFiletime)));
    }
    public string Evidence() {
        return Obj("contract_version",Q("m6.physical-owner-identity.v2"),"windows_node_identity_sha256",Q(NodeSha),
            "boot_counter",MineruResidentWire.Integer(BootCounter),"boot_identity_version",Q("m6.windows-boot-counter.v1"),
            "windows_boot_utc",Q(BootUtc),"gpu_device_identity_sha256",Q(GpuDeviceSha),"clock",ClockRaw,
            "pid",MineruResidentWire.Integer(ProcessId),"creation_filetime_100ns",MineruResidentWire.Integer(CreationFiletime));
    }
}
