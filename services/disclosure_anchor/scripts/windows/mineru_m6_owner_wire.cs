// M6 v1 canonical closed wire on the already-qualified strict UTF-8 parser.
// No dynamic code, serializer type metadata, arbitrary dictionaries or commands.
using System;
using System.Collections.Generic;
using System.Text.RegularExpressions;

public static class MineruM6OwnerWire {
    public const int MaximumWireBytes = 65536;
    static string Q(string text) { return MineruResidentWire.Quote(text); }
    static int ScalarLength(string text) {
        int count=0;
        for(int i=0;i<text.Length;i++,count++) if(Char.IsHighSurrogate(text[i])) i++;
        return count;
    }
    static string Value(MineruJsonValue value,string type) {
        if(type[0]=='?') return value.Raw=="null" ? "null" : Value(value,type.Substring(1));
        if(type=="n" || type=="p") {
            long number=value.Integer();
            if(type=="p" && number==0) throw new FormatException("positive M6 counter required");
            return MineruResidentWire.Integer(number);
        }
        if(type=="bool") {
            if(value.Raw!="true" && value.Raw!="false") throw new FormatException("M6 boolean required");
            return value.Raw;
        }
        if(type=="clock") return Clock(value);
        if(type=="resources") return Resources(value);
        if(type=="event") return Producer(value);
        if(type=="command") return Command(value);
        if(type=="payload") return Payload(value);
        string text=value.String();
        if(type=="hash") {
            if(!Regex.IsMatch(text,@"\Asha256:[0-9a-f]{64}\z")) throw new FormatException("M6 hash malformed");
        } else if(type=="id" || type=="doc") {
            int length=ScalarLength(text), maximum=type=="doc" ? 64 : 128;
            if(length<1 || length>maximum || (type=="doc" && text.Trim()!=text))
                throw new FormatException("M6 identifier bound");
            foreach(char c in text) if(c<32 || c==127 || (type=="id" && c==32))
                throw new FormatException("M6 identifier control character");
        } else if(type.StartsWith("=")) {
            if(Array.IndexOf(type.Substring(1).Split('|'),text)<0) throw new FormatException("M6 closed vocabulary");
        } else throw new InvalidOperationException("unknown M6 wire type");
        return Q(text);
    }
    public static string Shape(MineruJsonValue value, params string[] rules) {
        List<string> keys=new List<string>(), pairs=new List<string>();
        foreach(string rule in rules) {
            int split=rule.IndexOf(':');
            if(split<=0) throw new InvalidOperationException("M6 wire rule malformed");
            string name=rule.Substring(0,split);
            keys.Add(name); pairs.Add(name); pairs.Add(Value(value.Get(name),rule.Substring(split+1)));
        }
        value.Keys(keys.ToArray());
        return MineruResidentWire.Object(pairs.ToArray());
    }
    public static string Clock(MineruJsonValue value) {
        string canonical=Shape(value,"host_assignment_identity_sha256:hash","boot_identity_sha256:hash",
            "qpc_frequency_hz:p","clock_domain_identity_sha256:hash");
        string domain=MineruResidentWire.Object("boot_identity_sha256",value.Get("boot_identity_sha256").Raw,
            "clock_source",Q("QueryPerformanceCounter"),"frequency_hz",value.Get("qpc_frequency_hz").Raw);
        if(value.Get("clock_domain_identity_sha256").String()!=MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(domain)))
            throw new FormatException("M6 QPC clock domain binding differs");
        return canonical;
    }
    public static string Resources(MineruJsonValue value) {
        string canonical=Shape(value,"max_events:p","max_record_bytes:p","max_log_bytes:p","max_attempts:p",
            "max_verifier_backlog_bytes:p","stop_admission_budget_ticks:p");
        long events=value.Get("max_events").Integer(), record=value.Get("max_record_bytes").Integer(),
            bytes=value.Get("max_log_bytes").Integer(), attempts=value.Get("max_attempts").Integer();
        if(events>1000000 || record>1048576 || bytes>1073741824 || attempts>100000 || bytes<record || events<attempts)
            throw new FormatException("M6 evidence envelope inconsistent");
        return canonical;
    }
    public static string Anchor(MineruJsonValue value) {
        string canonical=Shape(value,"contract_version:=m6.owner-anchor.v1","run_id:id","clock:clock",
            "owner_process_epoch_sha256:hash","owner_source_sha256:hash","gpu_device_identity_sha256:hash",
            "t0_ticks:n","planned_seconds:p","deadline_ticks:p","max_close_ticks:p","resources:resources");
        long deadline=checked(value.Get("t0_ticks").Integer()+checked(value.Get("planned_seconds").Integer()*
            value.Get("clock").Get("qpc_frequency_hz").Integer()));
        if(deadline!=value.Get("deadline_ticks").Integer() || value.Get("max_close_ticks").Integer()<deadline)
            throw new FormatException("M6 original interval differs");
        return canonical;
    }
    public static string Producer(MineruJsonValue value) {
        return Shape(value,"contract_version:=m6.producer-event.v1","run_id:id","spec_sha256:hash",
            "producer_kind:=owner|e2e_runner|service_runner|public_verifier|quality_verifier",
            "producer_epoch_sha256:hash","producer_sequence:p","payload:payload");
    }
    public static string Record(string raw) {
        MineruJsonValue value=MineruResidentWire.Parse(raw,MaximumWireBytes);
        value.Keys("contract_version","event","stamp");
        if(value.Get("contract_version").String()!="m6.run-event.v1") throw new FormatException("M6 record version");
        string producer=Producer(value.Get("event"));
        string stamp=Shape(value.Get("stamp"),"sequence:p","received_qpc_ticks:n","boot_identity_sha256:hash",
            "owner_process_epoch_sha256:hash","producer_event_sha256:hash");
        if(value.Get("stamp").Get("producer_event_sha256").String()!=MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(producer)))
            throw new FormatException("M6 stamp does not bind entire producer bytes");
        string canonical=MineruResidentWire.Object("contract_version",Q("m6.run-event.v1"),"event",producer,"stamp",stamp);
        if(raw!=canonical) throw new FormatException("M6 record is not complete canonical JSON");
        return canonical;
    }
    public static string Request(string raw) {
        MineruJsonValue value=MineruResidentWire.Parse(raw,MaximumWireBytes);
        string canonical=Shape(value,"contract_version:=m6.owner-request.v1","run_id:id","spec_sha256:hash",
            "request_id:id","command:command");
        if(canonical!=raw) throw new FormatException("M6 request is not complete canonical JSON");
        if(value.Get("command").Get("kind").String()=="append") {
            MineruJsonValue producer=value.Get("command").Get("event");
            if(producer.Get("run_id").String()!=value.Get("run_id").String() ||
                producer.Get("spec_sha256").String()!=value.Get("spec_sha256").String())
                throw new FormatException("M6 request observation binding differs");
        }
        return canonical;
    }
    public static string Command(MineruJsonValue value) {
        string kind=value.Get("kind").String();
        switch(kind) {
            case "bind": return Shape(value,"kind:=bind","anchor_sha256:hash");
            case "status": case "lease": case "open": case "stop": return Shape(value,"kind:="+kind);
            case "append": return Shape(value,"kind:=append","event:event");
            case "admission_closed": return Shape(value,"kind:=admission_closed","runner_epoch_sha256:hash",
                "last_producer_sequence:n","admitted_attempt_count:n","unresolved_claim_count:n",
                "reconciliation_receipt_sha256:hash");
            case "close": return Shape(value,"kind:=close","ownership_receipt_sha256:hash","residual_count:n",
                "children_exited:bool","reason:=deadline_drained|stop_requested|failed");
            default: throw new FormatException("unknown M6 owner action");
        }
    }
    public static string Payload(MineruJsonValue value) {
        string kind=value.Get("kind").String();
        switch(kind) {
            case "run_started": return Shape(value,"kind:=run_started","clock:clock","t0_ticks:n","deadline_ticks:p");
            case "owner_resumed": return Shape(value,"kind:=owner_resumed","clock:clock","t0_ticks:n","deadline_ticks:p",
                "previous_owner_epoch_sha256:hash");
            case "admission_opened": case "stop_admission_requested": case "stop_admission_effective":
                return Shape(value,"kind:="+kind);
            case "attempt_admitted": return Shape(value,"kind:=attempt_admitted","attempt_id:id","fence_identity:id",
                "document_id:?doc","processing_run_id:?id","source_pdf_sha256:hash","source_byte_count:p",
                "source_page_count:p","process_profile_sha256:hash");
            case "remote_accepted": return Shape(value,"kind:=remote_accepted","attempt_id:id","remote_task_identity_sha256:hash",
                "acceptance_receipt_sha256:hash");
            case "publication_committed": return Shape(value,"kind:=publication_committed","attempt_id:id","processing_run_id:id",
                "document_id:doc","source_pdf_sha256:hash","source_page_count:p","ledger_seq:p","winner_sha256:hash","durable_base_sha256:hash");
            case "public_confirmation": return Shape(value,"kind:=public_confirmation","attempt_id:id","processing_run_id:id",
                "document_id:doc","source_pdf_sha256:hash","source_page_count:p","ledger_seq:p","winner_sha256:hash","durable_base_sha256:hash",
                "public_units_sha256:hash","artifact_closure_sha256:hash","consumer_check_receipt_sha256:hash",
                "history_audit_receipt_sha256:hash","verifier_identity:id");
            case "document_qualified": return Shape(value,"kind:=document_qualified","attempt_id:id","qualification_evidence_sha256:hash");
            case "service_validated": return Shape(value,"kind:=service_validated","attempt_id:id","provider_bundle_sha256:hash","validation_receipt_sha256:hash");
            case "attempt_final": {
                string canonical=Shape(value,"kind:=attempt_final","attempt_id:id","outcome:=published|diagnostic_disposed|failed|superseded",
                    "remote_disposition:=not_submitted|consumed|absent","remote_receipt_sha256:?hash","remote_task_identity_sha256:?hash","cleanup_receipt_sha256:hash");
                bool noRemote=value.Get("remote_disposition").String()=="not_submitted";
                if(noRemote!=(value.Get("remote_receipt_sha256").Raw=="null") || noRemote!=(value.Get("remote_task_identity_sha256").Raw=="null") ||
                    (noRemote && (value.Get("outcome").String()=="published" || value.Get("outcome").String()=="diagnostic_disposed")))
                    throw new FormatException("M6 final remote closure binding differs");
                return canonical;
            }
            case "verifier_drained": return Shape(value,"kind:=verifier_drained","drain_receipt_sha256:hash");
            case "resources_closed": return Shape(value,"kind:=resources_closed","residual_count:n","children_exited:bool","ownership_receipt_sha256:hash");
            case "run_closed": return Shape(value,"kind:=run_closed","tclose_ticks:n","reason:=deadline_drained|stop_requested|failed");
            case "measurement_incident": return Shape(value,"kind:=measurement_incident","code:id","evidence_sha256:hash");
            default: throw new FormatException("unknown M6 observation kind");
        }
    }
}
