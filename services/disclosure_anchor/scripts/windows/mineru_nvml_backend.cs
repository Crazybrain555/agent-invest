// Default-off PS5.1 backend. Compile once; no vendor CLI or helper per sample.
// The outer kill-on-close Job/deadline owner must bound every native call.
using System;
using System.ComponentModel;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

public sealed class MineruNvmlBackend : IDisposable {
    private const int NotSupported = 3;
    private IntPtr library = IntPtr.Zero;
    private IntPtr device = IntPtr.Zero;
    private FileStream dllPin;
    private bool initialized;
    private readonly string expectedUuid;
    private readonly int ownerThread = Thread.CurrentThread.ManagedThreadId;
    public readonly string DllSha256;
    public readonly string DeviceIdentitySha256;

    [StructLayout(LayoutKind.Sequential)]
    private struct MemoryInfo { public ulong total, free, used; }
    [StructLayout(LayoutKind.Sequential)]
    private struct Utilization { public uint gpu, memory; }
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int Init();
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int Shutdown();
    [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet=CharSet.Ansi)]
    private delegate int ByUuid(string uuid, out IntPtr handle);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl, CharSet=CharSet.Ansi)]
    private delegate int Uuid(IntPtr handle, StringBuilder uuid, uint capacity);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int Memory(IntPtr handle, out MemoryInfo info);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int Util(IntPtr handle, out Utilization info);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int Power(IntPtr handle, out uint milliwatts);

    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    private static extern IntPtr LoadLibraryExW(string name, IntPtr reserved, uint flags);
    [DllImport("kernel32.dll", CharSet=CharSet.Ansi, SetLastError=true)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)]
    private static extern IntPtr GetModuleHandleW(string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool FreeLibrary(IntPtr module);

    private Shutdown shutdown;
    private Uuid uuid;
    private Memory memory;
    private Util utilization;
    private Power power;

    private Delegate Bind(string name, Type type) {
        IntPtr address = GetProcAddress(library, name);
        if (address == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), name);
        return Marshal.GetDelegateForFunctionPointer(address, type);
    }

    public static string HashUtf8(string text) {
        using (SHA256 hash = SHA256.Create()) {
            return "sha256:" + BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(text))).Replace("-", "").ToLowerInvariant();
        }
    }

    private static void Check(int result, string operation) {
        if (result != 0) throw new InvalidOperationException("NVML " + operation + " failed, code=" + result.ToString(CultureInfo.InvariantCulture));
    }

    public MineruNvmlBackend(string expectedDllSha256, string gpuUuid) {
        if (!Regex.IsMatch(expectedDllSha256, "^sha256:[a-f0-9]{64}\\z") ||
            !Regex.IsMatch(gpuUuid, "^GPU-[a-fA-F0-9]{8}(-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}\\z")) {
            throw new ArgumentException("pinned NVML identity is invalid");
        }
        expectedUuid = gpuUuid;
        DeviceIdentitySha256 = HashUtf8("nvml.device-uuid.v1|" + gpuUuid);
        if (!Environment.Is64BitProcess) throw new InvalidOperationException("64-bit PowerShell is required");
        if (GetModuleHandleW("nvml.dll") != IntPtr.Zero) throw new InvalidOperationException("NVML was loaded before file attestation");
        string path = Path.Combine(Environment.SystemDirectory, "nvml.dll");
        try {
            // Hold a deny-write/deny-delete handle through Shutdown/FreeLibrary;
            // hashing a path then loading a replaceable file is not attestation.
            dllPin = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
            using (SHA256 hash = SHA256.Create()) {
                DllSha256 = "sha256:" + BitConverter.ToString(hash.ComputeHash(dllPin)).Replace("-", "").ToLowerInvariant();
            }
            if (DllSha256 != expectedDllSha256) throw new InvalidOperationException("NVML DLL hash differs from pinned source");
            // Absolute System32 path; dependencies limited to DLL dir/System32.
            library = LoadLibraryExW(path, IntPtr.Zero, 0x100 | 0x800);
            if (library == IntPtr.Zero) throw new Win32Exception();
            Init init = (Init)Bind("nvmlInit_v2", typeof(Init));
            shutdown = (Shutdown)Bind("nvmlShutdown", typeof(Shutdown));
            ByUuid byUuid = (ByUuid)Bind("nvmlDeviceGetHandleByUUID", typeof(ByUuid));
            uuid = (Uuid)Bind("nvmlDeviceGetUUID", typeof(Uuid));
            memory = (Memory)Bind("nvmlDeviceGetMemoryInfo", typeof(Memory));
            utilization = (Util)Bind("nvmlDeviceGetUtilizationRates", typeof(Util));
            power = (Power)Bind("nvmlDeviceGetPowerUsage", typeof(Power));
            Check(init(), "init");
            initialized = true;
            Check(byUuid(gpuUuid, out device), "device-by-uuid");
            VerifyDevice();
        } catch (Exception primary) {
            Exception cleanup = Release();
            if (cleanup != null) throw new AggregateException("NVML initialization and cleanup failed", primary, cleanup);
            throw;
        }
    }

    private void VerifyDevice() {
        StringBuilder actual = new StringBuilder(96);
        Check(uuid(device, actual, 96), "device-uuid");
        if (!String.Equals(actual.ToString(), expectedUuid, StringComparison.Ordinal))
            throw new InvalidOperationException("NVML device UUID drift");
    }

    public string ReadJson() {
        CheckOwner();
        if (!initialized || library == IntPtr.Zero) throw new ObjectDisposedException("MineruNvmlBackend");
        VerifyDevice();
        MemoryInfo mem;
        Utilization util;
        uint milliwatts;
        int memoryResult = memory(device, out mem);
        int utilResult = utilization(device, out util);
        int powerResult = power(device, out milliwatts);
        foreach (int result in new int[] {memoryResult, utilResult, powerResult}) {
            if (result != 0 && result != NotSupported) Check(result, "sample");
        }
        VerifyDevice();
        if (memoryResult == NotSupported || utilResult == NotSupported || powerResult == NotSupported)
            return "{\"reason\":\"collector_unsupported\",\"status\":\"unsupported\",\"values\":null}";
        if (mem.total < 1 || mem.used > mem.total || mem.free > mem.total || util.gpu > 100 || milliwatts > 1000000)
            throw new InvalidOperationException("NVML sample is outside the closed wire bounds");
        // NVML power is milliwatts; on this driver/GPU family it is averaged,
        // not instantaneous 4-Hz power. Decimal formatting also avoids locale
        // and exponent differences in the canonical JSON wire.
        string watts = ((decimal)milliwatts / 1000m).ToString("0.###", CultureInfo.InvariantCulture);
        return "{\"reason\":null,\"status\":\"supported\",\"values\":{" +
            "\"device_identity_sha256\":\"" + DeviceIdentitySha256 + "\"," +
            "\"framebuffer_free_bytes\":" + mem.free.ToString(CultureInfo.InvariantCulture) + "," +
            "\"framebuffer_total_bytes\":" + mem.total.ToString(CultureInfo.InvariantCulture) + "," +
            "\"framebuffer_used_bytes\":" + mem.used.ToString(CultureInfo.InvariantCulture) + "," +
            "\"power_usage_watts\":" + watts + "," +
            "\"utilization_pct\":" + util.gpu.ToString(CultureInfo.InvariantCulture) + "}}";
    }

    public void Dispose() {
        CheckOwner();
        Exception cleanup = Release();
        if (cleanup != null) throw cleanup;
    }

    private void CheckOwner() {
        // One sampler thread owns the native library and the pin. Reject any
        // cross-thread Read/Dispose before touching an unloadable pointer.
        if (Thread.CurrentThread.ManagedThreadId != ownerThread)
            throw new InvalidOperationException("NVML backend requires its single owning thread");
    }

    private Exception Release() {
        List<Exception> errors = new List<Exception>();
        if (initialized) {
            initialized = false;
            try { Check(shutdown(), "shutdown"); }
            catch (Exception error) { errors.Add(error); }
        }
        if (library != IntPtr.Zero) {
            IntPtr previous = library;
            library = IntPtr.Zero;
            try { if (!FreeLibrary(previous)) throw new Win32Exception(); }
            catch (Exception error) { errors.Add(error); }
        }
        FileStream pin = dllPin;
        dllPin = null;
        if (pin != null) {
            try { pin.Dispose(); }
            catch (Exception error) { errors.Add(error); }
        }
        return errors.Count == 0 ? null : new AggregateException("NVML cleanup failed", errors);
    }
}
