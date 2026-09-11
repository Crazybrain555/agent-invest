// Actual Windows private-directory/FileStream checks on fresh test artifacts.
// No business datasets, credentials or shared ACLs are read or changed.
using System;
using System.Collections.Generic;
using System.IO;

public static class MineruM6PrivateStoreChecks {
    static byte[] B(string text) { return MineruResidentWire.Utf8.GetBytes(text); }
    static string H(byte[] data) { return MineruResidentWire.Hash(data); }
    static string Q(string text) { return MineruResidentWire.Quote(text); }
    static void Check(bool good,string name,List<string> checks) {
        if(!good) throw new Exception("M6 private store check: "+name);checks.Add(Q(name));
    }
    static bool SharingViolation(Action action) {
        try {action();return false;}catch(IOException e) {if((e.HResult&65535)!=32) throw;return true;}
    }
    static bool Fails(Action action,string text) {
        try {action();return false;}catch(IOException e) {if(!e.Message.Contains(text)) throw;return true;}
    }
    public static string Run(string outputDirectory) {
        if(Directory.Exists(outputDirectory)) throw new IOException("New private-store test directory required");
        Directory.CreateDirectory(outputDirectory);
        List<string> checks=new List<string>();
        string root=Path.Combine(outputDirectory,"private");
        MineruM6PrivateStore.CreatePrivateDirectory(root);
        byte[] receipt=B("{\"fixture\":true}");string sha=H(receipt),name="receipt-"+sha.Substring(7)+".json";
        using(MineruM6PrivateStore store=new MineruM6PrivateStore(root,false,64,1048576)) {
            Check(store.ReadReceipt(sha)==null,"missing_receipt_is_explicit",checks);
            Check(SharingViolation(delegate{using(MineruM6PrivateStore second=new MineruM6PrivateStore(root,true,64,1048576)) {}}),
                "exclusive_single_writer_lock",checks);
            store.WriteImmutable(name,receipt);
            Check(store.ReadReceipt(sha)==MineruResidentWire.Utf8.GetString(receipt),"exact_receipt_hash_and_bytes",checks);
            Check(SharingViolation(delegate{File.WriteAllBytes(Path.Combine(root,name),B("{}"));}),
                "pinned_receipt_cannot_be_overwritten",checks);
            store.WriteImmutable(name,receipt);
            Check(Fails(delegate{store.WriteImmutable(name,B("{}"));},"immutable artifact changed"),"immutable_retry_identity",checks);
            store.WriteControl("admission-closed","{\"fixture\":true}");
            Check(store.ReadControl("admission-closed")=="{\"fixture\":true}","immutable_control_roundtrip",checks);
            store.WriteImmutable("deployment.json",B("{\"configuration_sha256\":\"test-identity\"}"));
            Check(Fails(delegate{store.WriteImmutable("deployment.json",B("{}"));},"immutable artifact changed"),
                "deployment_cannot_change_on_resume",checks);
            bool badName=false;
            try {store.Read("../outside.json");}catch(ArgumentException){badName=true;}
            Check(badName,"relative_path_escape_rejected",checks);
            using(FileStream file=store.OpenJournalFile("events.jsonl",false)) {
                file.Write(receipt,0,receipt.Length);file.Flush(true);
                Check(SharingViolation(delegate{using(FileStream other=File.OpenRead(Path.Combine(root,"events.jsonl"))) {}}),
                    "journal_reader_must_share_existing_writer",checks);
                using(FileStream reader=new FileStream(Path.Combine(root,"events.jsonl"),FileMode.Open,FileAccess.Read,FileShare.ReadWrite)) {
                    byte[] prefix=new byte[receipt.Length];int n=reader.Read(prefix,0,prefix.Length);
                    Check(n==prefix.Length && H(prefix)==H(receipt),"independent_handle_reads_flushed_prefix",checks);
                    Check(SharingViolation(delegate{using(FileStream second=new FileStream(Path.Combine(root,"events.jsonl"),FileMode.Open,FileAccess.Write,FileShare.ReadWrite)) {}}),
                        "readable_journal_still_denies_second_writer",checks);
                    Check(SharingViolation(delegate{File.Delete(Path.Combine(root,"events.jsonl"));}),
                        "readable_journal_still_denies_delete",checks);
                }
            }
            using(FileStream file=store.OpenJournalFile("writer-guard.json",false)) {
                Check(SharingViolation(delegate{using(FileStream reader=new FileStream(Path.Combine(root,"writer-guard.json"),FileMode.Open,FileAccess.Read,FileShare.ReadWrite)) {}}),
                    "writer_guard_remains_exclusive",checks);
            }
            store.Diagnostic("partial_request_eof",B("{\"partial\":"));
            int pairs=0;
            foreach(string path in Directory.EnumerateFiles(root,"diagnostic-*.json")) {
                MineruJsonValue meta=MineruResidentWire.Parse(File.ReadAllText(path),65536);
                string body=Path.ChangeExtension(path,"bin");
                Check(meta.Get("body_sha256").String()==H(File.ReadAllBytes(body)),"diagnostic_original_byte_binding",checks);pairs++;
            }
            Check(pairs==1,"one_bounded_diagnostic_pair",checks);
        }
        using(MineruM6PrivateStore recovered=new MineruM6PrivateStore(root,true,64,1048576)) {
            Check(recovered.ReadReceipt(sha)==MineruResidentWire.Utf8.GetString(receipt),"private_store_reopens_original_receipts",checks);
            recovered.CloseReadPins();
            File.WriteAllBytes(Path.Combine(root,name),B("{}"));
            Check(Fails(delegate{recovered.ReadReceipt(sha);},"receipt hash differs"),"changed_receipt_detected_after_reopen",checks);
        }
        string countRoot=Path.Combine(outputDirectory,"count-bound");
        MineruM6PrivateStore.CreatePrivateDirectory(countRoot);
        using(MineruM6PrivateStore store=new MineruM6PrivateStore(countRoot,false,8,65536)) {
            for(int i=0;i<8;i++) store.WriteImmutable("diagnostic-"+i.ToString("x32")+".json",B("{}"));
            Check(Fails(delegate{store.WriteImmutable("diagnostic-"+new string('a',32)+".json",B("{}"));},"receipt handle bound") &&
                Directory.GetFiles(countRoot,"diagnostic-*.json").Length==8,
                "artifact_and_pinned_handle_count_bound",checks);
        }
        using(MineruM6PrivateStore store=new MineruM6PrivateStore(countRoot,true,8,65536)) {
            Check(Fails(delegate{store.WriteImmutable("diagnostic-"+new string('b',32)+".json",B("{}"));},"artifact budget"),
                "artifact_budget_survives_resume",checks);
        }
        string byteRoot=Path.Combine(outputDirectory,"byte-bound");
        MineruM6PrivateStore.CreatePrivateDirectory(byteRoot);
        using(MineruM6PrivateStore store=new MineruM6PrivateStore(byteRoot,false,8,65536)) {
            // An independently uploaded receipt must consume the same budget
            // when first read, not bypass accounting because the owner did not write it.
            byte[] large=B(new string('x',40000));string uploaded="receipt-"+H(large).Substring(7)+".json";
            File.WriteAllBytes(Path.Combine(byteRoot,uploaded),large);store.Read(uploaded);
            Check(Fails(delegate{store.WriteImmutable("diagnostic-"+new string('a',32)+".bin",large);},"artifact budget"),
                "external_receipt_read_counts_against_bytes",checks);
        }
        string pendingRoot=Path.Combine(outputDirectory,"pending");
        MineruM6PrivateStore.CreatePrivateDirectory(pendingRoot);
        string pending=Path.Combine(pendingRoot,"anchor.json.pending-"+new string('a',32));
        File.WriteAllBytes(pending,B("{"));
        Check(Fails(delegate{using(MineruM6PrivateStore store=new MineruM6PrivateStore(pendingRoot,false,8,65536)) {}},"pending artifact") &&
            File.ReadAllText(pending)=="{","uncertain_pending_artifact_preserved",checks);
        string openRoot=Path.Combine(outputDirectory,"inherited-acl");Directory.CreateDirectory(openRoot);
        Check(Fails(delegate{using(MineruM6PrivateStore store=new MineruM6PrivateStore(openRoot,false,8,65536)) {}},"private owner/ACL"),
            "inherited_shared_acl_not_accepted_as_private",checks);
        return MineruResidentWire.Object("status",Q("pass"),"scope",Q("fresh_windows_private_storage_only"),
            "checks","["+String.Join(",",checks.ToArray())+"]");
    }
}
