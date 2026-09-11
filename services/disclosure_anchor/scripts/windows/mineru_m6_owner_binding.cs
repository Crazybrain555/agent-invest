// Native projection of the closed Python M6RunSpec, consumed only at bootstrap.
// The Python controller still validates the complete typed contract. No business
// interpretation, defaults, arbitrary serializer metadata or alternative schema.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

public static class MineruM6OwnerBinding {
    static string Q(string value) { return MineruResidentWire.Quote(value); }
    static string Obj(params string[] values) { return MineruResidentWire.Object(values); }
    static string Field(MineruJsonValue value,string key,string type) {
        string raw=Obj(key,value.Get(key).Raw);
        return MineruResidentWire.Parse(MineruM6OwnerWire.Shape(MineruResidentWire.Parse(raw,65536),key+":"+type),65536).Get(key).Raw;
    }
    static int CompareScalars(string left,string right) {
        // Python sorts Unicode scalar values; UTF-16 ordinal ordering differs
        // for supplementary characters. Valid UTF-8 byte ordering is equivalent.
        byte[] a=MineruResidentWire.Utf8.GetBytes(left),b=MineruResidentWire.Utf8.GetBytes(right);
        for(int i=0;i<Math.Min(a.Length,b.Length);i++) if(a[i]!=b[i]) return a[i]<b[i] ? -1 : 1;
        return a.Length.CompareTo(b.Length);
    }
    public static MineruJsonValue Validate(string specRaw,string anchorRaw) {
        MineruJsonValue spec=MineruResidentWire.Parse(specRaw,65536),anchor=MineruResidentWire.Parse(anchorRaw,65536);
        if(MineruM6OwnerWire.Anchor(anchor)!=anchorRaw) throw new FormatException("M6 noncanonical bootstrap anchor");
        string[] scalarRules={"contract_version:=m6.run-spec.v1","run_id:id","campaign_id:id",
            "mode:=service_diagnostic|e2e_publication","phase:=short_batch|hour_baseline|stability_repeat|recovery_experiment",
            "start_condition:=cold|resident_warm|warm_service_disclosed","manifest_sha256:hash","scope_sha256:?hash",
            "quality_plan_sha256:hash","t0_ticks:n","planned_seconds:p","deadline_ticks:p","max_close_ticks:p"};
        List<string> keys=new List<string>(),pairs=new List<string>();
        foreach(string rule in scalarRules) {
            int split=rule.IndexOf(':');string key=rule.Substring(0,split);
            keys.Add(key);pairs.Add(key);pairs.Add(Field(spec,key,rule.Substring(split+1)));
        }
        foreach(string name in new string[]{"clock","resources","runtime","carry_in_attempt_ids"}) keys.Add(name);
        spec.Keys(keys.ToArray());
        pairs.Add("clock");pairs.Add(MineruM6OwnerWire.Clock(spec.Get("clock")));
        pairs.Add("resources");pairs.Add(MineruM6OwnerWire.Resources(spec.Get("resources")));
        MineruJsonValue runtime=spec.Get("runtime");
        string commit=runtime.Get("source_commit").String();
        if(!Regex.IsMatch(commit,@"\A[0-9a-f]{40}\z")) throw new FormatException("M6 source commit shape");
        string[] runtimeHashes={"source_manifest_sha256","runtime_bundle_identity_sha256","process_profile_sha256",
            "worker_profile_sha256","owner_source_sha256","gpu_device_identity_sha256","deployment_qualification_sha256"};
        List<string> runtimeKeys=new List<string>(),runtimePairs=new List<string>();
        runtimeKeys.Add("source_commit");runtimePairs.Add("source_commit");runtimePairs.Add(Q(commit));
        foreach(string key in runtimeHashes) {
            runtimeKeys.Add(key);runtimePairs.Add(key);runtimePairs.Add(Field(runtime,key,key=="worker_profile_sha256" ? "?hash" : "hash"));
        }
        runtime.Keys(runtimeKeys.ToArray());pairs.Add("runtime");pairs.Add(Obj(runtimePairs.ToArray()));
        MineruJsonValue carry=spec.Get("carry_in_attempt_ids");
        if(carry.Count>100000 || carry.Count>spec.Get("resources").Get("max_attempts").Integer())
            throw new FormatException("M6 carry-in bound");
        StringBuilder carryRaw=new StringBuilder("[");string previous=null;
        for(int i=0;i<carry.Count;i++) {
            string raw=Obj("id",carry.Item(i).Raw);
            string validated=MineruM6OwnerWire.Shape(MineruResidentWire.Parse(raw,65536),"id:id");
            string id=MineruResidentWire.Parse(validated,65536).Get("id").String();
            if(previous!=null && CompareScalars(previous,id)>=0) throw new FormatException("M6 carry-in must be sorted and unique");
            if(i!=0) carryRaw.Append(',');carryRaw.Append(Q(id));previous=id;
        }
        carryRaw.Append(']');pairs.Add("carry_in_attempt_ids");pairs.Add(carryRaw.ToString());
        if(Obj(pairs.ToArray())!=specRaw) throw new FormatException("M6 bootstrap spec is not canonical");
        foreach(string name in new string[]{"run_id","clock","t0_ticks","planned_seconds","deadline_ticks","max_close_ticks","resources"})
            if(spec.Get(name).Raw!=anchor.Get(name).Raw) throw new FormatException("M6 spec differs from anchor: "+name);
        foreach(string name in new string[]{"owner_source_sha256","gpu_device_identity_sha256"})
            if(runtime.Get(name).Raw!=anchor.Get(name).Raw) throw new FormatException("M6 runtime differs from anchor: "+name);
        bool e2e=spec.Get("mode").String()=="e2e_publication";
        if(e2e==(spec.Get("scope_sha256").Raw=="null") || e2e==(runtime.Get("worker_profile_sha256").Raw=="null"))
            throw new FormatException("M6 mode scope/worker authority differs");
        string phase=spec.Get("phase").String();
        if((phase=="hour_baseline" || phase=="stability_repeat") && spec.Get("planned_seconds").Integer()<3600)
            throw new FormatException("M6 formal window below one hour");
        return spec;
    }
}

// Endpoint authentication errors carry no body. They cannot consume unbounded
// sidecars. Authenticated raw bytes and non-success replies remain durable; their
// bounded store failure must propagate, never turn into a success acknowledgement.
public sealed class MineruM6OwnerDiagnostics {
    sealed class PendingDrain {
        public string FirstRequest,LastRequest,HashChain;
        public long Count;
    }
    readonly Action<string,byte[]> persist;
    readonly int ownerThread;
    readonly Dictionary<string,long> noise=new Dictionary<string,long>(StringComparer.Ordinal);
    readonly SortedDictionary<string,PendingDrain> pendingDrains=new SortedDictionary<string,PendingDrain>(StringComparer.Ordinal);
    long postSealNoise;
    bool sealedNoise,failed;
    static readonly string[] Codes={"connection_bound","authentication_rejected","authentication_header_invalid",
        "peer_connection_lost","partial_request_eof","peer_closed","request_framing_invalid","request_utf8_invalid",
        "request_shape_invalid","request_body_bound_or_crlf","reply_connection_lost","reply_zero_progress",
        "request_timeout","reply_timeout","other"};
    public MineruM6OwnerDiagnostics(Action<string,byte[]> persistAuthenticatedDiagnostic) {
        if(persistAuthenticatedDiagnostic==null) throw new ArgumentNullException("persistAuthenticatedDiagnostic");
        persist=persistAuthenticatedDiagnostic;ownerThread=Thread.CurrentThread.ManagedThreadId;
        foreach(string code in Codes) noise.Add(code,0);
    }
    void ThreadCheck() {
        if(ownerThread!=Thread.CurrentThread.ManagedThreadId) throw new InvalidOperationException("M6 diagnostic owner thread differs");
        if(failed) throw new InvalidOperationException("M6 diagnostic storage outcome uncertain; process must exit");
    }
    static long Increment(long count) { return count==Int64.MaxValue ? count : count+1; }
    static string Q(string value) { return MineruResidentWire.Quote(value); }
    static string H(string value) { return MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(value)); }
    void Persist(string code,byte[] body) {
        try {persist(code,body);}catch {failed=true;throw;}
    }
    public void Record(string code,byte[] authenticatedBody) {
        ThreadCheck();
        if(code==null || !Regex.IsMatch(code,@"\A[a-z][a-z0-9_]{0,127}\z") ||
            (authenticatedBody!=null && authenticatedBody.Length>65536)) throw new ArgumentException("M6 diagnostic input bound");
        if(authenticatedBody!=null) {Persist(code,authenticatedBody);return;}
        if(sealedNoise) {postSealNoise=Increment(postSealNoise);return;}
        string key=noise.ContainsKey(code) ? code : "other";
        noise[key]=Increment(noise[key]);
    }
    public void ObserveReply(string requestRaw,string replyRaw) {
        ThreadCheck();MineruJsonValue reply=MineruResidentWire.Parse(replyRaw,65536);
        if(reply.Get("outcome").String()=="ok") return;
        string code=reply.Get("error_code").String();
        if(code=="verifier_drain_pending" && reply.Get("outcome").String()=="rejected") {
            // Only this no-effect flow-control reply may aggregate new envelope
            // nonces. Entire command/producer bytes, run and spec remain the key.
            // Different business evidence always gets its own original record.
            MineruM6OwnerWire.Request(requestRaw);
            MineruJsonValue request=MineruResidentWire.Parse(requestRaw,65536),command=request.Get("command");
            if(command.Get("kind").String()=="append" && command.Get("event").Get("payload").Get("kind").String()=="verifier_drained") {
                if(sealedNoise) throw new InvalidOperationException("M6 pending drain after diagnostic seal");
                string key=H(MineruResidentWire.Object("run_id",request.Get("run_id").Raw,"spec_sha256",request.Get("spec_sha256").Raw,
                    "command",command.Raw)),requestSha=H(requestRaw);
                PendingDrain pending;
                if(!pendingDrains.TryGetValue(key,out pending)) {
                    if(pendingDrains.Count>=32) {failed=true;throw new IOException("M6 distinct pending-drain diagnostic bound");}
                    Persist(code,MineruResidentWire.Utf8.GetBytes(requestRaw));
                    pending=new PendingDrain {FirstRequest=requestSha,LastRequest=requestSha,HashChain=requestSha,Count=1};
                    pendingDrains.Add(key,pending);
                } else {
                    pending.Count=Increment(pending.Count);pending.LastRequest=requestSha;
                    pending.HashChain=H(pending.HashChain+"\n"+requestSha);
                }
                return;
            }
        }
        Record(code,MineruResidentWire.Utf8.GetBytes(requestRaw));
    }
    public void SealNoise() {
        ThreadCheck();if(sealedNoise) return;
        List<string> values=new List<string>();
        foreach(string code in Codes) {values.Add(code);values.Add(MineruResidentWire.Integer(noise[code]));}
        List<string> drains=new List<string>();
        foreach(KeyValuePair<string,PendingDrain> pair in pendingDrains) drains.Add(MineruResidentWire.Object(
            "command_identity_sha256",Q(pair.Key),"first_request_sha256",Q(pair.Value.FirstRequest),
            "last_request_sha256",Q(pair.Value.LastRequest),"request_hash_chain",Q(pair.Value.HashChain),
            "occurrences",MineruResidentWire.Integer(pair.Value.Count)));
        string raw=MineruResidentWire.Object("contract_version",Q("m6.transport-noise-summary.v2"),
            "counts",MineruResidentWire.Object(values.ToArray()),"pending_drains","["+String.Join(",",drains.ToArray())+"]");
        Persist("noise_summary",MineruResidentWire.Utf8.GetBytes(raw));sealedNoise=true;
    }
    public long PostSealNoise { get { ThreadCheck();return postSealNoise; } }
}
