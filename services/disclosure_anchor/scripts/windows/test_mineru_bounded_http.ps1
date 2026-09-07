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
# Opt-in standalone real-loopback mechanism gate. Test-only compiler/thread are
# outside every measured exporter. Never use this fixture as a runtime backend.
. ([IO.Path]::Combine($PSScriptRoot,'load_mineru_telemetry_assembly.ps1'))
$prepared = Import-MineruTelemetryPreparedAssembly @PSBoundParameters
$checks = [Collections.Generic.List[string]]::new()
function Assert-True([bool]$Value,[string]$Name) {
    if (-not $Value) { throw ('assertion failed: ' + $Name) }
    $checks.Add($Name)
}
try {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
public sealed class MineruHttpProbeServer : IDisposable {
    readonly TcpListener listener;
    readonly Task worker;
    public readonly int Port;
    public int Connections;
    public int Requests;
    public MineruHttpProbeServer(string head,byte[] body,int repetitions,int headerDelay,int bodyDelay) {
        listener=new TcpListener(IPAddress.Loopback,0); listener.Start(1);
        Port=((IPEndPoint)listener.LocalEndpoint).Port;
        worker=Task.Factory.StartNew(delegate {
            try {
                using(TcpClient peer=listener.AcceptTcpClient()) {
                    Connections++; peer.ReceiveTimeout=2000; peer.SendTimeout=2000;
                    using(NetworkStream stream=peer.GetStream()) {
                        for(int attempt=0;attempt<repetitions;attempt++) {
                            StringBuilder request=new StringBuilder();
                            while(!request.ToString().EndsWith("\r\n\r\n",StringComparison.Ordinal)) {
                                int value=stream.ReadByte(); if(value<0) throw new IOException("probe request EOF");
                                request.Append((char)value); if(request.Length>8192) throw new IOException("probe request bound");
                            }
                            Requests++; Thread.Sleep(headerDelay);
                            byte[] headers=Encoding.ASCII.GetBytes(head); stream.Write(headers,0,headers.Length); stream.Flush();
                            Thread.Sleep(bodyDelay); stream.Write(body,0,body.Length); stream.Flush();
                        }
                    }
                }
            } finally { listener.Stop(); }
        });
    }
    public void Finish(bool peerMayAbort) {
        try { if(!worker.Wait(5000)) throw new TimeoutException("probe did not quiesce"); }
        catch(AggregateException error) {
            if(!peerMayAbort) throw;
            foreach(Exception item in error.Flatten().InnerExceptions) if(!(item is IOException)) throw;
        }
    }
    public void Dispose() { listener.Stop(); Finish(true); }
}
'@
    $utf8 = [Text.UTF8Encoding]::new($false,$true)
    $normalHead = "HTTP/1.1 200 OK`r`nContent-Length: 2`r`nConnection: keep-alive`r`n`r`n"
    $server = [MineruHttpProbeServer]::new($normalHead,$utf8.GetBytes('{}'),3,0,0)
    $client = [MineruBoundedHttp]::new($server.Port,$prepared.Manifest.http_assembly_sha256)
    try {
        foreach ($index in 1..3) {
            Assert-True ($client.Get('/health',64,[MineruResidentWire]::Deadline(2000)) -ceq '{}') 'persistent_http_body'
        }
        $server.Finish($false)
        Assert-True ($server.Connections -eq 1 -and $server.Requests -eq 3) 'one_connection_three_requests'
        Assert-True ($null -eq $client.Pending) 'successful_io_quiescent'
    } finally { $client.Dispose(); $server.Dispose() }
    $cases = @(
        @{name='chunked'; head="HTTP/1.1 200 OK`r`nTransfer-Encoding: chunked`r`n`r`n"; body="2`r`n{}`r`n0`r`n`r`n"; maximum=64; hd=0; bd=0; ok=$true},
        @{name='chunked_overflow'; head="HTTP/1.1 200 OK`r`nTransfer-Encoding: chunked`r`n`r`n"; body="3`r`nxxx`r`n0`r`n`r`n"; maximum=2; hd=0; bd=0; ok=$false},
        @{name='declared_overflow'; head=$normalHead; body='{}'; maximum=1; hd=0; bd=0; ok=$false},
        @{name='truncated'; head=$normalHead; body='{'; maximum=64; hd=0; bd=0; ok=$false},
        @{name='header_deadline'; head=$normalHead; body='{}'; maximum=64; hd=400; bd=0; ok=$false},
        @{name='body_deadline'; head=$normalHead; body='{}'; maximum=64; hd=0; bd=400; ok=$false},
        @{name='redirect'; head="HTTP/1.1 302 Found`r`nLocation: http://127.0.0.1:1/health`r`nContent-Length: 0`r`n`r`n"; body=''; maximum=64; hd=0; bd=0; ok=$false},
        @{name='content_encoding'; head="HTTP/1.1 200 OK`r`nContent-Encoding: gzip`r`nContent-Length: 2`r`n`r`n"; body='{}'; maximum=64; hd=0; bd=0; ok=$false}
    )
    foreach ($case in $cases) {
        $server = [MineruHttpProbeServer]::new($case.head,$utf8.GetBytes($case.body),1,$case.hd,$case.bd)
        $client = [MineruBoundedHttp]::new($server.Port,$prepared.Manifest.http_assembly_sha256)
        try {
            $rejected = $false; $body = $null
            try { $body = $client.Get('/metrics',$case.maximum,[MineruResidentWire]::Deadline(150)) } catch { $rejected=$true }
            Assert-True ($rejected -ne $case.ok) $case.name
            if ($case.ok) { Assert-True ($body -ceq '{}') 'chunked_decoded' }
            else {
                Assert-True ($null -eq $client.Pending -or $client.Pending.IsCompleted) 'failed_io_quiescent'
                $reused = $false
                try { $null=$client.Get('/health',64,[MineruResidentWire]::Deadline(1000)); $reused=$true } catch {}
                Assert-True (-not $reused) 'failed_instance_not_reused'
            }
            $server.Finish(-not $case.ok)
        } finally { $client.Dispose(); $server.Dispose() }
    }
    [ordered]@{contract_version='mineru.bounded-http-mechanism-test.v1'; checks=$checks.ToArray();
        prepared_manifest_sha256=$ExpectedManifestSha256; source_sha256=$ExpectedWireSourceSha256} | ConvertTo-Json -Depth 4 -Compress
} finally { foreach ($pin in $prepared.Pins) { $pin.Dispose() } }
