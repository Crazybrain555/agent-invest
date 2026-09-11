using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;

public static class MineruM6OwnerBindingChecks {
    static string Q(string text) { return MineruResidentWire.Quote(text); }
    static string O(params string[] pairs) { return MineruResidentWire.Object(pairs); }
    static string PendingRequest(int number,string receipt) {
        string hash="sha256:"+new string('a',64);
        string producer=O("contract_version",Q("m6.producer-event.v1"),"run_id",Q("flow"),"spec_sha256",Q(hash),
            "producer_kind",Q("quality_verifier"),"producer_epoch_sha256",Q(hash),"producer_sequence","1",
            "payload",O("kind",Q("verifier_drained"),"drain_receipt_sha256",Q(receipt)));
        return O("contract_version",Q("m6.owner-request.v1"),"run_id",Q("flow"),"spec_sha256",Q(hash),
            "request_id",Q("r"+number),"command",O("kind",Q("append"),"event",producer));
    }
    static void Check(bool condition,string name,List<string> checks) {
        if(!condition) throw new Exception("M6 binding check: "+name);
        checks.Add(MineruResidentWire.Quote(name));
    }
    public static string Run(string vectorsRaw,string outputDirectory) {
        MineruJsonValue vectors=MineruResidentWire.Parse(vectorsRaw,262144);
        List<string> checks=new List<string>();
        MineruJsonValue cases=vectors.Get("cases");
        for(int i=0;i<cases.Count;i++) {
            MineruJsonValue item=cases.Item(i);bool accepted=true;
            try {MineruM6OwnerBinding.Validate(item.Get("spec").String(),item.Get("anchor").String());}
            catch(FormatException) {accepted=false;}
            Check(accepted==(item.Get("valid").Raw=="true"),item.Get("name").String(),checks);
        }
        MineruM6PrivateStore.CreatePrivateDirectory(outputDirectory);
        using(MineruM6PrivateStore store=new MineruM6PrivateStore(outputDirectory,false,16,1048576)) {
            MineruM6OwnerDiagnostics diagnostics=new MineruM6OwnerDiagnostics(store.Diagnostic);
            for(int i=0;i<10000;i++) diagnostics.Record("authentication_rejected",null);
            Check(Directory.GetFiles(outputDirectory,"diagnostic-*").Length==0,"noise_does_not_allocate_artifacts",checks);
            byte[] first=MineruResidentWire.Utf8.GetBytes("{\"partial\":");
            byte[] second=MineruResidentWire.Utf8.GetBytes("{\"partial\":1");
            diagnostics.Record("request_shape_invalid",first);diagnostics.Record("request_shape_invalid",second);
            HashSet<string> bodyHashes=new HashSet<string>(StringComparer.Ordinal);
            foreach(string path in Directory.GetFiles(outputDirectory,"diagnostic-*.bin")) bodyHashes.Add(MineruResidentWire.Hash(File.ReadAllBytes(path)));
            Check(bodyHashes.SetEquals(new string[]{MineruResidentWire.Hash(first),MineruResidentWire.Hash(second)}),
                "different_authenticated_original_bytes_retained",checks);
            diagnostics.ObserveReply("{\"fixture\":true}","{\"error_code\":\"test_refused\",\"outcome\":\"rejected\"}");
            Check(Directory.GetFiles(outputDirectory,"diagnostic-*.bin").Length==3,"non_success_reply_retains_request",checks);
            diagnostics.SealNoise();diagnostics.SealNoise();
            bool summary=false;
            foreach(string path in Directory.GetFiles(outputDirectory,"diagnostic-*.json")) {
                MineruJsonValue meta=MineruResidentWire.Parse(File.ReadAllText(path),65536);
                byte[] body=File.ReadAllBytes(Path.ChangeExtension(path,"bin"));
                Check(meta.Get("body_sha256").String()==MineruResidentWire.Hash(body),"diagnostic_hash_binds_original_bytes",checks);
                if(meta.Get("code").String()=="noise_summary") {
                    summary=true;MineruJsonValue counts=MineruResidentWire.Parse(MineruResidentWire.Utf8.GetString(body),65536).Get("counts");
                    Check(counts.Get("authentication_rejected").Integer()==10000,"finite_summary_preserves_noise_count",checks);
                }
            }
            Check(summary && Directory.GetFiles(outputDirectory,"diagnostic-*.bin").Length==4,"summary_written_once",checks);
            diagnostics.Record("peer_closed",null);
            Check(diagnostics.PostSealNoise==1 && Directory.GetFiles(outputDirectory,"diagnostic-*.bin").Length==4,
                "postseal_noise_is_bounded_tail_metadata",checks);
            Exception failure=null;Thread thread=new Thread(delegate(){try {diagnostics.Record("peer_closed",null);}catch(Exception error){failure=error;}});
            thread.Start();thread.Join();
            Check(failure is InvalidOperationException,"foreign_thread_rejected",checks);
        }
        MineruM6OwnerDiagnostics failing=new MineruM6OwnerDiagnostics(delegate(string code,byte[] body){throw new IOException("injected_write_failure");});
        bool propagated=false;try {failing.Record("test_refused",new byte[]{1});}catch(IOException error){if(error.Message!="injected_write_failure")throw;propagated=true;}
        Check(propagated,"authenticated_storage_failure_propagates",checks);
        bool latched=false;try {failing.Record("test_refused",new byte[]{1});}catch(InvalidOperationException){latched=true;}
        Check(latched,"uncertain_storage_cannot_retry",checks);
        int seals=0;MineruM6OwnerDiagnostics failedSeal=new MineruM6OwnerDiagnostics(delegate(string code,byte[] body){seals++;throw new IOException("injected_seal_failure");});
        try {failedSeal.SealNoise();}catch(IOException error){if(error.Message!="injected_seal_failure")throw;}
        bool sealLatched=false;try {failedSeal.SealNoise();}catch(InvalidOperationException){sealLatched=true;}
        Check(sealLatched && seals==1,"failed_seal_cannot_write_second_summary",checks);
        string pendingRoot=outputDirectory+"-pending";
        MineruM6PrivateStore.CreatePrivateDirectory(pendingRoot);
        using(MineruM6PrivateStore store=new MineruM6PrivateStore(pendingRoot,false,8,65536)) {
            MineruM6OwnerDiagnostics diagnostics=new MineruM6OwnerDiagnostics(store.Diagnostic);
            string receipt="sha256:"+new string('b',64),reply="{\"error_code\":\"verifier_drain_pending\",\"outcome\":\"rejected\"}";
            string chain=null;
            for(int i=0;i<1000;i++) {
                string request=PendingRequest(i,receipt),hash=MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(request));
                chain=chain==null ? hash : MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(chain+"\n"+hash));
                diagnostics.ObserveReply(request,reply);
            }
            Check(Directory.GetFiles(pendingRoot,"diagnostic-*.bin").Length==1,"pending_same_command_new_nonce_one_original",checks);
            diagnostics.ObserveReply(PendingRequest(1000,"sha256:"+new string('c',64)),reply);
            Check(Directory.GetFiles(pendingRoot,"diagnostic-*.bin").Length==2,"changed_pending_business_bytes_retained",checks);
            diagnostics.ObserveReply(PendingRequest(1001,receipt),"{\"outcome\":\"ok\"}");
            Check(Directory.GetFiles(pendingRoot,"diagnostic-*.bin").Length==2,"ok_reply_no_diagnostic",checks);
            diagnostics.Record("unrecognized_transport_code",null);diagnostics.SealNoise();
            bool countAndChain=false,other=false;
            foreach(string path in Directory.GetFiles(pendingRoot,"diagnostic-*.json")) {
                MineruJsonValue meta=MineruResidentWire.Parse(File.ReadAllText(path),65536);
                if(meta.Get("code").String()!="noise_summary")continue;
                MineruJsonValue summary=MineruResidentWire.Parse(File.ReadAllText(Path.ChangeExtension(path,"bin")),65536),drains=summary.Get("pending_drains");
                other=summary.Get("counts").Get("other").Integer()==1;
                for(int i=0;i<drains.Count;i++) if(drains.Item(i).Get("occurrences").Integer()==1000)
                    countAndChain=drains.Item(i).Get("request_hash_chain").String()==chain;
            }
            Check(countAndChain && other,"pending_summary_binds_count_and_entire_request_hash_chain",checks);
            diagnostics.ObserveReply(PendingRequest(1002,receipt),"{\"error_code\":\"producer_event_conflict\",\"outcome\":\"conflict\"}");
            Check(Directory.GetFiles(pendingRoot,"diagnostic-*.bin").Length==4,"conflict_reply_always_preserves_original",checks);
        }
        return MineruResidentWire.Object("status",MineruResidentWire.Quote("pass"),"checks","["+String.Join(",",checks.ToArray())+"]");
    }
}
