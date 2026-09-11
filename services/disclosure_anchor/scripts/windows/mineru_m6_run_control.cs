// Fixed M6 control state, separate from sockets, bootstrap and business work.
// The host supplies an exclusive durable journal and verified private receipts.
using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;

public sealed class MineruM6ControlRefusal : InvalidOperationException {
    public readonly string Code;
    public MineruM6ControlRefusal(string code) : base(code) { Code=code; }
}

public sealed class MineruM6RunControl {
    readonly MineruM6Journal journal;
    readonly MineruJsonValue anchor;
    readonly string runId,specSha,anchorSha,ownerEpoch,runnerRole,runnerEpoch,drainRole;
    readonly Dictionary<string,string> roles;
    readonly Func<long> qpc;
    readonly Func<string,string> readReceipt;
    readonly Action<string,string> archiveControl;
    readonly Func<string,string> readArchivedControl;
    readonly Action assertOwnResourcesClosed;
    readonly long maximumLease,deadline,t0,stopBudget,maxAttempts;
    readonly Dictionary<string,string> producerOriginal=new Dictionary<string,string>(StringComparer.Ordinal);
    readonly HashSet<string> producerVariants=new HashSet<string>(StringComparer.Ordinal);
    readonly Dictionary<string,long> producerMaximum=new Dictionary<string,long>(StringComparer.Ordinal);
    readonly Dictionary<string,long> producerCount=new Dictionary<string,long>(StringComparer.Ordinal);
    readonly HashSet<string> admitted=new HashSet<string>(StringComparer.Ordinal);
    readonly HashSet<string> final=new HashSet<string>(StringComparer.Ordinal);
    long ownerSequence,lastClock,lastAdmissionSequence;
    bool opened,requested,stopped,drained,resourcesClosed,failed,closed,observationRefused;
    string ackRaw,closureRaw,resourcesReceiptSha;

    static string Q(string value) { return MineruResidentWire.Quote(value); }
    static string N(long value) { return MineruResidentWire.Integer(value); }
    static string Obj(params string[] values) { return MineruResidentWire.Object(values); }
    static string Hash(string raw) { return MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(raw)); }
    static string S(MineruJsonValue value,string key) { return value.Get(key).String(); }
    static string ProducerKey(MineruJsonValue value) {
        return S(value,"producer_kind")+"\0"+S(value,"producer_epoch_sha256")+"\0"+value.Get("producer_sequence").Raw;
    }
    static void Require(bool condition,string code) { if(!condition) throw new MineruM6ControlRefusal(code); }
    static void RequireRecord(bool condition,string code) { if(!condition) throw new IOException(code); }

    public MineruM6RunControl(string anchorRaw,string boundSpecSha,string currentOwnerEpoch,string mode,
                             Dictionary<string,string> authenticatedRoleEpochs,MineruM6Journal durableJournal,
                             Func<long> physicalQpc,long maximumLeaseTicks,
                             long propagationReserveTicks,Func<string,string> readPinnedReceipt,
                             Action<string,string> persistControlReceipt,Func<string,string> readControlReceipt,
                             Action assertNativeClosure) {
        anchor=MineruResidentWire.Parse(anchorRaw,65536);
        Require(MineruM6OwnerWire.Anchor(anchor)==anchorRaw,"anchor_not_canonical");
        Require(mode=="service_diagnostic" || mode=="e2e_publication","unknown_run_mode");
        Require(durableJournal!=null && authenticatedRoleEpochs!=null && physicalQpc!=null && readPinnedReceipt!=null &&
                persistControlReceipt!=null && readControlReceipt!=null && assertNativeClosure!=null,"owner_dependency_missing");
        roles=new Dictionary<string,string>(authenticatedRoleEpochs,StringComparer.Ordinal);
        runnerRole=mode=="service_diagnostic" ? "service_runner" : "e2e_runner";
        drainRole=mode=="service_diagnostic" ? "quality_verifier" : "public_verifier";
        Require(roles.ContainsKey("controller") && roles.ContainsKey(runnerRole) && roles.ContainsKey("quality_verifier") &&
                (mode!="e2e_publication" || roles.ContainsKey("public_verifier")),"caller_roles_missing");
        foreach(KeyValuePair<string,string> role in roles) {
            Require(role.Key=="controller" || role.Key==runnerRole || role.Key=="quality_verifier" ||
                    (mode=="e2e_publication" && role.Key=="public_verifier"),"extra_caller_role");
            MineruM6OwnerWire.Shape(MineruResidentWire.Parse(Obj("epoch",Q(role.Value)),1024),"epoch:hash");
        }
        MineruM6OwnerWire.Shape(MineruResidentWire.Parse(Obj("spec",Q(boundSpecSha),"epoch",Q(currentOwnerEpoch)),1024),"spec:hash","epoch:hash");
        runId=S(anchor,"run_id"); specSha=boundSpecSha; anchorSha=Hash(anchorRaw); ownerEpoch=currentOwnerEpoch;
        runnerEpoch=roles[runnerRole]; journal=durableJournal; qpc=physicalQpc;
        readReceipt=readPinnedReceipt; archiveControl=persistControlReceipt; assertOwnResourcesClosed=assertNativeClosure;
        readArchivedControl=readControlReceipt;
        t0=anchor.Get("t0_ticks").Integer(); deadline=anchor.Get("deadline_ticks").Integer();
        stopBudget=anchor.Get("resources").Get("stop_admission_budget_ticks").Integer();
        maxAttempts=anchor.Get("resources").Get("max_attempts").Integer();
        Require(journal.MaximumEvents==anchor.Get("resources").Get("max_events").Integer() &&
                journal.MaximumRecordBytes==anchor.Get("resources").Get("max_record_bytes").Integer() &&
                journal.MaximumLogBytes==anchor.Get("resources").Get("max_log_bytes").Integer(),"journal_bounds_differ_from_anchor");
        Require(propagationReserveTicks>0 && propagationReserveTicks<stopBudget && maximumLeaseTicks>0 &&
                maximumLeaseTicks<=stopBudget-propagationReserveTicks,"lease_has_no_stop_reserve");
        maximumLease=maximumLeaseTicks; lastClock=t0;
        long count=0;
        foreach(string raw in journal.ReadRecords()) {
            MineruM6OwnerWire.Record(raw);
            MineruJsonValue record=MineruResidentWire.Parse(raw,65536);
            Require(record.Get("stamp").Get("sequence").Integer()==++count,"control_replay_sequence_gap");
            Require(S(record.Get("event"),"run_id")==runId && S(record.Get("event"),"spec_sha256")==specSha &&
                    S(record.Get("stamp"),"boot_identity_sha256")==S(anchor.Get("clock"),"boot_identity_sha256"),"control_replay_identity_drift");
            Apply(record);
            lastClock=record.Get("stamp").Get("received_qpc_ticks").Integer();
        }
        Require(count==journal.LastSequence && (count==0 || lastClock==journal.LastTick),"control_replay_incomplete");
        Require(!closed && !journal.IsClosed,"closed_run_requires_offline_reconciliation");
        if(stopped) AdmissionClosed(ArchivedCommand("admission-closed","admission_closed"),true);
        if(resourcesClosed) {
            MineruJsonValue archived=ArchivedCommand("resources-closed","close");
            Require(S(archived,"ownership_receipt_sha256")==resourcesReceiptSha,"archived_closure_journal_differs");
            Receipt(resourcesReceiptSha,"m6.ownership-closure.v1");
            closureRaw=archived.Raw;
        }
        if(count==0) {
            Require(ownerEpoch==S(anchor,"owner_process_epoch_sha256"),"initial_owner_epoch_differs");
            Owner(Obj("kind",Q("run_started"),"clock",anchor.Get("clock").Raw,"t0_ticks",N(t0),"deadline_ticks",N(deadline)),t0);
        } else {
            Require(ownerEpoch!=journal.LastOwnerEpoch,"resume_requires_new_process_incarnation");
            Owner(Obj("kind",Q("owner_resumed"),"clock",anchor.Get("clock").Raw,"t0_ticks",N(t0),"deadline_ticks",N(deadline),
                      "previous_owner_epoch_sha256",Q(journal.LastOwnerEpoch)),Now());
            // A recovered owner never grants new admissions, even before deadline.
            Stop();
        }
    }

    long Now() {
        long tick=qpc();
        if(tick<lastClock) throw new IOException("physical M6 QPC regressed");
        lastClock=tick; return tick;
    }
    string PayloadRole(string kind) {
        switch(kind) {
            case "public_confirmation": return "public_verifier";
            case "document_qualified": return "quality_verifier";
            case "verifier_drained": return drainRole;
            case "attempt_admitted": case "remote_accepted": case "attempt_final": return runnerRole;
            case "publication_committed": return runnerRole=="e2e_runner" ? runnerRole : "forbidden";
            case "service_validated": return runnerRole=="service_runner" ? runnerRole : "forbidden";
            default: return "owner";
        }
    }
    void Apply(MineruJsonValue record) {
        MineruJsonValue producer=record.Get("event"),payload=producer.Get("payload");
        string key=ProducerKey(producer),sha=S(record.Get("stamp"),"producer_event_sha256"),prior;
        producerVariants.Add(key+"\0"+sha);
        if(producerOriginal.TryGetValue(key,out prior)) { if(prior!=sha) failed=true; return; }
        producerOriginal.Add(key,sha);
        string role=S(producer,"producer_kind"),epoch=S(producer,"producer_epoch_sha256"),producerId=role+"\0"+epoch;
        long sequence=producer.Get("producer_sequence").Integer(),maximum;
        producerMaximum.TryGetValue(producerId,out maximum);
        producerMaximum[producerId]=Math.Max(sequence,maximum);
        producerCount[producerId]=producerCount.ContainsKey(producerId) ? producerCount[producerId]+1 : 1;
        if(role=="owner" && epoch==ownerEpoch) ownerSequence=Math.Max(ownerSequence,sequence);
        string kind=S(payload,"kind");
        RequireRecord(PayloadRole(kind)==role,"journal_role_mismatch");
        if(role!="owner") RequireRecord(roles.ContainsKey(role) && roles[role]==epoch,"journal_caller_epoch_changed");
        else {
            RequireRecord(epoch==S(record.Get("stamp"),"owner_process_epoch_sha256"),"journal_owner_epoch_differs");
            if(kind=="run_started" || kind=="owner_resumed") {
                RequireRecord(payload.Get("clock").Raw==anchor.Get("clock").Raw && payload.Get("t0_ticks").Integer()==t0 &&
                        payload.Get("deadline_ticks").Integer()==deadline,"journal_original_interval_differs");
                if(kind=="run_started") RequireRecord(record.Get("stamp").Get("sequence").Integer()==1 &&
                    record.Get("stamp").Get("received_qpc_ticks").Integer()==t0,"journal_start_stamp_differs");
            }
        }
        switch(kind) {
            case "admission_opened": opened=true; break;
            case "stop_admission_requested": requested=true; break;
            case "stop_admission_effective": stopped=true; break;
            case "verifier_drained": drained=true; break;
            case "resources_closed": resourcesClosed=true; resourcesReceiptSha=S(payload,"ownership_receipt_sha256"); break;
            case "run_closed": closed=true; break;
            case "measurement_incident":
                failed=true;
                if(S(payload,"code").StartsWith("observation_refused_",StringComparison.Ordinal)) observationRefused=true;
                break;
            case "attempt_admitted":
                RequireRecord(admitted.Count<maxAttempts || admitted.Contains(S(payload,"attempt_id")),"attempt_index_bound");
                lastAdmissionSequence=Math.Max(lastAdmissionSequence,sequence);
                admitted.Add(S(payload,"attempt_id")); break;
            case "attempt_final": final.Add(S(payload,"attempt_id")); break;
        }
    }
    MineruM6AppendResult Owner(string payload,long tick) {
        string producer=Obj("contract_version",Q("m6.producer-event.v1"),"run_id",Q(runId),"spec_sha256",Q(specSha),
            "producer_kind",Q("owner"),"producer_epoch_sha256",Q(ownerEpoch),"producer_sequence",N(ownerSequence+1),"payload",payload);
        MineruM6AppendResult result=journal.Append(producer,tick,ownerEpoch);
        Apply(MineruResidentWire.Parse(result.Record,65536)); return result;
    }
    void Stop() { if(!requested && !closed) Owner(Obj("kind",Q("stop_admission_requested")),Now()); }
    public void Tick() { if(!closed && Now()>=deadline) Stop(); }
    public bool IsClosed { get { return closed; } }
    public string State { get { return closed ? "closed" : failed ? "failed" : stopped ? "draining" : requested ? "stopping" : opened ? "open" : "bound"; } }

    string ReceiptBytes(string sha) {
        string raw=readReceipt(sha);
        Require(raw!=null && MineruResidentWire.Utf8.GetByteCount(raw)<=65536,"receipt_missing_or_over_bound");
        Require(Hash(raw)==sha,"receipt_content_hash_differs");
        return raw;
    }
    MineruJsonValue Receipt(string sha,string version) {
        string raw=ReceiptBytes(sha);
        MineruJsonValue value=MineruResidentWire.Parse(raw,65536);
        Require(S(value,"contract_version")==version && S(value,"run_id")==runId && S(value,"spec_sha256")==specSha &&
                S(value,"runner_epoch_sha256")==runnerEpoch,"receipt_run_binding_differs");
        return value;
    }
    // ASCII sort of per-ID UTF-8 hashes avoids platform Unicode collation and
    // keeps each control receipt bounded even for a large declared attempt set.
    // The original identifiers remain individually recoverable from the journal.
    public static string AttemptSetSha(IEnumerable<string> attemptIds) {
        List<string> hashes=new List<string>();
        foreach(string id in attemptIds) hashes.Add(Hash(id));
        hashes.Sort(StringComparer.Ordinal);
        using(SHA256 hash=SHA256.Create()) {
            foreach(string item in hashes) {
                byte[] line=MineruResidentWire.Utf8.GetBytes(item+"\n");
                hash.TransformBlock(line,0,line.Length,null,0);
            }
            hash.TransformFinalBlock(new byte[0],0,0);
            return "sha256:"+BitConverter.ToString(hash.Hash).Replace("-","").ToLowerInvariant();
        }
    }
    MineruJsonValue ArchivedCommand(string name,string kind) {
        string raw=readArchivedControl(name);
        Require(!String.IsNullOrEmpty(raw),"archived_control_receipt_missing");
        MineruJsonValue value=MineruResidentWire.Parse(raw,65536);
        Require(MineruM6OwnerWire.Command(value)==raw && S(value,"kind")==kind,"archived_control_receipt_differs");
        return value;
    }
    void AdmissionClosed(MineruJsonValue command,bool restoring=false) {
        string sha=S(command,"reconciliation_receipt_sha256");
        if(stopped && ackRaw!=null) { Require(ackRaw==command.Raw,"admission_ack_changed"); return; }
        Require(requested,"admission_stop_not_requested");
        Require(S(command,"runner_epoch_sha256")==runnerEpoch,"admission_ack_runner_differs");
        long maximum=0; producerMaximum.TryGetValue(runnerRole+"\0"+runnerEpoch,out maximum);
        long ackSequence=command.Get("last_producer_sequence").Integer(),count=0;
        producerCount.TryGetValue(runnerRole+"\0"+runnerEpoch,out count);
        // Exact ACK recovery may occur after subsequent finalization events.
        // It still must cover every admission and a gap-free observed prefix.
        Require(ackSequence>=lastAdmissionSequence && ackSequence<=maximum && count==maximum &&
                command.Get("admitted_attempt_count").Integer()==admitted.Count,"admission_ack_index_differs");
        MineruJsonValue receipt=Receipt(sha,"m6.admission-reconciliation.v1");
        string canonical=MineruM6OwnerWire.Shape(receipt,"contract_version:=m6.admission-reconciliation.v1","run_id:id",
            "spec_sha256:hash","runner_epoch_sha256:hash","last_producer_sequence:n","admitted_attempt_count:n",
            "admitted_attempt_set_sha256:hash","unresolved_claim_count:n","unresolved_receipt_sha256:?hash");
        Require(canonical==receipt.Raw && S(receipt,"admitted_attempt_set_sha256")==AttemptSetSha(admitted) &&
                receipt.Get("admitted_attempt_count").Integer()==admitted.Count,"admission_receipt_set_differs");
        Require(receipt.Get("last_producer_sequence").Raw==command.Get("last_producer_sequence").Raw &&
                receipt.Get("unresolved_claim_count").Raw==command.Get("unresolved_claim_count").Raw,"admission_receipt_counts_differ");
        long unresolved=command.Get("unresolved_claim_count").Integer();
        if(unresolved==0) Require(receipt.Get("unresolved_receipt_sha256").Raw=="null","unexpected_unresolved_receipt");
        else {
            string unresolvedSha=S(receipt,"unresolved_receipt_sha256");
            ReceiptBytes(unresolvedSha);
        }
        if(!restoring) archiveControl("admission-closed",command.Raw); // Durable exact ACK before its event.
        if(!stopped) {
            if(unresolved>0) Owner(Obj("kind",Q("measurement_incident"),"code",Q("h0_committed_unclaimed"),"evidence_sha256",Q(sha)),Now());
            Owner(Obj("kind",Q("stop_admission_effective")),Now());
        }
        ackRaw=command.Raw;
    }
    void Close(MineruJsonValue command) {
        string sha=S(command,"ownership_receipt_sha256");
        if(closed) { Require(closureRaw==command.Raw,"closure_receipt_changed"); return; }
        if(resourcesClosed && closureRaw!=null) Require(closureRaw==command.Raw,"closure_receipt_changed");
        Require(stopped && drained && admitted.SetEquals(final),"business_drain_pending");
        foreach(KeyValuePair<string,long> producer in producerMaximum)
            Require(producerCount[producer.Key]==producer.Value,"producer_sequence_gap");
        Require(command.Get("residual_count").Integer()==0 && command.Get("children_exited").Raw=="true","resource_closure_pending");
        MineruJsonValue receipt=Receipt(sha,"m6.ownership-closure.v1");
        string canonical=MineruM6OwnerWire.Shape(receipt,"contract_version:=m6.ownership-closure.v1","run_id:id","spec_sha256:hash",
            "runner_epoch_sha256:hash","admitted_attempt_count:n","admitted_attempt_set_sha256:hash","final_attempt_count:n",
            "final_attempt_set_sha256:hash","residual_count:n","children_exited:bool","resource_audit_sha256:hash");
        Require(canonical==receipt.Raw && receipt.Get("admitted_attempt_count").Integer()==admitted.Count &&
                receipt.Get("final_attempt_count").Integer()==final.Count &&
                S(receipt,"admitted_attempt_set_sha256")==AttemptSetSha(admitted) &&
                S(receipt,"final_attempt_set_sha256")==AttemptSetSha(final),"closure_receipt_set_differs");
        Require(receipt.Get("residual_count").Raw=="0" && receipt.Get("children_exited").Raw=="true","closure_receipt_not_clean");
        string auditSha=S(receipt,"resource_audit_sha256");
        ReceiptBytes(auditSha);
        Require(S(command,"reason")!="deadline_drained" || Now()>=deadline,"deadline_not_reached");
        assertOwnResourcesClosed(); // Native self-Job and host-owned resources, never a producer boolean.
        archiveControl("resources-closed",command.Raw);
        if(!resourcesClosed) Owner(Obj("kind",Q("resources_closed"),"residual_count","0","children_exited","true",
                                      "ownership_receipt_sha256",Q(sha)),Now());
        long tick=Now();
        Owner(Obj("kind",Q("run_closed"),"tclose_ticks",N(tick),"reason",Q(failed ? "failed" : S(command,"reason"))),tick);
        closureRaw=command.Raw;
    }
    void ObserveRefusal(string code,string producerRaw) {
        // One bounded permanent incident makes rejected authenticated business
        // evidence visible to the accounting reducer. The host also retains raw
        // refused requests in its bounded sidecar. Ordinary pending drain is
        // flow control, not a contradictory business observation.
        if(!closed && !observationRefused) Owner(Obj("kind",Q("measurement_incident"),
            "code",Q("observation_refused_"+code),"evidence_sha256",Q(Hash(producerRaw))),Now());
        if(!closed) Stop();
    }
    // Authentication/run-binding refusals occur before reply construction.
    // The endpoint must close that request and retain its diagnostic.
    public string Handle(string requestRaw,string callerRole,string callerEpoch) {
        MineruM6OwnerWire.Request(requestRaw);
        Require(roles.ContainsKey(callerRole) && roles[callerRole]==callerEpoch,"unauthorized_caller_incarnation");
        MineruJsonValue request=MineruResidentWire.Parse(requestRaw,65536),command=request.Get("command");
        Require(S(request,"run_id")==runId && S(request,"spec_sha256")==specSha,"stored_run_spec_differs");
        Tick(); string kind=S(command,"kind"),recordRaw="null",outcome="ok",error=null;
        bool lease=false;
        try {
            switch(kind) {
                case "bind":
                    Require(callerRole=="controller" && S(command,"anchor_sha256")==anchorSha,"bind_identity_or_role_differs"); break;
                case "status": break;
                case "open":
                    Require(callerRole=="controller","controller_required");
                    Require(!requested && !closed && Now()<deadline,"admission_cannot_reopen");
                    if(!opened) Owner(Obj("kind",Q("admission_opened")),Now()); break;
                case "stop":
                    Require(callerRole=="controller" || callerRole==runnerRole,"runner_or_controller_required"); Stop(); break;
                case "lease": Require(callerRole==runnerRole,"selected_runner_required"); lease=opened && !requested && !closed && !failed; break;
                case "admission_closed": Require(callerRole==runnerRole,"selected_runner_required"); AdmissionClosed(command); break;
                case "close": Require(callerRole=="controller","controller_required"); Close(command); break;
                case "append": {
                    MineruJsonValue producer=command.Get("event"),payload=producer.Get("payload");
                    string payloadKind=S(payload,"kind");
                    Require(callerRole!="controller" && S(producer,"producer_kind")==callerRole &&
                            S(producer,"producer_epoch_sha256")==callerEpoch && PayloadRole(payloadKind)==callerRole,"observation_role_differs");
                    string prior; bool existingKey=producerOriginal.TryGetValue(ProducerKey(producer),out prior);
                    bool exactVariant=producerVariants.Contains(ProducerKey(producer)+"\0"+Hash(producer.Raw));
                    try {
                        Require((!closed && !resourcesClosed) || exactVariant,"owner_already_closed");
                        if(!existingKey) {
                            // stop_requested still admits claims already in
                            // flight. Only actual effective stop closes this gate.
                            if(payloadKind=="verifier_drained") Require(stopped && admitted.SetEquals(final),"verifier_drain_pending");
                            else if(payloadKind=="attempt_admitted") {
                                Require(opened && !stopped,"admission_not_open");
                                Require(!admitted.Contains(S(payload,"attempt_id")),"duplicate_attempt_admission");
                                Require(admitted.Count<maxAttempts,"attempt_index_bound");
                            } else Require(admitted.Contains(S(payload,"attempt_id")),"attempt_not_admitted");
                            Require(!drained,"verifier_already_drained");
                        }
                        // Reserve worst-case bytes as well as record slots.
                        // Exhaustion remains visible; retries need no new slot.
                        if(!exactVariant) Require(journal.HasAppendHeadroom(8),"journal_owner_headroom");
                    } catch(MineruM6ControlRefusal refusal) {
                        if(refusal.Code!="verifier_drain_pending") ObserveRefusal(refusal.Code,producer.Raw);
                        throw;
                    }
                    MineruM6AppendResult result=journal.Append(producer.Raw,Now(),ownerEpoch);
                    recordRaw=result.Record;
                    if(!result.Duplicate) Apply(MineruResidentWire.Parse(result.Record,65536));
                    if(result.Conflict) { outcome="conflict"; error="producer_event_conflict"; Stop(); }
                    break;
                }
                default: throw new MineruM6ControlRefusal("unsupported_owner_action");
            }
        } catch(MineruM6ControlRefusal refusal) { outcome="rejected"; error=refusal.Code; }
        // IO/clock/parse/unknown outcomes propagate to the host failure sink.
        long now=Now();
        if(!closed && now>=deadline) { Stop(); lease=false; now=Now(); }
        long until=lease ? Math.Min(deadline,checked(now+maximumLease)) : 0;
        string status=Obj("contract_version",Q("m6.owner-status.v1"),"run_id",Q(runId),"spec_sha256",Q(specSha),
            "anchor_sha256",Q(anchorSha),"owner_process_epoch_sha256",Q(ownerEpoch),"observed_qpc_ticks",N(now),
            "state",Q(State),"last_sequence",N(journal.LastSequence),"admission_valid_until_ticks",until>now ? N(until) : "null");
        return Obj("contract_version",Q("m6.owner-reply.v1"),"request_sha256",Q(Hash(requestRaw)),"outcome",Q(outcome),
                   "status",status,"record",recordRaw,"error_code",error==null ? "null" : Q(error));
    }
}
