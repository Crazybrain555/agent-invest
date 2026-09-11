// Independently authored actual-owner parent/controller. No production helpers
// compute expected journal order, hashes, process exit, ACLs or receipt contents.
// JavaScriptSerializer is used only as a JSON reader; J() is an independent
// canonical serializer for this ASCII, integral, zero-PDF test protocol.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32;

public static class M6LiveParent {
    static readonly UTF8Encoding Utf8=new UTF8Encoding(false,true);
    static readonly JavaScriptSerializer Json=new JavaScriptSerializer {MaxJsonLength=4194304,RecursionLimit=64};
    static string host,root,fixture,gpu,sourceHash,sourceCommit,node,nvml;
    static readonly List<object> results=new List<object>();
    static readonly List<Exception> cleanupErrors=new List<Exception>();
    static int serial;
    static void Check(bool yes,string why){if(!yes)throw new IOException("ASSERT: "+why);}
    static Dictionary<string,object> D(object x){return (Dictionary<string,object>)x;}
    static Dictionary<string,object> O(params object[] values){Dictionary<string,object> r=new Dictionary<string,object>(StringComparer.Ordinal);for(int i=0;i<values.Length;i+=2)r.Add((string)values[i],values[i+1]);return r;}
    static string S(object x){return (string)x;}
    static long N(object x){return Convert.ToInt64(x,CultureInfo.InvariantCulture);}
    static string Quote(string x){StringBuilder b=new StringBuilder("\"");foreach(char c in x){switch(c){case '"':b.Append("\\\"");break;case '\\':b.Append("\\\\");break;case '\n':b.Append("\\n");break;case '\r':b.Append("\\r");break;case '\t':b.Append("\\t");break;default:if(c<32)b.Append("\\u").Append(((int)c).ToString("x4",CultureInfo.InvariantCulture));else b.Append(c);break;}}return b.Append('"').ToString();}
    static string J(object x){
        if(x==null)return "null";if(x is string)return Quote((string)x);if(x is bool)return (bool)x?"true":"false";
        Dictionary<string,object> d=x as Dictionary<string,object>;
        if(d!=null){List<string> keys=new List<string>(d.Keys);keys.Sort(StringComparer.Ordinal);List<string> p=new List<string>();foreach(string k in keys)p.Add(Quote(k)+":"+J(d[k]));return "{"+String.Join(",",p.ToArray())+"}";}
        if(x is IEnumerable){List<string> p=new List<string>();foreach(object v in (IEnumerable)x)p.Add(J(v));return "["+String.Join(",",p.ToArray())+"]";}
        if(x is int || x is long || x is uint || x is ulong)return Convert.ToString(x,CultureInfo.InvariantCulture);
        throw new IOException("unexpected nonintegral test JSON type "+x.GetType().Name);
    }
    static Dictionary<string,object> Parse(string raw){Dictionary<string,object> value=D(Json.DeserializeObject(raw));Check(J(value)==raw,"canonical JSON differs");return value;}
    static string Hash(byte[] bytes){using(SHA256 sha=SHA256.Create())return "sha256:"+BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();}
    static string Hash(string text){return Hash(Utf8.GetBytes(text));}
    static string FileHash(string path){using(FileStream stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite))using(SHA256 sha=SHA256.Create())return "sha256:"+BitConverter.ToString(sha.ComputeHash(stream)).Replace("-","").ToLowerInvariant();}
    static string Label(string value){return Hash("independent-live-zero-pdf|"+value);}
    static byte[] Read(string path,int maximum){using(FileStream f=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite)){byte[] bytes=new byte[maximum+1];int count=0;while(count<bytes.Length){int n=f.Read(bytes,count,bytes.Length-count);if(n==0)break;count+=n;}Check(count<=maximum,"file bound "+path);Array.Resize(ref bytes,count);return bytes;}}
    static void WriteNew(string path,string raw){byte[] bytes=Utf8.GetBytes(raw);using(FileStream f=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.None)){f.Write(bytes,0,bytes.Length);f.Flush(true);}}
    static void PrivateDirectory(string path){
        using(WindowsIdentity user=WindowsIdentity.GetCurrent()){
            DirectorySecurity acl=new DirectorySecurity();acl.SetAccessRuleProtection(true,false);acl.SetOwner(user.User);
            foreach(SecurityIdentifier sid in new SecurityIdentifier[]{user.User,new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null)})acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
            Directory.CreateDirectory(path,acl);DirectorySecurity actual=Directory.GetAccessControl(path);
            Check(user.User.Equals(actual.GetOwner(typeof(SecurityIdentifier)))&&actual.AreAccessRulesProtected,"private directory exact owner and protected DACL");
        }
    }
    static void PrivateFile(string path,string raw){
        WriteNew(path,raw);using(WindowsIdentity user=WindowsIdentity.GetCurrent()){
            FileSecurity acl=new FileSecurity();acl.SetOwner(user.User);acl.SetAccessRuleProtection(true,false);
            foreach(SecurityIdentifier sid in new SecurityIdentifier[]{user.User,new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null)})acl.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,AccessControlType.Allow));File.SetAccessControl(path,acl);
            FileSecurity actual=File.GetAccessControl(path);Check(user.User.Equals(actual.GetOwner(typeof(SecurityIdentifier))),"configuration has exact current-user owner");
        }
    }
    static int Port(){TcpListener l=new TcpListener(IPAddress.Loopback,0);l.Start();try{return ((IPEndPoint)l.LocalEndpoint).Port;}finally{l.Stop();}}
    sealed class Session : IDisposable {
        public readonly string Dir,RunDir,RunId,Config,ConfigSha,RunnerEpoch,QualityEpoch;
        public readonly int PortNumber;
        public readonly Dictionary<string,string> Tokens=new Dictionary<string,string>();
        public readonly List<object> Processes=new List<object>();
        public M6BoundedProcess Process;
        public Dictionary<string,object> Anchor,Ready;
        public string AnchorRaw,AnchorHash,Spec,SpecHash,Owner,InitialOwner;
        public int Seconds,Grace;
        TcpClient channel;
        NetworkStream stream;
        public Session(string name,int seconds,int grace){
            Dir=Path.Combine(root,name);PrivateDirectory(Dir);string runroot=Path.Combine(Dir,"runs");PrivateDirectory(runroot);
            RunId="independent-"+name+"-"+Guid.NewGuid().ToString("N");RunDir=Path.Combine(runroot,Hash(RunId).Substring(7));
            PortNumber=Port();Seconds=seconds;Grace=grace;RunnerEpoch=Label(RunId+"|runner");QualityEpoch=Label(RunId+"|quality");
            Dictionary<string,object> roles=O();foreach(string role in new string[]{"controller","service_runner","quality_verifier"}){
                byte[] token=new byte[32];using(RandomNumberGenerator rng=RandomNumberGenerator.Create())rng.GetBytes(token);
                Tokens[role]=BitConverter.ToString(token).Replace("-","").ToLowerInvariant();
                roles.Add(role,O("token",Tokens[role],"epoch_sha256",role=="service_runner"?RunnerEpoch:role=="quality_verifier"?QualityEpoch:Label(RunId+"|controller")));
            }
            long f=Stopwatch.Frequency;
            Dictionary<string,object> resources=O("max_events",256,"max_record_bytes",16384,"max_log_bytes",4194304,"max_attempts",16,"max_verifier_backlog_bytes",1048576,"stop_admission_budget_ticks",10*f);
            Dictionary<string,object> cfg=O("contract_version","m6.owner-deployment.v1","run_id",RunId,"run_root",runroot,"mode","service_diagnostic","owner_source_sha256",sourceHash,"expected_node_sha256",node,"gpu_uuid",gpu,"nvml_dll_sha256",nvml,"port",PortNumber,"resources",resources,"max_artifacts",256,"max_artifact_bytes",16777216,"maximum_lease_ticks",f,"propagation_reserve_ticks",f,"roles",roles);
            Config=Path.Combine(Dir,"deployment-private.json");PrivateFile(Config,J(cfg));ConfigSha=FileHash(Config);
        }
        public void Start(bool resume,bool requireReady){
            Check(Process==null,"previous exact process must be reaped before resume");
            long hard=resume?N(Anchor["max_close_ticks"]):0;
            string prefix=Path.Combine(Dir,(resume?"resumed":"initial")+"-owner");
            Process=new M6BoundedProcess(host,new string[]{Config,ConfigSha,FileHash(host),Seconds.ToString(CultureInfo.InvariantCulture),Grace.ToString(CultureInfo.InvariantCulture),"536870912",hard.ToString(CultureInfo.InvariantCulture),resume?AnchorHash:"none"},Dir,prefix);
            WriteNew(prefix+".launch.json",J(O("pid",Process.Pid,"creation_filetime_100ns",Process.CreationFiletime,"host_sha256",FileHash(host),"config_sha256",ConfigSha,"resume",resume,"original_hard_deadline_ticks",hard)));
            if(!requireReady)return;
            Stopwatch wait=Stopwatch.StartNew();string line=null;
            while(wait.ElapsedMilliseconds<25000){string raw=Process.ReadStdout();int newline=raw.IndexOf('\n');if(newline>=0){line=raw.Substring(0,newline).TrimEnd('\r');break;}if(Process.ExactProcessExited)throw new IOException("owner exited before READY: "+Process.ReadStderr());Thread.Sleep(20);}
            Check(line!=null,"bounded READY timeout");Ready=Parse(line);WriteNew(Path.Combine(Dir,(resume?"resume":"initial")+"-ready.json"),line);
            Check(S(Ready["status"])==(resume?"ready_recovered":"ready_unbound"),"READY state");
            string rawAnchor=J(Ready["anchor"]);Dictionary<string,object> anchor=D(Ready["anchor"]);
            Check(Hash(rawAnchor)==S(Ready["anchor_sha256"]),"READY hash binds exact anchor");
            Owner=S(Ready["owner_epoch_sha256"]);
            if(!resume){Anchor=anchor;AnchorRaw=rawAnchor;AnchorHash=Hash(rawAnchor);InitialOwner=Owner;Check(S(anchor["owner_process_epoch_sha256"])==Owner,"initial owner epoch");Check(N(anchor["t0_ticks"])+Seconds*Stopwatch.Frequency==N(anchor["deadline_ticks"]),"original deadline duration");Check(N(anchor["deadline_ticks"])+Grace*Stopwatch.Frequency==N(anchor["max_close_ticks"]),"original hard lifetime");Check(N(Ready["journal_prefix_bytes"])==0&&S(Ready["journal_prefix_sha256"])==Hash(""),"unbound READY has empty journal");}
            else{Check(rawAnchor==AnchorRaw,"resume keeps exact original anchor");Check(Owner!=InitialOwner,"resume has explicit new incarnation");Check(S(Ready["spec_sha256"])==SpecHash,"resume keeps exact spec hash");byte[] journal=Read(Path.Combine(RunDir,"events.jsonl"),4194304);int prefixLength=checked((int)N(Ready["journal_prefix_bytes"]));Check(journal.Length>=prefixLength,"advertised prefix exists");byte[] prefixBytes=new byte[prefixLength];Array.Copy(journal,prefixBytes,prefixLength);Check(Hash(prefixBytes)==S(Ready["journal_prefix_sha256"]),"independent original journal prefix hash");Check(prefixLength>0&&prefixBytes[prefixLength-1]==10,"READY prefix ends at durable record boundary");ValidateJournal(Utf8.GetString(prefixBytes));}
            Check(Stopwatch.GetTimestamp()<N(Anchor["max_close_ticks"]),"READY before original hard deadline");
            Dictionary<string,object> physical=null;
            foreach(string path in Directory.GetFiles(RunDir,"diagnostic-*.bin")){string raw=Utf8.GetString(Read(path,65536));if(raw.IndexOf("m6.physical-owner-identity.v2",StringComparison.Ordinal)>=0){Dictionary<string,object> candidate=Parse(raw);if(N(candidate["pid"])==Process.Pid)physical=candidate;}}
            Check(physical!=null,"host persisted independently reopened physical identity");Check(N(physical["creation_filetime_100ns"])==Process.CreationFiletime,"physical identity binds held PID and creation handle");Check(J(physical["clock"])==J(Anchor["clock"]),"physical identity keeps clock");
            string expectedOwner=Hash(J(O("run_id",RunId,"owner_source_sha256",sourceHash,"boot_identity_sha256",D(Anchor["clock"])["boot_identity_sha256"],"pid",Process.Pid,"creation_filetime_100ns",Process.CreationFiletime)));
            Check(expectedOwner==Owner,"owner epoch independently binds held PID/birth, source, run and physical boot");
        }
        public void FreezeSpec(){
            Dictionary<string,object> vectors=D(Json.DeserializeObject(File.ReadAllText(fixture,Utf8)));
            Dictionary<string,object> spec=D(Json.DeserializeObject(S(D(vectors["service"])["spec"])));
            foreach(string key in new string[]{"run_id","clock","resources","t0_ticks","planned_seconds","deadline_ticks","max_close_ticks"})spec[key]=Anchor[key];
            spec["carry_in_attempt_ids"]=new object[0];spec["campaign_id"]="independent-zero-pdf";
            Dictionary<string,object> runtime=D(spec["runtime"]);runtime["source_commit"]=sourceCommit;runtime["source_manifest_sha256"]=sourceHash;runtime["owner_source_sha256"]=Anchor["owner_source_sha256"];runtime["gpu_device_identity_sha256"]=Anchor["gpu_device_identity_sha256"];
            // Remaining fixture identities are explicitly synthetic contract
            // placeholders, never deployed worker/PDF qualification evidence.
            Spec=J(spec);SpecHash=Hash(Spec);WriteNew(Path.Combine(RunDir,"spec.json"),Spec);
            Check(FileHash(Path.Combine(RunDir,"anchor.json"))==AnchorHash,"spec publication did not move T0/anchor");
        }
        void Connect(){if(channel!=null)return;channel=new TcpClient(AddressFamily.InterNetwork);IAsyncResult pending=channel.BeginConnect(IPAddress.Loopback,PortNumber,null,null);using(pending.AsyncWaitHandle){Check(pending.AsyncWaitHandle.WaitOne(3000),"bounded loopback connect");channel.EndConnect(pending);}channel.NoDelay=true;stream=channel.GetStream();stream.ReadTimeout=4000;stream.WriteTimeout=4000;}
        public void Disconnect(){if(stream!=null){stream.Dispose();stream=null;}if(channel!=null){channel.Close();channel=null;}}
        public string Request(Dictionary<string,object> command){return J(O("contract_version","m6.owner-request.v1","run_id",RunId,"spec_sha256",SpecHash??Label("not-yet-bound"),"request_id","request-"+(++serial).ToString(CultureInfo.InvariantCulture),"command",command));}
        public Dictionary<string,object> Send(Dictionary<string,object> command,string role,string expected){
            string raw=Request(command);return Exchange(raw,role,expected);
        }
        public Dictionary<string,object> Exchange(string raw,string role,string expected){
            Connect();string transcript=Path.Combine(Dir,"exchange-"+(++serial).ToString("D4",CultureInfo.InvariantCulture));WriteNew(transcript+".request.json",raw);
            byte[] request=Utf8.GetBytes("M6-AUTH/1 "+Tokens[role]+"\n"+raw+"\n");stream.Write(request,0,request.Length);
            using(MemoryStream reply=new MemoryStream()){
                string transportOutcome="reading";bool newline=false;Exception exchangeFailure=null;
                try {
                    while(reply.Length<=65536){
                        int octet=stream.ReadByte();
                        if(octet<0){transportOutcome="eof";Check(expected=="EOF","unexpected response EOF");Check(reply.Length==0,"prebind EOF must contain zero reply bytes");Disconnect();return null;}
                        if(octet==10){newline=true;break;}reply.WriteByte((byte)octet);
                    }
                    transportOutcome=newline?"complete_line":"overbound";
                    Check(newline&&reply.Length<=65536,"bounded complete reply body");
                    string body=Utf8.GetString(reply.ToArray());WriteNew(transcript+".reply.json",body);Dictionary<string,object> value=Parse(body);
                    Check(S(value["request_sha256"])==Hash(raw),"reply exact request hash");Check(S(value["outcome"])==expected,"reply outcome expected "+expected+", got "+body);
                    Dictionary<string,object> status=D(value["status"]);Check(S(status["anchor_sha256"])==AnchorHash&&S(status["spec_sha256"])==SpecHash&&S(status["run_id"])==RunId,"stored run/spec/anchor identity");Check(S(status["owner_process_epoch_sha256"])==Owner,"explicit current owner identity");
                    Dictionary<string,object> submittedCommand=D(Parse(raw)["command"]);
                    if(S(submittedCommand["kind"])=="append" && S(value["outcome"])=="ok")
                        Check(value["record"]!=null,"successful append must return its durable accepted record");
                    if(value["record"]!=null){
                        if(S(submittedCommand["kind"])=="append") {
                            string submittedEvent=J(submittedCommand["event"]);
                            Dictionary<string,object> record=D(value["record"]);
                            Check(J(record["event"])==submittedEvent,"returned accepted event equals exact submitted producer bytes");
                            Check(S(D(record["stamp"])["producer_event_sha256"])==Hash(submittedEvent),"stamp binds submitted producer bytes, not merely its own returned event");
                        }
                        string accepted=J(value["record"]);string durable=Utf8.GetString(Read(Path.Combine(RunDir,"events.jsonl"),4194304));Check(Array.IndexOf(durable.Split('\n'),accepted)>=0,"accepted reply already durable in independently read journal");ValidateJournal(durable);
                    }
                    transportOutcome="validated_"+expected;return value;
                } catch(Exception error) {exchangeFailure=error;transportOutcome+="_failure:"+error.GetType().FullName;throw;}
                finally {
                    // Raw response bytes survive EOF, read timeout/error, overbound,
                    // UTF-8 decode failure and semantic validation failure. No auth
                    // header is ever included. A partial EOF cannot qualify prebind.
                    try {
                        byte[] bytes=reply.ToArray();if(newline){Array.Resize(ref bytes,bytes.Length+1);bytes[bytes.Length-1]=10;}
                        using(FileStream output=new FileStream(transcript+".reply-wire.bin",FileMode.CreateNew,FileAccess.Write,FileShare.None)){output.Write(bytes,0,bytes.Length);output.Flush(true);}
                        WriteNew(transcript+".transport.json",J(O("outcome",transportOutcome,"bytes",bytes.Length,"sha256",Hash(bytes))));
                    } catch(Exception evidenceFailure) {
                        if(exchangeFailure!=null)throw new AggregateException("response failure and raw evidence persistence failure",exchangeFailure,evidenceFailure);
                        throw;
                    }
                }
            }
        }

        public Dictionary<string,object> DrainEvent(){return O("contract_version","m6.producer-event.v1","run_id",RunId,"spec_sha256",SpecHash,"producer_kind","quality_verifier","producer_epoch_sha256",QualityEpoch,"producer_sequence",1,"payload",O("kind","verifier_drained","drain_receipt_sha256",Label(RunId+"|empty-verifier-drain")));}
        public string Receipt(Dictionary<string,object> value){string raw=J(value),sha=Hash(raw);WriteNew(Path.Combine(RunDir,"receipt-"+sha.Substring(7)+".json"),raw);return sha;}
        public void BindOpen(){Send(O("kind","bind","anchor_sha256",AnchorHash),"controller","ok");Send(O("kind","open"),"controller","ok");}
        public void StopAck(){Send(O("kind","stop"),"service_runner","ok");string receipt=Receipt(O("contract_version","m6.admission-reconciliation.v1","run_id",RunId,"spec_sha256",SpecHash,"runner_epoch_sha256",RunnerEpoch,"last_producer_sequence",0,"admitted_attempt_count",0,"admitted_attempt_set_sha256",Hash(""),"unresolved_claim_count",0,"unresolved_receipt_sha256",null));Send(O("kind","admission_closed","runner_epoch_sha256",RunnerEpoch,"last_producer_sequence",0,"admitted_attempt_count",0,"unresolved_claim_count",0,"reconciliation_receipt_sha256",receipt),"service_runner","ok");}
        public void Close(){
            string audit=Receipt(O("test_only",true,"pdf_count",0,"business_processes_started",0));
            string receipt=Receipt(O("contract_version","m6.ownership-closure.v1","run_id",RunId,"spec_sha256",SpecHash,"runner_epoch_sha256",RunnerEpoch,"admitted_attempt_count",0,"admitted_attempt_set_sha256",Hash(""),"final_attempt_count",0,"final_attempt_set_sha256",Hash(""),"residual_count",0,"children_exited",true,"resource_audit_sha256",audit));
            Dictionary<string,object> reply=Send(O("kind","close","ownership_receipt_sha256",receipt,"residual_count",0,"children_exited",true,"reason","stop_requested"),"controller","ok");Check(S(D(reply["status"])["state"])=="closed","close acknowledgement reports closed");Disconnect();Finish(0,10000,false);
            Dictionary<string,object> intent=Parse(Utf8.GetString(Read(Path.Combine(RunDir,"exit-observation.json"),65536)));Check(!(bool)intent["external_process_exit_verified"],"exit intent does not self-attest external closure");
            foreach(string file in Directory.GetFiles(RunDir)){using(FileStream exclusive=new FileStream(file,FileMode.Open,FileAccess.Read,FileShare.None)){Check(exclusive.Length>=0,"all host-owned file pins released");}if(Path.GetFileName(file).StartsWith("receipt-",StringComparison.Ordinal))Check(Path.GetFileNameWithoutExtension(file).Substring(8)==FileHash(file).Substring(7),"closure/reconciliation receipt exact hash");}
        }
        public void Crash(){Disconnect();Process.CrashExactOwner();Finish(137,10000,true);}
        public void Finish(int expected,int wait,bool injected){
            Process.Finish(wait);Dictionary<string,object> evidence=O("pid",Process.Pid,"creation_filetime_100ns",Process.CreationFiletime,"exact_handle_signaled",Process.ExactProcessExited,"exit_code",Process.ExitCode,"active_job_processes",Process.ActiveJobProcesses,"total_job_processes",Process.TotalJobProcesses,"forced_termination",Process.ForcedTermination,"injected_crash",injected,"timed_out",Process.TimedOut,"cleanup_failure",Process.CleanupFailure,"stdout_path",Process.StdoutPath,"stderr_path",Process.StderrPath);
            Processes.Add(evidence);WriteNew(Path.Combine(Dir,"process-proof-"+Processes.Count.ToString(CultureInfo.InvariantCulture)+".json"),J(evidence));
            Check(Process.ExitCode==expected&&!Process.TimedOut,"exact process exit code and finite bound");Check(Process.ExactProcessExited&&Process.ActiveJobProcesses==0,"held owner handle and every Job descendant actually exited");Check(injected==Process.ForcedTermination,"unexpected forced cleanup cannot qualify natural closure");Process.Dispose();Process=null;
        }
        public List<string> ValidateJournal(string raw){
            Check(raw.EndsWith("\n",StringComparison.Ordinal),"complete journal lines");string[] lines=raw.Split('\n');long seq=0,tick=N(Anchor["t0_ticks"]);string previousOwner=InitialOwner;List<string> kinds=new List<string>();
            foreach(string line in lines){if(line.Length==0)continue;Dictionary<string,object> record=Parse(line),ev=D(record["event"]),stamp=D(record["stamp"]),payload=D(ev["payload"]);string kind=S(payload["kind"]);kinds.Add(kind);Check(N(stamp["sequence"])==++seq,"contiguous physical sequence");Check(N(stamp["received_qpc_ticks"])>=tick,"QPC monotonic journal");tick=N(stamp["received_qpc_ticks"]);Check(S(stamp["producer_event_sha256"])==Hash(J(ev)),"producer bytes hash");Check(S(ev["run_id"])==RunId&&S(ev["spec_sha256"])==SpecHash,"journal immutable run/spec");Check(S(stamp["boot_identity_sha256"])==S(D(Anchor["clock"])["boot_identity_sha256"]),"same physical boot");if(kind=="owner_resumed"){Check(S(payload["previous_owner_epoch_sha256"])==previousOwner,"bounded predecessor owner chain");previousOwner=S(stamp["owner_process_epoch_sha256"]);Check(previousOwner!=InitialOwner,"resume changes owner");}Check(S(stamp["owner_process_epoch_sha256"])==previousOwner,"owner cannot drift outside resumed record");if(kind=="run_started"||kind=="owner_resumed")Check(J(payload["clock"])==J(Anchor["clock"])&&N(payload["t0_ticks"])==N(Anchor["t0_ticks"])&&N(payload["deadline_ticks"])==N(Anchor["deadline_ticks"]),"journal keeps original clock/T0/deadline");if(seq==1)Check(kind=="run_started"&&tick==N(Anchor["t0_ticks"]),"first stamp is original T0");Check(kind!="attempt_admitted","zero PDF has no admission");}
            return kinds;
        }
        public void AssertPendingSummary(List<string> requests){
            Dictionary<string,object> summary=null;int retained=0;
            foreach(string metadataPath in Directory.GetFiles(RunDir,"diagnostic-*.json")){
                Dictionary<string,object> metadata=Parse(Utf8.GetString(Read(metadataPath,65536)));
                string code=S(metadata["code"]);string bodyPath=Path.ChangeExtension(metadataPath,"bin");
                if(code=="verifier_drain_pending") {retained++;Check(Utf8.GetString(Read(bodyPath,65536))==requests[0],"only complete first pending request retained");}
                if(code=="noise_summary")summary=Parse(Utf8.GetString(Read(bodyPath,65536)));
            }
            Check(retained==1&&summary!=null,"actual host wires pending aggregation and sealed diagnostic sink");
            object[] groups=(object[])summary["pending_drains"];Check(groups.Length==1,"one pending business command group");Dictionary<string,object> group=D(groups[0]);
            string first=Hash(requests[0]),last=Hash(requests[requests.Count-1]),chain=first;
            for(int i=1;i<requests.Count;i++)chain=Hash(chain+"\n"+Hash(requests[i]));
            Dictionary<string,object> firstRequest=Parse(requests[0]);string identity=Hash(J(O("command",firstRequest["command"],"run_id",RunId,"spec_sha256",SpecHash)));
            Check(S(group["command_identity_sha256"])==identity&&N(group["occurrences"])==requests.Count,"live pending identity/count");
            Check(S(group["first_request_sha256"])==first&&S(group["last_request_sha256"])==last&&S(group["request_hash_chain"])==chain,"live pending preserves every nonce hash in arrival order");
        }
        public void FinalJournal(string expected){string raw=Utf8.GetString(Read(Path.Combine(RunDir,"events.jsonl"),4194304));List<string> kinds=ValidateJournal(raw);Check(String.Join(",",kinds.ToArray())==expected,"independent expected event ordering: "+String.Join(",",kinds.ToArray()));Check(FileHash(Path.Combine(RunDir,"anchor.json"))==AnchorHash,"original anchor unchanged at end");}
        public void Dispose(){
            try{Disconnect();}catch(Exception error){cleanupErrors.Add(error);}
            if(Process!=null){
                try{Process.Dispose();}catch(Exception error){cleanupErrors.Add(error);}
                finally{Process=null;}
            }
        }
    }
    static void Scenario(string name,Action action){
        int startErrors=cleanupErrors.Count;Exception failure=null;
        try{action();}catch(Exception error){failure=error;}
        List<Exception> all=new List<Exception>();if(failure!=null)all.Add(failure);
        for(int i=startErrors;i<cleanupErrors.Count;i++)all.Add(cleanupErrors[i]);
        if(all.Count==0){results.Add(O("scenario",name,"status","pass"));Console.WriteLine("PASS "+name);}
        else{Exception error=all.Count==1?all[0]:new AggregateException("scenario and cleanup failures",all);results.Add(O("scenario",name,"status","fail","error",error.ToString()));WriteNew(Path.Combine(root,name+"-raw-failure.txt"),error.ToString());Console.WriteLine("FAIL "+name+": "+error.Message);}
    }

    public static int Main(string[] args){
        if(args.Length!=7){Console.Error.WriteLine("host.exe fixture output-root GPU-UUID production-source-manifest-sha256 source-commit expected-nvml-sha256");return 2;}
        host=Path.GetFullPath(args[0]);fixture=Path.GetFullPath(args[1]);root=Path.GetFullPath(args[2]);gpu=args[3];sourceHash=args[4];sourceCommit=args[5];nvml=args[6];
        try {
            Check(Directory.Exists(root),"runner created fresh output root");Check(!root.StartsWith(@"C:\ProgramData",StringComparison.OrdinalIgnoreCase),"disposable root");
            using(RegistryKey machine=RegistryKey.OpenBaseKey(RegistryHive.LocalMachine,RegistryView.Registry64))using(RegistryKey key=machine.OpenSubKey(@"SOFTWARE\Microsoft\Cryptography",false)){string guid=key.GetValue("MachineGuid") as string;Guid parsed;Check(guid!=null&&Guid.TryParse(guid.Trim(),out parsed),"typed actual Windows node identity");node=Hash(guid.Trim().ToLowerInvariant());}
            Check(FileHash(Path.Combine(Environment.SystemDirectory,"nvml.dll"))==nvml,"actual System32 NVML matches supplied qualification hash");
            const string normal="run_started,admission_opened,stop_admission_requested,stop_admission_effective,verifier_drained,resources_closed,run_closed";
            Scenario("normal",delegate{using(Session s=new Session("normal",45,45)){s.Start(false,true);s.FreezeSpec();s.BindOpen();s.StopAck();s.Send(O("kind","append","event",s.DrainEvent()),"quality_verifier","ok");s.Close();s.FinalJournal(normal);}});
            Scenario("sameboot",delegate{using(Session s=new Session("sameboot",45,45)){s.Start(false,true);s.FreezeSpec();s.BindOpen();s.StopAck();Dictionary<string,object> producer=s.DrainEvent();Dictionary<string,object> accepted=s.Send(O("kind","append","event",producer),"quality_verifier","ok");string oldRecord=J(accepted["record"]);long originalDeadline=N(s.Anchor["deadline_ticks"]);s.Crash();s.Start(true,true);Check(N(s.Anchor["deadline_ticks"])==originalDeadline,"crash did not reset deadline");s.Send(O("kind","open"),"controller","rejected");Dictionary<string,object> lease=s.Send(O("kind","lease"),"service_runner","ok");Check(D(lease["status"])["admission_valid_until_ticks"]==null,"recovery admission permanently stopped");Dictionary<string,object> retry=s.Send(O("kind","append","event",producer),"quality_verifier","ok");Check(J(retry["record"])==oldRecord,"known producer retry retains exact predecessor stamp");s.Close();s.FinalJournal("run_started,admission_opened,stop_admission_requested,stop_admission_effective,verifier_drained,owner_resumed,resources_closed,run_closed");}});
            Scenario("badtail",delegate{using(Session s=new Session("badtail",45,45)){s.Start(false,true);s.FreezeSpec();s.BindOpen();s.Crash();string path=Path.Combine(s.RunDir,"events.jsonl");using(FileStream f=new FileStream(path,FileMode.Append,FileAccess.Write,FileShare.None)){byte[] tail=Utf8.GetBytes("{\"event\":");f.Write(tail,0,tail.Length);f.Flush(true);}string journal=FileHash(path),guard=FileHash(Path.Combine(s.RunDir,"writer-guard.json"));s.Start(true,false);s.Finish(1,25000,false);Check(FileHash(path)==journal,"damaged tail preserved exactly");Check(FileHash(Path.Combine(s.RunDir,"writer-guard.json"))==guard,"guard preserved without repair");Check(!File.Exists(Path.Combine(s.RunDir,"exit-observation.json")),"bad tail has no clean exit intent");Check(new FileInfo(Path.Combine(s.Dir,"resumed-owner.stdout.txt")).Length==0,"bad tail emits no READY");}});
            Scenario("prebind",delegate{using(Session s=new Session("prebind",45,45)){s.Start(false,true);string refused=s.Request(O("kind","status"));s.Exchange(refused,"controller","EOF");Check(!s.Process.ExactProcessExited,"prebind refusal keeps finite host alive");Check(!File.Exists(Path.Combine(s.RunDir,"events.jsonl")),"prebind status creates no journal success");bool retained=false;foreach(string path in Directory.GetFiles(s.RunDir,"diagnostic-*.bin")){if(Utf8.GetString(Read(path,65536))==refused)retained=true;}Check(retained,"authenticated prebind raw request retained without auth header");s.FreezeSpec();s.BindOpen();List<string> pendingRequests=new List<string>();for(int i=0;i<3;i++){string pendingRaw=s.Request(O("kind","append","event",s.DrainEvent()));pendingRequests.Add(pendingRaw);Dictionary<string,object> pending=s.Exchange(pendingRaw,"quality_verifier","rejected");Check(S(pending["error_code"])=="verifier_drain_pending","pending drain is nonincident flow control");}s.StopAck();s.Send(O("kind","append","event",s.DrainEvent()),"quality_verifier","ok");s.Close();s.FinalJournal(normal);s.AssertPendingSummary(pendingRequests);}});
            Scenario("unbound_expiry",delegate{using(Session s=new Session("unbound_expiry",3,15)){s.Start(false,true);s.Finish(124,30000,false);Check(!File.Exists(Path.Combine(s.RunDir,"spec.json"))&&!File.Exists(Path.Combine(s.RunDir,"exit-observation.json")),"finite unbound watchdog cannot manufacture clean closure");}});
            int failed=0;foreach(object r in results)if(S(D(r)["status"])!="pass")failed++;
            WriteNew(Path.Combine(root,"live-evidence.json"),J(O("contract_version","m6.independent-native-live.v1","host_sha256",FileHash(host),"test_parent_sha256",FileHash(typeof(M6LiveParent).Assembly.Location),"fixture_sha256",FileHash(fixture),"source_manifest_sha256",sourceHash,"nvml_dll_sha256",nvml,"node_sha256",node,"pdf_count",0,"business_actions",0,"failed",failed,"scenarios",results.ToArray())));return failed==0?0:1;
        }catch(Exception error){Console.Error.WriteLine(error);WriteNew(Path.Combine(root,"live-fatal-error.txt"),error.ToString());return 1;}
    }
}
