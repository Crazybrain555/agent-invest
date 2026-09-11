// Durable uncertainty marker, separate from the immutable append-only journal.
// A complete LF after a failed journal flush must not become a clean recovery.
using System;
using System.IO;

public sealed class MineruM6WriterGuard : IDisposable {
    readonly Stream stream;
    readonly Action durableFlush;
    readonly string runId,specSha;
    bool poisoned,disposed;
    long sequence;
    string recordSha,state;

    public MineruM6WriterGuard(Stream ownedStream,Action flushToDevice,string run,string spec) {
        if(ownedStream==null || flushToDevice==null || !ownedStream.CanRead || !ownedStream.CanWrite || !ownedStream.CanSeek)
            throw new ArgumentException("M6 writer guard requires an owned seekable stream");
        MineruM6OwnerWire.Shape(MineruResidentWire.Parse(MineruResidentWire.Object("run",Q(run),"spec",Q(spec)),1024),
            "run:id","spec:hash");
        stream=ownedStream; durableFlush=flushToDevice; runId=run; specSha=spec;
        if(stream.Length==0) Write("clean",0,null);
        else {
            if(stream.Length>4096) throw new IOException("M6 writer guard size invalid");
            stream.Position=0;
            byte[] bytes=new byte[checked((int)stream.Length)]; int count=0;
            while(count<bytes.Length) {
                int n=stream.Read(bytes,count,bytes.Length-count);
                if(n==0) throw new EndOfStreamException("M6 writer guard truncated");
                count+=n;
            }
            string raw=MineruResidentWire.Utf8.GetString(bytes);
            MineruJsonValue value=MineruResidentWire.Parse(raw,4096);
            string canonical=MineruM6OwnerWire.Shape(value,"contract_version:=m6.writer-guard.v1",
                "run_id:id","spec_sha256:hash","state:=clean|dirty","sequence:n","record_sha256:?hash");
            if(raw!=canonical || value.Get("run_id").String()!=runId || value.Get("spec_sha256").String()!=specSha)
                throw new IOException("M6 writer guard identity differs");
            state=value.Get("state").String(); sequence=value.Get("sequence").Integer();
            recordSha=value.Get("record_sha256").Raw=="null" ? null : value.Get("record_sha256").String();
            if((sequence==0)!=(recordSha==null)) throw new IOException("M6 writer guard sequence/hash differs");
        }
    }
    static string Q(string text) { return MineruResidentWire.Quote(text); }
    void AssertUsable() {
        if(poisoned || disposed) throw new InvalidOperationException("M6 writer guard uncertain or disposed");
    }
    void Write(string nextState,long nextSequence,string nextSha) {
        AssertUsable();
        string raw=MineruResidentWire.Object("contract_version",Q("m6.writer-guard.v1"),"run_id",Q(runId),
            "spec_sha256",Q(specSha),"state",Q(nextState),"sequence",MineruResidentWire.Integer(nextSequence),
            "record_sha256",nextSha==null ? "null" : Q(nextSha));
        byte[] bytes=MineruResidentWire.Utf8.GetBytes(raw);
        try {
            stream.Position=0; stream.Write(bytes,0,bytes.Length); stream.SetLength(bytes.Length);
            durableFlush();
            state=nextState; sequence=nextSequence; recordSha=nextSha;
        } catch { poisoned=true; throw; }
    }
    public void VerifyRecovered(long lastSequence,string lastRecordSha) {
        AssertUsable();
        if(state!="clean" || sequence!=lastSequence || recordSha!=lastRecordSha)
            throw new MineruM6JournalDamage("writer_guard_uncertain",0,null);
    }
    public void Prepare(long nextSequence,string nextRecordSha) {
        AssertUsable();
        if(state!="clean" || nextSequence!=sequence+1) throw new IOException("M6 writer guard append sequence differs");
        Write("dirty",nextSequence,nextRecordSha);
    }
    public void Committed(long nextSequence,string nextRecordSha) {
        AssertUsable();
        if(state!="dirty" || sequence!=nextSequence || recordSha!=nextRecordSha)
            throw new IOException("M6 writer guard commit differs");
        Write("clean",nextSequence,nextRecordSha);
    }
    public void Dispose() {
        if(disposed) return;
        stream.Dispose(); disposed=true;
    }
}
