// Independent M6 journal / writer-guard / private-store tests on real
// FileStreams under a disposable temp root. Part of test_mineru_m6_native_suite.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;

// Owns exclusive journal/guard FileStreams exactly the way the production host
// does, with an injectable durable-flush seam for the journal.
public sealed class MineruM6JournalRig : IDisposable {
    public readonly string Dir, EventsPath, GuardPath;
    public FileStream Events, GuardStream;
    public MineruM6WriterGuard Guard;
    public MineruM6Journal Journal;
    public bool FailNextJournalFlush;
    public int JournalFlushes;
    public static string Run { get { return MineruResidentWire.Parse(MineruM6NativeSuite.FS("service", "spec"), 65536).Get("run_id").String(); } }
    public static string Spec { get { return MineruM6NativeSuite.FS("identities", "spec_sha256"); } }
    public static string Boot { get { return MineruM6NativeSuite.FS("identities", "boot_identity_sha256"); } }
    public static string Owner { get { return MineruM6NativeSuite.FS("identities", "owner_epoch"); } }
    public static string Owner2 { get { return MineruM6NativeSuite.FS("identities", "owner_epoch_2"); } }

    public MineruM6JournalRig(string dir, bool resume, int maxRecord, int maxEvents, long maxLog) {
        Dir = dir; EventsPath = Path.Combine(dir, "events.jsonl"); GuardPath = Path.Combine(dir, "writer-guard.json");
        Open(resume, maxRecord, maxEvents, maxLog);
    }
    public MineruM6JournalRig(string dir, bool resume) : this(dir, resume, 16384, 1000, 1000000) {}
    void Open(bool resume, int maxRecord, int maxEvents, long maxLog) {
        Events = new FileStream(EventsPath, resume ? FileMode.Open : FileMode.CreateNew, FileAccess.ReadWrite, FileShare.Read);
        try {
            GuardStream = new FileStream(GuardPath, resume ? FileMode.Open : FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None);
        } catch { Events.Dispose(); throw; }
        try {
            Guard = new MineruM6WriterGuard(GuardStream, delegate { GuardStream.Flush(true); }, Run, Spec);
            Journal = new MineruM6Journal(Events, delegate {
                JournalFlushes++;
                if (FailNextJournalFlush) { FailNextJournalFlush = false; throw new IOException("injected flush failure"); }
                Events.Flush(true);
            }, maxRecord, maxEvents, maxLog, Run, Spec, Boot, Guard);
        } catch { Events.Dispose(); GuardStream.Dispose(); throw; }
    }
    public void Dispose() {
        if (Journal != null) Journal.Dispose(); else if (Events != null) Events.Dispose();
        if (Guard != null) Guard.Dispose(); else if (GuardStream != null) GuardStream.Dispose();
        Journal = null; Guard = null; Events = null; GuardStream = null;
    }
    // Fixture record i: producer bytes, its stamp tick and owner epoch.
    public static string ProducerRaw(int i) { return MineruM6NativeSuite.F("journal", "producer_events").Item(i).String(); }
    public static string RecordRaw(int i) { return MineruM6NativeSuite.F("journal", "records").Item(i).String(); }
    public static long RecordTick(int i) { return MineruResidentWire.Parse(RecordRaw(i), 65536).Get("stamp").Get("received_qpc_ticks").Integer(); }
    public static string RecordOwner(int i) { return MineruResidentWire.Parse(RecordRaw(i), 65536).Get("stamp").Get("owner_process_epoch_sha256").String(); }
    public MineruM6AppendResult AppendFixture(int i) { return Journal.Append(ProducerRaw(i), RecordTick(i), RecordOwner(i)); }
    public static string Producer(string kind, string epoch, long sequence, string payloadRaw) {
        return MineruResidentWire.Object("contract_version", "\"m6.producer-event.v1\"", "run_id", MineruResidentWire.Quote(Run),
            "spec_sha256", MineruResidentWire.Quote(Spec), "producer_kind", MineruResidentWire.Quote(kind),
            "producer_epoch_sha256", MineruResidentWire.Quote(epoch), "producer_sequence", sequence.ToString(CultureInfo.InvariantCulture),
            "payload", payloadRaw);
    }
    public static byte[] ReadAll(string path) {
        using (FileStream f = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        using (MemoryStream m = new MemoryStream()) { f.CopyTo(m); return m.ToArray(); }
    }
    public static void AppendRaw(string path, byte[] bytes) {
        using (FileStream f = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { f.Position = f.Length; f.Write(bytes, 0, bytes.Length); f.Flush(true); }
    }
}

public static class MineruM6JournalTests {
    static byte[] B(string s) { return MineruM6NativeSuite.Utf8.GetBytes(s); }

    public static void Test01_FirstAppendProducesExactPythonRecordAndCleanGuard() {
        string dir = MineruM6NativeSuite.NewTempDir("journal");
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false)) {
            MineruM6AppendResult result = rig.AppendFixture(0);
            MineruM6NativeSuite.Check(!result.Duplicate && !result.Conflict, "first append is original");
            MineruM6NativeSuite.Equal(MineruM6JournalRig.RecordRaw(0), result.Record, "native stamp equals Python-built record bytes");
            MineruM6NativeSuite.Equal(1, rig.JournalFlushes, "one durable flush per append");
            MineruM6NativeSuite.Equal(1, rig.Journal.LastSequence, "sequence");
            MineruM6NativeSuite.Equal(MineruM6JournalRig.RecordTick(0), rig.Journal.LastTick, "tick");
        }
        byte[] expected = B(MineruM6JournalRig.RecordRaw(0) + "\n");
        MineruM6NativeSuite.EqualBytes(expected, MineruM6JournalRig.ReadAll(Path.Combine(dir, "events.jsonl")), "journal file bytes");
        MineruJsonValue guard = MineruResidentWire.Parse(MineruM6NativeSuite.Utf8.GetString(MineruM6JournalRig.ReadAll(Path.Combine(dir, "writer-guard.json"))), 4096);
        MineruM6NativeSuite.Equal("clean", guard.Get("state").String(), "guard state after commit");
        MineruM6NativeSuite.Equal(1, guard.Get("sequence").Integer(), "guard sequence");
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.Sha(MineruM6JournalRig.RecordRaw(0)), guard.Get("record_sha256").String(), "guard binds exact record hash");
    }

    public static void Test02_ExactRetryReturnsOriginalStampWithoutWriting() {
        string dir = MineruM6NativeSuite.NewTempDir("journal");
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false)) {
            rig.AppendFixture(0);
            long length = new FileInfo(rig.EventsPath).Length;
            MineruM6AppendResult retry = rig.Journal.Append(MineruM6JournalRig.ProducerRaw(0), MineruM6JournalRig.RecordTick(0) + 999, MineruM6JournalRig.Owner);
            MineruM6NativeSuite.Check(retry.Duplicate && !retry.Conflict, "retry flagged duplicate");
            MineruM6NativeSuite.Equal(MineruM6JournalRig.RecordRaw(0), retry.Record, "retry returns the original stamp, not a new tick");
            MineruM6NativeSuite.Equal(length, new FileInfo(rig.EventsPath).Length, "no bytes written for a retry");
            MineruM6NativeSuite.Equal(1, rig.JournalFlushes, "no flush for a retry");
        }
    }

    public static void Test03_ChangedBytesUnderSameProducerKeyAreRetainedAsConflict() {
        string dir = MineruM6NativeSuite.NewTempDir("journal");
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false)) {
            rig.AppendFixture(0); rig.AppendFixture(1);
            string conflicting = MineruM6JournalRig.Producer("owner", MineruM6JournalRig.Owner, 2, "{\"kind\":\"stop_admission_requested\"}");
            MineruM6NativeSuite.Check(conflicting != MineruM6JournalRig.ProducerRaw(1), "conflict input differs");
            MineruM6AppendResult result = rig.Journal.Append(conflicting, MineruM6JournalRig.RecordTick(1) + 5, MineruM6JournalRig.Owner);
            MineruM6NativeSuite.Check(result.Conflict && !result.Duplicate, "conflict retained as a new original record");
            MineruM6NativeSuite.Equal(3, rig.Journal.LastSequence, "conflict consumed owner sequence 3");
            MineruM6NativeSuite.Check(rig.Journal.HasConflicts, "journal reports conflicts");
            MineruJsonValue stamped = MineruResidentWire.Parse(result.Record, 65536);
            MineruM6NativeSuite.Equal(conflicting, stamped.Get("event").Raw, "conflict reply carries the submitted bytes");
            MineruM6AppendResult again = rig.Journal.Append(conflicting, 0, MineruM6JournalRig.Owner);
            MineruM6NativeSuite.Check(again.Duplicate && again.Conflict, "exact retry of the conflicting variant is a duplicate that still reports conflict");
        }
        using (MineruM6JournalRig reopened = new MineruM6JournalRig(dir, true)) {
            MineruM6NativeSuite.Check(reopened.Journal.HasConflicts, "conflict visible after recovery");
            MineruM6NativeSuite.Equal(3, reopened.Journal.LastSequence, "all three physical records recovered");
            int count = 0; foreach (string raw in reopened.Journal.ReadRecords()) { MineruM6OwnerWire.Record(raw); count++; }
            MineruM6NativeSuite.Equal(3, count, "ReadRecords yields every physical record");
        }
    }

    public static void Test04_FailedDurableFlushPoisonsAndStaysDirtyOnRecovery() {
        string dir = MineruM6NativeSuite.NewTempDir("journal");
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false)) {
            rig.AppendFixture(0);
            rig.FailNextJournalFlush = true;
            MineruM6NativeSuite.Throws<IOException>(delegate { rig.AppendFixture(1); }, "append with failed flush");
            MineruM6NativeSuite.Equal(1, rig.Journal.LastSequence, "no ACK/index for the uncertain record");
            MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { rig.AppendFixture(2); }, "instance poisoned after uncertain IO");
            MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { foreach (string r in rig.Journal.ReadRecords()) { } }, "replay refused after uncertain IO");
        }
        MineruJsonValue guard = MineruResidentWire.Parse(MineruM6NativeSuite.Utf8.GetString(MineruM6JournalRig.ReadAll(Path.Combine(dir, "writer-guard.json"))), 4096);
        MineruM6NativeSuite.Equal("dirty", guard.Get("state").String(), "guard stays dirty");
        MineruM6JournalDamage damage = MineruM6NativeSuite.Throws<MineruM6JournalDamage>(delegate { new MineruM6JournalRig(dir, true).Dispose(); },
            "complete LF after failed flush must not recover clean");
        MineruM6NativeSuite.Equal("writer_guard_uncertain", damage.Code, "damage code");
    }

    public static void Test05_TornTailIsPreservedAndNeverRepaired() {
        string dir = MineruM6NativeSuite.NewTempDir("journal");
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false)) { rig.AppendFixture(0); rig.AppendFixture(1); }
        byte[] before = MineruM6JournalRig.ReadAll(Path.Combine(dir, "events.jsonl"));
        byte[] tail = B(MineruM6JournalRig.RecordRaw(2).Substring(0, 40));
        MineruM6JournalRig.AppendRaw(Path.Combine(dir, "events.jsonl"), tail);
        byte[] guardBefore = MineruM6JournalRig.ReadAll(Path.Combine(dir, "writer-guard.json"));
        MineruM6JournalDamage damage = MineruM6NativeSuite.Throws<MineruM6JournalDamage>(delegate { new MineruM6JournalRig(dir, true).Dispose(); }, "torn tail");
        MineruM6NativeSuite.Equal("event_log_truncated", damage.Code, "damage code");
        MineruM6NativeSuite.Equal(before.Length, damage.Offset, "damage offset is the exact valid prefix length");
        byte[] after = MineruM6JournalRig.ReadAll(Path.Combine(dir, "events.jsonl"));
        MineruM6NativeSuite.Equal(before.Length + tail.Length, after.Length, "torn bytes retained, not truncated");
        MineruM6NativeSuite.EqualBytes(guardBefore, MineruM6JournalRig.ReadAll(Path.Combine(dir, "writer-guard.json")), "guard untouched by failed recovery");
    }

    public static void Test06_PythonNegativeRecordsRefusedAtJournalLayer() {
        MineruJsonValue negatives = MineruM6NativeSuite.F("journal", "negatives");
        int checkedCount = 0;
        for (int i = 0; i < negatives.Count; i++) {
            MineruJsonValue item = negatives.Item(i);
            if (item.Get("layer").String() != "journal") continue;
            string name = item.Get("name").String(), record = item.Get("record").String(), expect = item.Get("expect").String();
            int prefix = (int)item.Get("after_prefix").Integer();
            string dir = MineruM6NativeSuite.NewTempDir("neg-" + i);
            long prefixBytes = 0;
            using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false)) {
                for (int p = 0; p < prefix; p++) rig.AppendFixture(p);
                prefixBytes = rig.Journal.Bytes;
            }
            MineruM6JournalRig.AppendRaw(Path.Combine(dir, "events.jsonl"), B(record + "\n"));
            byte[] before = MineruM6JournalRig.ReadAll(Path.Combine(dir, "events.jsonl"));
            MineruM6JournalDamage damage = MineruM6NativeSuite.Throws<MineruM6JournalDamage>(delegate { new MineruM6JournalRig(dir, true).Dispose(); }, "negative " + name);
            MineruM6NativeSuite.Equal(expect, damage.Code, "negative " + name + " code");
            MineruM6NativeSuite.Equal(prefixBytes, damage.Offset, "negative " + name + " offset");
            MineruM6NativeSuite.EqualBytes(before, MineruM6JournalRig.ReadAll(Path.Combine(dir, "events.jsonl")), "negative " + name + " file unchanged");
            checkedCount++;
        }
        MineruM6NativeSuite.Check(checkedCount >= 4, "journal-layer negatives present in fixture");
    }

    public static void Test07_EventBoundAndReservedHeadroom() {
        string dir = MineruM6NativeSuite.NewTempDir("journal");
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false, 16384, 3, 1000000)) {
            rig.AppendFixture(0); rig.AppendFixture(1); rig.AppendFixture(2);
            MineruM6NativeSuite.Check(!rig.Journal.HasAppendHeadroom(0), "no headroom at the record bound");
            MineruM6JournalBound bound = MineruM6NativeSuite.Throws<MineruM6JournalBound>(delegate { rig.AppendFixture(3); }, "fourth append");
            MineruM6NativeSuite.Equal("event_log_bound_exhausted", bound.Code, "bound code");
            int count = 0; foreach (string r in rig.Journal.ReadRecords()) count++;
            MineruM6NativeSuite.Equal(3, count, "bound exhaustion does not poison reading");
            MineruM6NativeSuite.Check(rig.AppendFixture(0).Duplicate, "exact retry still served at the bound");
        }
        int maxRecord = 2000;
        long firstBytes = B(MineruM6JournalRig.RecordRaw(0)).Length + 1;
        MineruM6NativeSuite.Check(firstBytes <= maxRecord, "fixture record fits the test bound");
        string dir2 = MineruM6NativeSuite.NewTempDir("journal");
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir2, false, maxRecord, 1000, firstBytes + 3 * (maxRecord + 1))) {
            MineruM6NativeSuite.Check(rig.Journal.HasAppendHeadroom(2), "three worst-case slots fit before the first record");
            rig.AppendFixture(0);
            MineruM6NativeSuite.Check(rig.Journal.HasAppendHeadroom(2), "three worst-case slots remain");
            MineruM6NativeSuite.Check(!rig.Journal.HasAppendHeadroom(3), "a fourth worst-case slot does not fit");
            MineruM6NativeSuite.Throws<ArgumentOutOfRangeException>(delegate { rig.Journal.HasAppendHeadroom(-1); }, "negative reserve");
        }
    }

    public static void Test08_LengthChangedOutsideOwnerPoisons() {
        MemoryStream events = new MemoryStream(), guardStream = new MemoryStream();
        MineruM6WriterGuard guard = new MineruM6WriterGuard(guardStream, delegate { }, MineruM6JournalRig.Run, MineruM6JournalRig.Spec);
        MineruM6Journal journal = new MineruM6Journal(events, delegate { }, 16384, 100, 100000, MineruM6JournalRig.Run, MineruM6JournalRig.Spec, MineruM6JournalRig.Boot, guard);
        journal.Append(MineruM6JournalRig.ProducerRaw(0), MineruM6JournalRig.RecordTick(0), MineruM6JournalRig.Owner);
        events.SetLength(events.Length + 1);
        MineruM6NativeSuite.Throws<IOException>(delegate { journal.HasAppendHeadroom(0); }, "external length change detected");
        MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { journal.Append(MineruM6JournalRig.ProducerRaw(1), MineruM6JournalRig.RecordTick(1), MineruM6JournalRig.Owner); }, "poisoned after external change");
        journal.Dispose(); guard.Dispose();
    }

    public static void Test09_SealedByRunClosedRefusesNewStampsButServesExactRetries() {
        string dir = MineruM6NativeSuite.NewTempDir("journal");
        long closeTick = MineruM6JournalRig.RecordTick(0) + 100;
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false)) {
            rig.AppendFixture(0);
            string closed = MineruM6JournalRig.Producer("owner", MineruM6JournalRig.Owner, 2,
                "{\"kind\":\"run_closed\",\"reason\":\"stop_requested\",\"tclose_ticks\":" + closeTick.ToString(CultureInfo.InvariantCulture) + "}");
            MineruM6NativeSuite.Throws<MineruM6JournalDamage>(delegate { rig.Journal.Append(closed, closeTick + 1, MineruM6JournalRig.Owner); },
                "run_closed whose tclose is not the owner receipt tick");
            MineruM6NativeSuite.Equal(1, rig.Journal.LastSequence, "refused close not written");
            rig.Journal.Append(closed, closeTick, MineruM6JournalRig.Owner);
            MineruM6NativeSuite.Check(rig.Journal.IsClosed, "sealed");
            MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { rig.AppendFixture(1); }, "new stamp after close");
            MineruM6NativeSuite.Check(rig.AppendFixture(0).Duplicate, "exact retry after close returns the original stamp");
        }
        using (MineruM6JournalRig reopened = new MineruM6JournalRig(dir, true))
            MineruM6NativeSuite.Check(reopened.Journal.IsClosed && reopened.Journal.LastSequence == 2, "closure recovered");
    }

    public static void Test10_OverBoundProducerRefusedWithoutPoison() {
        string dir = MineruM6NativeSuite.NewTempDir("journal");
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false, 2000, 100, 100000)) {
            string huge = "{\"x\":\"" + new string('a', 2100) + "\"}";
            MineruM6JournalBound bound = MineruM6NativeSuite.Throws<MineruM6JournalBound>(delegate { rig.Journal.Append(huge, 1, MineruM6JournalRig.Owner); }, "over-bound producer");
            MineruM6NativeSuite.Equal("producer_record_over_bound", bound.Code, "bound code");
            MineruM6NativeSuite.Throws<FormatException>(delegate { rig.Journal.Append("{\"x\":1}", 1, MineruM6JournalRig.Owner); }, "non-producer JSON");
            MineruM6NativeSuite.Check(!rig.AppendFixture(0).Duplicate, "journal still usable after refusals");
        }
    }

    public static void Test11_SameBootResumeChainMatchesPythonAndRefusesDrift() {
        string dir = MineruM6NativeSuite.NewTempDir("journal");
        string resumedRecord = MineruM6NativeSuite.FS("journal", "resumed_record");
        MineruJsonValue resumed = MineruResidentWire.Parse(resumedRecord, 65536);
        string resumedProducer = resumed.Get("event").Raw;
        long resumedTick = resumed.Get("stamp").Get("received_qpc_ticks").Integer();
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, false)) { for (int i = 0; i < 5; i++) rig.AppendFixture(i); }
        using (MineruM6JournalRig rig = new MineruM6JournalRig(dir, true)) {
            MineruM6NativeSuite.Equal(5, rig.Journal.LastSequence, "five records recovered");
            MineruM6NativeSuite.Equal(MineruM6JournalRig.Owner, rig.Journal.LastOwnerEpoch, "predecessor epoch recovered");
            long before = rig.Journal.Bytes;
            // Wrong stamp epoch for an owner producer: refused before any write.
            MineruM6NativeSuite.Throws<MineruM6JournalDamage>(delegate { rig.Journal.Append(resumedProducer, resumedTick, MineruM6JournalRig.Owner); }, "resume stamped by old epoch");
            // Resume naming a different predecessor: refused before any write.
            string wrongPrevious = resumedProducer.Replace(MineruM6JournalRig.Owner, MineruM6NativeSuite.LabelHash("someone-else"));
            MineruM6NativeSuite.Check(wrongPrevious != resumedProducer, "predecessor edit applied");
            MineruM6NativeSuite.Throws<MineruM6JournalDamage>(delegate { rig.Journal.Append(wrongPrevious, resumedTick, MineruM6JournalRig.Owner2); }, "resume naming wrong predecessor");
            // New epoch without an owner_resumed record: refused.
            string silent = MineruM6JournalRig.Producer("owner", MineruM6JournalRig.Owner2, 1, "{\"kind\":\"admission_opened\"}");
            MineruM6NativeSuite.Throws<MineruM6JournalDamage>(delegate { rig.Journal.Append(silent, resumedTick, MineruM6JournalRig.Owner2); }, "epoch change without resume");
            MineruM6NativeSuite.Equal(before, rig.Journal.Bytes, "refusals wrote nothing");
            MineruM6NativeSuite.Equal(5, rig.Journal.LastSequence, "sequence unchanged");
            MineruM6AppendResult ok = rig.Journal.Append(resumedProducer, resumedTick, MineruM6JournalRig.Owner2);
            MineruM6NativeSuite.Equal(resumedRecord, ok.Record, "native owner_resumed stamp equals Python-built record");
            MineruM6NativeSuite.Equal(MineruM6JournalRig.Owner2, rig.Journal.LastOwnerEpoch, "epoch advanced");
            MineruM6NativeSuite.Check(rig.AppendFixture(2).Duplicate, "old producer retry returns its predecessor-owner stamp");
            MineruM6NativeSuite.Equal(MineruM6JournalRig.RecordRaw(2), rig.AppendFixture(2).Record, "predecessor stamp bytes unchanged");
        }
    }

    public static void Test12_WriterGuardStandaloneInvariants() {
        MemoryStream stream = new MemoryStream();
        MineruM6WriterGuard guard = new MineruM6WriterGuard(stream, delegate { }, MineruM6JournalRig.Run, MineruM6JournalRig.Spec);
        guard.VerifyRecovered(0, null);
        MineruM6JournalDamage d = MineruM6NativeSuite.Throws<MineruM6JournalDamage>(delegate { guard.VerifyRecovered(1, MineruM6NativeSuite.LabelHash("r")); }, "clean guard with wrong sequence");
        MineruM6NativeSuite.Equal("writer_guard_uncertain", d.Code, "damage code");
        MineruM6NativeSuite.Throws<IOException>(delegate { guard.Prepare(2, MineruM6NativeSuite.LabelHash("r")); }, "prepare skipping a sequence");
        string sha = MineruM6NativeSuite.LabelHash("record-1");
        guard.Prepare(1, sha);
        MineruM6NativeSuite.Throws<IOException>(delegate { guard.Prepare(2, sha); }, "prepare while dirty");
        MineruM6NativeSuite.Throws<IOException>(delegate { guard.Committed(1, MineruM6NativeSuite.LabelHash("other")); }, "commit with different hash");
        guard.Committed(1, sha);
        MineruJsonValue onDisk = MineruResidentWire.Parse(MineruM6NativeSuite.Utf8.GetString(stream.ToArray()), 4096);
        MineruM6NativeSuite.Equal("clean", onDisk.Get("state").String(), "clean after commit");
        // Identity: a guard file for another run must be refused on open.
        MemoryStream copy = new MemoryStream(stream.ToArray());
        MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6WriterGuard(copy, delegate { }, "run-other", MineruM6JournalRig.Spec); }, "guard run identity differs");
        MemoryStream copy2 = new MemoryStream(stream.ToArray());
        MineruM6WriterGuard reopened = new MineruM6WriterGuard(copy2, delegate { }, MineruM6JournalRig.Run, MineruM6JournalRig.Spec);
        reopened.VerifyRecovered(1, sha);
        MemoryStream failing = new MemoryStream();
        bool fail = false;
        MineruM6WriterGuard poisoned = new MineruM6WriterGuard(failing, delegate { if (fail) throw new IOException("injected"); }, MineruM6JournalRig.Run, MineruM6JournalRig.Spec);
        fail = true;
        MineruM6NativeSuite.Throws<IOException>(delegate { poisoned.Prepare(1, sha); }, "guard flush failure");
        MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { poisoned.VerifyRecovered(0, null); }, "guard poisoned after uncertain IO");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6WriterGuard(new MemoryStream(new byte[4], false), delegate { }, MineruM6JournalRig.Run, MineruM6JournalRig.Spec); }, "read-only stream refused");
    }

    public static void Test13_JournalConstructionBounds() {
        MemoryStream events = new MemoryStream(), guardStream = new MemoryStream();
        MineruM6WriterGuard guard = new MineruM6WriterGuard(guardStream, delegate { }, MineruM6JournalRig.Run, MineruM6JournalRig.Spec);
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Journal(events, delegate { }, 60001, 10, 100000, MineruM6JournalRig.Run, MineruM6JournalRig.Spec, MineruM6JournalRig.Boot, guard); }, "record bound above 60000");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Journal(events, delegate { }, 100, 10, 99, MineruM6JournalRig.Run, MineruM6JournalRig.Spec, MineruM6JournalRig.Boot, guard); }, "log below record");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Journal(events, delegate { }, 100, 1000001, 100000, MineruM6JournalRig.Run, MineruM6JournalRig.Spec, MineruM6JournalRig.Boot, guard); }, "events above 1,000,000");
        MineruM6NativeSuite.Throws<FormatException>(delegate { new MineruM6Journal(events, delegate { }, 100, 10, 1000, "bad run", MineruM6JournalRig.Spec, MineruM6JournalRig.Boot, guard); }, "run id with space");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Journal(events, delegate { }, 100, 10, 1000, MineruM6JournalRig.Run, MineruM6JournalRig.Spec, MineruM6JournalRig.Boot, null); }, "guard required");
    }
}

public static class MineruM6StoreTests {
    static byte[] B(string s) { return MineruM6NativeSuite.Utf8.GetBytes(s); }
    public static string NewPrivateRun(string label) {
        string parent = MineruM6NativeSuite.NewTempDir(label);
        string path = Path.Combine(parent, "run");
        MineruM6PrivateStore.CreatePrivateDirectory(path);
        return path;
    }
    static void AssertPrivateAcl(string path) {
        DirectorySecurity security = Directory.GetAccessControl(path, AccessControlSections.Access | AccessControlSections.Owner);
        using (WindowsIdentity me = WindowsIdentity.GetCurrent()) {
            MineruM6NativeSuite.Check(security.AreAccessRulesProtected, "DACL protected from inheritance");
            MineruM6NativeSuite.Check(me.User.Equals(security.GetOwner(typeof(SecurityIdentifier))), "owner is the current user");
            SecurityIdentifier system = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
            int allows = 0;
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier))) {
                if (rule.AccessControlType != AccessControlType.Allow) continue;
                allows++;
                MineruM6NativeSuite.Check(rule.IdentityReference.Equals(me.User) || rule.IdentityReference.Equals(system), "allow rule only for user/SYSTEM: " + rule.IdentityReference);
            }
            MineruM6NativeSuite.Check(allows >= 1, "at least the owner allow rule exists");
        }
    }

    public static void Test01_CreatePrivateDirectoryProducesOwnerOnlyProtectedAcl() {
        string path = NewPrivateRun("store");
        AssertPrivateAcl(path);
        MineruM6NativeSuite.Throws<IOException>(delegate { MineruM6PrivateStore.CreatePrivateDirectory(path); }, "existing directory refused");
        MineruM6NativeSuite.Throws<IOException>(delegate { MineruM6PrivateStore.CreatePrivateDirectory(Path.Combine(path, "a", "b")); }, "missing parent refused");
        MineruM6NativeSuite.Throws<IOException>(delegate { MineruM6PrivateStore.CreatePrivateDirectory("relative\\dir"); }, "relative path refused");
    }

    public static void Test02_InheritedAclDirectoryIsRefused() {
        string plain = Path.Combine(MineruM6NativeSuite.NewTempDir("store"), "plain");
        Directory.CreateDirectory(plain);
        MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6PrivateStore(plain, false, 8, 65536).Dispose(); }, "inherited ACL directory");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6PrivateStore(plain, false, 7, 65536).Dispose(); }, "artifact bound below 8");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6PrivateStore(plain, false, 8, 65535).Dispose(); }, "byte bound below 64 KiB");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6PrivateStore("\\\\server\\share\\x", false, 8, 65536).Dispose(); }, "UNC path refused");
    }

    public static void Test03_ImmutableArtifactsAndBudgets() {
        string path = NewPrivateRun("store");
        using (MineruM6PrivateStore store = new MineruM6PrivateStore(path, false, 8, 65536)) {
            MineruM6NativeSuite.Equal(8, store.RemainingArtifactCount, "initial count");
            MineruM6NativeSuite.Equal(65536, store.RemainingArtifactBytes, "initial bytes");
            MineruM6NativeSuite.Check(store.Read("spec.json") == null, "absent artifact reads null");
            byte[] anchor = B("{\"a\":1}");
            store.WriteImmutable("anchor.json", anchor);
            MineruM6NativeSuite.EqualBytes(anchor, MineruM6JournalRig.ReadAll(Path.Combine(path, "anchor.json")), "anchor bytes on disk");
            MineruM6NativeSuite.Equal(7, store.RemainingArtifactCount, "one artifact counted");
            MineruM6NativeSuite.Equal(65536 - anchor.Length, store.RemainingArtifactBytes, "bytes counted");
            store.WriteImmutable("anchor.json", anchor);
            MineruM6NativeSuite.Equal(7, store.RemainingArtifactCount, "identical rewrite is a no-op");
            MineruM6NativeSuite.Throws<IOException>(delegate { store.WriteImmutable("anchor.json", B("{\"a\":2}")); }, "changed immutable bytes");
            MineruM6NativeSuite.EqualBytes(anchor, MineruM6JournalRig.ReadAll(Path.Combine(path, "anchor.json")), "original bytes intact");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { store.WriteImmutable("notes.txt", anchor); }, "unknown artifact name");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { store.WriteImmutable("anchor.json.pending-" + new string('0', 32), anchor); }, "pending name refused");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { store.WriteImmutable("spec.json", new byte[65537]); }, "over 64 KiB");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { store.Read("..\\anchor.json"); }, "path traversal name");
            string[] leftovers = Directory.GetFiles(path, "*.pending-*");
            MineruM6NativeSuite.Equal(0, leftovers.Length, "no pending temp files after publication");
            // Budget exhaustion: 7 remaining; a diagnostic with body consumes two.
            store.Diagnostic("code_one", B("body"));
            MineruM6NativeSuite.Equal(5, store.RemainingArtifactCount, "body diagnostic uses .bin and .json");
            store.Diagnostic("code_two", null); store.Diagnostic("code_three", null); store.Diagnostic("code_four", null);
            store.Diagnostic("code_five", null); store.Diagnostic("code_six", null);
            MineruM6NativeSuite.Equal(0, store.RemainingArtifactCount, "exhausted");
            MineruM6NativeSuite.Throws<IOException>(delegate { store.Diagnostic("code_seven", null); }, "budget exhausted propagates");
        }
    }

    public static void Test04_OwnerLockIsExclusiveAndResumeSemantics() {
        string path = NewPrivateRun("store");
        MineruM6PrivateStore first = new MineruM6PrivateStore(path, false, 8, 65536);
        MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6PrivateStore(path, true, 8, 65536).Dispose(); }, "second owner while lock held");
        first.Dispose();
        MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6PrivateStore(path, false, 8, 65536).Dispose(); }, "fresh (non-resume) open on an existing run");
        using (MineruM6PrivateStore resumed = new MineruM6PrivateStore(path, true, 8, 65536))
            MineruM6NativeSuite.Equal(8, resumed.RemainingArtifactCount, "resume with no artifacts");
        MineruM6NativeSuite.Throws<ObjectDisposedException>(delegate { first.Read("anchor.json"); }, "disposed store");
    }

    public static void Test05_ResumeRefusesPendingUnknownAndNestedEntries() {
        string path = NewPrivateRun("store");
        using (MineruM6PrivateStore store = new MineruM6PrivateStore(path, false, 8, 65536)) store.WriteImmutable("anchor.json", B("{}"));
        string pending = Path.Combine(path, "spec.json.pending-" + new string('a', 32));
        File.WriteAllBytes(pending, B("{}"));
        MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6PrivateStore(path, true, 8, 65536).Dispose(); }, "pending artifact requires reconciliation");
        File.Delete(pending);
        File.WriteAllBytes(Path.Combine(path, "stray.txt"), B("x"));
        MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6PrivateStore(path, true, 8, 65536).Dispose(); }, "unknown artifact name");
        File.Delete(Path.Combine(path, "stray.txt"));
        Directory.CreateDirectory(Path.Combine(path, "nested"));
        MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6PrivateStore(path, true, 8, 65536).Dispose(); }, "nested directory");
        Directory.Delete(Path.Combine(path, "nested"));
        File.WriteAllBytes(Path.Combine(path, "spec.json"), new byte[65537]);
        MineruM6NativeSuite.Throws<IOException>(delegate { new MineruM6PrivateStore(path, true, 8, 65536).Dispose(); }, "existing over-bound artifact");
        File.Delete(Path.Combine(path, "spec.json"));
        using (MineruM6PrivateStore store = new MineruM6PrivateStore(path, true, 8, 65536)) {
            MineruM6NativeSuite.Equal(7, store.RemainingArtifactCount, "existing artifacts are counted on resume");
            MineruM6NativeSuite.EqualBytes(B("{}"), store.Read("anchor.json"), "existing artifact readable");
        }
    }

    public static void Test06_ReceiptsAndControlSidecars() {
        string path = NewPrivateRun("store");
        using (MineruM6PrivateStore store = new MineruM6PrivateStore(path, false, 16, 65536)) {
            string receipt = "{\"r\":1}"; string sha = MineruM6NativeSuite.Sha(receipt);
            MineruM6NativeSuite.Check(store.ReadReceipt(sha) == null, "missing receipt is null");
            store.WriteImmutable("receipt-" + sha.Substring(7) + ".json", B(receipt));
            MineruM6NativeSuite.Equal(receipt, store.ReadReceipt(sha), "receipt read back");
            string lying = MineruM6NativeSuite.LabelHash("lying");
            File.WriteAllBytes(Path.Combine(path, "receipt-" + lying.Substring(7) + ".json"), B("{\"r\":2}"));
            MineruM6NativeSuite.Throws<IOException>(delegate { store.ReadReceipt(lying); }, "receipt content hash differs from its name");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { store.ReadReceipt("sha256:zz"); }, "receipt sha shape");
            store.WriteControl("admission-closed", "{\"kind\":\"admission_closed\"}");
            MineruM6NativeSuite.Equal("{\"kind\":\"admission_closed\"}", store.ReadControl("admission-closed"), "control sidecar");
            MineruM6NativeSuite.Check(store.ReadControl("resources-closed") == null, "absent control sidecar");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { store.WriteControl("anchor", "{}"); }, "control kind closed set");
            MineruM6NativeSuite.Throws<IOException>(delegate { store.WriteControl("admission-closed", "{\"kind\":\"other\"}"); }, "control sidecar immutable");
        }
    }

    public static void Test07_DiagnosticArtifactsBindBodyHash() {
        string path = NewPrivateRun("store");
        using (MineruM6PrivateStore store = new MineruM6PrivateStore(path, false, 16, 65536)) {
            byte[] body = B("{\"raw\":\"request\"}");
            store.Diagnostic("request_shape_invalid", body);
            store.Diagnostic("peer_closed", null);
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { store.Diagnostic("Bad-Code", null); }, "code shape");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { store.Diagnostic("ok", new byte[65537]); }, "body bound");
        }
        int withBody = 0, without = 0;
        foreach (string file in Directory.GetFiles(path, "diagnostic-*.json")) {
            MineruJsonValue value = MineruResidentWire.Parse(MineruM6NativeSuite.Utf8.GetString(MineruM6JournalRig.ReadAll(file)), 4096);
            value.Keys("body_sha256", "code", "contract_version");
            MineruM6NativeSuite.Equal("m6.transport-diagnostic.v1", value.Get("contract_version").String(), "diagnostic version");
            if (value.Get("body_sha256").Raw == "null") { without++; MineruM6NativeSuite.Equal("peer_closed", value.Get("code").String(), "no-body code"); continue; }
            withBody++;
            string bin = file.Substring(0, file.Length - 5) + ".bin";
            MineruM6NativeSuite.Check(File.Exists(bin), "body sidecar exists");
            MineruM6NativeSuite.Equal(value.Get("body_sha256").String(), MineruM6NativeSuite.Sha(MineruM6JournalRig.ReadAll(bin)), "body hash binds sidecar bytes");
            MineruM6NativeSuite.Equal("request_shape_invalid", value.Get("code").String(), "body code");
        }
        MineruM6NativeSuite.Equal(1, withBody, "one body diagnostic"); MineruM6NativeSuite.Equal(1, without, "one no-body diagnostic");
    }

    public static void Test08_JournalFileSharingDeniesWritersAndDeletion() {
        string path = NewPrivateRun("store");
        using (MineruM6PrivateStore store = new MineruM6PrivateStore(path, false, 8, 65536))
        using (FileStream events = store.OpenJournalFile("events.jsonl", false))
        using (FileStream guard = store.OpenJournalFile("writer-guard.json", false)) {
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { store.OpenJournalFile("anchor.json", false); }, "journal name closed set");
            using (FileStream reader = new FileStream(events.Name, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                MineruM6NativeSuite.Equal(0, reader.Length, "read-only ReadWrite-sharing reader coexists with the writer");
            MineruM6NativeSuite.ThrowsAny(delegate { new FileStream(events.Name, FileMode.Open, FileAccess.ReadWrite, FileShare.ReadWrite).Dispose(); }, "second writer denied");
            MineruM6NativeSuite.ThrowsAny(delegate { File.Delete(events.Name); }, "deletion denied while owned");
            MineruM6NativeSuite.ThrowsAny(delegate { new FileStream(guard.Name, FileMode.Open, FileAccess.Read, FileShare.ReadWrite).Dispose(); }, "guard denies readers");
            MineruM6NativeSuite.Throws<IOException>(delegate { store.OpenJournalFile("events.jsonl", false).Dispose(); }, "CreateNew on existing journal");
        }
    }

    public static void Test09_ReadPinsHoldAndRelease() {
        string path = NewPrivateRun("store");
        using (MineruM6PrivateStore store = new MineruM6PrivateStore(path, false, 8, 65536)) {
            store.WriteImmutable("spec.json", B("{\"s\":1}"));
            store.Read("spec.json");
            MineruM6NativeSuite.ThrowsAny(delegate { File.Delete(Path.Combine(path, "spec.json")); }, "pinned artifact cannot be deleted");
            store.CloseReadPins();
            File.Delete(Path.Combine(path, "spec.json"));
            MineruM6NativeSuite.Check(!File.Exists(Path.Combine(path, "spec.json")), "pin released after CloseReadPins");
        }
    }
}
