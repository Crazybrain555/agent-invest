// Default-off .NET Framework 4.8 / PS5.1 support. Precompile before measurement.
// Full canonical wire bytes are still independently checked by the Python owner.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

public sealed class MineruJsonValue {
    public readonly string Raw;
    readonly string text;
    readonly Dictionary<string, MineruJsonValue> members;
    readonly List<MineruJsonValue> items;
    internal MineruJsonValue(string raw, string value, Dictionary<string, MineruJsonValue> map,
                             List<MineruJsonValue> array) { Raw=raw; text=value; members=map; items=array; }
    public string String() { if(text==null) throw new FormatException("JSON string required"); return text; }
    public long Integer() {
        long result;
        if(!Regex.IsMatch(Raw,@"\A(?:0|[1-9][0-9]*)\z") ||
           !Int64.TryParse(Raw,NumberStyles.None,CultureInfo.InvariantCulture,out result))
            throw new FormatException("bounded nonnegative JSON integer required");
        return result;
    }
    public double Number() {
        double result;
        if(text!=null || members!=null || items!=null ||
           !Double.TryParse(Raw,NumberStyles.Float,CultureInfo.InvariantCulture,out result) ||
           Double.IsInfinity(result) || Double.IsNaN(result)) throw new FormatException("finite JSON number required");
        return result;
    }
    public MineruJsonValue Get(string name) {
        MineruJsonValue value;
        if(members==null || !members.TryGetValue(name,out value)) throw new FormatException("missing JSON member: "+name);
        return value;
    }
    public int Count { get { if(items==null) throw new FormatException("JSON array required"); return items.Count; } }
    public MineruJsonValue Item(int index) { if(items==null) throw new FormatException("JSON array required"); return items[index]; }
    public void Keys(params string[] expected) {
        if(members==null || members.Count!=expected.Length) throw new FormatException("JSON object shape differs");
        Dictionary<string,bool> seen=new Dictionary<string,bool>(StringComparer.Ordinal);
        foreach(string name in expected) {
            if(seen.ContainsKey(name) || !members.ContainsKey(name)) throw new FormatException("JSON object shape differs");
            seen.Add(name,true);
        }
    }
}

public static class MineruResidentWire {
    public static readonly UTF8Encoding Utf8=new UTF8Encoding(false,true);
    public static string Hash(byte[] bytes) {
        using(SHA256 hash=SHA256.Create())
            return "sha256:"+BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();
    }
    public static string Quote(string value) {
        StringBuilder result=new StringBuilder("\"");
        foreach(char c in value) {
            switch(c) {
                case '"': result.Append("\\\""); break; case '\\': result.Append("\\\\"); break;
                case '\b': result.Append("\\b"); break; case '\f': result.Append("\\f"); break;
                case '\n': result.Append("\\n"); break; case '\r': result.Append("\\r"); break;
                case '\t': result.Append("\\t"); break;
                default: if(c<32) result.Append("\\u").Append(((int)c).ToString("x4",CultureInfo.InvariantCulture));
                         else result.Append(c); break;
            }
        }
        return result.Append('"').ToString();
    }
    public static string Integer(long value) {
        if(value<0) throw new ArgumentException("negative counter");
        return value.ToString(CultureInfo.InvariantCulture);
    }
    public static string Object(params string[] pairs) {
        if(pairs.Length%2!=0 || pairs.Length>128) throw new ArgumentException("bounded key/value pairs required");
        SortedDictionary<string,string> values=new SortedDictionary<string,string>(StringComparer.Ordinal);
        for(int i=0;i<pairs.Length;i+=2) { Parse(pairs[i+1],65536); values.Add(pairs[i],pairs[i+1]); }
        StringBuilder result=new StringBuilder("{");
        foreach(KeyValuePair<string,string> pair in values) {
            if(result.Length>1) result.Append(','); result.Append(Quote(pair.Key)).Append(':').Append(pair.Value);
        }
        result.Append('}'); if(Utf8.GetByteCount(result.ToString())>65536) throw new FormatException("JSON output bound");
        return result.ToString();
    }
    public static MineruJsonValue Parse(string value,int maximumBytes) {
        if(value==null || maximumBytes<1 || maximumBytes>262144 || Utf8.GetByteCount(value)>maximumBytes)
            throw new FormatException("JSON byte bound");
        Parser parser=new Parser(value); MineruJsonValue result=parser.Value(0); parser.End(); return result;
    }
    // All subtrees are parsed, including fields the caller will not forward.
    // No duplicate keys (including escaped aliases), NaN/Infinity, trailing data,
    // lone surrogates, illegal grammar, depth>32 or >8192 values are accepted.
    sealed class Parser {
        readonly string source; int offset,nodes;
        public Parser(string value) { source=value; }
        void Space() { while(offset<source.Length && " \t\r\n".IndexOf(source[offset])>=0) offset++; }
        void Take(char c) { if(offset>=source.Length || source[offset++]!=c) throw new FormatException("JSON syntax"); }
        public void End() { Space(); if(offset!=source.Length) throw new FormatException("JSON trailing data"); }
        string String() {
            Take('"'); StringBuilder value=new StringBuilder(); bool ended=false;
            while(offset<source.Length) {
                char c=source[offset++]; if(c=='"') { ended=true; break; }
                if(c<32) throw new FormatException("JSON control character");
                if(c=='\\') {
                    if(offset>=source.Length) throw new FormatException("JSON escape");
                    c=source[offset++];
                    switch(c) {
                        case '"': case '\\': case '/': break;
                        case 'b': c='\b'; break; case 'f': c='\f'; break;
                        case 'n': c='\n'; break; case 'r': c='\r'; break; case 't': c='\t'; break;
                        case 'u':
                            if(offset+4>source.Length || !Regex.IsMatch(source.Substring(offset,4),@"\A[0-9a-fA-F]{4}\z"))
                                throw new FormatException("JSON unicode escape");
                            c=(char)Int32.Parse(source.Substring(offset,4),NumberStyles.HexNumber,CultureInfo.InvariantCulture);
                            offset+=4; break;
                        default: throw new FormatException("JSON escape");
                    }
                }
                value.Append(c);
            }
            if(!ended) throw new FormatException("unterminated JSON string");
            string result=value.ToString(); Utf8.GetByteCount(result); return result;
        }
        public MineruJsonValue Value(int depth) {
            if(depth>32 || ++nodes>8192) throw new FormatException("JSON complexity bound");
            Space(); int start=offset;
            if(offset>=source.Length) throw new FormatException("JSON value absent");
            char kind=source[offset]; string text=null;
            Dictionary<string,MineruJsonValue> members=null; List<MineruJsonValue> items=null;
            if(kind=='"') text=String();
            else if(kind=='{') {
                offset++; Space(); members=new Dictionary<string,MineruJsonValue>(StringComparer.Ordinal);
                if(offset<source.Length && source[offset]=='}') offset++;
                else while(true) {
                    Space(); string key=String(); Space(); Take(':'); MineruJsonValue item=Value(depth+1);
                    if(members.ContainsKey(key)) throw new FormatException("duplicate JSON key"); members.Add(key,item);
                    Space(); if(offset<source.Length && source[offset]=='}') { offset++; break; } Take(',');
                }
            } else if(kind=='[') {
                offset++; Space(); items=new List<MineruJsonValue>();
                if(offset<source.Length && source[offset]==']') offset++;
                else while(true) {
                    items.Add(Value(depth+1)); Space();
                    if(offset<source.Length && source[offset]==']') { offset++; break; } Take(',');
                }
            } else {
                while(offset<source.Length && ",]} \t\r\n".IndexOf(source[offset])<0) offset++;
                string token=source.Substring(start,offset-start);
                if(token!="true" && token!="false" && token!="null") {
                    double number;
                    if(!Regex.IsMatch(token,@"\A-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\z") ||
                       !Double.TryParse(token,NumberStyles.Float,CultureInfo.InvariantCulture,out number) ||
                       Double.IsNaN(number) || Double.IsInfinity(number)) throw new FormatException("invalid JSON number");
                }
            }
            return new MineruJsonValue(source.Substring(start,offset-start),text,members,items);
        }
    }
    public static long Deadline(int milliseconds) {
        if(milliseconds<1 || milliseconds>7200000) throw new ArgumentException("finite deadline required");
        return checked(Stopwatch.GetTimestamp()+(long)Math.Ceiling(milliseconds*(decimal)Stopwatch.Frequency/1000));
    }
    public static int Remaining(long deadline) {
        long remaining=deadline-Stopwatch.GetTimestamp();
        if(remaining<=0) throw new TimeoutException("resident operation deadline");
        return checked((int)Math.Min(Int32.MaxValue,Math.Ceiling(remaining*1000m/Stopwatch.Frequency)));
    }
    public static void Wait(Task task,long deadline) {
        if(!task.Wait(Remaining(deadline))) throw new TimeoutException("resident I/O deadline");
        task.GetAwaiter().GetResult(); Remaining(deadline);
    }
    public static string WindowsArgument(string value) {
        if(value==null || value.IndexOf('\0')>=0) throw new ArgumentException("invalid process argument");
        StringBuilder result=new StringBuilder("\""); int slashes=0;
        foreach(char c in value) {
            if(c=='\\') { slashes++; continue; }
            if(c=='"') { result.Append('\\',slashes*2+1).Append(c); slashes=0; continue; }
            result.Append('\\',slashes).Append(c); slashes=0;
        }
        return result.Append('\\',slashes*2).Append('"').ToString();
    }
    public static string DeflateBase64(string value) {
        byte[] bytes=Utf8.GetBytes(value);
        if(bytes.Length>196608) throw new ArgumentException("bootstrap bound");
        using(MemoryStream buffer=new MemoryStream()) {
            using(DeflateStream zip=new DeflateStream(buffer,CompressionLevel.Optimal,true)) zip.Write(bytes,0,bytes.Length);
            return Convert.ToBase64String(buffer.ToArray());
        }
    }
}

public sealed class MineruBoundedLineReader {
    readonly Stream stream; readonly byte[] buffer=new byte[65537]; int count;
    public Task Pending { get; private set; }
    public MineruBoundedLineReader(Stream input) { stream=input; }
    public string Read(long deadline) {
        while(true) {
            MineruResidentWire.Remaining(deadline);
            int line=Array.IndexOf(buffer,(byte)10,0,count);
            if(line>=0) {
                if(line>0 && buffer[line-1]==13) throw new FormatException("CRLF forbidden on Linux wire");
                string result=MineruResidentWire.Utf8.GetString(buffer,0,line);
                count-=line+1; Buffer.BlockCopy(buffer,line+1,buffer,0,count); return result;
            }
            if(count>=buffer.Length) throw new FormatException("Linux line byte bound");
            Task<int> read=stream.ReadAsync(buffer,count,Math.Min(4096,buffer.Length-count)); Pending=read;
            MineruResidentWire.Wait(read,deadline); Pending=null;
            if(read.Result==0) {
                if(count!=0) throw new FormatException("Linux partial frame at EOF"); return null;
            }
            count+=read.Result;
        }
    }
}

public sealed class MineruLinuxStdio : IDisposable {
    readonly Process process=new Process(); readonly FileStream executablePin;
    readonly int owner=Thread.CurrentThread.ManagedThreadId;
    MineruBoundedLineReader output; Task<string> error; Task pendingWrite;
    bool started,closed,failed; public readonly string ExecutableSha256;
    public bool CleanExitVerified { get; private set; }
    public int Pid { get { return process.Id; } }
    public long CreationFiletime100ns { get { return process.StartTime.ToUniversalTime().ToFileTimeUtc(); } }
    public MineruLinuxStdio(string dockerPath,string expectedDockerSha256,string containerName,string imageId,
                           string supervisorSource,string samplerSource,string configJson) {
        if(!Regex.IsMatch(containerName,@"\Am6-resident-[a-f0-9]{32}\z") ||
           !Regex.IsMatch(imageId,@"\Asha256:[a-f0-9]{64}\z") ||
           !String.Equals(Path.GetFileName(dockerPath),"docker.exe",StringComparison.OrdinalIgnoreCase) ||
           !Path.IsPathRooted(dockerPath)) throw new ArgumentException("fixed Docker identity required");
        byte[] supervisor=MineruResidentWire.Utf8.GetBytes(supervisorSource), sampler=MineruResidentWire.Utf8.GetBytes(samplerSource);
        if(supervisor.Length<1 || supervisor.Length>65536 || sampler.Length<1 || sampler.Length>65536)
            throw new ArgumentException("source bounds");
        MineruResidentWire.Parse(configJson,8192);
        string packed=MineruResidentWire.Object(
            "config_base64",MineruResidentWire.Quote(Convert.ToBase64String(MineruResidentWire.Utf8.GetBytes(configJson))),
            "sampler_sha256",MineruResidentWire.Quote(MineruResidentWire.Hash(sampler)),
            "sampler_source",MineruResidentWire.Quote(Convert.ToBase64String(sampler)),
            "supervisor_source",MineruResidentWire.Quote(Convert.ToBase64String(supervisor)));
        string code="import base64,zlib,json,sys;d=json.loads(zlib.decompress(base64.b64decode('"+
            MineruResidentWire.DeflateBase64(packed)+"'),-15));_resident_source_bytes=base64.b64decode(d['supervisor_source']);"+
            "sys.argv=['linux-resident-supervisor','--config-base64',d['config_base64'],'--sampler-source-base64',"+
            "d['sampler_source'],'--sampler-sha256',d['sampler_sha256']];exec(compile(_resident_source_bytes,'<resident-supervisor>','exec'))";
        string[] arguments={"--host","npipe:////./pipe/dockerDesktopLinuxEngine","run","--rm","--interactive","--pull=never",
            "--name",containerName,"--network=none","--read-only","--cap-drop=ALL","--security-opt=no-new-privileges",
            "--pid=host","--cgroupns=host","--entrypoint","/usr/bin/python3.12",imageId,"-u","-c",code};
        StringBuilder command=new StringBuilder();
        foreach(string arg in arguments) { if(command.Length>0) command.Append(' '); command.Append(MineruResidentWire.WindowsArgument(arg)); }
        if(command.Length+dockerPath.Length+4>32766) throw new ArgumentException("Docker argv bound");
        executablePin=new FileStream(dockerPath,FileMode.Open,FileAccess.Read,FileShare.Read);
        try {
            using(SHA256 hash=SHA256.Create()) ExecutableSha256="sha256:"+BitConverter.ToString(hash.ComputeHash(executablePin)).Replace("-","").ToLowerInvariant();
            if(ExecutableSha256!=expectedDockerSha256) throw new InvalidOperationException("Docker executable hash drift");
            process.StartInfo=new ProcessStartInfo(dockerPath,command.ToString()) {
                UseShellExecute=false,CreateNoWindow=true,RedirectStandardInput=true,RedirectStandardOutput=true,RedirectStandardError=true
            };
            if(!process.Start()) throw new InvalidOperationException("Docker start failed"); started=true;
            output=new MineruBoundedLineReader(process.StandardOutput.BaseStream);
            error=DrainError(process.StandardError.BaseStream);
        } catch(Exception primary) {
            try { Dispose(); } catch(Exception cleanup) { throw new AggregateException("Docker startup and cleanup failed",primary,cleanup); }
            throw;
        }
    }
    static async Task<string> DrainError(Stream stream) {
        byte[] buffer=new byte[4096];
        using(MemoryStream diagnostic=new MemoryStream()) {
            while(true) {
                int read=await stream.ReadAsync(buffer,0,buffer.Length).ConfigureAwait(false);
                if(read==0) return MineruResidentWire.Utf8.GetString(diagnostic.ToArray());
                if(diagnostic.Length+read>65536) throw new IOException("Docker stderr byte bound");
                diagnostic.Write(buffer,0,read);
            }
        }
    }
    void Check() {
        if(Thread.CurrentThread.ManagedThreadId!=owner) throw new InvalidOperationException("Linux stdio crossed owner thread");
        if(closed || failed || CleanExitVerified || !started) throw new ObjectDisposedException("MineruLinuxStdio");
        if(error.IsFaulted || error.IsCanceled) error.GetAwaiter().GetResult();
    }
    public string Read(long deadline) {
        Check(); try { return output.Read(deadline); } catch { failed=true; throw; }
    }
    public void Write(string json,long deadline) {
        Check();
        try {
            MineruResidentWire.Remaining(deadline); MineruResidentWire.Parse(json,1023);
            byte[] bytes=MineruResidentWire.Utf8.GetBytes(json+"\n");
            pendingWrite=process.StandardInput.BaseStream.WriteAsync(bytes,0,bytes.Length);
            MineruResidentWire.Wait(pendingWrite,deadline); pendingWrite=null;
            // BaseStream bypasses StreamWriter.AutoFlush; a small command may
            // otherwise remain in FileStream's user-space buffer indefinitely.
            pendingWrite=process.StandardInput.BaseStream.FlushAsync();
            MineruResidentWire.Wait(pendingWrite,deadline); pendingWrite=null;
        } catch { failed=true; throw; }
    }
    public void Finish(long deadline) {
        Check();
        try {
            if(Read(deadline)!=null) throw new FormatException("unexpected Linux frame after closed");
            if(!process.WaitForExit(MineruResidentWire.Remaining(deadline))) throw new TimeoutException("Docker exit deadline");
            MineruResidentWire.Remaining(deadline); MineruResidentWire.Wait(error,deadline);
            if(process.ExitCode!=0 || error.Result.Length!=0) throw new InvalidOperationException("Docker exit/error was not clean: "+error.Result);
            CleanExitVerified=true;
        } catch { failed=true; throw; }
    }
    public void Dispose() {
        if(Thread.CurrentThread.ManagedThreadId!=owner) throw new InvalidOperationException("Linux stdio crossed owner thread");
        if(closed) return; List<Exception> failures=new List<Exception>();
        if(started) {
            try {
                if(!process.WaitForExit(0)) {
                    try { process.Kill(); } catch { if(!process.WaitForExit(0)) throw; }
                    if(!process.WaitForExit(2000)) throw new TimeoutException("Docker CLI did not exit after kill");
                }
            } catch(Exception ex) { failures.Add(ex); }
            foreach(Stream stream in new Stream[] {process.StandardInput.BaseStream,process.StandardOutput.BaseStream,process.StandardError.BaseStream}) {
                try { stream.Dispose(); } catch(Exception ex) { failures.Add(ex); }
            }
            foreach(Task task in new Task[] {output==null?null:output.Pending,pendingWrite,error}) {
                if(task==null) continue;
                try { if(!task.Wait(1000)) throw new TimeoutException("Docker I/O did not quiesce"); }
                catch(AggregateException ex) { if(!task.IsCompleted) failures.Add(ex); /* canceled/faulted terminal I/O is observed */ }
                catch(Exception ex) { failures.Add(ex); }
            }
        }
        try { process.Dispose(); } catch(Exception ex) { failures.Add(ex); }
        try { if(executablePin!=null) executablePin.Dispose(); } catch(Exception ex) { failures.Add(ex); }
        if(failures.Count>0) throw new AggregateException("Linux stdio cleanup failed",failures);
        closed=true;
    }
}

// One immutable loopback endpoint and persistent connection pool per owner.
// A failed operation permanently poisons the instance. The outer atomic Job
// remains the hard fuse for native startup/disposal stalls.
public sealed class MineruBoundedHttp : IDisposable {
    readonly int owner=Thread.CurrentThread.ManagedThreadId;
    readonly string origin;
    readonly FileStream assemblyPin;
    HttpClient client;
    CancellationTokenSource cancellation;
    volatile Stream activeStream;
    bool closed,failed;
    public Task<string> Pending { get; private set; }
    public readonly string HttpAssemblySha256;
    public MineruBoundedHttp(int port,string expectedHttpAssemblySha256) {
        if(port<1 || port>65535) throw new ArgumentException("explicit loopback port required");
        origin="http://127.0.0.1:"+port.ToString(CultureInfo.InvariantCulture);
        assemblyPin=new FileStream(typeof(HttpClient).Assembly.Location,FileMode.Open,FileAccess.Read,FileShare.Read);
        try {
            using(SHA256 hash=SHA256.Create()) HttpAssemblySha256="sha256:"+
                BitConverter.ToString(hash.ComputeHash(assemblyPin)).Replace("-","").ToLowerInvariant();
            if(HttpAssemblySha256!=expectedHttpAssemblySha256) throw new InvalidOperationException("loaded HTTP assembly drift");
            HttpClientHandler handler=new HttpClientHandler {
                UseProxy=false,AllowAutoRedirect=false,UseCookies=false,UseDefaultCredentials=false,
                AutomaticDecompression=DecompressionMethods.None,MaxResponseHeadersLength=16
            };
            client=new HttpClient(handler,true); client.Timeout=Timeout.InfiniteTimeSpan;
        } catch(Exception primary) {
            try { Dispose(); } catch(Exception cleanup) { throw new AggregateException("HTTP startup and cleanup failed",primary,cleanup); }
            throw;
        }
    }
    void Check() {
        if(Thread.CurrentThread.ManagedThreadId!=owner) throw new InvalidOperationException("HTTP crossed owner thread");
        if(closed || failed) throw new ObjectDisposedException("MineruBoundedHttp");
    }
    public string Get(string path,int maximumBytes,long deadline) {
        Check();
        if(path!="/health" && path!="/agent/telemetry/http-requests/v1" && path!="/metrics")
            throw new ArgumentException("fixed telemetry path required");
        if(maximumBytes<1 || maximumBytes>196608) throw new ArgumentException("HTTP response bound");
        try {
            MineruResidentWire.Remaining(deadline);
            cancellation=new CancellationTokenSource();
            Pending=Fetch(path,maximumBytes,deadline,cancellation.Token);
            MineruResidentWire.Wait(Pending,deadline);
            string result=Pending.Result; Pending=null;
            cancellation.Dispose(); cancellation=null; return result;
        } catch(Exception primary) {
            failed=true;
            try { Dispose(); } catch(Exception cleanup) { throw new AggregateException("HTTP operation and cleanup failed",primary,cleanup); }
            throw;
        }
    }
    async Task<string> Fetch(string path,int maximumBytes,long deadline,CancellationToken token) {
        using(HttpRequestMessage request=new HttpRequestMessage(HttpMethod.Get,origin+path)) {
            request.Headers.AcceptEncoding.ParseAdd("identity");
            using(HttpResponseMessage response=await client.SendAsync(request,HttpCompletionOption.ResponseHeadersRead,token).ConfigureAwait(false)) {
                MineruResidentWire.Remaining(deadline);
                if(response.StatusCode!=HttpStatusCode.OK) throw new IOException("telemetry HTTP status: "+(int)response.StatusCode);
                if(response.Content==null || response.Content.Headers.ContentEncoding.Count!=0)
                    throw new IOException("unexpected HTTP content encoding");
                long? length=response.Content.Headers.ContentLength;
                if(length.HasValue && (length.Value<0 || length.Value>maximumBytes)) throw new IOException("HTTP content length bound");
                if(length.HasValue && response.Headers.TransferEncoding.Count!=0) throw new IOException("ambiguous HTTP framing");
                using(Stream stream=await response.Content.ReadAsStreamAsync().ConfigureAwait(false)) {
                    activeStream=stream;
                    try {
                        byte[] bytes=new byte[maximumBytes+1]; int count=0;
                        while(true) {
                            MineruResidentWire.Remaining(deadline); token.ThrowIfCancellationRequested();
                            int read=await stream.ReadAsync(bytes,count,Math.Min(4096,bytes.Length-count),token).ConfigureAwait(false);
                            MineruResidentWire.Remaining(deadline);
                            if(read==0) break;
                            count+=read; if(count>maximumBytes) throw new IOException("HTTP body byte bound");
                        }
                        if(length.HasValue && count!=length.Value) throw new IOException("HTTP body length mismatch");
                        return MineruResidentWire.Utf8.GetString(bytes,0,count);
                    } finally { activeStream=null; }
                }
            }
        }
    }
    public void Dispose() {
        if(Thread.CurrentThread.ManagedThreadId!=owner) throw new InvalidOperationException("HTTP crossed owner thread");
        if(closed) return;
        failed=true; List<Exception> failures=new List<Exception>();
        try { if(cancellation!=null) cancellation.Cancel(); } catch(Exception ex) { failures.Add(ex); }
        try { Stream stream=activeStream; if(stream!=null) stream.Dispose(); } catch(Exception ex) { failures.Add(ex); }
        try { if(client!=null) client.Dispose(); } catch(Exception ex) { failures.Add(ex); }
        if(Pending!=null) {
            try { if(!Pending.Wait(1000)) throw new TimeoutException("HTTP I/O did not quiesce"); }
            catch(AggregateException ex) { if(!Pending.IsCompleted) failures.Add(ex); /* terminal task fault observed */ }
            catch(Exception ex) { failures.Add(ex); }
        }
        try { if(cancellation!=null) cancellation.Dispose(); } catch(Exception ex) { failures.Add(ex); }
        try { assemblyPin.Dispose(); } catch(Exception ex) { failures.Add(ex); }
        if(failures.Count>0) throw new AggregateException("HTTP cleanup failed",failures);
        closed=true;
    }
}

public sealed class MineruQueueTelemetry {
    readonly long servingNamespacePid;
    readonly string model;
    readonly int owner=Thread.CurrentThread.ManagedThreadId;
    long preemptions=-1;
    static readonly string[] metricNames={"vllm:num_requests_running","vllm:num_requests_waiting",
        "vllm:kv_cache_usage_perc","vllm:num_preemptions_total"};
    public MineruQueueTelemetry(long expectedServingNamespacePid,string expectedModel) {
        // Owner resolves /proc/<pinned host PID>/status NSpid in preflight and
        // binds it to boot/PID/starttime/runtime. Never infer that it is PID 1.
        if(expectedServingNamespacePid<1 || String.IsNullOrEmpty(expectedModel) || expectedModel.Length>4096)
            throw new ArgumentException("serving PID and exact model identity required");
        servingNamespacePid=expectedServingNamespacePid; model=expectedModel;
    }
    static void Expect(MineruJsonValue value,string key,long expected) {
        if(value.Get(key).Integer()!=expected) throw new FormatException("API health drift: "+key);
    }
    static long Count(string value) {
        // Do not round through Double/Decimal: e.g. 1.000...001 and 1e-999
        // must not become an integer due to precision loss or underflow.
        if(value.Length>128) throw new FormatException("metric count length");
        Match parts=Regex.Match(value,@"\A(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?\z");
        int exponent=0;
        if(!parts.Success || (parts.Groups[3].Success &&
           (!Int32.TryParse(parts.Groups[3].Value,NumberStyles.AllowLeadingSign,CultureInfo.InvariantCulture,out exponent) ||
            exponent < -1000 || exponent > 1000))) throw new FormatException("bounded exact metric count required");
        string digits=parts.Groups[1].Value+parts.Groups[2].Value;
        int point=parts.Groups[1].Length+exponent;
        for(int i=Math.Max(0,point);i<digits.Length;i++)
            if(digits[i]!='0') throw new FormatException("fractional metric count");
        if(point<=0) return 0;
        string integer=digits.Substring(0,Math.Min(point,digits.Length)).TrimStart('0');
        if(integer.Length==0) return 0;
        int zeros=Math.Max(0,point-digits.Length);
        if(integer.Length+zeros>19) throw new FormatException("metric count overflow");
        long result;
        if(!Int64.TryParse(integer+new string('0',zeros),NumberStyles.None,CultureInfo.InvariantCulture,out result))
            throw new FormatException("metric count overflow");
        return result;
    }
    Dictionary<string,string> Metrics(string text) {
        if(MineruResidentWire.Utf8.GetByteCount(text)>196608) throw new FormatException("metrics byte bound");
        Dictionary<string,string> result=new Dictionary<string,string>(StringComparer.Ordinal);
        using(StringReader reader=new StringReader(text)) {
            string line;
            while((line=reader.ReadLine())!=null) {
                if(line.Length==0 || line[0]=='#') continue;
                int end=0; while(end<line.Length && line[end]!='{' && line[end]!=' ' && line[end]!='\t') end++;
                string name=line.Substring(0,end);
                if(Array.IndexOf(metricNames,name)<0) continue;
                // Exactly one series per required metric; extra engines/models
                // are drift, never silently summed or filtered away.
                Match sample=Regex.Match(line,@"\A[^{}\s]+\{(.*)\}[ \t]+([^ \t]+)\z");
                if(!sample.Success || result.ContainsKey(name)) throw new FormatException("missing/duplicate metric identity");
                string labels=sample.Groups[1].Value; int offset=0;
                Dictionary<string,string> identity=new Dictionary<string,string>(StringComparer.Ordinal);
                while(offset<labels.Length) {
                    Match label=Regex.Match(labels.Substring(offset),"\\A([a-zA-Z_][a-zA-Z0-9_]*)=\"((?:[^\"\\\\\r\n]|\\\\[\\\\\"n])*)\"(?:,|$)");
                    if(!label.Success || identity.ContainsKey(label.Groups[1].Value)) throw new FormatException("metric label shape");
                    identity.Add(label.Groups[1].Value,MineruResidentWire.Parse("\""+label.Groups[2].Value+"\"",8192).String());
                    offset+=label.Length;
                    if(offset==labels.Length && labels[offset-1]==',') throw new FormatException("metric trailing label comma");
                }
                if(identity.Count!=2 || !identity.ContainsKey("engine") || identity["engine"]!="0" ||
                   !identity.ContainsKey("model_name") || identity["model_name"]!=model) throw new FormatException("vLLM engine/model drift");
                result.Add(name,sample.Groups[2].Value);
            }
        }
        if(result.Count!=4) throw new FormatException("required vLLM metric absent"); return result;
    }
    public string Observe(string healthJson,string httpJson,string metricsText) {
        if(Thread.CurrentThread.ManagedThreadId!=owner) throw new InvalidOperationException("queue crossed owner thread");
        MineruJsonValue health=MineruResidentWire.Parse(healthJson,8192);
        health.Keys("status","version","protocol_version","queued_tasks","processing_tasks","completed_tasks","failed_tasks",
            "max_concurrent_requests","max_pending_tasks_requested","max_pending_tasks_effective","processing_window_size",
            "task_retention_seconds","task_cleanup_interval_seconds","task_protocol_schema","task_protocol_runtime");
        if(health.Get("status").String()!="healthy" || health.Get("version").String()!="3.4.4" ||
           health.Get("task_protocol_schema").String()!="mineru-task-protocol.v2") throw new FormatException("API identity drift");
        Expect(health,"protocol_version",2); Expect(health,"max_concurrent_requests",1);
        Expect(health,"max_pending_tasks_requested",1); Expect(health,"max_pending_tasks_effective",1);
        Expect(health,"processing_window_size",16); Expect(health,"task_retention_seconds",600);
        Expect(health,"task_cleanup_interval_seconds",30);
        MineruJsonValue runtime=health.Get("task_protocol_runtime");
        runtime.Keys("schema","enabled","task_registry_max_records","task_result_reservation_bytes","max_unacked_result_bytes");
        if(runtime.Get("schema").String()!="mineru-task-runtime.v1" || runtime.Get("enabled").Raw!="true")
            throw new FormatException("task runtime disabled/drift");
        Expect(runtime,"task_registry_max_records",128); Expect(runtime,"task_result_reservation_bytes",268435456);
        Expect(runtime,"max_unacked_result_bytes",2147483648);
        long queued=health.Get("queued_tasks").Integer(),processing=health.Get("processing_tasks").Integer();
        // Health terminal counts are registry gauges, not monotonic counters.
        health.Get("completed_tasks").Integer(); health.Get("failed_tasks").Integer();
        if(queued>1 || processing>1 || queued+processing>1) throw new FormatException("API task capacity overflow");
        MineruJsonValue http=MineruResidentWire.Parse(httpJson,1024);
        http.Keys("contract_version","process_id","active_requests","pending_requests");
        if(http.Get("contract_version").String()!="mineru.api-http-request-snapshot.v1" ||
           http.Get("process_id").Integer()!=servingNamespacePid) throw new FormatException("serving HTTP process drift");
        Dictionary<string,string> metrics=Metrics(metricsText);
        long running=Count(metrics[metricNames[0]]),waiting=Count(metrics[metricNames[1]]),currentPreemptions=Count(metrics[metricNames[3]]);
        string ratio=metrics[metricNames[2]];
        double ratioNumber=MineruResidentWire.Parse(ratio,128).Number();
        if(ratioNumber<0 || ratioNumber>1) throw new FormatException("KV ratio outside unit interval");
        if(currentPreemptions<preemptions)
            throw new FormatException("queue counter rollback within pinned epoch");
        string values=MineruResidentWire.Object(
            "api_queued_tasks",MineruResidentWire.Integer(queued),"api_processing_tasks",MineruResidentWire.Integer(processing),
            "api_nonterminal_tasks",MineruResidentWire.Integer(queued+processing),"api_max_pending_tasks","1",
            "api_http_active_requests",MineruResidentWire.Integer(http.Get("active_requests").Integer()),
            "api_http_pending_requests",MineruResidentWire.Integer(http.Get("pending_requests").Integer()),
            "vllm_requests_running",MineruResidentWire.Integer(running),"vllm_requests_waiting",MineruResidentWire.Integer(waiting),
            "vllm_kv_cache_usage_ratio",ratio,"vllm_preemptions_total",MineruResidentWire.Integer(currentPreemptions));
        preemptions=currentPreemptions;
        return MineruResidentWire.Object("reason","null","status","\"supported\"","values",values);
    }
}
