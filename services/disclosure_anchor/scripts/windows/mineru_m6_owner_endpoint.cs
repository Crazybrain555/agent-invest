// Bounded loopback-only transport. No worker pool, subprocess, command execution
// or automatic request retry. Run on the same thread as the journal/control.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text.RegularExpressions;
using System.Threading;

public sealed class MineruM6Principal {
    public readonly string Role,Epoch;
    public MineruM6Principal(string role,string epoch) {
        MineruM6OwnerWire.Shape(MineruResidentWire.Parse(MineruResidentWire.Object("role",MineruResidentWire.Quote(role),
            "epoch",MineruResidentWire.Quote(epoch)),1024),"role:=controller|service_runner|e2e_runner|public_verifier|quality_verifier","epoch:hash");
        Role=role; Epoch=epoch;
    }
}

public sealed class MineruM6Credentials : IDisposable {
    readonly List<byte[]> tokens=new List<byte[]>();
    readonly List<MineruM6Principal> principals=new List<MineruM6Principal>();
    bool disposed;
    public MineruM6Credentials(Dictionary<MineruM6Principal,string> privateTokens) {
        if(privateTokens==null || privateTokens.Count<2 || privateTokens.Count>5) throw new ArgumentException("M6 role token bound");
        HashSet<string> unique=new HashSet<string>(StringComparer.Ordinal),roles=new HashSet<string>(StringComparer.Ordinal);
        foreach(KeyValuePair<MineruM6Principal,string> pair in privateTokens) {
            if(pair.Key==null || pair.Value==null || !Regex.IsMatch(pair.Value,@"\A[0-9a-f]{64}\z") ||
                !unique.Add(pair.Value) || !roles.Add(pair.Key.Role)) throw new ArgumentException("M6 distinct role tokens required");
            principals.Add(pair.Key); tokens.Add(MineruResidentWire.Utf8.GetBytes(pair.Value));
        }
    }
    public MineruM6Principal Authenticate(byte[] header,int length) {
        if(disposed) throw new ObjectDisposedException("M6 credentials");
        const string prefix="M6-AUTH/1 ";
        if(length!=prefix.Length+64) return null;
        int prefixDifference=0;
        for(int i=0;i<prefix.Length;i++) prefixDifference|=header[i]^(byte)prefix[i];
        MineruM6Principal result=null;
        // Compare all bytes of every configured token. Neither token content
        // nor which role matched chooses an early-return path.
        for(int i=0;i<tokens.Count;i++) {
            int difference=prefixDifference;
            for(int j=0;j<64;j++) difference|=header[prefix.Length+j]^tokens[i][j];
            if(difference==0) result=principals[i];
        }
        return result;
    }
    public void Dispose() { if(disposed) return; foreach(byte[] token in tokens) Array.Clear(token,0,token.Length); disposed=true; }
}

public sealed class MineruM6Endpoint : IDisposable {
    sealed class Peer {
        public Socket Socket;
        public readonly byte[] Header=new byte[74],Body=new byte[MineruM6OwnerWire.MaximumWireBytes];
        public int HeaderUsed,BodyUsed,OutputSent;
        public byte[] Output;
        public MineruM6Principal Principal;
        public long Deadline;
        public bool ReadingBody;
    }
    readonly Socket listener;
    readonly List<Peer> peers=new List<Peer>();
    readonly MineruM6Credentials credentials;
    readonly Func<string,MineruM6Principal,string> handle;
    readonly Action tick;
    readonly Func<bool> isClosed;
    readonly Action<string,byte[]> diagnostic;
    readonly long requestTicks,idleTicks;
    readonly int maximumPeers;
    readonly int ownerThread;
    readonly byte[] receive=new byte[4096];
    long lastClock;
    bool disposed;

    public MineruM6Endpoint(int port,MineruM6Credentials roleCredentials,
        Func<string,MineruM6Principal,string> requestHandler,Action observeDeadline,Func<bool> ownerIsClosed,
        Action<string,byte[]> persistBoundedDiagnostic,int requestTimeoutMilliseconds,int idleTimeoutMilliseconds,int maximumConnections) {
        if(port<1024 || port>65535 || requestTimeoutMilliseconds<1 || requestTimeoutMilliseconds>30000 ||
            idleTimeoutMilliseconds<requestTimeoutMilliseconds || idleTimeoutMilliseconds>60000 ||
            maximumConnections<2 || maximumConnections>8 || roleCredentials==null || requestHandler==null ||
            observeDeadline==null || ownerIsClosed==null || persistBoundedDiagnostic==null)
            throw new ArgumentException("M6 bounded endpoint configuration required");
        credentials=roleCredentials;handle=requestHandler;tick=observeDeadline;isClosed=ownerIsClosed;diagnostic=persistBoundedDiagnostic;
        maximumPeers=maximumConnections; ownerThread=Thread.CurrentThread.ManagedThreadId;
        requestTicks=checked((long)requestTimeoutMilliseconds*Stopwatch.Frequency/1000);
        idleTicks=checked((long)idleTimeoutMilliseconds*Stopwatch.Frequency/1000);
        listener=new Socket(AddressFamily.InterNetwork,SocketType.Stream,ProtocolType.Tcp);
        try {
            listener.ExclusiveAddressUse=true;listener.Blocking=false;
            listener.Bind(new IPEndPoint(IPAddress.Loopback,port));listener.Listen(maximumConnections);
        } catch { listener.Dispose();throw; }
    }
    long Now() {
        long value=Stopwatch.GetTimestamp();
        if(value<lastClock) throw new IOException("M6 endpoint physical QPC regression");
        lastClock=value;return value;
    }
    void CheckThread() {
        if(disposed || Thread.CurrentThread.ManagedThreadId!=ownerThread)
            throw new InvalidOperationException("M6 endpoint must run on its live journal owner thread");
    }
    static bool NotReady(SocketError error) { return error==SocketError.WouldBlock || error==SocketError.IOPending || error==SocketError.NoBufferSpaceAvailable; }
    static bool PeerGone(SocketError error) {
        return error==SocketError.ConnectionReset || error==SocketError.ConnectionAborted || error==SocketError.Shutdown ||
               error==SocketError.NetworkReset;
    }
    byte[] AuthenticatedBody(Peer peer) {
        if(peer.Principal==null || peer.BodyUsed==0) return null;
        byte[] result=new byte[peer.BodyUsed];Array.Copy(peer.Body,result,result.Length);return result;
    }
    void Drop(Peer peer,string code) {
        // Auth headers are never passed to a sink, even in malformed requests.
        diagnostic(code,AuthenticatedBody(peer));
        ClosePeer(peer);
    }
    void ClosePeer(Peer peer) {
        Array.Clear(peer.Header,0,peer.Header.Length);Array.Clear(peer.Body,0,peer.Body.Length);
        peer.Socket.Dispose();peers.Remove(peer);
    }
    void AcceptOne() {
        Socket socket;
        try {socket=listener.Accept();}
        catch(SocketException error) {if(NotReady(error.SocketErrorCode)) return;throw;}
        if(peers.Count>=maximumPeers) {socket.Dispose();diagnostic("connection_bound",null);return;}
        try {
            if(!IPAddress.IsLoopback(((IPEndPoint)socket.RemoteEndPoint).Address)) throw new IOException("M6 non-loopback peer");
            socket.Blocking=false;socket.NoDelay=true;socket.ReceiveBufferSize=8192;socket.SendBufferSize=8192;
            peers.Add(new Peer {Socket=socket,Deadline=checked(Now()+requestTicks)});
        } catch {socket.Dispose();throw;}
    }
    void Read(Peer peer) {
        SocketError error;int count=peer.Socket.Receive(receive,0,receive.Length,SocketFlags.None,out error);
        if(NotReady(error)) return;
        if(PeerGone(error)) {Drop(peer,"peer_connection_lost");return;}
        if(error!=SocketError.Success) throw new SocketException((int)error);
        if(count==0) {Drop(peer,peer.BodyUsed!=0 || peer.HeaderUsed!=0 ? "partial_request_eof" : "peer_closed");return;}
        if(peer.HeaderUsed==0 && !peer.ReadingBody) peer.Deadline=checked(Now()+requestTicks);
        for(int i=0;i<count;i++) {
            byte value=receive[i];
            if(!peer.ReadingBody) {
                if(value==10) {
                    peer.Principal=credentials.Authenticate(peer.Header,peer.HeaderUsed);
                    Array.Clear(peer.Header,0,peer.Header.Length);
                    if(peer.Principal==null) {Drop(peer,"authentication_rejected");return;}
                    peer.ReadingBody=true;
                } else {
                    if(peer.HeaderUsed>=peer.Header.Length || value==13) {Drop(peer,"authentication_header_invalid");return;}
                    peer.Header[peer.HeaderUsed++]=value;
                }
            } else if(value==10) {
                if(i!=count-1 || peer.BodyUsed==0) {Drop(peer,"request_framing_invalid");return;}
                string request;
                try {
                    request=MineruResidentWire.Utf8.GetString(peer.Body,0,peer.BodyUsed);
                    MineruM6OwnerWire.Request(request);
                } catch(System.Text.DecoderFallbackException) {Drop(peer,"request_utf8_invalid");return;}
                  catch(FormatException) {Drop(peer,"request_shape_invalid");return;}
                string reply;
                try {reply=handle(request,peer.Principal);}
                catch(MineruM6ControlRefusal refusal) {Drop(peer,refusal.Code);return;}
                // Handler/storage/receipt failures are not malformed requests:
                // propagate them to the host's permanent failure/drain path.
                if(reply==null || reply.IndexOf('\n')>=0 || reply.IndexOf('\r')>=0 ||
                    MineruResidentWire.Utf8.GetByteCount(reply)>MineruM6OwnerWire.MaximumWireBytes)
                    throw new IOException("M6 owner handler returned invalid bounded reply");
                MineruResidentWire.Parse(reply,MineruM6OwnerWire.MaximumWireBytes);
                peer.Output=MineruResidentWire.Utf8.GetBytes(reply+"\n");peer.OutputSent=0;
                return;
            } else {
                if(peer.BodyUsed>=peer.Body.Length || value==13) {Drop(peer,"request_body_bound_or_crlf");return;}
                peer.Body[peer.BodyUsed++]=value;
            }
        }
    }
    void Write(Peer peer) {
        SocketError error;int count=peer.Socket.Send(peer.Output,peer.OutputSent,peer.Output.Length-peer.OutputSent,SocketFlags.None,out error);
        if(NotReady(error)) return;
        if(PeerGone(error)) {Drop(peer,"reply_connection_lost");return;}
        if(error!=SocketError.Success) throw new SocketException((int)error);
        if(count<=0) {Drop(peer,"reply_zero_progress");return;}
        peer.OutputSent+=count;
        if(peer.OutputSent==peer.Output.Length) {
            peer.Output=null;peer.OutputSent=0;peer.HeaderUsed=0;peer.BodyUsed=0;peer.ReadingBody=false;peer.Principal=null;
            Array.Clear(peer.Body,0,peer.Body.Length);peer.Deadline=checked(Now()+idleTicks);
        }
    }
    public void Run(Action ready) {
        CheckThread();if(ready==null) throw new ArgumentNullException("ready");
        ready();
        while(true) {
            tick();bool closing=isClosed();
            if(!closing) AcceptOne();
            foreach(Peer peer in peers.ToArray()) {
                if(Now()>=peer.Deadline) {Drop(peer,peer.Output==null ? "request_timeout" : "reply_timeout");continue;}
                if(peer.Output!=null) Write(peer);
                else if(closing || isClosed()) ClosePeer(peer);
                else {try {Read(peer);}finally{Array.Clear(receive,0,receive.Length);}}
            }
            // Closing sends only already pending replies, with the existing
            // finite request deadline. It never admits a new request afterwards.
            if(isClosed() && peers.Count==0) return;
            Thread.Sleep(5);
        }
    }
    public void Dispose() {
        if(disposed) return;CheckThread();
        List<Exception> failures=new List<Exception>();
        foreach(Peer peer in peers.ToArray()) {try {ClosePeer(peer);}catch(Exception error){failures.Add(error);}}
        try {listener.Dispose();}catch(Exception error){failures.Add(error);}
        if(failures.Count!=0) throw new AggregateException("M6 endpoint resource closure failed",failures);
        disposed=true;
    }
}
