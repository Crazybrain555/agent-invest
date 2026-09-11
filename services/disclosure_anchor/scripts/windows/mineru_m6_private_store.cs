// Run-private, bounded immutable receipts and diagnostics. Journal/guard retain
// their own durability protocol. No arbitrary file paths or in-place repair.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Win32.SafeHandles;

public sealed class MineruM6PrivateStore : IDisposable {
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern uint GetFinalPathNameByHandleW(SafeFileHandle file,StringBuilder path,uint capacity,uint flags);
    readonly string root;
    readonly FileStream ownerLock;
    readonly Dictionary<string,FileStream> pins=new Dictionary<string,FileStream>(StringComparer.Ordinal);
    readonly HashSet<string> counted=new HashSet<string>(StringComparer.Ordinal);
    readonly int maximumArtifacts;
    readonly long maximumArtifactBytes;
    int artifacts;
    long artifactBytes;
    bool disposed;

    public static void CreatePrivateDirectory(string path) {
        if(!Path.IsPathRooted(path) || Path.GetFullPath(path)!=path || Directory.Exists(path) || File.Exists(path))
            throw new IOException("New absolute private run directory required");
        string parent=Path.GetDirectoryName(path);
        if(!Directory.Exists(parent)) throw new IOException("Existing authorized run parent required");
        DirectorySecurity security=new DirectorySecurity();
        security.SetAccessRuleProtection(true,false);
        using(WindowsIdentity identity=WindowsIdentity.GetCurrent()) {
            if(identity.User==null) throw new IOException("Current Windows user SID unavailable");
            security.SetOwner(identity.User);
            foreach(SecurityIdentifier sid in new SecurityIdentifier[]{identity.User,new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null)})
                security.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit,PropagationFlags.None,AccessControlType.Allow));
        }
        Directory.CreateDirectory(path,security);
        AssertPrivateDirectory(path);
    }
    static void AssertPrivateDirectory(string path) {
        if((File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0) throw new IOException("M6 run directory is a reparse point");
        DirectorySecurity security=Directory.GetAccessControl(path,AccessControlSections.Access|AccessControlSections.Owner);
        using(WindowsIdentity identity=WindowsIdentity.GetCurrent()) {
            if(identity.User==null || !identity.User.Equals(security.GetOwner(typeof(SecurityIdentifier))) || !security.AreAccessRulesProtected)
                throw new IOException("M6 run directory private owner/ACL differs");
            SecurityIdentifier system=new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null);
            foreach(FileSystemAccessRule rule in security.GetAccessRules(true,true,typeof(SecurityIdentifier)))
                if(rule.AccessControlType==AccessControlType.Allow && !rule.IdentityReference.Equals(identity.User) && !rule.IdentityReference.Equals(system))
                    throw new IOException("M6 run directory grants another principal access");
        }
    }
    public MineruM6PrivateStore(string runDirectory,bool resume,int maxArtifacts,long maxArtifactBytes) {
        if(!Path.IsPathRooted(runDirectory) || Path.GetFullPath(runDirectory)!=runDirectory ||
            runDirectory.StartsWith(@"\\",StringComparison.Ordinal) || maxArtifacts<8 || maxArtifacts>16384 ||
            maxArtifactBytes<65536 || maxArtifactBytes>268435456) throw new ArgumentException("M6 private storage bounds/path");
        root=runDirectory;maximumArtifacts=maxArtifacts;maximumArtifactBytes=maxArtifactBytes;
        AssertPrivateDirectory(root);
        ownerLock=new FileStream(Path.Combine(root,"owner.lock"),resume ? FileMode.Open : FileMode.CreateNew,FileAccess.ReadWrite,FileShare.None);
        try {
            AssertPath(ownerLock,"owner.lock");
            foreach(string path in Directory.EnumerateFileSystemEntries(root)) {
                string name=Path.GetFileName(path);
                if((File.GetAttributes(path)&(FileAttributes.Directory|FileAttributes.ReparsePoint))!=0)
                    throw new IOException("Unexpected M6 private store entry");
                if(name=="owner.lock" || name=="events.jsonl" || name=="writer-guard.json") continue;
                if(!NameValid(name)) throw new IOException("Unknown M6 private artifact name");
                long length=new FileInfo(path).Length;
                Reserve(length);
                counted.Add(name);
                if(length>65536) throw new IOException("Existing M6 artifact is over bound");
                // Pending writes survive a crash as evidence. Recovery never
                // promotes or removes an uncertain artifact.
                if(name.Contains(".pending-")) throw new IOException("M6 pending artifact requires explicit reconciliation");
            }
        } catch {ownerLock.Dispose();throw;}
    }
    static bool NameValid(string name) {
        return Regex.IsMatch(name,@"\A(?:anchor|spec|deployment|admission-closed|resources-closed|exit-observation|receipt-[0-9a-f]{64}|diagnostic-[0-9a-f]{32})(?:\.json|\.bin)(?:\.pending-[0-9a-f]{32})?\z");
    }
    void Live() { if(disposed) throw new ObjectDisposedException("M6 private store"); }
    public int RemainingArtifactCount { get { Live();return maximumArtifacts-artifacts; } }
    public long RemainingArtifactBytes { get { Live();return maximumArtifactBytes-artifactBytes; } }
    void Reserve(long bytes) {
        if(bytes<0 || artifacts>=maximumArtifacts || artifactBytes>maximumArtifactBytes-bytes)
            throw new IOException("M6 private artifact budget exhausted");
        artifacts++;artifactBytes+=bytes;
    }
    void AssertPath(FileStream file,string name) {
        StringBuilder actual=new StringBuilder(32768);
        uint length=GetFinalPathNameByHandleW(file.SafeFileHandle,actual,(uint)actual.Capacity,0);
        if(length==0) throw new Win32Exception();
        if(length>=actual.Capacity || !String.Equals(actual.ToString(),@"\\?\"+Path.Combine(root,name),StringComparison.OrdinalIgnoreCase))
            throw new IOException("M6 opened artifact resolved outside its pinned path");
    }
    FileStream Pin(string name) {
        Live();
        if(!NameValid(name) || name.Contains(".pending-")) throw new ArgumentException("Fixed M6 artifact name required");
        FileStream file;
        if(pins.TryGetValue(name,out file)) return file;
        if(pins.Count>=maximumArtifacts) throw new IOException("M6 receipt handle bound");
        try {file=new FileStream(Path.Combine(root,name),FileMode.Open,FileAccess.Read,FileShare.Read);}
        catch(FileNotFoundException) {return null;}
        try {
            AssertPath(file,name);if(file.Length>65536) throw new IOException("M6 receipt byte bound");
            if(!counted.Contains(name)) {Reserve(file.Length);counted.Add(name);}
            pins.Add(name,file);return file;
        }
        catch {file.Dispose();throw;}
    }
    public byte[] Read(string name) {
        FileStream file=Pin(name);if(file==null) return null;
        file.Position=0;byte[] data=new byte[checked((int)file.Length)];int used=0;
        while(used<data.Length) {int count=file.Read(data,used,data.Length-used);if(count==0) throw new EndOfStreamException("M6 pinned artifact truncated");used+=count;}
        return data;
    }
    public string ReadReceipt(string sha) {
        if(sha==null || !Regex.IsMatch(sha,@"\Asha256:[0-9a-f]{64}\z")) throw new ArgumentException("M6 receipt SHA");
        byte[] data=Read("receipt-"+sha.Substring(7)+".json");
        if(data==null) return null;
        if(MineruResidentWire.Hash(data)!=sha) throw new IOException("M6 private receipt hash differs");
        return MineruResidentWire.Utf8.GetString(data);
    }
    public void WriteImmutable(string name,byte[] data) {
        Live();
        if(!NameValid(name) || name.Contains(".pending-") || data==null || data.Length>65536)
            throw new ArgumentException("Fixed bounded M6 artifact required");
        byte[] old=Read(name);
        if(old!=null) {
            if(MineruResidentWire.Hash(old)!=MineruResidentWire.Hash(data)) throw new IOException("M6 immutable artifact changed");
            return;
        }
        Reserve(data.Length); // Uncertain writes retain their budget.
        string pending=name+".pending-"+Guid.NewGuid().ToString("N");
        using(FileStream output=new FileStream(Path.Combine(root,pending),FileMode.CreateNew,FileAccess.Write,FileShare.None)) {
            AssertPath(output,pending);output.Write(data,0,data.Length);output.Flush(true);
        }
        File.Move(Path.Combine(root,pending),Path.Combine(root,name));
        counted.Add(name);
        byte[] reread=Read(name);
        if(reread==null || MineruResidentWire.Hash(reread)!=MineruResidentWire.Hash(data)) throw new IOException("M6 artifact publication differs");
        // This proves file Flush(true), not a POSIX directory fsync guarantee.
        // The external controller reopens and hashes all final artifacts.
    }
    public void WriteControl(string name,string raw) {
        if(name!="admission-closed" && name!="resources-closed") throw new ArgumentException("M6 control sidecar kind");
        MineruResidentWire.Parse(raw,65536);WriteImmutable(name+".json",MineruResidentWire.Utf8.GetBytes(raw));
    }
    public string ReadControl(string name) {
        if(name!="admission-closed" && name!="resources-closed") throw new ArgumentException("M6 control sidecar kind");
        byte[] bytes=Read(name+".json");return bytes==null ? null : MineruResidentWire.Utf8.GetString(bytes);
    }
    public void Diagnostic(string code,byte[] authenticatedBody) {
        if(code==null || !Regex.IsMatch(code,@"\A[a-z][a-z0-9_]{0,127}\z") ||
            (authenticatedBody!=null && authenticatedBody.Length>65536)) throw new ArgumentException("Bounded M6 diagnostic");
        string name="diagnostic-"+Guid.NewGuid().ToString("N"),bodySha=null;
        if(authenticatedBody!=null) {bodySha=MineruResidentWire.Hash(authenticatedBody);WriteImmutable(name+".bin",authenticatedBody);}
        string raw=MineruResidentWire.Object("contract_version",MineruResidentWire.Quote("m6.transport-diagnostic.v1"),
            "code",MineruResidentWire.Quote(code),"body_sha256",bodySha==null ? "null" : MineruResidentWire.Quote(bodySha));
        WriteImmutable(name+".json",MineruResidentWire.Utf8.GetBytes(raw));
    }
    public FileStream OpenJournalFile(string name,bool resume) {
        Live();
        if(name!="events.jsonl" && name!="writer-guard.json") throw new ArgumentException("M6 mutable journal name");
        // One writer, plus independent readers of a known flushed prefix. A
        // reader uses Read access/ReadWrite sharing to accommodate this writer;
        // this handle still denies every other writer, deletion and rename.
        FileShare share=name=="events.jsonl" ? FileShare.Read : FileShare.None;
        FileStream stream=new FileStream(Path.Combine(root,name),resume ? FileMode.Open : FileMode.CreateNew,FileAccess.ReadWrite,share);
        try {AssertPath(stream,name);return stream;}catch {stream.Dispose();throw;}
    }
    public void CloseReadPins() {
        Live();List<Exception> failures=new List<Exception>();
        foreach(string key in new List<string>(pins.Keys)) {
            try {pins[key].Dispose();pins.Remove(key);}catch(Exception error){failures.Add(error);}
        }
        if(failures.Count!=0) throw new AggregateException("M6 artifact read handle closure failed",failures);
    }
    public void Dispose() {
        if(disposed) return;
        CloseReadPins();ownerLock.Dispose();disposed=true;
    }
}
