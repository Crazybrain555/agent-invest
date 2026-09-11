// Independent test-only suspended launcher. Redirects to owned files, not pipes.
// Holds the original kernel process handle and an unnamed outer Job containing
// only this invocation and descendants. No PID rediscovery or global process kill.
using System;
using System.ComponentModel;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

public sealed class M6BoundedProcess : IDisposable {
    [StructLayout(LayoutKind.Sequential)] struct SA { public int length; public IntPtr descriptor; public int inherit; }
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] struct SI {
        public int cb; public string reserved, desktop, title; public int x,y,xs,ys,xc,yc,fill,flags;
        public short show,reserved2; public IntPtr reservedPtr,input,output,error;
    }
    [StructLayout(LayoutKind.Sequential)] struct PI { public IntPtr process,thread; public int pid,tid; }
    [StructLayout(LayoutKind.Sequential)] struct IO { public ulong a,b,c,d,e,f; }
    [StructLayout(LayoutKind.Sequential)] struct BASIC { public long processTime,jobTime; public uint flags; public UIntPtr min,max; public uint active; public UIntPtr affinity; public uint priority,scheduling; }
    [StructLayout(LayoutKind.Sequential)] struct LIMIT { public BASIC basic; public IO io; public UIntPtr processMemory,jobMemory,peakProcess,peakJob; }
    [StructLayout(LayoutKind.Sequential)] struct ACCOUNT { public long user,kernel,pu,pk; public uint faults,total,active,terminated; }
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateFileW(string path,uint access,uint share,ref SA sa,uint disposition,uint flags,IntPtr template);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool CreateProcessW(string app,StringBuilder args,IntPtr pa,IntPtr ta,bool inherit,uint flags,IntPtr environment,string cwd,ref SI startup,out PI info);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CreateJobObjectW(IntPtr security,string name);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job,int kind,ref LIMIT limits,uint size);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job,int kind,out ACCOUNT account,uint size,IntPtr returned);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll",SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll",SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle,uint millis);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessTimes(IntPtr process,out long creation,out long exit,out long kernel,out long user);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process,out uint code);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateProcess(IntPtr process,uint code);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll",SetLastError=true)] static extern bool CloseHandle(IntPtr handle);
    IntPtr process,job;
    bool disposed;
    readonly Stopwatch elapsed=Stopwatch.StartNew();
    public readonly string StdoutPath,StderrPath;
    public int Pid {get;private set;}
    public long CreationFiletime {get;private set;}
    public long ElapsedMilliseconds {get{return elapsed.ElapsedMilliseconds;}}
    public bool TimedOut {get;private set;}
    public bool ForcedTermination {get;private set;}
    public string CleanupFailure {get;private set;}
    public uint TotalJobProcesses {get {return Accounting().total;}}
    public uint ActiveJobProcesses {get {return Accounting().active;}}
    public bool ExactProcessExited {get {return Wait(0);}}
    public int ExitCode {get {if(!Wait(0))throw new InvalidOperationException("held process has not exited");uint code;if(!GetExitCodeProcess(process,out code))throw new Win32Exception();return unchecked((int)code);}}
    static void CloseCollect(ref IntPtr handle,List<Exception> failures) {
        IntPtr value=handle;handle=IntPtr.Zero;
        if(value!=IntPtr.Zero && value!=new IntPtr(-1) && !CloseHandle(value))failures.Add(new Win32Exception());
    }
    public static string Quote(string value) {
        StringBuilder b=new StringBuilder("\"");int slashes=0;
        foreach(char c in value) {if(c=='\\'){slashes++;continue;}if(c=='\"'){b.Append('\\',slashes*2+1).Append(c);slashes=0;continue;}b.Append('\\',slashes).Append(c);slashes=0;}
        return b.Append('\\',slashes*2).Append('"').ToString();
    }
    public M6BoundedProcess(string exe,string[] args,string cwd,string outputPrefix) {
        StdoutPath=outputPrefix+".stdout.txt";StderrPath=outputPrefix+".stderr.txt";CleanupFailure="";
        IntPtr stdout=IntPtr.Zero,stderr=IntPtr.Zero,stdin=IntPtr.Zero,thread=IntPtr.Zero;
        bool assigned=false;
        List<Exception> failures=new List<Exception>();
        try {
            SA sa=new SA();sa.length=Marshal.SizeOf(typeof(SA));sa.inherit=1;
            stdout=CreateFileW(StdoutPath,0x40000000,3,ref sa,1,0x80,IntPtr.Zero);
            stderr=CreateFileW(StderrPath,0x40000000,3,ref sa,1,0x80,IntPtr.Zero);
            stdin=CreateFileW("NUL",0x80000000,3,ref sa,3,0x80,IntPtr.Zero);
            if(stdout==new IntPtr(-1)||stderr==new IntPtr(-1)||stdin==new IntPtr(-1))throw new Win32Exception();
            job=CreateJobObjectW(IntPtr.Zero,null);if(job==IntPtr.Zero)throw new Win32Exception();
            LIMIT limits=new LIMIT();limits.basic.flags=0x2000|0x200|8;limits.basic.active=8;limits.jobMemory=new UIntPtr(1073741824);
            if(!SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf(typeof(LIMIT))))throw new Win32Exception();
            SI si=new SI();si.cb=Marshal.SizeOf(typeof(SI));si.flags=0x100;si.input=stdin;si.output=stdout;si.error=stderr;
            StringBuilder command=new StringBuilder(Quote(exe));foreach(string arg in args)command.Append(' ').Append(Quote(arg));
            PI pi;if(!CreateProcessW(exe,command,IntPtr.Zero,IntPtr.Zero,true,0x4|0x08000000,IntPtr.Zero,cwd,ref si,out pi))throw new Win32Exception();
            process=pi.process;thread=pi.thread;Pid=pi.pid;long creation,exit,kernel,user;
            if(!GetProcessTimes(process,out creation,out exit,out kernel,out user))throw new Win32Exception();CreationFiletime=creation;
            if(!AssignProcessToJobObject(job,process))throw new Win32Exception();assigned=true;
            if(ResumeThread(thread)==UInt32.MaxValue)throw new Win32Exception();
        } catch(Exception original) {
            failures.Add(original);
            try {if(process!=IntPtr.Zero) {if(assigned){if(!TerminateJobObject(job,126))throw new Win32Exception();}else if(!TerminateProcess(process,126))throw new Win32Exception();if(!Wait(10000))throw new IOException("suspended launch cleanup process did not exit");}}
            catch(Exception cleanup){failures.Add(cleanup);}
        } finally {
            CloseCollect(ref thread,failures);CloseCollect(ref stdout,failures);CloseCollect(ref stderr,failures);CloseCollect(ref stdin,failures);
        }
        if(failures.Count!=0) {
            // A failed constructor has no caller-owned instance to Dispose.
            // Closing the only unnamed Job handle also terminates its members.
            CloseCollect(ref process,failures);CloseCollect(ref job,failures);
            throw new AggregateException("independent suspended launch failed; every original and cleanup error retained",failures);
        }
    }

    ACCOUNT Accounting(){ACCOUNT a;if(!QueryInformationJobObject(job,1,out a,(uint)Marshal.SizeOf(typeof(ACCOUNT)),IntPtr.Zero))throw new Win32Exception();return a;}
    public bool Wait(int milliseconds){if(milliseconds<0 || milliseconds>1800000)throw new ArgumentOutOfRangeException("milliseconds");uint result=WaitForSingleObject(process,(uint)milliseconds);if(result==0)return true;if(result==258)return false;throw new Win32Exception();}
    public void CrashExactOwner(){if(Wait(0))throw new IOException("owner exited before injected crash");ForcedTermination=true;if(!TerminateProcess(process,137))throw new Win32Exception();if(!Wait(10000))throw new IOException("injected crash did not terminate exact owner");}
    public void Finish(int milliseconds) {
        try {
            if(!Wait(milliseconds)){TimedOut=true;ForcedTermination=true;if(!TerminateJobObject(job,126))throw new Win32Exception();if(!Wait(10000))throw new IOException("timed-out exact process remains alive");}
            Stopwatch drain=Stopwatch.StartNew();while(Accounting().active!=0 && drain.ElapsedMilliseconds<10000)System.Threading.Thread.Sleep(20);
            if(Accounting().active!=0){ForcedTermination=true;if(!TerminateJobObject(job,126))throw new Win32Exception();drain.Restart();while(Accounting().active!=0 && drain.ElapsedMilliseconds<10000)System.Threading.Thread.Sleep(20);throw new IOException("descendant survived owner exit; exact outer Job terminated, active="+Accounting().active.ToString(CultureInfo.InvariantCulture));}
        }catch(Exception error){CleanupFailure=error.ToString();throw;}
    }
    public string ReadStdout(){return ReadOwned(StdoutPath);}
    public string ReadStderr(){return ReadOwned(StderrPath);}
    static string ReadOwned(string path) {
        using(FileStream f=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite)) {
            const int maximum=4194304;byte[] bytes=new byte[maximum+1];int used=0;
            while(used<bytes.Length){int n=f.Read(bytes,used,bytes.Length-used);if(n==0)break;used+=n;}
            if(used>maximum)throw new IOException("test output exceeds finite 4 MiB diagnostic limit; raw file preserved");
            return new UTF8Encoding(false,true).GetString(bytes,0,used);
        }
    }
    public void Dispose() {
        if(disposed)return;disposed=true;List<Exception> failures=new List<Exception>();
        try {
            if(process!=IntPtr.Zero && (!Wait(0)||Accounting().active!=0)) {
                ForcedTermination=true;if(!TerminateJobObject(job,126))throw new Win32Exception();
                if(!Wait(10000))throw new IOException("exact Job cleanup timeout");
                Stopwatch watch=Stopwatch.StartNew();while(Accounting().active!=0&&watch.ElapsedMilliseconds<10000)System.Threading.Thread.Sleep(20);
                if(Accounting().active!=0)throw new IOException("Job cleanup incomplete");
            }
        }catch(Exception error){failures.Add(error);}
        finally {CloseCollect(ref process,failures);CloseCollect(ref job,failures);}
        if(failures.Count!=0){CleanupFailure=new AggregateException(failures).ToString();throw new AggregateException("independent exact process cleanup failed",failures);}
    }
}
