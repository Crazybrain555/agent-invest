// Deterministic control fault injection. Memory streams and synthetic QPC are
// deliberately not proof of Windows durability or an M6 business run.
using System;
using System.Collections.Generic;
using System.IO;

public static class MineruM6ControlRegressions {
    static string Q(string x) { return MineruResidentWire.Quote(x); }
    static string H(string x) { return MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(x)); }
    static string S(MineruJsonValue x,string k) { return x.Get(k).String(); }
    static MineruJsonValue Parse(string x) { return MineruResidentWire.Parse(x,65536); }
    static void Check(bool good,string label) { if(!good) throw new Exception("M6 regression: "+label); }

    sealed class Fixture : IDisposable {
        public readonly MineruJsonValue Data;
        public readonly string Runner,Drain;
        public readonly Dictionary<string,string> Roles=new Dictionary<string,string>(StringComparer.Ordinal);
        public readonly Dictionary<string,string> Saved=new Dictionary<string,string>(StringComparer.Ordinal);
        public readonly Dictionary<string,string> Added=new Dictionary<string,string>(StringComparer.Ordinal);
        public readonly MemoryStream Log=new MemoryStream(),GuardBytes=new MemoryStream();
        public readonly MineruM6WriterGuard Guard;
        public readonly MineruM6Journal Journal;
        public MineruM6RunControl Control;
        public long Tick=100;
        public bool Clean=true;
        public Fixture(MineruJsonValue data) {
            Data=data;Runner=S(data,"runner");Drain=Runner=="service_runner" ? "quality_verifier" : "public_verifier";
            foreach(string role in new string[]{"controller",Runner,"quality_verifier"})
                Roles.Add(role,S(data.Get("roles"),role));
            if(Drain=="public_verifier") Roles.Add(Drain,S(data.Get("roles"),Drain));
            Guard=new MineruM6WriterGuard(GuardBytes,delegate{},S(data,"run_id"),S(data,"spec_sha256"));
            Journal=new MineruM6Journal(Log,delegate{},16384,1000,1000000,S(data,"run_id"),S(data,"spec_sha256"),S(data,"boot"),Guard);
            Control=Construct(S(data,"owner_epoch"));
        }
        public MineruM6RunControl Construct(string epoch) {
            return new MineruM6RunControl(S(Data,"anchor"),S(Data,"spec_sha256"),epoch,S(Data,"mode"),Roles,
                Journal,delegate{return Tick;},4,6,Read,
                delegate(string key,string value) {
                    string previous;
                    if(Saved.TryGetValue(key,out previous) && previous!=value) throw new IOException("immutable_sidecar_changed");
                    Saved[key]=value;
                },delegate(string key){string raw;return Saved.TryGetValue(key,out raw) ? raw : null;},
                delegate{if(!Clean) throw new MineruM6ControlRefusal("native_resources_pending");});
        }
        public string Read(string sha) { string raw;return Added.TryGetValue(sha,out raw) ? raw : S(Data.Get("receipts"),sha); }
        public string Request(string key) { return S(Data.Get("requests"),key); }
        public MineruJsonValue CallRaw(string raw,string role) { return Parse(Control.Handle(raw,role,Roles[role])); }
        public MineruJsonValue Call(string key,string role) { return CallRaw(Request(key),role); }
        public string ExtraAdmission(bool sameAttempt) {
            long seq=Parse(Request("attempt_final")).Get("command").Get("event").Get("producer_sequence").Integer()+1;
            string raw=Request("attempt_admitted").Replace("\"producer_sequence\":1","\"producer_sequence\":"+seq);
            return sameAttempt ? raw : raw.Replace("\"attempt-0\"","\"attempt-1\"");
        }
        public void BeforeDrain(bool quality) {
            Tick=101;Call("open","controller");Tick=200;Call("attempt_admitted",Runner);
            Tick=220;Call("stop",Runner);Tick=221;Call("ack",Runner);
            Tick=230;Call("remote_accepted",Runner);
            Tick=240;Call(Runner=="service_runner" ? "service_validated" : "publication_committed",Runner);
            if(Runner=="e2e_runner") {Tick=245;Call("public_confirmation","public_verifier");}
            if(quality) {Tick=250;Call("document_qualified","quality_verifier");}
            Tick=260;Call("attempt_final",Runner);Tick=270;
        }
        public string ChangedClose() {
            string oldSha=S(Parse(Request("close")).Get("command"),"ownership_receipt_sha256");
            string oldReceipt=Read(oldSha),oldAudit=S(Parse(oldReceipt),"resource_audit_sha256");
            string newAudit=MineruResidentWire.Object("fixture",Q("changed-after-native-reconciliation"));
            Added[H(newAudit)]=newAudit;
            string receipt=oldReceipt.Replace(oldAudit,H(newAudit));Added[H(receipt)]=receipt;
            return Request("close").Replace(oldSha,H(receipt));
        }
        public string RawLog() { return MineruResidentWire.Utf8.GetString(Log.ToArray()); }
        public void Save(string directory,string name) { File.WriteAllText(Path.Combine(directory,S(Data,"mode")+"-"+name+".jsonl"),RawLog(),MineruResidentWire.Utf8); }
        public void Dispose() { Journal.Dispose();Guard.Dispose(); }
    }
    public static string Run(string vectors,bool fixedBehavior,string outputDirectory) {
        if(Directory.Exists(outputDirectory)) throw new IOException("New regression evidence directory required");
        Directory.CreateDirectory(outputDirectory);
        MineruJsonValue cases=Parse(vectors).Get("cases");
        List<string> checks=new List<string>();
        for(int i=0;i<cases.Count;i++) {
            MineruJsonValue data=cases.Item(i);
            using(Fixture f=new Fixture(data)) {
                f.BeforeDrain(false);f.Call("verifier_drained",f.Drain);
                MineruJsonValue reply=f.Call("document_qualified","quality_verifier");
                Check(S(reply,"outcome")=="rejected" && S(reply,"error_code")=="verifier_already_drained","late quality rejected");
                f.Tick=300;reply=f.Call("close","controller");
                Check(f.Runner=="service_runner" ? S(reply,"error_code")=="producer_sequence_gap" : f.Journal.IsClosed,
                    "late-quality closure result remains explicit");
                bool incident=f.RawLog().Contains("\"code\":\"observation_refused_verifier_already_drained\"");
                Check(incident==fixedBehavior,"late evidence must survive in accounting");
                f.Save(outputDirectory,"late-quality");checks.Add(Q("late_quality_"+(fixedBehavior ? "incident" : "original_lost")));
            }
            using(Fixture f=new Fixture(data)) {
                f.Tick=101;f.Call("open","controller");f.Tick=200;f.Call("attempt_admitted",f.Runner);
                f.Tick=220;f.Call("stop",f.Runner);f.Tick=221;f.Call("ack",f.Runner);
                MineruJsonValue reply=f.CallRaw(f.ExtraAdmission(false),f.Runner);
                Check(S(reply,"outcome")==(fixedBehavior ? "rejected" : "ok"),"effective stop admission gate");
                if(fixedBehavior) Check(S(reply,"error_code")=="admission_not_open","effective stop error code");
                f.Save(outputDirectory,"post-stop-admission");checks.Add(Q("post_stop_"+(fixedBehavior ? "rejected" : "original_admitted")));
            }
            using(Fixture f=new Fixture(data)) {
                MineruJsonValue reply=f.Call("attempt_admitted",f.Runner);
                Check(S(reply,"outcome")==(fixedBehavior ? "rejected" : "ok"),"bound admission gate");
                if(fixedBehavior) Check(S(reply,"error_code")=="admission_not_open","bound admission error code");
                f.Save(outputDirectory,"before-open-admission");checks.Add(Q("before_open_"+(fixedBehavior ? "rejected" : "original_admitted")));
            }
            using(Fixture f=new Fixture(data)) {
                f.BeforeDrain(true);f.Call("verifier_drained",f.Drain);f.Clean=false;
                MineruJsonValue reply=f.Call("close","controller");
                Check(S(reply,"error_code")=="native_resources_pending" && !f.Journal.IsClosed,"native pending typed refusal");
                Check(f.Saved.ContainsKey("resources-closed")!=fixedBehavior,"native failure must not pin provisional closure");
                f.Clean=true;f.Tick=300;bool locked=false;
                try {reply=f.CallRaw(f.ChangedClose(),"controller");}
                catch(IOException e) {if(e.Message!="immutable_sidecar_changed") throw;locked=true;}
                Check(locked!=fixedBehavior && f.Journal.IsClosed==fixedBehavior,"revised closure after native cleanup");
                f.Save(outputDirectory,"changed-closure");checks.Add(Q("closure_"+(fixedBehavior ? "reconciled" : "original_locked")));
            }
            if(!fixedBehavior) continue;
            using(Fixture f=new Fixture(data)) {
                f.Tick=101;f.Call("open","controller");f.Tick=200;
                for(int attempt=0;attempt<100;attempt++) {
                    string raw=f.Request("attempt_admitted").Replace("\"producer_sequence\":1","\"producer_sequence\":"+(attempt+1))
                        .Replace("\"attempt-0\"","\"attempt-"+attempt+"\"");
                    Check(S(f.CallRaw(raw,f.Runner),"outcome")=="ok","within declared attempt bound");
                }
                string extra=f.Request("attempt_admitted").Replace("\"producer_sequence\":1","\"producer_sequence\":101")
                    .Replace("\"attempt-0\"","\"attempt-100\"");
                MineruJsonValue reply=f.CallRaw(extra,f.Runner);
                Check(S(reply,"error_code")=="attempt_index_bound" && f.RawLog().Contains("observation_refused_attempt_index_bound"),
                    "attempt budget refusal retained");
                checks.Add(Q("attempt_bound_incident"));
            }
            using(Fixture f=new Fixture(data)) {
                f.Tick=101;f.Call("open","controller");f.Tick=200;f.Call("attempt_admitted",f.Runner);
                int sequence=2;
                while(f.Journal.HasAppendHeadroom(8) && sequence<2000) {
                    string raw=f.Request("remote_accepted").Replace("\"producer_sequence\":2","\"producer_sequence\":"+sequence);
                    Check(S(f.CallRaw(raw,f.Runner),"outcome")=="ok","producer within journal budget");sequence++;
                }
                Check(sequence<2000,"journal bound reached finitely");
                string next=f.Request("remote_accepted").Replace("\"producer_sequence\":2","\"producer_sequence\":"+sequence);
                MineruJsonValue reply=f.CallRaw(next,f.Runner);
                Check(S(reply,"error_code")=="journal_owner_headroom" && f.RawLog().Contains("observation_refused_journal_owner_headroom"),
                    "journal budget has room for incident and stop");
                checks.Add(Q("journal_headroom_incident"));
            }
            using(Fixture f=new Fixture(data)) {
                f.Tick=101;f.Call("open","controller");f.Tick=36100;
                MineruJsonValue reply=f.Call("lease",f.Runner);
                Check(S(reply.Get("status"),"state")=="stopping" && reply.Get("status").Get("admission_valid_until_ticks").Raw=="null",
                    "deadline autonomously requests stop");
                f.Tick=36101;reply=f.Call("attempt_admitted",f.Runner);
                Check(S(reply,"outcome")=="ok" && reply.Get("record").Get("stamp").Get("received_qpc_ticks").Integer()==36101,
                    "in-flight late claim retained before effective stop");
                checks.Add(Q("deadline_stop_retains_late_claim_responsibility"));
            }
            using(Fixture f=new Fixture(data)) {
                long before=f.Journal.LastSequence;bool rejected=false;
                try {f.Construct(S(data,"owner_epoch"));}
                catch(MineruM6ControlRefusal e) {if(e.Code!="resume_requires_new_process_incarnation") throw;rejected=true;}
                Check(rejected && f.Journal.LastSequence==before,"same epoch recovery cannot stamp");
                checks.Add(Q("same_epoch_resume_rejected_before_write"));
            }
        }
        return MineruResidentWire.Object("status",Q("pass"),"fixed_behavior",fixedBehavior ? "true" : "false",
            "scope",Q("synthetic_control_fault_injection"),"checks","["+String.Join(",",checks.ToArray())+"]");
    }
}
