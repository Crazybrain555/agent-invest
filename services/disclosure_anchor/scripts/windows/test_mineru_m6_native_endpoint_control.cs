// Independent M6 credentials / diagnostics sink / loopback endpoint / run
// control tests. Part of test_mineru_m6_native_suite (C# 5, warnaserror).
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

// ---------------------------------------------------------------------------
// Credentials
// ---------------------------------------------------------------------------
public static class MineruM6CredentialTests {
    public static string Token(string role) { return MineruM6NativeSuite.Hex64("token|" + role); }
    public static MineruM6Principal Principal(string role) { return new MineruM6Principal(role, MineruM6NativeSuite.LabelHash("epoch|" + role)); }
    public static MineruM6Credentials Standard() {
        Dictionary<MineruM6Principal, string> map = new Dictionary<MineruM6Principal, string>();
        foreach (string role in new string[] { "controller", "service_runner", "quality_verifier" }) map.Add(Principal(role), Token(role));
        return new MineruM6Credentials(map);
    }
    static byte[] Header(string token) { return MineruM6NativeSuite.Utf8.GetBytes("M6-AUTH/1 " + token); }

    public static void Test01_BoundsAndDistinctness() {
        Dictionary<MineruM6Principal, string> one = new Dictionary<MineruM6Principal, string>();
        one.Add(Principal("controller"), Token("controller"));
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Credentials(one); }, "fewer than two roles");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Credentials(null); }, "null map");
        Dictionary<MineruM6Principal, string> dupToken = new Dictionary<MineruM6Principal, string>();
        dupToken.Add(Principal("controller"), Token("x")); dupToken.Add(Principal("service_runner"), Token("x"));
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Credentials(dupToken); }, "duplicate token across roles");
        Dictionary<MineruM6Principal, string> dupRole = new Dictionary<MineruM6Principal, string>();
        dupRole.Add(new MineruM6Principal("controller", MineruM6NativeSuite.LabelHash("e1")), Token("a"));
        dupRole.Add(new MineruM6Principal("controller", MineruM6NativeSuite.LabelHash("e2")), Token("b"));
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Credentials(dupRole); }, "duplicate role with different epochs");
        Dictionary<MineruM6Principal, string> badToken = new Dictionary<MineruM6Principal, string>();
        badToken.Add(Principal("controller"), Token("a").ToUpperInvariant()); badToken.Add(Principal("service_runner"), Token("b"));
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Credentials(badToken); }, "uppercase token");
        MineruM6NativeSuite.Throws<FormatException>(delegate { new MineruM6Principal("admin", MineruM6NativeSuite.LabelHash("e")); }, "role outside closed set");
        MineruM6NativeSuite.Throws<FormatException>(delegate { new MineruM6Principal("controller", "not-a-hash"); }, "epoch shape");
    }

    public static void Test02_AuthenticateExactBytesOnly() {
        using (MineruM6Credentials credentials = Standard()) {
            byte[] good = Header(Token("service_runner"));
            MineruM6Principal principal = credentials.Authenticate(good, good.Length);
            MineruM6NativeSuite.Check(principal != null && principal.Role == "service_runner", "exact token maps to its role");
            MineruM6NativeSuite.Equal(MineruM6NativeSuite.LabelHash("epoch|service_runner"), principal.Epoch, "principal carries the bound epoch");
            MineruM6NativeSuite.Check(credentials.Authenticate(good, good.Length - 1) == null, "short length");
            byte[] flipped = (byte[])good.Clone(); flipped[flipped.Length - 1] ^= 1;
            MineruM6NativeSuite.Check(credentials.Authenticate(flipped, flipped.Length) == null, "one-bit token difference");
            byte[] prefix = (byte[])good.Clone(); prefix[0] = (byte)'m';
            MineruM6NativeSuite.Check(credentials.Authenticate(prefix, prefix.Length) == null, "prefix case difference");
            byte[] padded = new byte[good.Length + 1]; Array.Copy(good, padded, good.Length); padded[good.Length] = (byte)' ';
            MineruM6NativeSuite.Check(credentials.Authenticate(padded, padded.Length) == null, "trailing byte");
        }
        MineruM6Credentials disposed = Standard(); disposed.Dispose();
        byte[] again = Header(Token("controller"));
        MineruM6NativeSuite.Throws<ObjectDisposedException>(delegate { disposed.Authenticate(again, again.Length); }, "disposed credentials");
    }
}

// ---------------------------------------------------------------------------
// Diagnostics sink
// ---------------------------------------------------------------------------
public static class MineruM6DiagnosticsTests {
    sealed class Sink {
        public readonly List<KeyValuePair<string, byte[]>> Persisted = new List<KeyValuePair<string, byte[]>>();
        public bool Fail;
        public void Persist(string code, byte[] body) {
            if (Fail) throw new IOException("injected diagnostic storage failure");
            Persisted.Add(new KeyValuePair<string, byte[]>(code, body));
        }
    }
    static readonly string[] NoiseCodes = { "connection_bound", "authentication_rejected", "authentication_header_invalid",
        "peer_connection_lost", "partial_request_eof", "peer_closed", "request_framing_invalid", "request_utf8_invalid",
        "request_shape_invalid", "request_body_bound_or_crlf", "reply_connection_lost", "reply_zero_progress",
        "request_timeout", "reply_timeout", "other" };
    static string Reply(string outcome, string code) {
        return "{\"error_code\":" + (code == null ? "null" : "\"" + code + "\"") + ",\"outcome\":\"" + outcome + "\"}";
    }
    static MineruJsonValue Summary(Sink sink) {
        KeyValuePair<string, byte[]> last = sink.Persisted[sink.Persisted.Count - 1];
        MineruM6NativeSuite.Equal("noise_summary", last.Key, "summary code");
        MineruJsonValue value = MineruResidentWire.Parse(MineruM6NativeSuite.Utf8.GetString(last.Value), 65536);
        value.Keys("contract_version", "counts", "pending_drains");
        MineruM6NativeSuite.Equal("m6.transport-noise-summary.v2", value.Get("contract_version").String(), "summary version");
        value.Get("counts").Keys(NoiseCodes);
        return value;
    }

    public static void Test01_NoiseAggregatesUntilSealAndNeverPersistsPerEvent() {
        Sink sink = new Sink();
        MineruM6OwnerDiagnostics diagnostics = new MineruM6OwnerDiagnostics(sink.Persist);
        diagnostics.Record("peer_closed", null); diagnostics.Record("peer_closed", null);
        diagnostics.Record("some_future_code", null);
        MineruM6NativeSuite.Equal(0, sink.Persisted.Count, "no-body noise is not persisted per event");
        diagnostics.SealNoise();
        MineruM6NativeSuite.Equal(1, sink.Persisted.Count, "one summary");
        MineruJsonValue summary = Summary(sink);
        MineruM6NativeSuite.Equal(2, summary.Get("counts").Get("peer_closed").Integer(), "peer_closed count");
        MineruM6NativeSuite.Equal(1, summary.Get("counts").Get("other").Integer(), "unknown code counted as other");
        MineruM6NativeSuite.Equal(0, summary.Get("counts").Get("authentication_rejected").Integer(), "untouched counter is zero");
        MineruM6NativeSuite.Equal(0, summary.Get("pending_drains").Count, "no pending drains");
        diagnostics.Record("peer_closed", null);
        MineruM6NativeSuite.Equal(1, diagnostics.PostSealNoise, "post-seal noise counted");
        MineruM6NativeSuite.Equal(1, sink.Persisted.Count, "post-seal noise not persisted");
        diagnostics.SealNoise();
        MineruM6NativeSuite.Equal(1, sink.Persisted.Count, "seal is idempotent");
    }

    public static void Test02_AuthenticatedBodiesAndNonSuccessRepliesAreRetainedRaw() {
        Sink sink = new Sink();
        MineruM6OwnerDiagnostics diagnostics = new MineruM6OwnerDiagnostics(sink.Persist);
        byte[] body = MineruM6NativeSuite.Utf8.GetBytes("garbage é");
        diagnostics.Record("request_shape_invalid", body);
        MineruM6NativeSuite.Equal(1, sink.Persisted.Count, "raw body persisted immediately");
        MineruM6NativeSuite.EqualBytes(body, sink.Persisted[0].Value, "exact bytes");
        string request = MineruM6NativeSuite.FS("requests", "status");
        diagnostics.ObserveReply(request, Reply("ok", null));
        MineruM6NativeSuite.Equal(1, sink.Persisted.Count, "ok reply is not a diagnostic");
        diagnostics.ObserveReply(request, Reply("rejected", "controller_required"));
        MineruM6NativeSuite.Equal("controller_required", sink.Persisted[1].Key, "rejected reply retained under its error code");
        MineruM6NativeSuite.EqualBytes(MineruM6NativeSuite.Utf8.GetBytes(request), sink.Persisted[1].Value, "rejected request bytes");
        diagnostics.ObserveReply(request, Reply("conflict", "producer_event_conflict"));
        MineruM6NativeSuite.Equal("producer_event_conflict", sink.Persisted[2].Key, "conflict retained");
        diagnostics.SealNoise();
        MineruJsonValue summary = Summary(sink);
        MineruM6NativeSuite.Equal(0, summary.Get("counts").Get("request_shape_invalid").Integer(), "body diagnostics are not counted as noise");
    }

    public static void Test03_RepeatedPendingDrainAggregatesByCommandIdentity() {
        Sink sink = new Sink();
        MineruM6OwnerDiagnostics diagnostics = new MineruM6OwnerDiagnostics(sink.Persist);
        string a = MineruM6NativeSuite.FS("requests", "drain_a"), b = MineruM6NativeSuite.FS("requests", "drain_b");
        diagnostics.ObserveReply(a, Reply("rejected", "verifier_drain_pending"));
        MineruM6NativeSuite.Equal(1, sink.Persisted.Count, "first pending drain retains the complete raw request");
        MineruM6NativeSuite.Equal("verifier_drain_pending", sink.Persisted[0].Key, "code");
        MineruM6NativeSuite.EqualBytes(MineruM6NativeSuite.Utf8.GetBytes(a), sink.Persisted[0].Value, "first request bytes");
        diagnostics.ObserveReply(b, Reply("rejected", "verifier_drain_pending"));
        MineruM6NativeSuite.Equal(1, sink.Persisted.Count, "nonce-only change allocates no new artifact");
        string otherReceipt = MineruM6NativeSuite.LabelHash("another-drain-receipt");
        MineruJsonValue parsedA = MineruResidentWire.Parse(a, 65536);
        string receipt = parsedA.Get("command").Get("event").Get("payload").Get("drain_receipt_sha256").String();
        string c = a.Replace(receipt, otherReceipt);
        MineruM6NativeSuite.Check(c != a, "distinct business bytes prepared");
        diagnostics.ObserveReply(c, Reply("rejected", "verifier_drain_pending"));
        MineruM6NativeSuite.Equal(2, sink.Persisted.Count, "different business evidence gets its own record");
        diagnostics.SealNoise();
        MineruJsonValue summary = Summary(sink);
        MineruM6NativeSuite.Equal(2, summary.Get("pending_drains").Count, "two groups");
        MineruJsonValue group = null;
        for (int i = 0; i < 2; i++)
            if (summary.Get("pending_drains").Item(i).Get("command_identity_sha256").String() == MineruM6NativeSuite.FS("pending_drain", "identity_sha256"))
                group = summary.Get("pending_drains").Item(i);
        MineruM6NativeSuite.Check(group != null, "group keyed by Python command identity (run, spec, canonical command)");
        group.Keys("command_identity_sha256", "first_request_sha256", "last_request_sha256", "occurrences", "request_hash_chain");
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("pending_drain", "request_a_sha256"), group.Get("first_request_sha256").String(), "first");
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("pending_drain", "request_b_sha256"), group.Get("last_request_sha256").String(), "last");
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("pending_drain", "chain_after_two"), group.Get("request_hash_chain").String(), "hash chain in arrival order");
        MineruM6NativeSuite.Equal(2, group.Get("occurrences").Integer(), "occurrences");
        MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { diagnostics.ObserveReply(a, Reply("rejected", "verifier_drain_pending")); }, "pending drain after seal");
    }

    public static void Test04_PendingDrainGroupBoundPoisons() {
        Sink sink = new Sink();
        MineruM6OwnerDiagnostics diagnostics = new MineruM6OwnerDiagnostics(sink.Persist);
        string a = MineruM6NativeSuite.FS("requests", "drain_a");
        string receipt = MineruResidentWire.Parse(a, 65536).Get("command").Get("event").Get("payload").Get("drain_receipt_sha256").String();
        for (int i = 0; i < 32; i++) diagnostics.ObserveReply(a.Replace(receipt, MineruM6NativeSuite.LabelHash("drain-" + i)), Reply("rejected", "verifier_drain_pending"));
        MineruM6NativeSuite.Equal(32, sink.Persisted.Count, "32 distinct groups retained");
        MineruM6NativeSuite.Throws<IOException>(delegate { diagnostics.ObserveReply(a.Replace(receipt, MineruM6NativeSuite.LabelHash("drain-33")), Reply("rejected", "verifier_drain_pending")); }, "33rd group");
        MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { diagnostics.SealNoise(); }, "poisoned sink cannot seal");
        MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { diagnostics.Record("peer_closed", null); }, "poisoned sink refuses noise");
    }

    public static void Test05_StorageFailurePropagatesAndPoisons() {
        Sink sink = new Sink();
        MineruM6OwnerDiagnostics diagnostics = new MineruM6OwnerDiagnostics(sink.Persist);
        diagnostics.Record("peer_closed", null);
        sink.Fail = true;
        MineruM6NativeSuite.Throws<IOException>(delegate { diagnostics.Record("request_shape_invalid", new byte[] { 1 }); }, "storage failure surfaces");
        sink.Fail = false;
        MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { diagnostics.SealNoise(); }, "no summary after uncertain IO");
        MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { long n = diagnostics.PostSealNoise; MineruM6NativeSuite.Log("post seal " + n); }, "poisoned property");
    }

    public static void Test06_InputBoundsAndThreadOwnership() {
        Sink sink = new Sink();
        MineruM6NativeSuite.Throws<ArgumentNullException>(delegate { new MineruM6OwnerDiagnostics(null); }, "sink required");
        MineruM6OwnerDiagnostics diagnostics = new MineruM6OwnerDiagnostics(sink.Persist);
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { diagnostics.Record("Bad", null); }, "code shape");
        MineruM6NativeSuite.Throws<ArgumentException>(delegate { diagnostics.Record("ok", new byte[65537]); }, "body bound");
        Exception fromOtherThread = null;
        Thread other = new Thread(delegate() { try { diagnostics.Record("peer_closed", null); } catch (Exception error) { fromOtherThread = error; } });
        other.Start(); other.Join();
        MineruM6NativeSuite.Check(fromOtherThread is InvalidOperationException, "other thread refused");
        diagnostics.SealNoise();
        MineruM6NativeSuite.Equal(0, Summary(sink).Get("counts").Get("peer_closed").Integer(), "cross-thread call had no effect");
    }
}

// ---------------------------------------------------------------------------
// Loopback endpoint on a dedicated owner thread.
// ---------------------------------------------------------------------------
public sealed class MineruM6EndpointRig : IDisposable {
    public readonly int Port;
    public readonly List<KeyValuePair<string, byte[]>> Diagnostics = new List<KeyValuePair<string, byte[]>>();
    public Func<string, MineruM6Principal, string> Handler;
    public volatile bool Closed, SinkFails;
    public Exception RunError;
    public readonly ManualResetEvent Ready = new ManualResetEvent(false), Finished = new ManualResetEvent(false);
    readonly Thread thread;
    public static int FreePort() {
        TcpListener probe = new TcpListener(IPAddress.Loopback, 0);
        probe.Start(); int port = ((IPEndPoint)probe.LocalEndpoint).Port; probe.Stop();
        return port;
    }
    public MineruM6EndpointRig(int requestMs, int idleMs, int maxConnections) {
        Port = FreePort();
        Handler = delegate(string raw, MineruM6Principal principal) { return MineruM6NativeSuite.FS("reply", "canonical"); };
        thread = new Thread(delegate() {
            MineruM6Endpoint endpoint = null;
            try {
                using (MineruM6Credentials credentials = MineruM6CredentialTests.Standard()) {
                    endpoint = new MineruM6Endpoint(Port, credentials,
                        delegate(string raw, MineruM6Principal principal) { return Handler(raw, principal); },
                        delegate { }, delegate { return Closed; },
                        delegate(string code, byte[] body) {
                            if (SinkFails) throw new IOException("injected sink failure");
                            lock (Diagnostics) Diagnostics.Add(new KeyValuePair<string, byte[]>(code, body));
                        }, requestMs, idleMs, maxConnections);
                    endpoint.Run(delegate { Ready.Set(); });
                    endpoint.Dispose(); endpoint = null;
                }
            } catch (Exception error) {
                RunError = error;
                try { if (endpoint != null) endpoint.Dispose(); } catch (Exception cleanup) { MineruM6NativeSuite.Log("endpoint cleanup: " + cleanup.Message); }
            } finally { Ready.Set(); Finished.Set(); }
        });
        thread.IsBackground = true; thread.Start();
        MineruM6NativeSuite.Check(Ready.WaitOne(5000), "endpoint ready");
        if (RunError != null) throw new MineruM6TestFailure("endpoint failed to start: " + RunError.Message);
    }
    public int WaitDiagnostics(int count, int timeoutMs) {
        DateTime until = DateTime.UtcNow.AddMilliseconds(timeoutMs);
        while (DateTime.UtcNow < until) { lock (Diagnostics) if (Diagnostics.Count >= count) return Diagnostics.Count; Thread.Sleep(10); }
        lock (Diagnostics) return Diagnostics.Count;
    }
    public KeyValuePair<string, byte[]> Diagnostic(int index) { lock (Diagnostics) return Diagnostics[index]; }
    public void Dispose() {
        Closed = true;
        if (!Finished.WaitOne(10000)) MineruM6NativeSuite.Log("endpoint thread did not finish within 10s");
    }
}

public sealed class MineruM6TestClient : IDisposable {
    readonly TcpClient tcp; readonly NetworkStream stream; readonly MemoryStream buffer = new MemoryStream();
    public MineruM6TestClient(int port) {
        tcp = new TcpClient(); tcp.NoDelay = true; tcp.Connect(IPAddress.Loopback, port);
        stream = tcp.GetStream(); stream.ReadTimeout = 8000; stream.WriteTimeout = 8000;
    }
    public void Send(byte[] bytes) { stream.Write(bytes, 0, bytes.Length); stream.Flush(); }
    public void Send(string text) { Send(MineruM6NativeSuite.Utf8.GetBytes(text)); }
    public void Request(string token, string body) { Send("M6-AUTH/1 " + token + "\n" + body + "\n"); }
    // Returns one line without LF, or null on EOF/reset (the owner closed the connection).
    public string ReadLine() {
        byte[] chunk = new byte[4096];
        while (true) {
            byte[] have = buffer.ToArray();
            int index = Array.IndexOf(have, (byte)10);
            if (index >= 0) {
                string line = MineruM6NativeSuite.Utf8.GetString(have, 0, index);
                buffer.SetLength(0); buffer.Write(have, index + 1, have.Length - index - 1);
                return line;
            }
            int read;
            try { read = stream.Read(chunk, 0, chunk.Length); }
            catch (IOException) { return null; }
            if (read == 0) return null;
            buffer.Write(chunk, 0, read);
        }
    }
    public void ShutdownSend() { tcp.Client.Shutdown(SocketShutdown.Send); }
    public void Dispose() { try { stream.Dispose(); } catch (Exception) { } tcp.Close(); }
}

public static class MineruM6EndpointTests {
    static string Tok(string role) { return MineruM6CredentialTests.Token(role); }
    static string Status { get { return MineruM6NativeSuite.FS("requests", "status"); } }

    public static void Test01_ConfigurationBounds() {
        using (MineruM6Credentials credentials = MineruM6CredentialTests.Standard()) {
            Func<string, MineruM6Principal, string> handler = delegate(string r, MineruM6Principal p) { return "{}"; };
            Action tick = delegate { }; Func<bool> closed = delegate { return false; }; Action<string, byte[]> sink = delegate(string c, byte[] b) { };
            int port = MineruM6EndpointRig.FreePort();
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Endpoint(80, credentials, handler, tick, closed, sink, 1000, 2000, 2).Dispose(); }, "privileged port");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Endpoint(port, credentials, handler, tick, closed, sink, 0, 2000, 2).Dispose(); }, "zero request timeout");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Endpoint(port, credentials, handler, tick, closed, sink, 2000, 1000, 2).Dispose(); }, "idle below request");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Endpoint(port, credentials, handler, tick, closed, sink, 1000, 2000, 1).Dispose(); }, "fewer than two peers");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Endpoint(port, credentials, handler, tick, closed, sink, 1000, 2000, 9).Dispose(); }, "more than eight peers");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Endpoint(port, credentials, null, tick, closed, sink, 1000, 2000, 2).Dispose(); }, "handler required");
            MineruM6NativeSuite.Throws<ArgumentException>(delegate { new MineruM6Endpoint(port, credentials, handler, tick, closed, sink, 30001, 60000, 2).Dispose(); }, "request timeout over 30s");
        }
    }

    public static void Test02_AuthenticatedRoundTripsKeepTheConnection() {
        using (MineruM6EndpointRig rig = new MineruM6EndpointRig(3000, 6000, 4))
        using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
            MineruM6Principal seen = null;
            rig.Handler = delegate(string raw, MineruM6Principal principal) { seen = principal; return MineruM6NativeSuite.FS("reply", "canonical"); };
            client.Request(Tok("controller"), Status);
            MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("reply", "canonical"), client.ReadLine(), "reply line");
            MineruM6NativeSuite.Check(seen != null && seen.Role == "controller", "handler receives the authenticated principal");
            client.Request(Tok("service_runner"), Status);
            MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("reply", "canonical"), client.ReadLine(), "second request on the same connection");
            MineruM6NativeSuite.Check(seen.Role == "service_runner", "each request re-authenticates its own header");
            MineruM6NativeSuite.Equal(0, rig.WaitDiagnostics(1, 200), "no diagnostics for clean exchanges");
        }
    }

    public static void Test03_UnauthenticatedRequestsCloseWithoutBodyOrReply() {
        using (MineruM6EndpointRig rig = new MineruM6EndpointRig(3000, 6000, 4)) {
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Request(new string('0', 64), Status);
                MineruM6NativeSuite.Check(client.ReadLine() == null, "no reply for a rejected token");
            }
            MineruM6NativeSuite.Equal(1, rig.WaitDiagnostics(1, 3000), "one diagnostic");
            MineruM6NativeSuite.Equal("authentication_rejected", rig.Diagnostic(0).Key, "code");
            MineruM6NativeSuite.Check(rig.Diagnostic(0).Value == null, "no body retained for an unauthenticated peer");
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Send("M6-AUTH/1 " + Tok("controller") + "\r\n" + Status + "\n");
                MineruM6NativeSuite.Check(client.ReadLine() == null, "CRLF header closed");
            }
            MineruM6NativeSuite.Equal(2, rig.WaitDiagnostics(2, 3000), "second diagnostic");
            MineruM6NativeSuite.Equal("authentication_header_invalid", rig.Diagnostic(1).Key, "CR in header");
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Send(new string('A', 90) + "\n");
                MineruM6NativeSuite.Check(client.ReadLine() == null, "oversized header closed");
            }
            MineruM6NativeSuite.Equal(3, rig.WaitDiagnostics(3, 3000), "third diagnostic");
            MineruM6NativeSuite.Equal("authentication_header_invalid", rig.Diagnostic(2).Key, "header over 74 bytes");
            MineruM6NativeSuite.Check(rig.Diagnostic(1).Value == null && rig.Diagnostic(2).Value == null, "header failures carry no body");
        }
    }

    public static void Test04_AuthenticatedMalformedBodiesAreRetainedWithoutTheToken() {
        using (MineruM6EndpointRig rig = new MineruM6EndpointRig(3000, 6000, 4)) {
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Request(Tok("controller"), "not json");
                MineruM6NativeSuite.Check(client.ReadLine() == null, "malformed body closed");
            }
            MineruM6NativeSuite.Equal(1, rig.WaitDiagnostics(1, 3000), "diagnostic");
            MineruM6NativeSuite.Equal("request_shape_invalid", rig.Diagnostic(0).Key, "shape code");
            MineruM6NativeSuite.Equal("not json", MineruM6NativeSuite.Utf8.GetString(rig.Diagnostic(0).Value), "exact raw body retained");
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Send("M6-AUTH/1 " + Tok("controller") + "\nabc\r\n");
                MineruM6NativeSuite.Check(client.ReadLine() == null, "CR in body closed");
            }
            MineruM6NativeSuite.Equal(2, rig.WaitDiagnostics(2, 3000), "second");
            MineruM6NativeSuite.Equal("request_body_bound_or_crlf", rig.Diagnostic(1).Key, "CR code");
            MineruM6NativeSuite.Equal("abc", MineruM6NativeSuite.Utf8.GetString(rig.Diagnostic(1).Value), "bytes before CR retained");
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                byte[] header = MineruM6NativeSuite.Utf8.GetBytes("M6-AUTH/1 " + Tok("controller") + "\n");
                byte[] payload = new byte[header.Length + 3];
                Array.Copy(header, payload, header.Length); payload[header.Length] = 0xff; payload[header.Length + 1] = 0xfe; payload[header.Length + 2] = 10;
                client.Send(payload);
                MineruM6NativeSuite.Check(client.ReadLine() == null, "invalid UTF-8 closed");
            }
            MineruM6NativeSuite.Equal(3, rig.WaitDiagnostics(3, 3000), "third");
            MineruM6NativeSuite.Equal("request_utf8_invalid", rig.Diagnostic(2).Key, "utf8 code");
            MineruM6NativeSuite.EqualBytes(new byte[] { 0xff, 0xfe }, rig.Diagnostic(2).Value, "invalid bytes retained verbatim");
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Request(Tok("controller"), "");
                MineruM6NativeSuite.Check(client.ReadLine() == null, "empty body closed");
            }
            MineruM6NativeSuite.Equal(4, rig.WaitDiagnostics(4, 3000), "fourth");
            MineruM6NativeSuite.Equal("request_framing_invalid", rig.Diagnostic(3).Key, "empty frame code");
            for (int i = 0; i < 4; i++) {
                byte[] body = rig.Diagnostic(i).Value;
                if (body == null) continue;
                // Lenient decoder: one retained body is deliberately invalid UTF-8.
                MineruM6NativeSuite.Check(Encoding.UTF8.GetString(body).IndexOf(Tok("controller"), StringComparison.Ordinal) < 0, "token never retained in diagnostic " + i);
            }
        }
    }

    public static void Test05_ControlRefusalClosesWithRetainedRequest() {
        using (MineruM6EndpointRig rig = new MineruM6EndpointRig(3000, 6000, 4)) {
            rig.Handler = delegate(string raw, MineruM6Principal principal) { throw new MineruM6ControlRefusal("owner_not_bound"); };
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Request(Tok("controller"), Status);
                MineruM6NativeSuite.Check(client.ReadLine() == null, "refusal has no reply body");
            }
            MineruM6NativeSuite.Equal(1, rig.WaitDiagnostics(1, 3000), "diagnostic");
            MineruM6NativeSuite.Equal("owner_not_bound", rig.Diagnostic(0).Key, "refusal code");
            MineruM6NativeSuite.Equal(Status, MineruM6NativeSuite.Utf8.GetString(rig.Diagnostic(0).Value), "refused request retained verbatim");
            MineruM6NativeSuite.Check(rig.RunError == null, "refusal does not fail the endpoint");
        }
    }

    public static void Test06_HandlerOrSinkFailurePropagatesInsteadOfReplying() {
        using (MineruM6EndpointRig rig = new MineruM6EndpointRig(3000, 6000, 4)) {
            rig.Handler = delegate(string raw, MineruM6Principal principal) { return "{\"a\":1}\n{\"b\":2}"; };
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Request(Tok("controller"), Status);
                MineruM6NativeSuite.Check(client.ReadLine() == null, "no partial reply");
            }
            MineruM6NativeSuite.Check(rig.Finished.WaitOne(5000), "endpoint loop terminated");
            MineruM6NativeSuite.Check(rig.RunError is IOException, "invalid handler reply is an IO failure, got " + (rig.RunError == null ? "none" : rig.RunError.GetType().Name));
        }
        using (MineruM6EndpointRig rig = new MineruM6EndpointRig(3000, 6000, 4)) {
            rig.SinkFails = true;
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Request(new string('1', 64), Status);
                MineruM6NativeSuite.Check(client.ReadLine() == null, "closed");
            }
            MineruM6NativeSuite.Check(rig.Finished.WaitOne(5000), "endpoint loop terminated on sink failure");
            MineruM6NativeSuite.Check(rig.RunError is IOException, "diagnostic storage failure propagates");
        }
    }

    public static void Test07_TimeoutsPartialEofAndConnectionBound() {
        using (MineruM6EndpointRig rig = new MineruM6EndpointRig(2000, 4000, 2)) {
            using (MineruM6TestClient idle = new MineruM6TestClient(rig.Port)) {
                MineruM6NativeSuite.Check(idle.ReadLine() == null, "silent peer closed after the request deadline");
            }
            MineruM6NativeSuite.Equal(1, rig.WaitDiagnostics(1, 3000), "timeout diagnostic");
            MineruM6NativeSuite.Equal("request_timeout", rig.Diagnostic(0).Key, "request_timeout");
            using (MineruM6TestClient partial = new MineruM6TestClient(rig.Port)) {
                partial.Send("M6-AUTH/1 " + Tok("controller") + "\n{\"partial");
                partial.ShutdownSend();
                MineruM6NativeSuite.Check(partial.ReadLine() == null, "partial peer closed");
            }
            MineruM6NativeSuite.Equal(2, rig.WaitDiagnostics(2, 3000), "partial diagnostic");
            MineruM6NativeSuite.Equal("partial_request_eof", rig.Diagnostic(1).Key, "partial_request_eof");
            MineruM6NativeSuite.Equal("{\"partial", MineruM6NativeSuite.Utf8.GetString(rig.Diagnostic(1).Value), "partial body retained");
            using (MineruM6TestClient a = new MineruM6TestClient(rig.Port))
            using (MineruM6TestClient b = new MineruM6TestClient(rig.Port)) {
                Thread.Sleep(100);
                using (MineruM6TestClient c = new MineruM6TestClient(rig.Port)) {
                    MineruM6NativeSuite.Check(c.ReadLine() == null, "third peer refused");
                }
                MineruM6NativeSuite.Equal(3, rig.WaitDiagnostics(3, 3000), "bound diagnostic");
                MineruM6NativeSuite.Equal("connection_bound", rig.Diagnostic(2).Key, "connection_bound");
                a.Request(Tok("controller"), Status);
                MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("reply", "canonical"), a.ReadLine(), "bounded peers still served");
                b.Request(Tok("controller"), Status);
                MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("reply", "canonical"), b.ReadLine(), "second bounded peer served");
            }
        }
    }

    public static void Test08_ClosingStopsAdmittingConnections() {
        MineruM6EndpointRig rig = new MineruM6EndpointRig(3000, 6000, 4);
        try {
            using (MineruM6TestClient client = new MineruM6TestClient(rig.Port)) {
                client.Request(Tok("controller"), Status);
                MineruM6NativeSuite.Check(client.ReadLine() != null, "served before closing");
                rig.Closed = true;
                MineruM6NativeSuite.Check(client.ReadLine() == null, "idle peer closed once the owner is closed");
            }
            MineruM6NativeSuite.Check(rig.Finished.WaitOne(5000), "Run returned after closure");
            MineruM6NativeSuite.Check(rig.RunError == null, "clean return");
            MineruM6NativeSuite.ThrowsAny(delegate { new MineruM6TestClient(rig.Port).Dispose(); }, "listener released after closure");
        } finally { rig.Dispose(); }
    }

    public static void Test09_OwnerThreadIsEnforced() {
        using (MineruM6Credentials credentials = MineruM6CredentialTests.Standard()) {
            MineruM6Endpoint endpoint = new MineruM6Endpoint(MineruM6EndpointRig.FreePort(), credentials,
                delegate(string r, MineruM6Principal p) { return "{}"; }, delegate { }, delegate { return true; }, delegate(string c, byte[] b) { }, 1000, 2000, 2);
            Exception fromOther = null;
            Thread other = new Thread(delegate() { try { endpoint.Run(delegate { }); } catch (Exception error) { fromOther = error; } });
            other.Start(); other.Join();
            MineruM6NativeSuite.Check(fromOther is InvalidOperationException, "Run from another thread refused");
            endpoint.Dispose();
            MineruM6NativeSuite.Throws<InvalidOperationException>(delegate { endpoint.Run(delegate { }); }, "disposed endpoint");
        }
    }
}

// ---------------------------------------------------------------------------
// Run control over a real journal; deterministic injected QPC clock.
// ---------------------------------------------------------------------------
public sealed class MineruM6ControlRig : IDisposable {
    public readonly string Dir;
    public MineruM6JournalRig Journal;
    public MineruM6RunControl Control;
    public long Clock;
    public int ClosureAsserts;
    public readonly Dictionary<string, string> Receipts = new Dictionary<string, string>(StringComparer.Ordinal);
    public readonly Dictionary<string, string> Controls = new Dictionary<string, string>(StringComparer.Ordinal);
    public readonly Dictionary<string, string> Roles = new Dictionary<string, string>(StringComparer.Ordinal);
    public readonly string AnchorRaw, SpecSha, RunId, ControllerEpoch, RunnerEpoch, VerifierEpoch;
    public readonly long T0, Deadline, StopBudget;
    public const long MaxLease = 10000000, Reserve = 10000000;

    public MineruM6ControlRig(string dir, bool resume, string ownerEpoch, string anchorRaw, int maxEvents, int maxRecord, long maxLog) {
        Dir = dir;
        AnchorRaw = anchorRaw ?? MineruM6NativeSuite.FS("service", "anchor");
        MineruJsonValue anchor = MineruResidentWire.Parse(AnchorRaw, 65536);
        RunId = anchor.Get("run_id").String(); T0 = anchor.Get("t0_ticks").Integer(); Deadline = anchor.Get("deadline_ticks").Integer();
        StopBudget = anchor.Get("resources").Get("stop_admission_budget_ticks").Integer();
        SpecSha = MineruM6NativeSuite.FS("identities", "spec_sha256");
        ControllerEpoch = MineruM6NativeSuite.LabelHash("controller-epoch");
        RunnerEpoch = MineruM6NativeSuite.FS("identities", "runner_epoch");
        VerifierEpoch = MineruM6NativeSuite.FS("identities", "verifier_epoch");
        Roles.Add("controller", ControllerEpoch); Roles.Add("service_runner", RunnerEpoch); Roles.Add("quality_verifier", VerifierEpoch);
        Clock = T0;
        Journal = new MineruM6JournalRig(dir, resume, maxRecord, maxEvents, maxLog);
        try { Build(ownerEpoch, MaxLease, Reserve); } catch { Journal.Dispose(); throw; }
    }
    public MineruM6ControlRig(string dir, bool resume, string ownerEpoch)
        : this(dir, resume, ownerEpoch, null, 1000, 16384, 1000000) {}
    // Adopts an already-open journal (e.g. a recovered one) without building control;
    // the caller invokes Build so construction refusals can be asserted.
    public MineruM6ControlRig(MineruM6JournalRig existing, long clock) {
        Dir = existing.Dir;
        AnchorRaw = MineruM6NativeSuite.FS("service", "anchor");
        MineruJsonValue anchor = MineruResidentWire.Parse(AnchorRaw, 65536);
        RunId = anchor.Get("run_id").String(); T0 = anchor.Get("t0_ticks").Integer(); Deadline = anchor.Get("deadline_ticks").Integer();
        StopBudget = anchor.Get("resources").Get("stop_admission_budget_ticks").Integer();
        SpecSha = MineruM6NativeSuite.FS("identities", "spec_sha256");
        ControllerEpoch = MineruM6NativeSuite.LabelHash("controller-epoch");
        RunnerEpoch = MineruM6NativeSuite.FS("identities", "runner_epoch");
        VerifierEpoch = MineruM6NativeSuite.FS("identities", "verifier_epoch");
        Roles.Add("controller", ControllerEpoch); Roles.Add("service_runner", RunnerEpoch); Roles.Add("quality_verifier", VerifierEpoch);
        Journal = existing; Clock = clock;
    }
    public static MineruM6ControlRig Fresh() {
        return new MineruM6ControlRig(MineruM6NativeSuite.NewTempDir("control"), false, MineruM6NativeSuite.FS("identities", "owner_epoch"));
    }
    public void Build(string ownerEpoch, long maxLease, long reserve) {
        Control = new MineruM6RunControl(AnchorRaw, SpecSha, ownerEpoch, "service_diagnostic", Roles, Journal.Journal,
            delegate { return Clock; }, maxLease, reserve,
            delegate(string sha) { string raw; return Receipts.TryGetValue(sha, out raw) ? raw : null; },
            delegate(string name, string raw) { Controls[name] = raw; },
            delegate(string name) { string raw; return Controls.TryGetValue(name, out raw) ? raw : null; },
            delegate { ClosureAsserts++; });
    }
    public void Dispose() { if (Journal != null) Journal.Dispose(); Journal = null; }

    static string Q(string s) { return MineruResidentWire.Quote(s); }
    static string N(long n) { return n.ToString(CultureInfo.InvariantCulture); }
    public string Request(string requestId, string commandRaw) {
        return MineruResidentWire.Object("contract_version", "\"m6.owner-request.v1\"", "run_id", Q(RunId), "spec_sha256", Q(SpecSha),
            "request_id", Q(requestId), "command", commandRaw);
    }
    public string Simple(string kind) { return Request("req-" + kind + "-" + Guid.NewGuid().ToString("N").Substring(0, 8), "{\"kind\":\"" + kind + "\"}"); }
    public string Bind() { return Request("req-bind", MineruResidentWire.Object("kind", "\"bind\"", "anchor_sha256", Q(MineruM6NativeSuite.Sha(AnchorRaw)))); }
    public string Append(string producerRaw) { return Request("req-append-" + Guid.NewGuid().ToString("N").Substring(0, 8), MineruResidentWire.Object("kind", "\"append\"", "event", producerRaw)); }
    public string Producer(string kind, string epoch, long sequence, string payloadRaw) {
        return MineruResidentWire.Object("contract_version", "\"m6.producer-event.v1\"", "run_id", Q(RunId), "spec_sha256", Q(SpecSha),
            "producer_kind", Q(kind), "producer_epoch_sha256", Q(epoch), "producer_sequence", N(sequence), "payload", payloadRaw);
    }
    public string Admitted(string attemptId) {
        return MineruResidentWire.Object("kind", "\"attempt_admitted\"", "attempt_id", Q(attemptId), "fence_identity", "\"fence\"", "document_id", "null",
            "processing_run_id", "null", "source_pdf_sha256", Q(MineruM6NativeSuite.LabelHash("pdf|" + attemptId)), "source_byte_count", "10",
            "source_page_count", "1", "process_profile_sha256", Q(MineruM6NativeSuite.LabelHash("profile")));
    }
    public string Final(string attemptId) {
        return MineruResidentWire.Object("kind", "\"attempt_final\"", "attempt_id", Q(attemptId), "outcome", "\"failed\"", "remote_disposition", "\"not_submitted\"",
            "remote_receipt_sha256", "null", "remote_task_identity_sha256", "null", "cleanup_receipt_sha256", Q(MineruM6NativeSuite.LabelHash("cleanup|" + attemptId)));
    }
    public string Drained() { return MineruResidentWire.Object("kind", "\"verifier_drained\"", "drain_receipt_sha256", Q(MineruM6NativeSuite.LabelHash("drain"))); }
    // Independent implementation of the documented attempt-set rule.
    public static string AttemptSet(IEnumerable<string> ids) {
        List<string> lines = new List<string>();
        foreach (string id in ids) lines.Add(MineruM6NativeSuite.Sha(id));
        lines.Sort(StringComparer.Ordinal);
        StringBuilder text = new StringBuilder();
        foreach (string line in lines) text.Append(line).Append('\n');
        return MineruM6NativeSuite.Sha(text.ToString());
    }
    public string Register(string raw) { string sha = MineruM6NativeSuite.Sha(raw); Receipts[sha] = raw; return sha; }
    public string ReconciliationReceipt(long lastSequence, string[] admitted, long unresolved, string unresolvedSha) {
        return Register(MineruResidentWire.Object("contract_version", "\"m6.admission-reconciliation.v1\"", "run_id", Q(RunId), "spec_sha256", Q(SpecSha),
            "runner_epoch_sha256", Q(RunnerEpoch), "last_producer_sequence", N(lastSequence), "admitted_attempt_count", N(admitted.Length),
            "admitted_attempt_set_sha256", Q(AttemptSet(admitted)), "unresolved_claim_count", N(unresolved),
            "unresolved_receipt_sha256", unresolvedSha == null ? "null" : Q(unresolvedSha)));
    }
    public string Ack(long lastSequence, long count, long unresolved, string receiptSha) {
        return Request("req-ack-" + Guid.NewGuid().ToString("N").Substring(0, 8), MineruResidentWire.Object("kind", "\"admission_closed\"",
            "runner_epoch_sha256", Q(RunnerEpoch), "last_producer_sequence", N(lastSequence), "admitted_attempt_count", N(count),
            "unresolved_claim_count", N(unresolved), "reconciliation_receipt_sha256", Q(receiptSha)));
    }
    public string ClosureReceipt(string[] admitted, string[] finals, long residual, bool children) {
        string audit = Register("{\"audit\":\"" + Guid.NewGuid().ToString("N") + "\"}");
        return Register(MineruResidentWire.Object("contract_version", "\"m6.ownership-closure.v1\"", "run_id", Q(RunId), "spec_sha256", Q(SpecSha),
            "runner_epoch_sha256", Q(RunnerEpoch), "admitted_attempt_count", N(admitted.Length), "admitted_attempt_set_sha256", Q(AttemptSet(admitted)),
            "final_attempt_count", N(finals.Length), "final_attempt_set_sha256", Q(AttemptSet(finals)), "residual_count", N(residual),
            "children_exited", children ? "true" : "false", "resource_audit_sha256", Q(audit)));
    }
    public string Close(string receiptSha, long residual, bool children, string reason) {
        return Request("req-close-" + Guid.NewGuid().ToString("N").Substring(0, 8), MineruResidentWire.Object("kind", "\"close\"",
            "ownership_receipt_sha256", Q(receiptSha), "residual_count", N(residual), "children_exited", children ? "true" : "false", "reason", Q(reason)));
    }
    // Handles a request and applies the Python M6OwnerReply/M6OwnerStatus invariants to the reply.
    public MineruJsonValue Handle(string raw, string role) {
        string epoch = Roles[role];
        string reply = Control.Handle(raw, role, epoch);
        MineruJsonValue value = MineruResidentWire.Parse(reply, 65536);
        value.Keys("contract_version", "error_code", "outcome", "record", "request_sha256", "status");
        MineruM6NativeSuite.Equal("m6.owner-reply.v1", value.Get("contract_version").String(), "reply version");
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.Sha(raw), value.Get("request_sha256").String(), "reply binds this exact request");
        MineruJsonValue status = value.Get("status");
        status.Keys("admission_valid_until_ticks", "anchor_sha256", "contract_version", "last_sequence", "observed_qpc_ticks",
            "owner_process_epoch_sha256", "run_id", "spec_sha256", "state");
        MineruM6NativeSuite.Equal(RunId, status.Get("run_id").String(), "status run");
        MineruM6NativeSuite.Equal(SpecSha, status.Get("spec_sha256").String(), "status spec");
        MineruM6NativeSuite.Equal(MineruM6NativeSuite.Sha(AnchorRaw), status.Get("anchor_sha256").String(), "status anchor");
        string outcome = value.Get("outcome").String();
        MineruM6NativeSuite.Check((outcome == "ok") == (value.Get("error_code").Raw == "null"), "outcome/error agreement");
        MineruM6NativeSuite.Check(outcome != "conflict" || value.Get("record").Raw != "null", "conflict carries its record");
        MineruM6NativeSuite.Check(outcome != "rejected" || value.Get("record").Raw == "null", "rejected carries no record");
        if (status.Get("admission_valid_until_ticks").Raw != "null")
            MineruM6NativeSuite.Check(status.Get("state").String() == "open" && status.Get("admission_valid_until_ticks").Integer() > status.Get("observed_qpc_ticks").Integer(),
                "lease only while open and in the future");
        if (value.Get("record").Raw != "null") {
            MineruJsonValue record = value.Get("record");
            MineruM6NativeSuite.Equal(record.Raw, MineruM6OwnerWire.Record(record.Raw), "reply record canonical");
            MineruM6NativeSuite.Check(record.Get("stamp").Get("sequence").Integer() <= status.Get("last_sequence").Integer(), "record sequence within status");
            MineruM6NativeSuite.Check(record.Get("stamp").Get("received_qpc_ticks").Integer() <= status.Get("observed_qpc_ticks").Integer(), "record tick within status");
        }
        return value;
    }
    public string Ok(string raw, string role) {
        MineruJsonValue reply = Handle(raw, role);
        if (reply.Get("outcome").String() != "ok") throw new MineruM6TestFailure("expected ok, got " + reply.Get("outcome").String() + "/" + reply.Get("error_code").Raw + " for " + raw);
        return reply.Get("record").Raw;
    }
    public string Rejected(string raw, string role) {
        MineruJsonValue reply = Handle(raw, role);
        if (reply.Get("outcome").String() != "rejected") throw new MineruM6TestFailure("expected rejected, got " + reply.Get("outcome").String() + " for " + raw);
        return reply.Get("error_code").String();
    }
    public string State() { return Control.State; }
    public List<string> Records() { List<string> all = new List<string>(); foreach (string r in Journal.Journal.ReadRecords()) all.Add(r); return all; }
    public string Kind(int index) { return MineruResidentWire.Parse(Records()[index], 65536).Get("event").Get("payload").Get("kind").String(); }
    // Drive the run to a state where close is possible: bind, open, admit A, final A, stop, ack, drain.
    public string[] DriveToDrained(string reason) {
        Ok(Bind(), "controller"); Clock += 10; Ok(Simple("open"), "controller"); Clock += 10;
        Ok(Append(Producer("service_runner", RunnerEpoch, 1, Admitted("attempt-x"))), "service_runner"); Clock += 10;
        Ok(Append(Producer("service_runner", RunnerEpoch, 2, Final("attempt-x"))), "service_runner"); Clock += 10;
        Ok(Simple("stop"), "service_runner"); Clock += 10;
        Ok(Ack(2, 1, 0, ReconciliationReceipt(2, new string[] { "attempt-x" }, 0, null)), "service_runner"); Clock += 10;
        Ok(Append(Producer("quality_verifier", VerifierEpoch, 1, Drained())), "quality_verifier"); Clock += 10;
        return new string[] { "attempt-x" };
    }
}

public static class MineruM6ControlTests {
    static string FixtureProducer(int i) { return MineruM6JournalRig.ProducerRaw(i); }
    static string FixtureRecord(int i) { return MineruM6JournalRig.RecordRaw(i); }

    public static void Test01_ConstructionStampsRunStartedAtT0AndValidatesDependencies() {
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            List<string> records = rig.Records();
            MineruM6NativeSuite.Equal(1, records.Count, "one record after construction");
            MineruM6NativeSuite.Equal(FixtureRecord(0), records[0], "run_started equals the Python-built record (sequence 1 at T0)");
            MineruM6NativeSuite.Equal("bound", rig.State(), "initial state");
            MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("attempt_set", "sha256"), MineruM6ControlRig.AttemptSet(new string[] { "attempt-b", "attempt-a", "attempt-é-1" }), "independent attempt-set rule matches Python");
            MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("attempt_set", "sha256"), MineruM6RunControl.AttemptSetSha(new string[] { "attempt-é-1", "attempt-b", "attempt-a" }), "native attempt-set rule matches Python");
        }
        string dir = MineruM6NativeSuite.NewTempDir("control");
        MineruM6ControlRefusal refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { new MineruM6ControlRig(dir, false, MineruM6NativeSuite.LabelHash("stranger")).Dispose(); }, "owner epoch differs from anchor on a fresh run");
        MineruM6NativeSuite.Equal("initial_owner_epoch_differs", refusal.Code, "refusal code");
        string dir2 = MineruM6NativeSuite.NewTempDir("control");
        refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { new MineruM6ControlRig(dir2, false, MineruM6NativeSuite.FS("identities", "owner_epoch"), null, 999, 16384, 1000000).Dispose(); }, "journal bounds differ from anchor");
        MineruM6NativeSuite.Equal("journal_bounds_differ_from_anchor", refusal.Code, "bounds code");
        using (MineruM6ControlRig probe = new MineruM6ControlRig(new MineruM6JournalRig(MineruM6NativeSuite.NewTempDir("control"), false), 0)) {
            string owner = MineruM6NativeSuite.FS("identities", "owner_epoch");
            refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { probe.Build(owner, probe.StopBudget, MineruM6ControlRig.Reserve); }, "lease leaves no stop reserve");
            MineruM6NativeSuite.Equal("lease_has_no_stop_reserve", refusal.Code, "lease code");
            refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { probe.Build(owner, MineruM6ControlRig.MaxLease, 0); }, "zero propagation reserve");
            MineruM6NativeSuite.Equal("lease_has_no_stop_reserve", refusal.Code, "reserve code");
            probe.Roles.Add("public_verifier", MineruM6NativeSuite.LabelHash("pv"));
            refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { probe.Build(owner, MineruM6ControlRig.MaxLease, MineruM6ControlRig.Reserve); }, "public verifier in service mode");
            MineruM6NativeSuite.Equal("extra_caller_role", refusal.Code, "extra role code");
            probe.Roles.Remove("public_verifier"); probe.Roles.Remove("quality_verifier");
            refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { probe.Build(owner, MineruM6ControlRig.MaxLease, MineruM6ControlRig.Reserve); }, "missing verifier role");
            MineruM6NativeSuite.Equal("caller_roles_missing", refusal.Code, "missing role code");
            MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate {
                new MineruM6RunControl(MineruM6NativeSuite.FS("service", "anchor"), probe.SpecSha, owner, "benchmark", probe.Roles, probe.Journal.Journal,
                    delegate { return 0L; }, MineruM6ControlRig.MaxLease, MineruM6ControlRig.Reserve, delegate(string s) { return null; },
                    delegate(string n, string r) { }, delegate(string n) { return null; }, delegate { });
            }, "unknown mode");
            MineruM6NativeSuite.Equal(0, probe.Journal.Journal.LastSequence, "construction refusals write nothing");
        }
    }

    public static void Test02_PreOpenControlAndBindingRefusals() {
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            MineruJsonValue lease = rig.Handle(rig.Simple("lease"), "service_runner");
            MineruM6NativeSuite.Equal("ok", lease.Get("outcome").String(), "lease before open is ok");
            MineruM6NativeSuite.Equal("null", lease.Get("status").Get("admission_valid_until_ticks").Raw, "lease before open is null");
            MineruM6NativeSuite.Equal("controller_required", rig.Rejected(rig.Simple("open"), "service_runner"), "runner cannot open");
            MineruM6NativeSuite.Equal("runner_or_controller_required", rig.Rejected(rig.Simple("stop"), "quality_verifier"), "verifier cannot stop");
            MineruM6NativeSuite.Equal("selected_runner_required", rig.Rejected(rig.Simple("lease"), "quality_verifier"), "verifier cannot lease");
            string wrongAnchor = rig.Request("req-bind-wrong", MineruResidentWire.Object("kind", "\"bind\"", "anchor_sha256", MineruResidentWire.Quote(MineruM6NativeSuite.LabelHash("other-anchor"))));
            MineruM6NativeSuite.Equal("bind_identity_or_role_differs", rig.Rejected(wrongAnchor, "controller"), "bind with a different anchor");
            MineruM6NativeSuite.Equal("bind_identity_or_role_differs", rig.Rejected(rig.Bind(), "service_runner"), "bind by runner");
            MineruM6ControlRefusal refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { rig.Control.Handle(rig.Simple("status"), "controller", MineruM6NativeSuite.LabelHash("stale")); }, "stale caller epoch");
            MineruM6NativeSuite.Equal("unauthorized_caller_incarnation", refusal.Code, "epoch refusal is not a reply");
            string otherRun = rig.Simple("status").Replace(rig.RunId, "run-other");
            refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { rig.Control.Handle(otherRun, "controller", rig.ControllerEpoch); }, "other run id");
            MineruM6NativeSuite.Equal("stored_run_spec_differs", refusal.Code, "run binding refusal is not a reply");
            MineruM6NativeSuite.Throws<FormatException>(delegate { rig.Control.Handle("{\"kind\":\"exec\"}", "controller", rig.ControllerEpoch); }, "non-request bytes");
            MineruM6NativeSuite.Equal(1, rig.Records().Count, "control refusals write nothing");
            MineruM6NativeSuite.Equal("bound", rig.State(), "still bound");
            rig.Ok(rig.Bind(), "controller");
            rig.Ok(rig.Bind(), "controller");
            MineruM6NativeSuite.Equal("ok", rig.Handle(rig.Simple("status"), "quality_verifier").Get("outcome").String(), "any role reads status");
        }
    }

    public static void Test03_HappyPathMatchesPythonRecordsAndCloses() {
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Clock = rig.T0 + 5; rig.Ok(rig.Bind(), "controller");
            rig.Clock = rig.T0 + 10; rig.Ok(rig.Simple("open"), "controller");
            MineruM6NativeSuite.Equal(FixtureRecord(1), rig.Records()[1], "admission_opened equals Python record");
            MineruM6NativeSuite.Equal("open", rig.State(), "open");
            rig.Clock = rig.T0 + 15;
            MineruJsonValue lease = rig.Handle(rig.Simple("lease"), "service_runner");
            MineruM6NativeSuite.Equal(rig.T0 + 15, lease.Get("status").Get("observed_qpc_ticks").Integer(), "observed tick is the injected clock");
            MineruM6NativeSuite.Equal(rig.T0 + 15 + MineruM6ControlRig.MaxLease, lease.Get("status").Get("admission_valid_until_ticks").Integer(), "lease is now + maximum lease, capped by deadline");
            rig.Clock = rig.T0 + 20;
            string appended = rig.Ok(MineruM6NativeSuite.FS("requests", "append"), "service_runner");
            MineruM6NativeSuite.Equal(FixtureRecord(2), appended, "attempt_admitted stamp equals Python record");
            rig.Clock = rig.T0 + 25;
            MineruM6NativeSuite.Equal(FixtureRecord(2), rig.Ok(MineruM6NativeSuite.FS("requests", "append"), "service_runner"), "exact retry returns the original stamp even later");
            MineruM6NativeSuite.Equal(3, rig.Journal.Journal.LastSequence, "retry wrote nothing");
            rig.Clock = rig.T0 + 30;
            MineruM6NativeSuite.Equal(FixtureRecord(3), rig.Ok(rig.Append(FixtureProducer(3)), "service_runner"), "attempt_final equals Python record");
            rig.Clock = rig.T0 + 40; rig.Ok(rig.Simple("stop"), "service_runner");
            MineruM6NativeSuite.Equal(FixtureRecord(4), rig.Records()[4], "stop_admission_requested equals Python record");
            MineruM6NativeSuite.Equal("stopping", rig.State(), "stopping");
            MineruM6NativeSuite.Equal("null", rig.Handle(rig.Simple("lease"), "service_runner").Get("status").Get("admission_valid_until_ticks").Raw, "no lease after stop requested");
            rig.Clock = rig.T0 + 45;
            string attempt = MineruResidentWire.Parse(FixtureProducer(2), 65536).Get("payload").Get("attempt_id").String();
            string ack = rig.Ack(2, 1, 0, rig.ReconciliationReceipt(2, new string[] { attempt }, 0, null));
            rig.Ok(ack, "service_runner");
            MineruM6NativeSuite.Equal("draining", rig.State(), "draining after ACK");
            MineruM6NativeSuite.Equal("stop_admission_effective", rig.Kind(5), "effective stop after validated ACK");
            MineruM6NativeSuite.Equal(MineruResidentWire.Parse(ack, 65536).Get("command").Raw, rig.Controls["admission-closed"], "exact ACK archived before its event");
            rig.Ok(ack, "service_runner");
            MineruM6NativeSuite.Equal(6, rig.Journal.Journal.LastSequence, "ACK retry is idempotent");
            rig.Clock = rig.T0 + 50;
            rig.Ok(MineruM6NativeSuite.FS("requests", "drain_a"), "quality_verifier");
            MineruM6NativeSuite.Equal("verifier_drained", rig.Kind(6), "drain recorded");
            rig.Clock = rig.T0 + 60;
            string receipt = rig.ClosureReceipt(new string[] { attempt }, new string[] { attempt }, 0, true);
            MineruM6NativeSuite.Equal("deadline_not_reached", rig.Rejected(rig.Close(receipt, 0, true, "deadline_drained"), "controller"), "deadline_drained before the deadline");
            MineruM6NativeSuite.Equal(0, rig.ClosureAsserts, "native closure not asserted for a refused close");
            string close = rig.Close(receipt, 0, true, "stop_requested");
            rig.Ok(close, "controller");
            MineruM6NativeSuite.Equal(1, rig.ClosureAsserts, "native closure asserted exactly once");
            MineruM6NativeSuite.Equal("closed", rig.State(), "closed");
            MineruM6NativeSuite.Check(rig.Control.IsClosed && rig.Journal.Journal.IsClosed, "journal sealed");
            MineruM6NativeSuite.Equal("resources_closed", rig.Kind(7), "resources_closed precedes run_closed");
            MineruJsonValue closedRecord = MineruResidentWire.Parse(rig.Records()[8], 65536);
            MineruM6NativeSuite.Equal("run_closed", closedRecord.Get("event").Get("payload").Get("kind").String(), "run_closed last");
            MineruM6NativeSuite.Equal(rig.T0 + 60, closedRecord.Get("event").Get("payload").Get("tclose_ticks").Integer(), "tclose is the owner receipt tick");
            MineruM6NativeSuite.Equal(rig.T0 + 60, closedRecord.Get("stamp").Get("received_qpc_ticks").Integer(), "run_closed stamped at tclose");
            MineruM6NativeSuite.Equal("stop_requested", closedRecord.Get("event").Get("payload").Get("reason").String(), "close reason");
            MineruM6NativeSuite.Equal(MineruResidentWire.Parse(close, 65536).Get("command").Raw, rig.Controls["resources-closed"], "closure command archived");
            rig.Clock = rig.T0 + 70;
            rig.Ok(close, "controller");
            MineruM6NativeSuite.Equal(9, rig.Journal.Journal.LastSequence, "close retry writes nothing");
            string other = rig.ClosureReceipt(new string[] { attempt }, new string[] { attempt }, 0, true);
            MineruM6NativeSuite.Equal("closure_receipt_changed", rig.Rejected(rig.Close(other, 0, true, "stop_requested"), "controller"), "different closure after close");
            MineruM6NativeSuite.Equal("owner_already_closed", rig.Rejected(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 3, rig.Final("late"))), "service_runner"), "new observation after close");
            MineruM6NativeSuite.Equal(FixtureRecord(2), rig.Ok(MineruM6NativeSuite.FS("requests", "append"), "service_runner"), "exact retry served after close");
            MineruM6NativeSuite.Equal(9, rig.Journal.Journal.LastSequence, "no new stamp after close");
            MineruM6NativeSuite.Equal("closed", rig.State(), "state unchanged by post-close refusals");
        }
    }

    public static void Test04_ConflictingProducerBytesAreStampedAndStopAdmission() {
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller"); rig.Clock += 10;
            rig.Ok(MineruM6NativeSuite.FS("requests", "append"), "service_runner"); rig.Clock += 10;
            string conflicting = rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Admitted("attempt-conflict"));
            MineruJsonValue reply = rig.Handle(rig.Append(conflicting), "service_runner");
            MineruM6NativeSuite.Equal("conflict", reply.Get("outcome").String(), "conflict outcome");
            MineruM6NativeSuite.Equal("producer_event_conflict", reply.Get("error_code").String(), "conflict code");
            MineruM6NativeSuite.Equal(conflicting, reply.Get("record").Get("event").Raw, "conflict reply carries the submitted bytes, not the predecessor");
            MineruM6NativeSuite.Equal(4, reply.Get("record").Get("stamp").Get("sequence").Integer(), "conflict durably stamped");
            MineruM6NativeSuite.Equal("stop_admission_requested", rig.Kind(4), "conflict stops admission");
            MineruM6NativeSuite.Equal("failed", rig.State(), "conflict marks the run failed");
        }
    }

    public static void Test05_RefusedBusinessObservationBindsOneIncidentAndStops() {
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller"); rig.Clock += 10;
            string producer = rig.Producer("quality_verifier", rig.VerifierEpoch, 1, rig.Drained());
            MineruM6NativeSuite.Equal("verifier_drain_pending", rig.Rejected(rig.Append(producer), "quality_verifier"), "drain before stop is pending");
            MineruM6NativeSuite.Equal(2, rig.Journal.Journal.LastSequence, "pending drain is flow control: no incident");
            MineruM6NativeSuite.Equal("open", rig.State(), "state unchanged by pending drain");
            string wrongRole = rig.Producer("quality_verifier", rig.VerifierEpoch, 2, rig.Admitted("attempt-w"));
            MineruM6NativeSuite.Equal("observation_role_differs", rig.Rejected(rig.Append(wrongRole), "service_runner"), "runner submitting a verifier producer");
            MineruM6NativeSuite.Equal(2, rig.Journal.Journal.LastSequence, "wrong-role request is refused before attributing a business observation");
            string refused = rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Final("never-admitted"));
            MineruM6NativeSuite.Equal("attempt_not_admitted", rig.Rejected(rig.Append(refused), "service_runner"), "correct authenticated producer has invalid business reference");
            List<string> records = rig.Records();
            MineruM6NativeSuite.Equal(4, records.Count, "incident + stop requested");
            MineruJsonValue incident = MineruResidentWire.Parse(records[2], 65536).Get("event").Get("payload");
            MineruM6NativeSuite.Equal("measurement_incident", incident.Get("kind").String(), "incident kind");
            MineruM6NativeSuite.Equal("observation_refused_attempt_not_admitted", incident.Get("code").String(), "incident code binds the refusal");
            MineruM6NativeSuite.Equal(MineruM6NativeSuite.Sha(refused), incident.Get("evidence_sha256").String(), "incident evidence is the refused producer bytes hash");
            MineruM6NativeSuite.Equal("stop_admission_requested", rig.Kind(3), "admission stopped");
            MineruM6NativeSuite.Equal("failed", rig.State(), "run reports failed");
            string notAdmitted = rig.Producer("service_runner", rig.RunnerEpoch, 2, rig.Final("second-never-admitted"));
            MineruM6NativeSuite.Equal("attempt_not_admitted", rig.Rejected(rig.Append(notAdmitted), "service_runner"), "second refusal");
            MineruM6NativeSuite.Equal(4, rig.Journal.Journal.LastSequence, "at most one incident per run");
            MineruM6NativeSuite.Equal("null", rig.Handle(rig.Simple("lease"), "service_runner").Get("status").Get("admission_valid_until_ticks").Raw, "no lease on a failed run");
            rig.Ok(rig.Ack(0, 0, 0, rig.ReconciliationReceipt(0, new string[0], 0, null)), "service_runner");
            rig.Ok(rig.Append(producer), "quality_verifier");
            rig.Ok(rig.Close(rig.ClosureReceipt(new string[0], new string[0], 0, true), 0, true, "stop_requested"), "controller");
            List<string> closedRecords = rig.Records();
            MineruJsonValue closure = MineruResidentWire.Parse(closedRecords[closedRecords.Count - 1], 65536).Get("event").Get("payload");
            MineruM6NativeSuite.Equal("failed", closure.Get("reason").String(), "actual closure after refused business observation cannot become a clean zero run");
        }
    }

    public static void Test06_AdmissionGateBeforeOpenAfterStopAndDuplicates() {
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10;
            MineruM6NativeSuite.Equal("admission_not_open", rig.Rejected(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Admitted("a"))), "service_runner"), "admission before open");
            MineruM6NativeSuite.Equal("failed", rig.State(), "refused admission is an incident");
        }
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Admitted("a"))), "service_runner"); rig.Clock += 10;
            MineruM6NativeSuite.Equal("duplicate_attempt_admission", rig.Rejected(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 2, rig.Admitted("a"))), "service_runner"), "same attempt under a new sequence");
        }
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller"); rig.Clock += 10;
            rig.Ok(rig.Simple("stop"), "controller"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Admitted("inflight"))), "service_runner");
            MineruM6NativeSuite.Equal("stopping", rig.State(), "in-flight claim reported after stop requested is retained");
            rig.Clock += 10; rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 2, rig.Final("inflight"))), "service_runner"); rig.Clock += 10;
            rig.Ok(rig.Ack(2, 1, 0, rig.ReconciliationReceipt(2, new string[] { "inflight" }, 0, null)), "service_runner"); rig.Clock += 10;
            MineruM6NativeSuite.Equal("admission_not_open", rig.Rejected(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 3, rig.Admitted("late"))), "service_runner"), "new admission after effective stop");
        }
    }

    public static void Test07_AdmissionClosedAckValidation() {
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Admitted("a"))), "service_runner"); rig.Clock += 10;
            string goodReceipt = rig.ReconciliationReceipt(1, new string[] { "a" }, 0, null);
            MineruM6NativeSuite.Equal("admission_stop_not_requested", rig.Rejected(rig.Ack(1, 1, 0, goodReceipt), "service_runner"), "ACK before stop");
            rig.Ok(rig.Simple("stop"), "controller"); rig.Clock += 10;
            MineruM6NativeSuite.Equal("selected_runner_required", rig.Rejected(rig.Ack(1, 1, 0, goodReceipt), "controller"), "ACK by controller");
            string wrongRunner = rig.Ack(1, 1, 0, goodReceipt).Replace(rig.RunnerEpoch, MineruM6NativeSuite.LabelHash("other-runner"));
            MineruM6NativeSuite.Equal("admission_ack_runner_differs", rig.Rejected(wrongRunner, "service_runner"), "ACK for another incarnation");
            MineruM6NativeSuite.Equal("admission_ack_index_differs", rig.Rejected(rig.Ack(1, 2, 0, goodReceipt), "service_runner"), "count differs from admissions");
            MineruM6NativeSuite.Equal("admission_ack_index_differs", rig.Rejected(rig.Ack(0, 1, 0, goodReceipt), "service_runner"), "sequence below last admission");
            MineruM6NativeSuite.Equal("receipt_missing_or_over_bound", rig.Rejected(rig.Ack(1, 1, 0, MineruM6NativeSuite.LabelHash("no-such-receipt")), "service_runner"), "receipt not pinned");
            string wrongSet = rig.ReconciliationReceipt(1, new string[] { "b" }, 0, null);
            MineruM6NativeSuite.Equal("admission_receipt_set_differs", rig.Rejected(rig.Ack(1, 1, 0, wrongSet), "service_runner"), "receipt attempt set differs");
            string unresolvedReceipt = rig.Register("{\"unresolved\":[\"h0\"]}");
            string withoutUnresolved = rig.ReconciliationReceipt(1, new string[] { "a" }, 1, null);
            MineruM6NativeSuite.Equal("admission_receipt_counts_differ", rig.Rejected(rig.Ack(1, 1, 0, withoutUnresolved), "service_runner"), "command/receipt unresolved counts differ");
            MineruM6NativeSuite.Equal("stopping", rig.State(), "no effective stop after refused ACKs");
            string unresolved = rig.ReconciliationReceipt(1, new string[] { "a" }, 1, unresolvedReceipt);
            string ack = rig.Ack(1, 1, 1, unresolved);
            rig.Ok(ack, "service_runner");
            MineruM6NativeSuite.Equal("measurement_incident", rig.Kind(4), "unclaimed H0 is a permanent incident");
            MineruM6NativeSuite.Equal("h0_committed_unclaimed", MineruResidentWire.Parse(rig.Records()[4], 65536).Get("event").Get("payload").Get("code").String(), "incident code");
            MineruM6NativeSuite.Equal("stop_admission_effective", rig.Kind(5), "stop becomes effective");
            MineruM6NativeSuite.Equal("failed", rig.State(), "failed");
            MineruM6NativeSuite.Equal("admission_ack_changed", rig.Rejected(rig.Ack(1, 1, 0, goodReceipt), "service_runner"), "a different ACK after the archived one");
            rig.Ok(ack, "service_runner");
            MineruM6NativeSuite.Equal(6, rig.Journal.Journal.LastSequence, "exact ACK retry writes nothing");
        }
    }

    public static void Test08_CloseRefusalsNeverAssertNativeClosure() {
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Admitted("a"))), "service_runner"); rig.Clock += 10;
            string receipt = rig.ClosureReceipt(new string[] { "a" }, new string[] { "a" }, 0, true);
            MineruM6NativeSuite.Equal("controller_required", rig.Rejected(rig.Close(receipt, 0, true, "stop_requested"), "service_runner"), "runner cannot close");
            MineruM6NativeSuite.Equal("business_drain_pending", rig.Rejected(rig.Close(receipt, 0, true, "stop_requested"), "controller"), "close before stop/final/drain");
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 2, rig.Final("a"))), "service_runner"); rig.Clock += 10;
            rig.Ok(rig.Simple("stop"), "service_runner"); rig.Clock += 10;
            rig.Ok(rig.Ack(2, 1, 0, rig.ReconciliationReceipt(2, new string[] { "a" }, 0, null)), "service_runner"); rig.Clock += 10;
            MineruM6NativeSuite.Equal("business_drain_pending", rig.Rejected(rig.Close(receipt, 0, true, "stop_requested"), "controller"), "close before verifier drain");
            rig.Ok(rig.Append(rig.Producer("quality_verifier", rig.VerifierEpoch, 1, rig.Drained())), "quality_verifier"); rig.Clock += 10;
            MineruM6NativeSuite.Equal("verifier_already_drained", rig.Rejected(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 3, rig.Final("a"))), "service_runner"), "new evidence after drain");
            MineruM6NativeSuite.Equal("resource_closure_pending", rig.Rejected(rig.Close(receipt, 1, true, "stop_requested"), "controller"), "residual resources");
            MineruM6NativeSuite.Equal("resource_closure_pending", rig.Rejected(rig.Close(receipt, 0, false, "stop_requested"), "controller"), "children not exited");
            string dirty = rig.ClosureReceipt(new string[] { "a" }, new string[] { "a" }, 1, true);
            MineruM6NativeSuite.Equal("closure_receipt_not_clean", rig.Rejected(rig.Close(dirty, 0, true, "stop_requested"), "controller"), "receipt admits residuals");
            string wrongFinal = rig.ClosureReceipt(new string[] { "a" }, new string[] { "b" }, 0, true);
            MineruM6NativeSuite.Equal("closure_receipt_set_differs", rig.Rejected(rig.Close(wrongFinal, 0, true, "stop_requested"), "controller"), "receipt final set differs");
            MineruM6NativeSuite.Equal(0, rig.ClosureAsserts, "no native closure assertion for refused closes");
            MineruM6NativeSuite.Check(!rig.Controls.ContainsKey("resources-closed"), "no closure sidecar archived");
            rig.Ok(rig.Close(receipt, 0, true, "stop_requested"), "controller");
            MineruM6NativeSuite.Equal(1, rig.ClosureAsserts, "native closure asserted for the accepted close");
            MineruM6NativeSuite.Equal("failed", MineruResidentWire.Parse(rig.Records()[rig.Records().Count - 1], 65536).Get("event").Get("payload").Get("reason").String(),
                "a refused observation earlier in the run forces reason failed");
        }
    }

    public static void Test09_DeadlineClosesTheGateAndAllowsDeadlineDrainedClose() {
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Admitted("a"))), "service_runner"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 2, rig.Final("a"))), "service_runner");
            rig.Clock = rig.Deadline;
            MineruJsonValue status = rig.Handle(rig.Simple("lease"), "service_runner");
            MineruM6NativeSuite.Equal("stopping", status.Get("status").Get("state").String(), "deadline requests stop on the next tick");
            MineruM6NativeSuite.Equal("null", status.Get("status").Get("admission_valid_until_ticks").Raw, "no lease at the deadline");
            MineruM6NativeSuite.Equal("stop_admission_requested", rig.Kind(4), "stop_admission_requested recorded");
            MineruM6NativeSuite.Equal(rig.Deadline, MineruResidentWire.Parse(rig.Records()[4], 65536).Get("stamp").Get("received_qpc_ticks").Integer(), "stop stamped at the observing tick");
            MineruM6NativeSuite.Equal("admission_cannot_reopen", rig.Rejected(rig.Simple("open"), "controller"), "open after deadline");
            rig.Clock += 10; rig.Ok(rig.Ack(2, 1, 0, rig.ReconciliationReceipt(2, new string[] { "a" }, 0, null)), "service_runner"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("quality_verifier", rig.VerifierEpoch, 1, rig.Drained())), "quality_verifier"); rig.Clock += 10;
            rig.Ok(rig.Close(rig.ClosureReceipt(new string[] { "a" }, new string[] { "a" }, 0, true), 0, true, "deadline_drained"), "controller");
            MineruM6NativeSuite.Equal("deadline_drained", MineruResidentWire.Parse(rig.Records()[rig.Records().Count - 1], 65536).Get("event").Get("payload").Get("reason").String(), "close reason");
        }
        using (MineruM6ControlRig rig = MineruM6ControlRig.Fresh()) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller");
            rig.Clock -= 5;
            MineruM6NativeSuite.Throws<IOException>(delegate { rig.Control.Handle(rig.Simple("status"), "controller", rig.ControllerEpoch); }, "QPC regression is a failure, not a reply");
        }
    }

    public static void Test10_SameBootResumeChainAndLockout() {
        string dir = MineruM6NativeSuite.NewTempDir("control");
        string owner = MineruM6NativeSuite.FS("identities", "owner_epoch"), owner2 = MineruM6NativeSuite.FS("identities", "owner_epoch_2");
        using (MineruM6ControlRig first = new MineruM6ControlRig(dir, false, owner)) {
            first.Clock = first.T0 + 5; first.Ok(first.Bind(), "controller");
            first.Clock = first.T0 + 10; first.Ok(first.Simple("open"), "controller");
            first.Clock = first.T0 + 20; first.Ok(MineruM6NativeSuite.FS("requests", "append"), "service_runner");
            first.Clock = first.T0 + 30; first.Ok(first.Append(FixtureProducer(3)), "service_runner");
            first.Clock = first.T0 + 40; first.Ok(first.Simple("stop"), "service_runner");
        }
        using (MineruM6ControlRig same = new MineruM6ControlRig(new MineruM6JournalRig(dir, true), 0)) {
            MineruM6ControlRefusal refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { same.Build(owner, MineruM6ControlRig.MaxLease, MineruM6ControlRig.Reserve); }, "resume with the same incarnation");
            MineruM6NativeSuite.Equal("resume_requires_new_process_incarnation", refusal.Code, "same-epoch resume code");
            MineruM6NativeSuite.Equal(5, same.Journal.Journal.LastSequence, "refused resume wrote nothing");
        }
        using (MineruM6ControlRig rig = new MineruM6ControlRig(new MineruM6JournalRig(dir, true), 0)) {
            rig.Clock = rig.T0 + 50;
            rig.Build(owner2, MineruM6ControlRig.MaxLease, MineruM6ControlRig.Reserve);
            {
                MineruM6NativeSuite.Equal(MineruM6NativeSuite.FS("journal", "resumed_record"), rig.Records()[5], "owner_resumed stamp equals Python record (original clock/T0/deadline, predecessor named)");
                MineruM6NativeSuite.Equal("stopping", rig.State(), "recovered owner keeps admission stopped");
                MineruM6NativeSuite.Equal(6, rig.Journal.Journal.LastSequence, "no duplicate stop request on resume");
                rig.Clock += 10;
                MineruJsonValue lease = rig.Handle(rig.Simple("lease"), "service_runner");
                MineruM6NativeSuite.Equal("null", lease.Get("status").Get("admission_valid_until_ticks").Raw, "no lease after recovery");
                MineruM6NativeSuite.Equal(owner2, lease.Get("status").Get("owner_process_epoch_sha256").String(), "status reports the new incarnation");
                rig.Clock += 10;
                MineruM6NativeSuite.Equal(FixtureRecord(2), rig.Ok(MineruM6NativeSuite.FS("requests", "append"), "service_runner"), "exact retry returns the predecessor owner's stamp");
                string attempt = MineruResidentWire.Parse(FixtureProducer(2), 65536).Get("payload").Get("attempt_id").String();
                rig.Clock += 10; rig.Ok(rig.Ack(2, 1, 0, rig.ReconciliationReceipt(2, new string[] { attempt }, 0, null)), "service_runner");
                rig.Clock += 10; rig.Ok(MineruM6NativeSuite.FS("requests", "drain_a"), "quality_verifier");
                rig.Clock += 10; rig.Ok(rig.Close(rig.ClosureReceipt(new string[] { attempt }, new string[] { attempt }, 0, true), 0, true, "stop_requested"), "controller");
                MineruM6NativeSuite.Equal("closed", rig.State(), "recovered run closes");
                MineruJsonValue last = MineruResidentWire.Parse(rig.Records()[rig.Records().Count - 1], 65536);
                MineruM6NativeSuite.Equal(owner2, last.Get("stamp").Get("owner_process_epoch_sha256").String(), "closure stamped by the recovered owner");
            }
        }
        MineruM6ControlRefusal closedRefusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { new MineruM6ControlRig(dir, true, MineruM6NativeSuite.LabelHash("owner-3")).Dispose(); }, "closed run cannot be resumed");
        MineruM6NativeSuite.Equal("closed_run_requires_offline_reconciliation", closedRefusal.Code, "closed-run resume code");
    }

    public static void Test11_ArchivedControlSidecarsMustMatchOnResume() {
        string dir = MineruM6NativeSuite.NewTempDir("control");
        string owner = MineruM6NativeSuite.FS("identities", "owner_epoch"), owner2 = MineruM6NativeSuite.FS("identities", "owner_epoch_2");
        Dictionary<string, string> receipts = null, controls = null;
        using (MineruM6ControlRig first = new MineruM6ControlRig(dir, false, owner)) {
            first.Ok(first.Bind(), "controller"); first.Clock += 10; first.Ok(first.Simple("open"), "controller"); first.Clock += 10;
            first.Ok(first.Append(first.Producer("service_runner", first.RunnerEpoch, 1, first.Admitted("a"))), "service_runner"); first.Clock += 10;
            first.Ok(first.Simple("stop"), "controller"); first.Clock += 10;
            first.Ok(first.Ack(1, 1, 0, first.ReconciliationReceipt(1, new string[] { "a" }, 0, null)), "service_runner");
            receipts = new Dictionary<string, string>(first.Receipts); controls = new Dictionary<string, string>(first.Controls);
        }
        using (MineruM6ControlRig rig = new MineruM6ControlRig(new MineruM6JournalRig(dir, true), 0)) {
            rig.Clock = rig.T0 + 100;
            MineruM6ControlRefusal refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { rig.Build(owner2, MineruM6ControlRig.MaxLease, MineruM6ControlRig.Reserve); }, "resume without the archived ACK");
            MineruM6NativeSuite.Equal("archived_control_receipt_missing", refusal.Code, "missing sidecar code");
            foreach (KeyValuePair<string, string> pair in receipts) rig.Receipts[pair.Key] = pair.Value;
            rig.Controls["admission-closed"] = "{\"kind\":\"status\"}";
            refusal = MineruM6NativeSuite.Throws<MineruM6ControlRefusal>(delegate { rig.Build(owner2, MineruM6ControlRig.MaxLease, MineruM6ControlRig.Reserve); }, "resume with a foreign sidecar");
            MineruM6NativeSuite.Equal("archived_control_receipt_differs", refusal.Code, "foreign sidecar code");
            MineruM6NativeSuite.Equal(5, rig.Journal.Journal.LastSequence, "refused resumes wrote nothing");
        }
        using (MineruM6ControlRig rig = new MineruM6ControlRig(new MineruM6JournalRig(dir, true), 0)) {
            rig.Clock = rig.T0 + 100;
            foreach (KeyValuePair<string, string> pair in receipts) rig.Receipts[pair.Key] = pair.Value;
            foreach (KeyValuePair<string, string> pair in controls) rig.Controls[pair.Key] = pair.Value;
            rig.Build(owner2, MineruM6ControlRig.MaxLease, MineruM6ControlRig.Reserve);
            MineruM6NativeSuite.Equal("draining", rig.State(), "archived ACK restores draining state");
            MineruM6NativeSuite.Equal("owner_resumed", rig.Kind(5), "owner_resumed appended after replay");
        }
    }

    public static void Test12_ControlLayerFixtureNegatives() {
        MineruJsonValue negatives = MineruM6NativeSuite.F("journal", "negatives");
        int seen = 0;
        for (int i = 0; i < negatives.Count; i++) {
            MineruJsonValue item = negatives.Item(i);
            if (item.Get("layer").String() != "control") continue;
            string name = item.Get("name").String(), record = item.Get("record").String(), expect = item.Get("expect").String();
            int prefix = (int)item.Get("after_prefix").Integer();
            string dir = MineruM6NativeSuite.NewTempDir("ctl-neg-" + i);
            // These records are wire/journal-valid but violate control role
            // semantics. Append through the actual durable journal so its guard
            // describes the same prefix; raw corruption would stop at the earlier
            // guard layer and never exercise the intended control replay gate.
            using (MineruM6JournalRig journalRig = new MineruM6JournalRig(dir, false)) {
                for (int p = 0; p < prefix; p++) journalRig.AppendFixture(p);
                MineruJsonValue negative = MineruResidentWire.Parse(record, 65536);
                MineruM6AppendResult appended = journalRig.Journal.Append(negative.Get("event").Raw,
                    negative.Get("stamp").Get("received_qpc_ticks").Integer(),
                    negative.Get("stamp").Get("owner_process_epoch_sha256").String());
                MineruM6NativeSuite.Equal(record, appended.Record, "independent negative record persisted exactly");
            }
            using (MineruM6ControlRig rig = new MineruM6ControlRig(new MineruM6JournalRig(dir, true), 0)) {
                rig.Clock = rig.T0 + 100;
                IOException error = MineruM6NativeSuite.Throws<IOException>(delegate { rig.Build(MineruM6NativeSuite.FS("identities", "owner_epoch_2"), MineruM6ControlRig.MaxLease, MineruM6ControlRig.Reserve); }, "control negative " + name);
                MineruM6NativeSuite.Equal(expect, error.Message, "control negative " + name + " code");
            }
            seen++;
        }
        MineruM6NativeSuite.Check(seen >= 1, "control-layer negatives present");
    }

    static string AnchorWithResources(string resourcesRaw) {
        MineruJsonValue anchor = MineruResidentWire.Parse(MineruM6NativeSuite.FS("service", "anchor"), 65536);
        List<string> pairs = new List<string>();
        foreach (string key in new string[] { "contract_version", "run_id", "clock", "owner_process_epoch_sha256", "owner_source_sha256", "gpu_device_identity_sha256", "t0_ticks", "planned_seconds", "deadline_ticks", "max_close_ticks" }) {
            pairs.Add(key); pairs.Add(anchor.Get(key).Raw);
        }
        pairs.Add("resources"); pairs.Add(resourcesRaw);
        return MineruResidentWire.Object(pairs.ToArray());
    }

    public static void Test13_OwnerHeadroomReserveRefusesProducerRecords() {
        string resources = "{\"max_attempts\":10,\"max_events\":12,\"max_log_bytes\":1000000,\"max_record_bytes\":16384,\"max_verifier_backlog_bytes\":1048576,\"stop_admission_budget_ticks\":300000000}";
        using (MineruM6ControlRig rig = new MineruM6ControlRig(MineruM6NativeSuite.NewTempDir("control"), false, MineruM6NativeSuite.FS("identities", "owner_epoch"), AnchorWithResources(resources), 12, 16384, 1000000)) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Admitted("a"))), "service_runner"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 2, rig.Admitted("b"))), "service_runner"); rig.Clock += 10;
            MineruM6NativeSuite.Equal("journal_owner_headroom", rig.Rejected(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 3, rig.Admitted("c"))), "service_runner"), "eight owner slots reserved");
            MineruM6NativeSuite.Equal("measurement_incident", rig.Kind(4), "exhaustion visible as an incident");
            MineruM6NativeSuite.Equal("failed", rig.State(), "failed");
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 2, rig.Admitted("b"))), "service_runner");
            MineruM6NativeSuite.Equal(6, rig.Journal.Journal.LastSequence, "exact retries need no new slot");
        }
    }

    public static void Test14_AttemptBoundFromAnchor() {
        string resources = "{\"max_attempts\":2,\"max_events\":1000,\"max_log_bytes\":1000000,\"max_record_bytes\":16384,\"max_verifier_backlog_bytes\":1048576,\"stop_admission_budget_ticks\":300000000}";
        using (MineruM6ControlRig rig = new MineruM6ControlRig(MineruM6NativeSuite.NewTempDir("control"), false, MineruM6NativeSuite.FS("identities", "owner_epoch"), AnchorWithResources(resources), 1000, 16384, 1000000)) {
            rig.Ok(rig.Bind(), "controller"); rig.Clock += 10; rig.Ok(rig.Simple("open"), "controller"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 1, rig.Admitted("a"))), "service_runner"); rig.Clock += 10;
            rig.Ok(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 2, rig.Admitted("b"))), "service_runner"); rig.Clock += 10;
            MineruM6NativeSuite.Equal("attempt_index_bound", rig.Rejected(rig.Append(rig.Producer("service_runner", rig.RunnerEpoch, 3, rig.Admitted("c"))), "service_runner"), "third admission over the bound");
            MineruM6NativeSuite.Equal("failed", rig.State(), "bound exhaustion is an incident");
        }
    }
}
