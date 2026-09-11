// Fixed M6 owner executable. Build and qualify the complete binary before a run.
// No arbitrary command execution, process spawning, automatic reconnect or repair.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.RegularExpressions;

public static class MineruM6OwnerHost {
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern uint GetFinalPathNameByHandleW(SafeFileHandle file,StringBuilder path,uint capacity,uint flags);
    static void ActualPath(FileStream file,string expected) {
        StringBuilder path=new StringBuilder(32768);
        uint size=GetFinalPathNameByHandleW(file.SafeFileHandle,path,(uint)path.Capacity,0);
        Require(size>0 && size<path.Capacity && String.Equals(path.ToString(),@"\\?\"+expected,StringComparison.OrdinalIgnoreCase),
            "M6 private file resolves outside its expected path");
    }
    static string Q(string s) { return MineruResidentWire.Quote(s); }
    static string N(long n) { return MineruResidentWire.Integer(n); }
    static string Obj(params string[] items) { return MineruResidentWire.Object(items); }
    static string H(byte[] b) { return MineruResidentWire.Hash(b); }
    static string H(string s) { return H(MineruResidentWire.Utf8.GetBytes(s)); }
    static string S(MineruJsonValue v,string key) { return v.Get(key).String(); }
    static long Number(string s) {
        return MineruResidentWire.Parse(s,64).Integer();
    }
    static void Require(bool yes,string reason) { if(!yes) throw new IOException(reason); }
    static void PrivatePath(string path) {
        Require(Path.IsPathRooted(path) && Path.GetFullPath(path)==path && !path.StartsWith(@"\\",StringComparison.Ordinal),
            "M6 private path must be absolute local canonical");
        string current=path;
        while(!String.IsNullOrEmpty(current)) {
            Require((File.GetAttributes(current)&FileAttributes.ReparsePoint)==0,"M6 private path traverses reparse point");
            current=Path.GetDirectoryName(current);
        }
    }
    static void PrivateDirectory(string path) {
        PrivatePath(path);
        DirectorySecurity security=Directory.GetAccessControl(path,AccessControlSections.Access|AccessControlSections.Owner);
        using(WindowsIdentity user=WindowsIdentity.GetCurrent()) {
            Require(user.User!=null && user.User.Equals(security.GetOwner(typeof(SecurityIdentifier))) && security.AreAccessRulesProtected,
                "M6 private directory owner or protected ACL differs");
            SecurityIdentifier system=new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null);
            foreach(FileSystemAccessRule rule in security.GetAccessRules(true,true,typeof(SecurityIdentifier)))
                Require(rule.AccessControlType!=AccessControlType.Allow || rule.IdentityReference.Equals(user.User) || rule.IdentityReference.Equals(system),
                    "M6 private directory grants another principal");
        }
    }
    static byte[] ReadPrivate(string path,string expectedHash) {
        PrivatePath(path);PrivateDirectory(Path.GetDirectoryName(path));
        using(FileStream file=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read)) {
            ActualPath(file,path);
            FileSecurity access=file.GetAccessControl();
            using(WindowsIdentity user=WindowsIdentity.GetCurrent()) {
                Require(user.User!=null && user.User.Equals(access.GetOwner(typeof(SecurityIdentifier))),"M6 private configuration owner differs");
                SecurityIdentifier system=new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null);
                foreach(FileSystemAccessRule rule in access.GetAccessRules(true,true,typeof(SecurityIdentifier)))
                    Require(rule.AccessControlType!=AccessControlType.Allow || rule.IdentityReference.Equals(user.User) || rule.IdentityReference.Equals(system),
                        "M6 private configuration grants another principal");
            }
            Require(file.Length>0 && file.Length<=65536,"M6 private configuration byte bound");
            byte[] result=new byte[checked((int)file.Length)];int used=0;
            while(used<result.Length) {int n=file.Read(result,used,result.Length-used);if(n==0)throw new EndOfStreamException();used+=n;}
            Require(H(result)==expectedHash,"M6 private configuration hash differs");return result;
        }
    }
    static FileStream PinBinary(string expectedHash) {
        string path=Assembly.GetExecutingAssembly().Location;PrivatePath(path);
        FileStream file=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read);
        try {
            ActualPath(file,path);
            Require(file.Length>0 && file.Length<=16777216,"M6 owner binary byte bound");
            using(System.Security.Cryptography.SHA256 sha=System.Security.Cryptography.SHA256.Create())
                Require("sha256:"+BitConverter.ToString(sha.ComputeHash(file)).Replace("-","").ToLowerInvariant()==expectedHash,
                    "M6 running binary differs from qualified executable");
            return file;
        } catch {file.Dispose();throw;}
    }

    sealed class Host : IDisposable {
        readonly MineruM6SelfJob job;
        readonly MineruM6PrivateStore store;
        readonly MineruM6Credentials credentials;
        readonly MineruM6OwnerDiagnostics diagnostics;
        readonly Dictionary<string,string> epochs=new Dictionary<string,string>(StringComparer.Ordinal);
        readonly string anchorRaw,anchorSha,mode,ownerEpoch,runId;
        readonly long maximumLease,reserve;
        readonly bool resume;
        MineruM6WriterGuard guard;
        MineruM6Journal journal;
        MineruM6RunControl control;
        MineruM6Endpoint endpoint;
        FileStream journalFile,guardFile,binaryPin;
        bool disposed,shutdownFailed;
        string boundSpecSha;

        public Host(MineruM6SelfJob lifetime,MineruJsonValue cfg,string configHash,string binaryHash,
                    long t0,long plannedSeconds,long graceSeconds,long hardDeadline,string originalAnchorSha) {
            job=lifetime;resume=originalAnchorSha!="none";
            cfg.Keys("contract_version","run_id","run_root","mode","owner_source_sha256","expected_node_sha256","gpu_uuid",
                "nvml_dll_sha256","port","resources","max_artifacts","max_artifact_bytes","maximum_lease_ticks",
                "propagation_reserve_ticks","roles");
            Require(S(cfg,"contract_version")=="m6.owner-deployment.v1","M6 deployment version");
            runId=S(cfg,"run_id");mode=S(cfg,"mode");
            MineruM6OwnerWire.Shape(MineruResidentWire.Parse(Obj("run_id",Q(runId),"mode",Q(mode),
                "source",cfg.Get("owner_source_sha256").Raw,"node",cfg.Get("expected_node_sha256").Raw,
                "nvml",cfg.Get("nvml_dll_sha256").Raw),4096),"run_id:id","mode:=service_diagnostic|e2e_publication","source:hash","node:hash","nvml:hash");
            string root=S(cfg,"run_root");PrivateDirectory(root);
            string directory=Path.Combine(root,H(runId).Substring(7));
            string resourceRaw=MineruM6OwnerWire.Resources(cfg.Get("resources"));
            Require(resourceRaw==cfg.Get("resources").Raw,"M6 deployment resources are not canonical");
            Require(cfg.Get("resources").Get("max_record_bytes").Integer()<=60000,"M6 native record envelope exceeded");
            maximumLease=cfg.Get("maximum_lease_ticks").Integer();reserve=cfg.Get("propagation_reserve_ticks").Integer();
            binaryPin=PinBinary(binaryHash);
            MineruM6OwnerIdentity identity=new MineruM6OwnerIdentity(S(cfg,"expected_node_sha256"),S(cfg,"gpu_uuid"),S(cfg,"nvml_dll_sha256"));
            ownerEpoch=identity.ProcessEpoch(runId,S(cfg,"owner_source_sha256"));
            if(!resume) MineruM6PrivateStore.CreatePrivateDirectory(directory);
            long artifacts=cfg.Get("max_artifacts").Integer(),artifactBytes=cfg.Get("max_artifact_bytes").Integer();
            Require(artifacts>=128 && artifactBytes>=8388608,"M6 private store lacks fixed closure/recovery reserve");
            store=new MineruM6PrivateStore(directory,resume,checked((int)artifacts),artifactBytes);
            // Both new and resumed owners require exactly the same deployment,
            // role/token bytes and executable. No credential contents are stored.
            store.WriteImmutable("deployment.json",MineruResidentWire.Utf8.GetBytes(Obj("contract_version",Q("m6.owner-deployment-binding.v1"),
                "config_sha256",Q(configHash),"binary_sha256",Q(binaryHash))));
            if(resume) {
                byte[] bytes=store.Read("anchor.json");Require(bytes!=null && H(bytes)==originalAnchorSha,"M6 original anchor unavailable or changed");
                anchorRaw=MineruResidentWire.Utf8.GetString(bytes);
                MineruJsonValue original=MineruResidentWire.Parse(anchorRaw,65536);
                Require(MineruM6OwnerWire.Anchor(original)==anchorRaw,"M6 original anchor is not canonical");
                Require(S(original,"run_id")==runId && original.Get("clock").Raw==identity.ClockRaw &&
                    S(original,"owner_source_sha256")==S(cfg,"owner_source_sha256") && S(original,"gpu_device_identity_sha256")==identity.GpuDeviceSha &&
                    original.Get("resources").Raw==resourceRaw && original.Get("max_close_ticks").Integer()==hardDeadline &&
                    original.Get("planned_seconds").Integer()==plannedSeconds,"M6 recovered physical identity, bounds or interval differs");
            } else {
                anchorRaw=Obj("contract_version",Q("m6.owner-anchor.v1"),"run_id",Q(runId),"clock",identity.ClockRaw,
                    "owner_process_epoch_sha256",Q(ownerEpoch),"owner_source_sha256",cfg.Get("owner_source_sha256").Raw,
                    "gpu_device_identity_sha256",Q(identity.GpuDeviceSha),"t0_ticks",N(t0),"planned_seconds",N(plannedSeconds),
                    "deadline_ticks",N(checked(t0+plannedSeconds*Stopwatch.Frequency)),"max_close_ticks",N(hardDeadline),"resources",resourceRaw);
                Require(MineruM6OwnerWire.Anchor(MineruResidentWire.Parse(anchorRaw,65536))==anchorRaw,"M6 startup anchor invalid");
                store.WriteImmutable("anchor.json",MineruResidentWire.Utf8.GetBytes(anchorRaw));
            }
            anchorSha=H(anchorRaw);store.Diagnostic("owner_identity",MineruResidentWire.Utf8.GetBytes(identity.Evidence()));
            string[] roles=mode=="service_diagnostic" ? new string[]{"controller","service_runner","quality_verifier"} :
                new string[]{"controller","e2e_runner","quality_verifier","public_verifier"};
            MineruJsonValue roleValues=cfg.Get("roles");roleValues.Keys(roles);
            Dictionary<MineruM6Principal,string> tokens=new Dictionary<MineruM6Principal,string>();
            foreach(string role in roles) {
                MineruJsonValue pair=roleValues.Get(role);pair.Keys("epoch_sha256","token");
                string epoch=S(pair,"epoch_sha256");epochs.Add(role,epoch);tokens.Add(new MineruM6Principal(role,epoch),S(pair,"token"));
            }
            credentials=new MineruM6Credentials(tokens);diagnostics=new MineruM6OwnerDiagnostics(PersistDiagnostic);
            if(resume) {byte[] spec=store.Read("spec.json");Require(spec!=null,"M6 original spec missing");InitializeControl(H(spec));}
            endpoint=new MineruM6Endpoint(checked((int)cfg.Get("port").Integer()),credentials,Handle,Tick,
                delegate {return control!=null && control.IsClosed;},diagnostics.Record,10000,60000,8);
        }
        void PersistDiagnostic(string code,byte[] body) {
            long bytes=(body==null ? 0 : body.Length)+1024;
            // Closing summary uses its reserved capacity. All ordinary raw
            // diagnostics leave room for fixed sidecars and bounded recoveries,
            // including artifacts already present when resuming the same run.
            if(code!="noise_summary") Require(store.RemainingArtifactCount>=66 && store.RemainingArtifactBytes>=4194304+bytes,
                "M6 authenticated diagnostic reserve exhausted");
            store.Diagnostic(code,body);
        }
        void Tick() { if(control!=null) control.Tick(); }
        void AssertNativeClosure() {
            diagnostics.SealNoise();store.CloseReadPins();
            if(binaryPin!=null) {binaryPin.Dispose();binaryPin=null;}
            job.AssertNoChildren();
            // Journal/guard, owner lock, credentials and pending endpoint reply
            // are finite closure-evidence tail resources. No business handles.
        }
        string Handle(string raw,MineruM6Principal principal) {
            MineruJsonValue request=MineruResidentWire.Parse(raw,65536);
            if(control==null) {
                if(principal.Role!="controller" || S(request.Get("command"),"kind")!="bind")
                    throw new MineruM6ControlRefusal("owner_not_bound");
                if(S(request,"run_id")!=runId || S(request.Get("command"),"anchor_sha256")!=anchorSha)
                    throw new MineruM6ControlRefusal("bind_identity_or_role_differs");
                InitializeControl(S(request,"spec_sha256"));
            }
            string reply=control.Handle(raw,principal.Role,principal.Epoch);diagnostics.ObserveReply(raw,reply);return reply;
        }
        void InitializeControl(string expectedSpecSha) {
            byte[] bytes=store.Read("spec.json");
            if(bytes==null || H(bytes)!=expectedSpecSha) throw new MineruM6ControlRefusal("bootstrap_spec_missing_or_changed");
            string specRaw=MineruResidentWire.Utf8.GetString(bytes);MineruJsonValue spec=MineruM6OwnerBinding.Validate(specRaw,anchorRaw);
            Require(S(spec,"mode")==mode,"M6 bound spec differs from deployment mode");
            MineruJsonValue limits=spec.Get("resources");
            journalFile=store.OpenJournalFile("events.jsonl",resume);guardFile=store.OpenJournalFile("writer-guard.json",resume);
            guard=new MineruM6WriterGuard(guardFile,delegate {guardFile.Flush(true);},runId,expectedSpecSha);
            journal=new MineruM6Journal(journalFile,delegate {journalFile.Flush(true);},checked((int)limits.Get("max_record_bytes").Integer()),
                checked((int)limits.Get("max_events").Integer()),limits.Get("max_log_bytes").Integer(),runId,expectedSpecSha,
                S(spec.Get("clock"),"boot_identity_sha256"),guard);
            int resumes=0;
            foreach(string record in journal.ReadRecords())
                if(S(MineruResidentWire.Parse(record,65536).Get("event").Get("payload"),"kind")=="owner_resumed") resumes++;
            Require(!resume || resumes<8,"M6 original run recovery count exhausted");
            control=new MineruM6RunControl(anchorRaw,expectedSpecSha,ownerEpoch,mode,epochs,journal,
                Stopwatch.GetTimestamp,maximumLease,reserve,store.ReadReceipt,store.WriteControl,store.ReadControl,AssertNativeClosure);
            boundSpecSha=expectedSpecSha;
        }
        public void Run() {
            try {
            endpoint.Run(delegate {
                long prefixBytes=journal==null ? 0 : journal.Bytes;
                string prefixSha=H(new byte[0]);
                if(journalFile!=null) {
                    journalFile.Position=0;
                    try {using(System.Security.Cryptography.SHA256 hash=System.Security.Cryptography.SHA256.Create())
                        prefixSha="sha256:"+BitConverter.ToString(hash.ComputeHash(journalFile)).Replace("-","").ToLowerInvariant();}
                    finally {journalFile.Position=prefixBytes;}
                }
                Console.WriteLine(Obj("status",Q(resume ? "ready_recovered" : "ready_unbound"),"anchor_sha256",Q(anchorSha),
                    "owner_epoch_sha256",Q(ownerEpoch),"anchor",anchorRaw,"spec_sha256",boundSpecSha==null ? "null" : Q(boundSpecSha),
                    "journal_prefix_bytes",N(prefixBytes),"journal_prefix_sha256",Q(prefixSha)));
            });
            Require(control!=null && control.IsClosed,"M6 endpoint returned before run closure");
            endpoint.Dispose();endpoint=null;job.AssertNoChildren();
            journal.Dispose();journal=null;journalFile=null;guard.Dispose();guard=null;guardFile=null;
            credentials.Dispose();
            store.WriteImmutable("exit-observation.json",MineruResidentWire.Utf8.GetBytes(Obj("contract_version",Q("m6.owner-exit-intent.v1"),
                "owner_epoch_sha256",Q(ownerEpoch),"qpc_ticks",N(Stopwatch.GetTimestamp()),"post_seal_noise_count",N(diagnostics.PostSealNoise),
                "external_process_exit_verified","false")));
            } catch {shutdownFailed=true;throw;}
        }
        public void Dispose() {
            if(disposed || shutdownFailed)return;
            disposed=true;
            // Failure remains visible. The finite Job/OS owns the last-resort
            // lifetime; never release the writer lock after failed pin closure.
            if(endpoint!=null) endpoint.Dispose();
            if(journal!=null) journal.Dispose();else if(journalFile!=null)journalFile.Dispose();
            if(guard!=null)guard.Dispose();else if(guardFile!=null)guardFile.Dispose();
            if(credentials!=null)credentials.Dispose();
            if(binaryPin!=null)binaryPin.Dispose();
            if(store!=null)store.Dispose();
        }
    }

    public static int Main(string[] args) {
        long t0=Stopwatch.GetTimestamp(); // First controlled entry, before run input preparation.
        try {
            if(args.Length!=8) throw new ArgumentException("M6 fixed deployment path/hash, binary hash, seconds, grace, memory, resume deadline and anchor hash required");
            long seconds=Number(args[3]),grace=Number(args[4]),memory=Number(args[5]),resumeDeadline=Number(args[6]);
            Require(seconds>0 && grace>0 && seconds+grace<=7200,"M6 bounded lifetime seconds required");
            bool resume=args[7]!="none";Require(resume==(resumeDeadline>0),"M6 explicit resume pair required");
            long hard=resume ? resumeDeadline : checked(t0+checked((seconds+grace)*Stopwatch.Frequency));
            MineruM6SelfJob job=MineruM6SelfJob.Enter(hard,memory);
            MineruJsonValue cfg=MineruResidentWire.Parse(MineruResidentWire.Utf8.GetString(ReadPrivate(args[0],args[1])),65536);
            using(Host host=new Host(job,cfg,args[1],args[2],t0,seconds,grace,hard,args[7])) host.Run();
            return 0;
        } catch(Exception error) {
            // Controlled parser/platform messages contain no config/token bytes.
            Console.Error.WriteLine(error.ToString());return 1;
        }
    }
}
