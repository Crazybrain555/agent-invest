// Independent M6 native owner component suite: runner, helpers and wire/binding
// parity. Compiled together with the 12 production sources by
// test_mineru_m6_native_suite.ps1 using csc.exe /noconfig /warnaserror+ and
// /main:MineruM6NativeSuite. C# 5 only (Framework64 v4.0.30319 compiler).
//
// Independence rules applied here: SHA-256 values are recomputed with
// System.Security.Cryptography directly, never through MineruResidentWire.Hash,
// and expected canonical bytes come from the Python-generated fixture, never
// from the native serializer under test.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;

public sealed class MineruM6TestFailure : Exception {
    public MineruM6TestFailure(string message) : base(message) {}
}

public static class MineruM6NativeSuite {
    public static string FixturePath, EvidencePath, TempRoot;
    public static MineruJsonValue Fixture;
    static readonly List<string[]> results = new List<string[]>();
    static readonly StringBuilder log = new StringBuilder();
    public static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);

    // ---- assertion helpers -------------------------------------------------
    public static void Check(bool condition, string what) {
        if (!condition) throw new MineruM6TestFailure(what);
    }
    public static void Equal(string expected, string actual, string what) {
        if (!String.Equals(expected, actual, StringComparison.Ordinal))
            throw new MineruM6TestFailure(what + "\n expected: " + Show(expected) + "\n actual:   " + Show(actual));
    }
    public static void Equal(long expected, long actual, string what) {
        if (expected != actual)
            throw new MineruM6TestFailure(what + " expected " + expected.ToString(CultureInfo.InvariantCulture) +
                                          " actual " + actual.ToString(CultureInfo.InvariantCulture));
    }
    public static void EqualBytes(byte[] expected, byte[] actual, string what) {
        if (expected.Length != actual.Length) throw new MineruM6TestFailure(what + " length " + expected.Length + " vs " + actual.Length);
        for (int i = 0; i < expected.Length; i++)
            if (expected[i] != actual[i]) throw new MineruM6TestFailure(what + " differs at byte " + i);
    }
    static string Show(string value) {
        if (value == null) return "<null>";
        if (value.Length > 400) return value.Substring(0, 400) + "...(" + value.Length + " chars)";
        return value;
    }
    public static T Throws<T>(Action action, string what) where T : Exception {
        try { action(); }
        catch (T error) { return error; }
        catch (Exception other) {
            throw new MineruM6TestFailure(what + ": expected " + typeof(T).Name + " but got " + other.GetType().Name + ": " + other.Message);
        }
        throw new MineruM6TestFailure(what + ": expected " + typeof(T).Name + " but nothing was thrown");
    }
    public static Exception ThrowsAny(Action action, string what) {
        try { action(); }
        catch (Exception error) { return error; }
        throw new MineruM6TestFailure(what + ": expected an exception but nothing was thrown");
    }
    public static void Log(string text) { log.Append(text).Append('\n'); }

    // ---- independent primitives -------------------------------------------
    public static string Sha(byte[] bytes) {
        using (SHA256 sha = SHA256.Create())
            return "sha256:" + BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
    }
    public static string Sha(string text) { return Sha(Utf8.GetBytes(text)); }
    public static string ShaFile(string path) {
        using (FileStream file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
        using (SHA256 sha = SHA256.Create())
            return "sha256:" + BitConverter.ToString(sha.ComputeHash(file)).Replace("-", "").ToLowerInvariant();
    }
    public static string Hex64(string seed) { return Sha(seed).Substring(7); }
    public static string LabelHash(string label) { return Sha("m6-native-suite|" + label); }
    public static string NewTempDir(string label) {
        string path = Path.Combine(TempRoot, label + "-" + Guid.NewGuid().ToString("N").Substring(0, 8));
        Directory.CreateDirectory(path);
        return path;
    }
    // Minimal JSON string quoting for evidence output, independent of the wire.
    public static string JsonQuote(string value) {
        StringBuilder result = new StringBuilder("\"");
        if (value != null) foreach (char c in value) {
            switch (c) {
                case '"': result.Append("\\\""); break;
                case '\\': result.Append("\\\\"); break;
                case '\n': result.Append("\\n"); break;
                case '\r': result.Append("\\r"); break;
                case '\t': result.Append("\\t"); break;
                default:
                    if (c < 32 || Char.IsSurrogate(c)) result.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                    else result.Append(c);
                    break;
            }
        }
        return result.Append('"').ToString();
    }

    // ---- fixture access ----------------------------------------------------
    public static MineruJsonValue F(params string[] path) {
        MineruJsonValue value = Fixture;
        foreach (string key in path) value = value.Get(key);
        return value;
    }
    public static string FS(params string[] path) { return F(path).String(); }
    public static long FI(params string[] path) { return F(path).Integer(); }

    // ---- runner ------------------------------------------------------------
    public static int Main(string[] args) {
        if (args.Length >= 2 && args[0] == "selfjob-child") return MineruM6SelfJobChild.Run(args);
        if (args.Length != 3) {
            Console.Error.WriteLine("usage: <fixture wire-vectors.v2.json> <evidence.json> <temp root>");
            return 2;
        }
        FixturePath = Path.GetFullPath(args[0]); EvidencePath = Path.GetFullPath(args[1]); TempRoot = Path.GetFullPath(args[2]);
        Directory.CreateDirectory(TempRoot);
        string fixtureText = File.ReadAllText(FixturePath, Utf8);
        Fixture = MineruResidentWire.Parse(fixtureText, 262144);
        Check(FS("contract_version") == "m6.owner-wire-vectors.v2", "fixture contract version");
        Type[] groups = {
            typeof(MineruM6WireTests), typeof(MineruM6BindingTests), typeof(MineruM6JournalTests),
            typeof(MineruM6StoreTests), typeof(MineruM6CredentialTests), typeof(MineruM6DiagnosticsTests),
            typeof(MineruM6EndpointTests), typeof(MineruM6ControlTests), typeof(MineruM6IdentityTests),
            typeof(MineruM6HostCliTests), typeof(MineruM6SelfJobNegativeTests),
        };
        int failed = 0, passed = 0;
        foreach (Type group in groups) {
            List<MethodInfo> methods = new List<MethodInfo>();
            foreach (MethodInfo method in group.GetMethods(BindingFlags.Public | BindingFlags.Static))
                if (method.Name.StartsWith("Test", StringComparison.Ordinal) && method.GetParameters().Length == 0) methods.Add(method);
            methods.Sort(delegate(MethodInfo a, MethodInfo b) { return String.CompareOrdinal(a.Name, b.Name); });
            foreach (MethodInfo method in methods) {
                string name = group.Name + "." + method.Name;
                try {
                    method.Invoke(null, null);
                    results.Add(new string[] { name, "pass", "" }); passed++;
                    Console.WriteLine("PASS " + name);
                } catch (TargetInvocationException wrapped) {
                    Exception error = wrapped.InnerException ?? wrapped;
                    results.Add(new string[] { name, "fail", error.GetType().FullName + ": " + error.Message + "\n" + error.StackTrace }); failed++;
                    Console.WriteLine("FAIL " + name + ": " + error.GetType().Name + ": " + error.Message);
                }
            }
        }
        WriteEvidence(passed, failed);
        Console.WriteLine("passed=" + passed + " failed=" + failed + " evidence=" + EvidencePath);
        return failed == 0 ? 0 : 1;
    }
    static void WriteEvidence(int passed, int failed) {
        StringBuilder json = new StringBuilder();
        json.Append("{\"contract_version\":\"m6.native-suite-evidence.v1\",");
        json.Append("\"fixture_path\":").Append(JsonQuote(FixturePath)).Append(',');
        json.Append("\"fixture_sha256\":").Append(JsonQuote(ShaFile(FixturePath))).Append(',');
        json.Append("\"suite_assembly_sha256\":").Append(JsonQuote(ShaFile(Assembly.GetExecutingAssembly().Location))).Append(',');
        json.Append("\"clr_version\":").Append(JsonQuote(Environment.Version.ToString())).Append(',');
        json.Append("\"is_64bit\":").Append(Environment.Is64BitProcess ? "true" : "false").Append(',');
        json.Append("\"temp_root\":").Append(JsonQuote(TempRoot)).Append(',');
        json.Append("\"passed\":").Append(passed).Append(",\"failed\":").Append(failed).Append(',');
        json.Append("\"results\":[");
        for (int i = 0; i < results.Count; i++) {
            if (i > 0) json.Append(',');
            json.Append("{\"name\":").Append(JsonQuote(results[i][0])).Append(",\"status\":").Append(JsonQuote(results[i][1]))
                .Append(",\"detail\":").Append(JsonQuote(results[i][2])).Append('}');
        }
        json.Append("],\"log\":").Append(JsonQuote(log.ToString())).Append('}');
        File.WriteAllText(EvidencePath, json.ToString(), Utf8);
    }
}

// ---------------------------------------------------------------------------
// Wire parity against the Python-generated vectors.
// ---------------------------------------------------------------------------
public static class MineruM6WireTests {
    static MineruJsonValue P(string raw) { return MineruResidentWire.Parse(raw, 65536); }

    public static void Test01_QuoteMatchesPythonJsonDumps() {
        MineruJsonValue cases = MineruM6NativeSuite.F("quote_cases");
        for (int i = 0; i < cases.Count; i++) {
            string value = cases.Item(i).Get("value").String(), quoted = cases.Item(i).Get("quoted").String();
            MineruM6NativeSuite.Equal(quoted, MineruResidentWire.Quote(value), "Quote parity for case " + i);
            MineruM6NativeSuite.Equal(value, P(quoted).String(), "Parse of Python-quoted string " + i);
        }
    }

    public static void Test02_AnchorCanonicalAndHash() {
        foreach (string mode in new string[] { "service", "e2e" }) {
            string anchor = MineruM6NativeSuite.FS(mode, "anchor");
            MineruM6NativeSuite.Equal(anchor, MineruM6OwnerWire.Anchor(P(anchor)), mode + " anchor canonical round trip");
            string expected = MineruM6NativeSuite.FS("identities", mode == "service" ? "anchor_sha256" : "e2e_anchor_sha256");
            MineruM6NativeSuite.Equal(expected, MineruM6NativeSuite.Sha(anchor), mode + " anchor hash matches Python");
        }
    }

    public static void Test03_ClockDomainBindingRejectsDrift() {
        // The top-level fixture display object is indented; the canonical
        // clock bytes come from the independently generated canonical anchor.
        MineruJsonValue clock = P(MineruM6NativeSuite.FS("service", "anchor")).Get("clock");
        MineruM6NativeSuite.Equal(clock.Raw, MineruM6OwnerWire.Clock(clock), "clock canonical");
        string drifted = clock.Raw.Replace("\"qpc_frequency_hz\":10000000", "\"qpc_frequency_hz\":10000001");
        MineruM6NativeSuite.Check(drifted != clock.Raw, "fixture clock frequency edit applied");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Clock(P(drifted)); }, "frequency drift must break the domain hash");
    }

    public static void Test04_RecordsProducersAndHashesMatchPython() {
        MineruJsonValue records = MineruM6NativeSuite.F("journal", "records"), hashes = MineruM6NativeSuite.F("journal", "record_hashes");
        MineruJsonValue producers = MineruM6NativeSuite.F("journal", "producer_events"), producerHashes = MineruM6NativeSuite.F("journal", "producer_hashes");
        for (int i = 0; i < records.Count; i++) {
            string raw = records.Item(i).String();
            MineruM6NativeSuite.Equal(raw, MineruM6OwnerWire.Record(raw), "record " + i + " canonical");
            MineruM6NativeSuite.Equal(hashes.Item(i).String(), MineruM6NativeSuite.Sha(raw), "record " + i + " hash");
            string producer = producers.Item(i).String();
            MineruM6NativeSuite.Equal(producer, MineruM6OwnerWire.Producer(P(producer)), "producer " + i + " canonical");
            MineruM6NativeSuite.Equal(producerHashes.Item(i).String(), MineruM6NativeSuite.Sha(producer), "producer " + i + " hash");
            MineruM6NativeSuite.Equal(producer, P(raw).Get("event").Raw, "record " + i + " embeds exact producer bytes");
        }
        string resumed = MineruM6NativeSuite.FS("journal", "resumed_record");
        MineruM6NativeSuite.Equal(resumed, MineruM6OwnerWire.Record(resumed), "owner_resumed record canonical");
    }

    public static void Test05_RequestsCanonicalAndCrossRunRejected() {
        foreach (string key in new string[] { "bind", "append", "status", "drain_a", "drain_b", "admission_closed", "close" }) {
            string raw = MineruM6NativeSuite.FS("requests", key);
            MineruM6NativeSuite.Equal(raw, MineruM6OwnerWire.Request(raw), "request " + key + " canonical");
        }
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("requests", "append_sha256"),
            MineruM6NativeSuite.Sha(MineruM6NativeSuite.FS("requests", "append")), "append request hash");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Request(MineruM6NativeSuite.FS("requests", "cross_run_append_rejected")); },
            "append whose event run_id differs from request run_id");
    }

    public static void Test06_ReplyParsesWithClosedKeys() {
        MineruJsonValue reply = P(MineruM6NativeSuite.FS("reply", "canonical"));
        reply.Keys("contract_version", "error_code", "outcome", "record", "request_sha256", "status");
        reply.Get("status").Keys("admission_valid_until_ticks", "anchor_sha256", "contract_version", "last_sequence",
            "observed_qpc_ticks", "owner_process_epoch_sha256", "run_id", "spec_sha256", "state");
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("reply", "sha256"), MineruM6NativeSuite.Sha(reply.Raw), "reply hash");
        MineruM6NativeSuite.Equal(reply.Get("record").Raw, MineruM6OwnerWire.Record(reply.Get("record").Raw), "reply record canonical");
    }

    public static void Test07_IdentifierLengthCountsUnicodeScalars() {
        string astral = "\U0001F600"; // one scalar, two UTF-16 units
        StringBuilder id128 = new StringBuilder();
        for (int i = 0; i < 128; i++) id128.Append(astral);
        string ok = MineruM6OwnerWire.Shape(P("{\"id\":" + MineruResidentWire.Quote(id128.ToString()) + "}"), "id:id");
        MineruM6NativeSuite.Check(ok.Length > 0, "128 astral scalars accepted as an id (Python max_length counts code points)");
        string over = id128.ToString() + astral;
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Shape(P("{\"id\":" + MineruResidentWire.Quote(over) + "}"), "id:id"); },
            "129 scalars exceed the id bound");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Shape(P("{\"id\":\"has space\"}"), "id:id"); }, "id with space");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Shape(P("{\"id\":\"\"}"), "id:id"); }, "empty id");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Shape(P("{\"d\":\" padded\"}"), "d:doc"); }, "doc id with leading space");
        MineruM6NativeSuite.Equal("{\"d\":\"in ner\"}", MineruM6OwnerWire.Shape(P("{\"d\":\"in ner\"}"), "d:doc"), "doc id allows inner space");
    }

    public static void Test08_IntegerStrictness() {
        foreach (string bad in new string[] { "01", "-1", "1.0", "1e3", "\"1\"", "9223372036854775808", "true" })
            MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Shape(P("{\"n\":" + bad + "}"), "n:n"); }, "non-canonical integer " + bad);
        MineruM6NativeSuite.Equal("{\"n\":9223372036854775807}", MineruM6OwnerWire.Shape(P("{\"n\":9223372036854775807}"), "n:n"), "Int64 max accepted");
        MineruM6NativeSuite.Equal("{\"n\":0}", MineruM6OwnerWire.Shape(P("{\"n\":0}"), "n:n"), "zero is a valid nonnegative");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Shape(P("{\"p\":0}"), "p:p"); }, "zero rejected as positive");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Shape(P("{\"b\":1}"), "b:bool"); }, "1 is not a boolean");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Shape(P("{\"h\":\"sha256:ABC\"}"), "h:hash"); }, "uppercase/short hash");
    }

    public static void Test09_ParserRejectsAmbiguousJson() {
        MineruM6NativeSuite.ThrowsAny(delegate { P("{\"a\":1,\"a\":2}"); }, "duplicate key");
        MineruM6NativeSuite.ThrowsAny(delegate { P("{\"a\":1,\"\\u0061\":2}"); }, "duplicate key via escape alias");
        MineruM6NativeSuite.ThrowsAny(delegate { P("{\"a\":1} x"); }, "trailing data");
        MineruM6NativeSuite.ThrowsAny(delegate { P("\"\\ud800\""); }, "lone surrogate");
        MineruM6NativeSuite.ThrowsAny(delegate { P("[NaN]"); }, "NaN");
        MineruM6NativeSuite.ThrowsAny(delegate { P("[1,]"); }, "trailing comma");
        StringBuilder deep = new StringBuilder();
        for (int i = 0; i < 40; i++) deep.Append('[');
        for (int i = 0; i < 40; i++) deep.Append(']');
        MineruM6NativeSuite.ThrowsAny(delegate { P(deep.ToString()); }, "depth over 32");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruResidentWire.Parse(new string(' ', 70000) + "1", 65536); }, "byte bound");
        MineruM6NativeSuite.Equal("\u00e9\U0001F600", P("\"\\u00e9\\ud83d\\ude00\"").String(), "escaped surrogate pair decodes");
    }

    public static void Test10_ObjectSortsKeysByOrdinalAndRejectsOversize() {
        string built = MineruResidentWire.Object("b", "1", "a", "2", "Z", "3");
        MineruM6NativeSuite.Equal("{\"Z\":3,\"a\":2,\"b\":1}", built, "ordinal key order (uppercase first)");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { MineruResidentWire.Object("a"); }, "odd pair count");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruResidentWire.Object("a", "not json"); }, "value must be JSON");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruResidentWire.Object("a", "\"" + new string('x', 65600) + "\""); }, "output bound");
    }

    public static void Test11_PayloadClosedVocabulary() {
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Payload(P("{\"kind\":\"attempt_started\"}")); }, "unknown payload kind");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Command(P("{\"kind\":\"exec\",\"argv\":[]}")); }, "no command execution kind");
        string h = MineruM6NativeSuite.LabelHash("x");
        // attempt_final closure binding: not_submitted must carry null remote receipt/task; published cannot be not_submitted.
        string okFinal = "{\"attempt_id\":\"a\",\"cleanup_receipt_sha256\":\"" + h + "\",\"kind\":\"attempt_final\",\"outcome\":\"failed\",\"remote_disposition\":\"not_submitted\",\"remote_receipt_sha256\":null,\"remote_task_identity_sha256\":null}";
        MineruM6NativeSuite.Equal(okFinal, MineruM6OwnerWire.Payload(P(okFinal)), "failed/not_submitted final accepted");
        string badFinal = okFinal.Replace("\"outcome\":\"failed\"", "\"outcome\":\"published\"");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Payload(P(badFinal)); }, "published without remote closure");
        string badReceipt = okFinal.Replace("\"remote_receipt_sha256\":null", "\"remote_receipt_sha256\":\"" + h + "\"");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Payload(P(badReceipt)); }, "not_submitted with a remote receipt");
    }

    public static void Test12_ResourcesEnvelopeConsistency() {
        string good = "{\"max_attempts\":10,\"max_events\":10,\"max_log_bytes\":100,\"max_record_bytes\":100,\"max_verifier_backlog_bytes\":1,\"stop_admission_budget_ticks\":1}";
        MineruM6NativeSuite.Equal(good, MineruM6OwnerWire.Resources(P(good)), "resources canonical");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Resources(P(good.Replace("\"max_events\":10", "\"max_events\":9"))); }, "events below attempts");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Resources(P(good.Replace("\"max_log_bytes\":100", "\"max_log_bytes\":99"))); }, "log below record");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Resources(P(good.Replace("\"max_events\":10", "\"max_events\":1000001"))); }, "events over Python bound");
    }
}

// ---------------------------------------------------------------------------
// Bootstrap spec/anchor binding against the Python contract vectors.
// ---------------------------------------------------------------------------
public static class MineruM6BindingTests {
    public static void Test01_PositiveSpecsBindToTheirAnchors() {
        foreach (string mode in new string[] { "service", "e2e" }) {
            string spec = MineruM6NativeSuite.FS(mode, "spec"), anchor = MineruM6NativeSuite.FS(mode, "anchor");
            MineruJsonValue value = MineruM6OwnerBinding.Validate(spec, anchor);
            MineruM6NativeSuite.Equal(spec, value.Raw, mode + " Validate returns the exact spec bytes");
        }
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("identities", "spec_sha256"), MineruM6NativeSuite.Sha(MineruM6NativeSuite.FS("service", "spec")), "service spec hash");
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("identities", "e2e_spec_sha256"), MineruM6NativeSuite.Sha(MineruM6NativeSuite.FS("e2e", "spec")), "e2e spec hash");
    }

    public static void Test02_CarryInAcceptsScalarOrderOnly() {
        MineruJsonValue carry = MineruResidentWire.Parse(MineruM6NativeSuite.FS("service", "spec"), 65536).Get("carry_in_attempt_ids");
        MineruJsonValue expected = MineruM6NativeSuite.F("service", "carry_in_scalar_order");
        MineruM6NativeSuite.Equal(expected.Count, carry.Count, "carry-in count");
        for (int i = 0; i < carry.Count; i++) MineruM6NativeSuite.Equal(expected.Item(i).String(), carry.Item(i).String(), "carry-in order " + i);
        // The positive spec contains a BMP id before an astral id; UTF-16 ordinal would reverse them.
        bool sawAstralAfterBmp = false;
        for (int i = 1; i < carry.Count; i++) {
            string prev = carry.Item(i - 1).String(), cur = carry.Item(i).String();
            if (Char.IsHighSurrogate(cur[0]) && !Char.IsHighSurrogate(prev[0]) && String.CompareOrdinal(prev, cur) > 0) sawAstralAfterBmp = true;
        }
        MineruM6NativeSuite.Check(sawAstralAfterBmp, "fixture exercises scalar-vs-UTF16 ordering divergence");
    }

    public static void Test03_EveryPythonRejectedVariantIsRejectedNatively() {
        MineruJsonValue negatives = MineruM6NativeSuite.F("binding_negatives");
        MineruM6NativeSuite.Check(negatives.Count >= 10, "fixture negative coverage");
        for (int i = 0; i < negatives.Count; i++) {
            MineruJsonValue item = negatives.Item(i);
            string name = item.Get("name").String(), spec = item.Get("spec").String(), anchor = item.Get("anchor").String();
            Exception error = MineruM6NativeSuite.ThrowsAny(delegate { MineruM6OwnerBinding.Validate(spec, anchor); }, "binding negative " + name);
            MineruM6NativeSuite.Check(error is FormatException || error is OverflowException,
                "binding negative " + name + " must be a controlled format refusal, got " + error.GetType().Name);
            MineruM6NativeSuite.Log("binding negative " + name + " -> " + error.GetType().Name + ": " + error.Message);
        }
    }

    public static void Test04_AnchorDeadlineIntervalMustBeExact() {
        string anchor = MineruM6NativeSuite.FS("service", "anchor");
        MineruJsonValue parsed = MineruResidentWire.Parse(anchor, 65536);
        long deadline = parsed.Get("deadline_ticks").Integer();
        string shifted = anchor.Replace("\"deadline_ticks\":" + deadline.ToString(CultureInfo.InvariantCulture),
                                        "\"deadline_ticks\":" + (deadline + 1).ToString(CultureInfo.InvariantCulture));
        MineruM6NativeSuite.Check(shifted != anchor, "deadline edit applied");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Anchor(MineruResidentWire.Parse(shifted, 65536)); }, "deadline != t0 + planned*freq");
        long maxClose = parsed.Get("max_close_ticks").Integer();
        string early = anchor.Replace("\"max_close_ticks\":" + maxClose.ToString(CultureInfo.InvariantCulture),
                                      "\"max_close_ticks\":" + (deadline - 1).ToString(CultureInfo.InvariantCulture));
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerWire.Anchor(MineruResidentWire.Parse(early, 65536)); }, "max_close before deadline");
    }

    public static void Test05_SpecAnchorPairMismatchAcrossModes() {
        MineruM6NativeSuite.ThrowsAny(delegate { MineruM6OwnerBinding.Validate(MineruM6NativeSuite.FS("service", "spec"), MineruM6NativeSuite.FS("e2e", "anchor")); },
            "service spec against e2e anchor (run id differs)");
    }

    public static void Test06_CarryInBoundAgainstAttempts() {
        string spec = MineruM6NativeSuite.FS("service", "spec"), anchor = MineruM6NativeSuite.FS("service", "anchor");
        MineruJsonValue parsed = MineruResidentWire.Parse(spec, 65536);
        long attempts = parsed.Get("resources").Get("max_attempts").Integer();
        StringBuilder ids = new StringBuilder("[");
        for (long i = 0; i <= attempts; i++) { if (i > 0) ids.Append(','); ids.Append("\"c").Append(i.ToString("D6", CultureInfo.InvariantCulture)).Append('"'); }
        ids.Append(']');
        string over = spec.Replace(parsed.Get("carry_in_attempt_ids").Raw, ids.ToString());
        MineruM6NativeSuite.Check(over != spec, "carry-in replacement applied");
        MineruM6NativeSuite.Throws<FormatException>(delegate { MineruM6OwnerBinding.Validate(over, anchor); }, "carry-in count above max_attempts");
    }
}
