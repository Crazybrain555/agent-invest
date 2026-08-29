param(
    [Parameter(Mandatory = $true)][string]$ExporterPath,
    [Parameter(Mandatory = $true)][ValidateSet('gpu_fast', 'host_slow')][string]$Lane,
    [Parameter(Mandatory = $true)][ValidateSet(250, 500, 1000)][int]$CadenceMilliseconds,
    [Parameter(Mandatory = $true)][ValidateRange(1024, 65535)][int]$Port,
    [Parameter(Mandatory = $true)][string]$IdentityJsonPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (($Lane -eq 'host_slow' -and $CadenceMilliseconds -ne 1000) -or
    ($Lane -eq 'gpu_fast' -and $CadenceMilliseconds -notin @(250, 500))) {
    throw 'lane/cadence combination is invalid'
}
# Default-off/runtime-unverified. The child starts suspended and enters the
# kill-on-close Job before its first instruction. Real PS5.1 testing remains an
# activation gate.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class MineruTelemetrySupervisor {
 const uint SUSPENDED=4, INFINITE=0xffffffff, KILL_ON_CLOSE=0x2000;
 [StructLayout(LayoutKind.Sequential)] struct SI { public uint cb; public string a,b,c; public uint d,e,f,g,h,i,j,k; public ushort l,m; public IntPtr n,o,p,q; }
 [StructLayout(LayoutKind.Sequential)] struct PI { public IntPtr process,thread; public uint processId,threadId; }
 [StructLayout(LayoutKind.Sequential)] struct IO { public ulong a,b,c,d,e,f; }
 [StructLayout(LayoutKind.Sequential)] struct BASIC { public long a,b; public uint flags; public UIntPtr c,d; public uint e; public UIntPtr f; public uint g,h; }
 [StructLayout(LayoutKind.Sequential)] struct EXTENDED { public BASIC basic; public IO io; public UIntPtr a,b,c,d; }
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcessW(string app,StringBuilder cmd,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr env,string cwd,ref SI si,out PI pi);
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObjectW(IntPtr a,string n);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr j,int c,ref EXTENDED i,uint n);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr j,IntPtr p);
 [DllImport("kernel32.dll",SetLastError=true)] static extern uint ResumeThread(IntPtr t);
 [DllImport("kernel32.dll",SetLastError=true)] static extern uint WaitForSingleObject(IntPtr h,uint m);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr p,out uint c);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateJobObject(IntPtr j,uint c);
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateProcess(IntPtr p,uint c);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
 static string Quote(string v) {
  if(v.IndexOf('\0')>=0) throw new ArgumentException("NUL");
  StringBuilder b=new StringBuilder("\""); int slashes=0;
  foreach(char ch in v) {
   if(ch=='\\') { slashes++; continue; }
   if(ch=='\"') { b.Append('\\',slashes*2+1).Append(ch); slashes=0; continue; }
   b.Append('\\',slashes).Append(ch); slashes=0;
  }
  b.Append('\\',slashes*2).Append('\"'); return b.ToString();
 }
 public static int Run(string powershell,string[] args) {
  IntPtr job=IntPtr.Zero; PI pi=new PI(); bool assigned=false;
  try {
   job=CreateJobObjectW(IntPtr.Zero,null); if(job==IntPtr.Zero) throw new Win32Exception();
   EXTENDED limit=new EXTENDED(); limit.basic.flags=KILL_ON_CLOSE;
   if(!SetInformationJobObject(job,9,ref limit,(uint)Marshal.SizeOf(typeof(EXTENDED)))) throw new Win32Exception();
   StringBuilder cmd=new StringBuilder(Quote(powershell)); foreach(string arg in args) cmd.Append(" ").Append(Quote(arg));
   SI si=new SI(); si.cb=(uint)Marshal.SizeOf(typeof(SI));
   if(!CreateProcessW(powershell,cmd,IntPtr.Zero,IntPtr.Zero,false,SUSPENDED,IntPtr.Zero,null,ref si,out pi)) throw new Win32Exception();
   if(!AssignProcessToJobObject(job,pi.process)) throw new Win32Exception(); assigned=true;
   if(ResumeThread(pi.thread)==0xffffffff) throw new Win32Exception();
   if(WaitForSingleObject(pi.process,INFINITE)!=0) throw new Win32Exception();
   uint code; if(!GetExitCodeProcess(pi.process,out code)) throw new Win32Exception(); return unchecked((int)code);
  } catch { if(assigned&&job!=IntPtr.Zero) TerminateJobObject(job,1); else if(pi.process!=IntPtr.Zero) TerminateProcess(pi.process,1); throw; }
  finally { if(pi.thread!=IntPtr.Zero) CloseHandle(pi.thread); if(pi.process!=IntPtr.Zero) CloseHandle(pi.process); if(job!=IntPtr.Zero) CloseHandle(job); }
 }
}
'@
$arguments = [string[]]@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$ExporterPath,'-Lane',$Lane,'-CadenceMilliseconds',[string]$CadenceMilliseconds,'-Port',[string]$Port,'-IdentityJsonPath',$IdentityJsonPath)
$powerShellExe = [System.IO.Path]::Combine($PSHOME, 'powershell.exe')
exit [MineruTelemetrySupervisor]::Run($powerShellExe,$arguments)
