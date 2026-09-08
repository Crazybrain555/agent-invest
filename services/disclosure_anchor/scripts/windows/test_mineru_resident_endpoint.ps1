param(
    [Parameter(Mandatory = $true)][string]$ManifestPath,
    [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedNvmlSourceSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedWireSourceSha256,
    [Parameter(Mandatory = $true)][string]$ExpectedSupervisorSourceSha256
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Explicit opt-in mechanism test. Only this fixture compiles/starts test tasks;
# measured exporter code has no test callbacks, compiler, or mock backend.
. ([IO.Path]::Combine($PSScriptRoot,'load_mineru_telemetry_assembly.ps1'))
$prepared = Import-MineruTelemetryPreparedAssembly @PSBoundParameters
try {
    $assemblyPath = [IO.Path]::Combine([IO.Path]::GetDirectoryName($ManifestPath),'mineru-telemetry.dll')
    Add-Type -ReferencedAssemblies @($assemblyPath,'System.dll','System.Core.dll') -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;
public static class MineruEndpointProbe {
    sealed class Fixture : IDisposable {
        public int Samples, Closes; public bool Delivered; public string CloseBoundary;
        public readonly int Port; public readonly string Path;
        public readonly ManualResetEventSlim Ready=new ManualResetEventSlim();
        public readonly ManualResetEventSlim CloseEntered=new ManualResetEventSlim();
        public readonly ManualResetEventSlim AllowClose=new ManualResetEventSlim();
        public readonly Task Server;
        public Fixture(int sampleDelay,bool gateClose) {
            TcpListener portProbe=new TcpListener(IPAddress.Loopback,0);
            portProbe.Start(); Port=((IPEndPoint)portProbe.LocalEndpoint).Port; portProbe.Stop();
            string session=Guid.NewGuid().ToString("N"); Path="/v1/"+session+"/gpu_fast";
            string hash=MineruResidentWire.Quote("sha256:"+new string('a',64));
            string identity=MineruResidentWire.Object("exporter_source_sha256",hash,"host_assignment_identity_sha256",hash,
                "boot_identity_sha256",hash,"runtime_bundle_identity_sha256",hash,"process_profile_sha256",hash,
                "clock_domain_identity_sha256",hash,"exporter_process_epoch_sha256",hash);
            Server=Task.Factory.StartNew(()=> {
                using(MineruResidentEndpoint endpoint=new MineruResidentEndpoint(Port,session,"gpu_fast",250,2000,2000,150,identity)) {
                    endpoint.Run(()=>Ready.Set(),deadline=> {
                        int count=Interlocked.Increment(ref Samples);
                        if(sampleDelay>0 && count==1) Thread.Sleep(sampleDelay);
                        return "{\"gpu\":{\"reason\":\"collector_unsupported\",\"status\":\"unsupported\",\"values\":null}}";
                    },deadline=> {
                        CloseBoundary=endpoint.CloseBoundaryJson;
                        Interlocked.Increment(ref Closes); CloseEntered.Set();
                        if(gateClose && !AllowClose.Wait(1000)) throw new TimeoutException("test close gate");
                        return "{\"closed\":true}";
                    });
                    Delivered=endpoint.CloseReplyDelivered;
                }
            });
            if(!Ready.Wait(1000)) { Dispose(); throw new TimeoutException("test READY deadline"); }
        }
        public string Get(string path,out int status) {
            HttpWebRequest request=(HttpWebRequest)WebRequest.Create("http://127.0.0.1:"+Port+path);
            request.Proxy=null; request.Timeout=1500; request.ReadWriteTimeout=1500; request.KeepAlive=true;
            HttpWebResponse response;
            try { response=(HttpWebResponse)request.GetResponse(); }
            catch(WebException error) { if(error.Response==null) throw; response=(HttpWebResponse)error.Response; }
            using(response) using(StreamReader reader=new StreamReader(response.GetResponseStream())) {
                status=(int)response.StatusCode; return reader.ReadToEnd();
            }
        }
        public bool Expired() {
            try { if(!Server.Wait(3500)) throw new TimeoutException("fixture server did not stop"); }
            catch(AggregateException error) {
                foreach(Exception item in error.Flatten().InnerExceptions) if(item is TimeoutException) return true;
                throw;
            }
            return false;
        }
        public void Dispose() {
            AllowClose.Set();
            try { if(!Server.Wait(3500)) throw new TimeoutException("fixture not quiescent"); }
            catch(AggregateException) { if(!Server.IsCompleted) throw; }
            Ready.Dispose(); CloseEntered.Dispose(); AllowClose.Dispose();
        }
    }
    static void Check(bool value,string name,List<string> checks) {
        if(!value) throw new InvalidOperationException("endpoint assertion failed: "+name); checks.Add(name);
    }
    public static string Run() {
        List<string> checks=new List<string>(); int status;
        using(Fixture f=new Fixture(0,false)) {
            Thread.Sleep(50); Check(f.Samples==0,"READY_does_not_sample",checks);
            f.Get("/v1/"+new string('0',32)+"/gpu_fast/after/0",out status);
            Check(status==404 && f.Samples==0,"old_session_cannot_start",checks);
            f.Get(f.Path+"/after/1",out status); Check(status==409 && f.Samples==0,"future_sequence_cannot_start",checks);
            string first=f.Get(f.Path+"/after/0",out status);
            Check(status==200 && MineruResidentWire.Parse(first,65536).Get("sequence").Integer()==1,"first_request_exact_sequence_one",checks);
            string duplicate=f.Get(f.Path+"/after/0",out status); Check(status==200 && duplicate==first,"lost_response_exact_retry",checks);
            string second=f.Get(f.Path+"/after/1",out status);
            Check(status==200 && MineruResidentWire.Parse(second,65536).Get("sequence").Integer()==2,"held_request_next_sample",checks);
            f.Get(f.Path+"/after/0",out status); Check(status==409,"old_gap_not_rebased",checks);
            f.Get(f.Path+"/after/02",out status); Check(status==404,"noncanonical_sequence_rejected",checks);
            f.Get(f.Path+"/close",out status); Check(status==200 && f.Server.Wait(1000) && f.Closes==1 && f.Delivered,"normal_close",checks);
            MineruJsonValue boundary=MineruResidentWire.Parse(f.CloseBoundary,4096);
            Check(boundary.Get("sample_count").Integer()==f.Samples && boundary.Get("last_sequence").Integer()==f.Samples &&
                boundary.Get("skipped_slots").Integer()==0 && boundary.Get("first_sampled_monotonic_ns").Integer()<=boundary.Get("last_sampled_monotonic_ns").Integer() &&
                boundary.Get("last_sampled_monotonic_ns").Integer()<=boundary.Get("closing_monotonic_ns").Integer(),"close_boundary_actual_samples",checks);
            int samples=f.Samples; Thread.Sleep(50); Check(f.Samples==samples,"no_sampling_after_close",checks);
        }
        using(Fixture f=new Fixture(0,false)) {
            Check(f.Expired() && f.Samples==0 && f.Closes==0,"unstarted_lease_expiry_no_success_receipt",checks);
        }
        using(Fixture f=new Fixture(0,false)) {
            long sequence=0;
            while(!f.Server.IsCompleted) {
                try {
                    string body=f.Get(f.Path+"/after/"+sequence,out status);
                    if(status==200) sequence=MineruResidentWire.Parse(body,65536).Get("sequence").Integer();
                } catch(WebException) { break; }
            }
            Check(f.Expired() && f.Samples>1 && f.Closes==0,"renewal_cannot_extend_hard_lifetime",checks);
        }
        using(Fixture f=new Fixture(800,false)) {
            f.Get(f.Path+"/after/0",out status);
            string body=f.Get(f.Path+"/after/1",out status);
            Check(status==409 || (status==200 && MineruResidentWire.Parse(body,65536).Get("sequence").Integer()>2),"missed_slots_expose_gap",checks);
            // Long sample may run into hard lifetime before a close can arrive;
            // this fixture intentionally requires failure, not cleanup success.
            Check(f.Expired(),"slow_callback_hits_finite_lifetime",checks);
        }
        using(Fixture f=new Fixture(0,true)) {
            using(TcpClient client=new TcpClient()) {
                client.Connect(IPAddress.Loopback,f.Port);
                byte[] request=System.Text.Encoding.ASCII.GetBytes("GET "+f.Path+"/close HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n");
                client.GetStream().Write(request,0,request.Length);
                Check(f.CloseEntered.Wait(1000),"close_started_before_reset",checks);
                client.Client.LingerState=new LingerOption(true,0); client.Close();
            }
            Thread.Sleep(50); f.AllowClose.Set();
            Check(f.Server.Wait(1000) && f.Closes==1,"reset_after_close_preserves_completed_cleanup",checks);
            MineruJsonValue boundary=MineruResidentWire.Parse(f.CloseBoundary,4096);
            Check(boundary.Get("sample_count").Integer()==0 && boundary.Get("last_sequence").Integer()==0 &&
                boundary.Get("first_sampled_monotonic_ns").Raw=="null" && boundary.Get("last_observed_at_utc").Raw=="null", "unstarted_close_no_synthetic_sample",checks);
        }
        return "{\"checks\":["+String.Join(",",checks.ConvertAll(MineruResidentWire.Quote).ToArray())+"],\"count\":"+checks.Count+"}";
    }
}
'@
    [Console]::Out.WriteLine([MineruEndpointProbe]::Run())
    $diagnosticExe=[IO.Path]::Combine($PSHOME,'powershell.exe')
    $diagnosticSha='sha256:'+(Get-FileHash -LiteralPath $diagnosticExe -Algorithm SHA256).Hash.ToLowerInvariant()
    function Invoke-DiagnosticProbe([string]$Code,[int]$Timeout,[int]$Maximum) {
        $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Code))
        return [MineruDiagnosticProcess]::Run($diagnosticExe,$diagnosticSha,[string[]]@('-NoProfile','-NonInteractive','-EncodedCommand',$encoded),$Timeout,$Maximum)
    }
    $normal=Invoke-DiagnosticProbe '[Console]::Out.Write("ok");[Console]::Error.Write("diagnostic");exit 7' 5000 1024
    if($normal.ExitCode -ne 7 -or $normal.StandardOutput -cne 'ok' -or $normal.StandardError -cne 'diagnostic'){throw 'diagnostic concurrent drain/exit failure'}
    foreach($probe in @(
        @{code='[Threading.Thread]::Sleep(60000)';timeout=1500;maximum=1024;reason='TimeoutException'},
        @{code='while($true){[Console]::Out.Write([string]::new([char]120,4096))}';timeout=5000;maximum=1024;reason='FormatException'}
    )) {
        $failure=$null; $elapsed=[Diagnostics.Stopwatch]::StartNew()
        try { $null=Invoke-DiagnosticProbe $probe.code $probe.timeout $probe.maximum } catch { $failure=$_.Exception }
        if($null -eq $failure -or $elapsed.ElapsedMilliseconds -gt $probe.timeout+6000 -or $failure.ToString() -notmatch $probe.reason){throw 'diagnostic failure not bounded or not expected'}
        while($null -ne $failure.InnerException -and -not $failure.Data.Contains('ProcessId')){$failure=$failure.InnerException}
        if(-not $failure.Data.Contains('ProcessId')){throw 'diagnostic missing exact process evidence'}
        $survivor=Get-Process -Id ([int]$failure.Data['ProcessId']) -ErrorAction SilentlyContinue
        if($null -ne $survivor){try{if($survivor.StartTime.ToUniversalTime().ToFileTimeUtc() -eq $failure.Data['CreationFiletime100ns']){throw 'diagnostic process survived failure'}}finally{$survivor.Dispose()}}
    }
    [Console]::Out.WriteLine('{"diagnostic_checks":["concurrent_stdout_stderr_exit","timeout_kill_reap","overflow_kill_reap"],"count":3}')
    # Exercise the actual PS artifact writer: final names appear only after a
    # complete flush/close + same-directory no-replace move. Collision evidence
    # is intentionally retained; no cleanup can erase the first artifact.
    . ([IO.Path]::Combine($PSScriptRoot,'load_mineru_resident_session.ps1'))
    function Get-MineruBootstrapSha([byte[]]$Bytes) { return [MineruResidentWire]::Hash($Bytes) }
    $pins=$prepared.Pins
    $runDirectory=[IO.Path]::Combine([IO.Path]::GetDirectoryName($ManifestPath),('artifact-test-'+[Guid]::NewGuid().ToString('N')))
    if([IO.Directory]::Exists($runDirectory)){throw 'new artifact test directory required'}
    $null=[IO.Directory]::CreateDirectory($runDirectory)
    Write-MineruSessionArtifact 'ready.json' '{"value":1}'
    if([IO.File]::ReadAllText([IO.Path]::Combine($runDirectory,'ready.json')) -cne '{"value":1}' -or
       @(Get-ChildItem -LiteralPath $runDirectory).Count -ne 1){throw 'artifact publication failed'}
    $rejected=$false
    try { Write-MineruSessionArtifact 'ready.json' '{"value":2}' } catch { $rejected=$true }
    if(-not $rejected -or [IO.File]::ReadAllText([IO.Path]::Combine($runDirectory,'ready.json')) -cne '{"value":1}' -or
       @(Get-ChildItem -LiteralPath $runDirectory -Filter '*.pending-*').Count -ne 1){throw 'no-replace collision lost evidence'}
    [Console]::Out.WriteLine('{"artifact_checks":["publish_complete_bytes","collision_rejected_original_preserved","failed_pending_evidence_retained"],"count":3}')
} finally { foreach ($pin in $prepared.Pins) { $pin.Dispose() } }
