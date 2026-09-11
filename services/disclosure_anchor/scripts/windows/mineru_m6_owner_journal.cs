// Bounded append-order M6 evidence. No tail truncation, sorting or repair.
using System;
using System.Collections.Generic;
using System.IO;

public sealed class MineruM6JournalDamage : IOException {
    public readonly long Offset;
    public readonly string Code;
    public MineruM6JournalDamage(string code,long offset,Exception inner) : base(code,inner) {
        Code=code; Offset=offset;
    }
}

public sealed class MineruM6AppendResult {
    public readonly string Record;
    public readonly bool Duplicate, Conflict;
    public MineruM6AppendResult(string record,bool duplicate,bool conflict) {
        Record=record; Duplicate=duplicate; Conflict=conflict;
    }
}

public sealed class MineruM6JournalBound : IOException {
    public readonly string Code;
    public MineruM6JournalBound(string code) : base(code) { Code=code; }
}

public sealed class MineruM6Journal : IDisposable {
    sealed class Index {
        public long Offset; public int Length; public string Hash;
    }
    readonly Stream stream;
    readonly Action durableFlush;
    readonly int maximumRecord, maximumEvents;
    readonly long maximumBytes;
    readonly string runId,specSha,bootSha;
    readonly MineruM6WriterGuard guard;
    readonly Dictionary<string,string> original=new Dictionary<string,string>(StringComparer.Ordinal);
    readonly Dictionary<string,Index> variants=new Dictionary<string,Index>(StringComparer.Ordinal);
    readonly List<Index> physicalRecords=new List<Index>();
    bool poisoned,disposed,closed;
    public long LastSequence { get; private set; }
    public long LastTick { get; private set; }
    public string LastOwnerEpoch { get; private set; }
    public long Bytes { get; private set; }
    public bool HasConflicts { get; private set; }
    public bool IsClosed { get { return closed; } }
    public int MaximumEvents { get { return maximumEvents; } }
    public int MaximumRecordBytes { get { return maximumRecord; } }
    public long MaximumLogBytes { get { return maximumBytes; } }
    string lastRecordSha;
    string originalClock;
    long originalT0,originalDeadline;

    public MineruM6Journal(Stream ownedStream,Action flushToDevice,int maximumRecordBytes,int maximumRecords,
                           long maximumLogBytes,string expectedRun,string expectedSpec,string expectedBoot,
                           MineruM6WriterGuard writerGuard) {
        if(ownedStream==null || flushToDevice==null || !ownedStream.CanRead || !ownedStream.CanWrite || !ownedStream.CanSeek ||
           maximumRecordBytes<1 || maximumRecordBytes>60000 || maximumRecords<1 || maximumRecords>1000000 ||
           maximumLogBytes<maximumRecordBytes || maximumLogBytes>1073741824 || writerGuard==null)
            throw new ArgumentException("M6 journal stream/bounds invalid");
        MineruM6OwnerWire.Shape(MineruResidentWire.Parse(MineruResidentWire.Object("run",Q(expectedRun),
            "spec",Q(expectedSpec),"boot",Q(expectedBoot)),1024),"run:id","spec:hash","boot:hash");
        // The production owner provides an exclusively held FileStream and
        // Flush(true). The injected seam exists for deterministic IO failures;
        // an in-memory test does not qualify durable Windows storage.
        stream=ownedStream; durableFlush=flushToDevice; maximumRecord=maximumRecordBytes;
        maximumEvents=maximumRecords; maximumBytes=maximumLogBytes;
        runId=expectedRun; specSha=expectedSpec; bootSha=expectedBoot;
        guard=writerGuard;
        try { Recover(); guard.VerifyRecovered(LastSequence,lastRecordSha); }
        catch { poisoned=true; throw; } // Caller owns disposal if construction fails.
    }
    static string Q(string text) { return MineruResidentWire.Quote(text); }
    static string Hash(string text) { return MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(text)); }
    static string Key(MineruJsonValue producer) {
        return producer.Get("producer_kind").String()+"\0"+producer.Get("producer_epoch_sha256").String()+"\0"+
            producer.Get("producer_sequence").Raw;
    }
    void AssertLive() {
        if(disposed || poisoned) throw new InvalidOperationException("M6 journal closed or IO outcome uncertain; reconcile raw evidence");
        if(stream.Length!=Bytes) { poisoned=true; throw new IOException("M6 journal length changed outside its owner"); }
    }
    void Recover() {
        long length=stream.Length;
        if(length>maximumBytes) throw new MineruM6JournalDamage("event_log_bound_exceeded",0,null);
        stream.Position=0;
        byte[] input=new byte[65536], line=new byte[maximumRecord+1];
        int used=0;
        while(true) {
            int read=stream.Read(input,0,input.Length);
            if(read==0) break;
            for(int i=0;i<read;i++) {
                if(input[i]==10) {
                    if(LastSequence>=maximumEvents) throw new MineruM6JournalDamage("event_log_bound_exceeded",Bytes,null);
                    string raw;
                    try {
                        raw=MineruResidentWire.Utf8.GetString(line,0,used);
                        MineruM6OwnerWire.Record(raw);
                        IndexRecord(raw,Bytes,used);
                    } catch(MineruM6JournalDamage) { throw; }
                    catch(Exception error) {
                        throw new MineruM6JournalDamage("malformed_event_record",Bytes,error);
                    }
                    Bytes+=used+1; used=0;
                } else {
                    if(used>=maximumRecord) throw new MineruM6JournalDamage("event_log_bound_exceeded",Bytes,null);
                    line[used++]=input[i];
                }
            }
        }
        if(used!=0) throw new MineruM6JournalDamage("event_log_truncated",Bytes,null);
        if(Bytes!=length) throw new MineruM6JournalDamage("event_log_length_changed",Bytes,null);
        stream.Position=Bytes;
    }
    MineruJsonValue ValidateRecord(string raw,long offset) {
        MineruM6OwnerWire.Record(raw);
        MineruJsonValue record=MineruResidentWire.Parse(raw,maximumRecord);
        MineruJsonValue producer=record.Get("event"),stamp=record.Get("stamp"),payload=producer.Get("payload");
        long sequence=stamp.Get("sequence").Integer(),tick=stamp.Get("received_qpc_ticks").Integer();
        string owner=stamp.Get("owner_process_epoch_sha256").String(),kind=payload.Get("kind").String();
        if(producer.Get("run_id").String()!=runId || producer.Get("spec_sha256").String()!=specSha ||
           stamp.Get("boot_identity_sha256").String()!=bootSha)
            throw new MineruM6JournalDamage("run_spec_or_boot_changed",offset,null);
        if(sequence!=LastSequence+1 || (LastSequence>0 && tick<LastTick) || closed)
            throw new MineruM6JournalDamage("physical_owner_order_invalid",offset,null);
        if(LastSequence==0 && kind!="run_started") throw new MineruM6JournalDamage("run_start_missing",offset,null);
        if(producer.Get("producer_kind").String()=="owner" && producer.Get("producer_epoch_sha256").String()!=owner)
            throw new MineruM6JournalDamage("owner_producer_epoch_mismatch",offset,null);
        if(kind=="run_started" || kind=="owner_resumed" || kind=="run_closed") {
            if(producer.Get("producer_kind").String()!="owner")
                throw new MineruM6JournalDamage("owner_event_role_mismatch",offset,null);
        }
        if(kind=="run_started" && (LastSequence!=0 || payload.Get("t0_ticks").Integer()!=tick))
            throw new MineruM6JournalDamage("invalid_run_start",offset,null);
        if(kind=="run_closed" && payload.Get("tclose_ticks").Integer()!=tick)
            throw new MineruM6JournalDamage("close_tick_not_owner_received",offset,null);
        if(kind=="owner_resumed" && (LastOwnerEpoch==null || owner==LastOwnerEpoch ||
            payload.Get("previous_owner_epoch_sha256").String()!=LastOwnerEpoch))
            throw new MineruM6JournalDamage("invalid_owner_resume",offset,null);
        if(kind=="owner_resumed" && (payload.Get("clock").Raw!=originalClock ||
            payload.Get("t0_ticks").Integer()!=originalT0 || payload.Get("deadline_ticks").Integer()!=originalDeadline))
            throw new MineruM6JournalDamage("original_clock_or_deadline_drift",offset,null);
        if(kind=="run_started" && (payload.Get("clock").Get("boot_identity_sha256").String()!=bootSha ||
            payload.Get("deadline_ticks").Integer()<=tick))
            throw new MineruM6JournalDamage("invalid_original_clock_or_deadline",offset,null);
        if(LastOwnerEpoch!=null && owner!=LastOwnerEpoch &&
           (kind!="owner_resumed" || payload.Get("previous_owner_epoch_sha256").String()!=LastOwnerEpoch))
            throw new MineruM6JournalDamage("owner_epoch_changed_without_resume",offset,null);
        return record;
    }
    void IndexRecord(string raw,long offset,int bytes) {
        MineruJsonValue record=ValidateRecord(raw,offset);
        MineruJsonValue producer=record.Get("event"),stamp=record.Get("stamp"),payload=producer.Get("payload");
        long sequence=stamp.Get("sequence").Integer(),tick=stamp.Get("received_qpc_ticks").Integer();
        string owner=stamp.Get("owner_process_epoch_sha256").String(),kind=payload.Get("kind").String();
        if(kind=="run_started") {
            originalClock=payload.Get("clock").Raw; originalT0=payload.Get("t0_ticks").Integer();
            originalDeadline=payload.Get("deadline_ticks").Integer();
        }
        string key=Key(producer),sha=stamp.Get("producer_event_sha256").String(),first;
        if(original.TryGetValue(key,out first)) {
            if(first!=sha) HasConflicts=true;
        } else original.Add(key,sha);
        string variant=key+"\0"+sha;
        Index index=new Index { Offset=offset, Length=bytes, Hash=Hash(raw) };
        physicalRecords.Add(index);
        if(!variants.ContainsKey(variant)) variants.Add(variant,index);
        LastSequence=sequence; LastTick=tick; LastOwnerEpoch=owner;
        lastRecordSha=Hash(raw);
        if(kind=="run_closed") closed=true;
    }
    string ReadOriginal(Index index) {
        byte[] raw=new byte[index.Length];
        stream.Position=index.Offset;
        try {
            int offset=0;
            while(offset<raw.Length) {
                int count=stream.Read(raw,offset,raw.Length-offset);
                if(count==0) throw new EndOfStreamException("M6 original stamp truncated");
                offset+=count;
            }
            string result=MineruResidentWire.Utf8.GetString(raw);
            if(Hash(result)!=index.Hash) throw new IOException("M6 original stamp bytes changed");
            return result;
        } finally { stream.Position=Bytes; }
    }
    public IEnumerable<string> ReadRecords() {
        // Reconstruct control from the same exclusively owned bytes, verified
        // against their recovered hashes, never a separately supplied snapshot.
        AssertLive();
        long count=LastSequence;
        foreach(Index record in physicalRecords) {
            AssertLive();
            if(count!=LastSequence) throw new InvalidOperationException("M6 journal changed during replay");
            yield return ReadOriginal(record);
        }
    }
    public bool HasAppendHeadroom(int reservedOwnerRecords) {
        AssertLive();
        if(reservedOwnerRecords<0) throw new ArgumentOutOfRangeException("reservedOwnerRecords");
        return LastSequence+reservedOwnerRecords+1<=maximumEvents &&
               Bytes+(long)(reservedOwnerRecords+1)*(maximumRecord+1)<=maximumBytes;
    }
    public MineruM6AppendResult Append(string producerRaw,long receivedTicks,string ownerEpoch) {
        AssertLive();
        if(producerRaw!=null && MineruResidentWire.Utf8.GetByteCount(producerRaw)>maximumRecord)
            throw new MineruM6JournalBound("producer_record_over_bound");
        MineruJsonValue producer=MineruResidentWire.Parse(producerRaw,maximumRecord);
        if(MineruM6OwnerWire.Producer(producer)!=producerRaw) throw new FormatException("M6 producer not canonical");
        string key=Key(producer),sha=Hash(producerRaw),first;
        bool conflict=original.TryGetValue(key,out first) && first!=sha;
        Index prior;
        if(variants.TryGetValue(key+"\0"+sha,out prior)) {
            try { return new MineruM6AppendResult(ReadOriginal(prior),true,conflict); }
            catch { poisoned=true; throw; }
        }
        if(closed) throw new InvalidOperationException("M6 journal already sealed by run_closed");
        string stamp=MineruResidentWire.Object("sequence",MineruResidentWire.Integer(LastSequence+1),
            "received_qpc_ticks",MineruResidentWire.Integer(receivedTicks),"boot_identity_sha256",Q(bootSha),
            "owner_process_epoch_sha256",Q(ownerEpoch),"producer_event_sha256",Q(sha));
        string raw=MineruResidentWire.Object("contract_version",Q("m6.run-event.v1"),"event",producerRaw,"stamp",stamp);
        byte[] data=MineruResidentWire.Utf8.GetBytes(raw+"\n");
        if(data.Length>maximumRecord+1) throw new MineruM6JournalBound("producer_record_over_bound");
        if(LastSequence>=maximumEvents || Bytes+data.Length>maximumBytes)
            throw new MineruM6JournalBound("event_log_bound_exhausted");
        // Validate the exact future bytes without mutating indexes/guard/log.
        // Bad callers cannot receive ACKs that this same reader later rejects.
        ValidateRecord(raw,Bytes);
        try {
            string recordSha=Hash(raw);
            guard.Prepare(LastSequence+1,recordSha);
            stream.Position=Bytes;
            stream.Write(data,0,data.Length);
            durableFlush(); // ACK and indexes are updated only after durable append.
            guard.Committed(LastSequence+1,recordSha);
            IndexRecord(raw,Bytes,data.Length-1);
            Bytes+=data.Length;
            return new MineruM6AppendResult(raw,false,conflict);
        } catch {
            // Even a complete LF followed by a failed flush is uncertain. This
            // instance can never append/ACK again. Recovery requires explicit
            // original-byte and failure-sidecar reconciliation by the owner.
            poisoned=true;
            throw;
        }
    }
    public void Dispose() {
        if(disposed) return;
        stream.Dispose();
        disposed=true;
    }
}
