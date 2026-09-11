// Real loopback socket mechanism checks; replies and clocks are synthetic.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Threading;

public static class MineruM6EndpointChecks {
    sealed class State { public volatile bool Closed; public Exception Failure; public int Calls; }
    static string Q(string value) { return MineruResidentWire.Quote(value); }
    static string H(string value) { return MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(value)); }
    static void Check(bool good,string name,List<string> checks) {
        if(!good) throw new Exception("M6 endpoint check: "+name);checks.Add(Q(name));
    }
    static Socket Connect(int port) {
        Socket socket=new Socket(AddressFamily.InterNetwork,SocketType.Stream,ProtocolType.Tcp);
        try {socket.ReceiveTimeout=3000;socket.SendTimeout=3000;socket.NoDelay=true;socket.Connect(IPAddress.Loopback,port);return socket;}
        catch {socket.Dispose();throw;}
    }
    static void Send(Socket socket,string text) {
        byte[] bytes=MineruResidentWire.Utf8.GetBytes(text);int sent=0;
        while(sent<bytes.Length) {int count=socket.Send(bytes,sent,bytes.Length-sent,SocketFlags.None);if(count<=0) throw new IOException("Test send stalled");sent+=count;}
    }
    static string Line(Socket socket) {
        using(MemoryStream bytes=new MemoryStream()) {
            byte[] buffer=new byte[256];
            while(bytes.Length<=65536) {
                int count=socket.Receive(buffer);
                if(count==0) return bytes.Length==0 ? null : throwPartial();
                for(int i=0;i<count;i++) {
                    if(buffer[i]==10) {
                        if(i!=count-1) throw new IOException("Extra test reply bytes");
                        return MineruResidentWire.Utf8.GetString(bytes.ToArray());
                    }
                    bytes.WriteByte(buffer[i]);
                }
            }
            throw new IOException("Test reply over bound");
        }
    }
    static string throwPartial() { throw new IOException("Partial test reply EOF"); }
    static string Request(string kind) {
        return MineruResidentWire.Object("contract_version",Q("m6.owner-request.v1"),"run_id",Q("socket-fixture"),
            "spec_sha256",Q(H("socket-fixture-spec")),"request_id",Q("request-"+kind),
            "command",MineruResidentWire.Object("kind",Q(kind)));
    }
    public static string Run(int port) {
        State state=new State();List<string> checks=new List<string>(),diagnostics=new List<string>();
        string runner=new string('0',64),controller=new string('1',64);
        string header="M6-AUTH/1 "+runner+"\n",controlHeader="M6-AUTH/1 "+controller+"\n",status=Request("status")+"\n";
        Dictionary<MineruM6Principal,string> tokens=new Dictionary<MineruM6Principal,string>();
        tokens.Add(new MineruM6Principal("service_runner",H("runner")),runner);
        tokens.Add(new MineruM6Principal("controller",H("controller")),controller);
        long limit=Stopwatch.GetTimestamp()+8*Stopwatch.Frequency;
        Thread client=new Thread(delegate() {
            try {
                using(Socket socket=Connect(port)) {Send(socket,"M6-AUTH/1 "+new string('2',64)+"\n"+status);Check(Line(socket)==null,"wrong_token_no_status",checks);}
                using(Socket socket=Connect(port)) {Send(socket,"M6-AUTH/1 "+runner+"\r\n"+status);Check(Line(socket)==null,"crlf_header_rejected",checks);}
                using(Socket socket=Connect(port)) {Send(socket,header+"{\"partial\":");socket.Shutdown(SocketShutdown.Send);Check(Line(socket)==null,"partial_body_eof_rejected",checks);}
                using(Socket socket=Connect(port)) {Send(socket,header+status+"\n");Check(Line(socket)==null,"coalesced_pipeline_rejected",checks);}
                using(Socket socket=Connect(port)) {Send(socket,header+"{}\n");Check(Line(socket)==null,"closed_request_shape_enforced",checks);}
                using(Socket socket=Connect(port)) {
                    Send(socket,header);Check(socket.Send(new byte[]{255,10})==2,"invalid_utf8_bytes_sent",checks);
                    Check(Line(socket)==null,"invalid_utf8_rejected_without_dispatch",checks);
                }
                using(Socket socket=Connect(port)) {Send(socket,header+new string('x',65537)+"\n");Check(Line(socket)==null,"over_bound_request_rejected",checks);}
                using(Socket socket=Connect(port)) {
                    Send(socket,header.Substring(0,20));Thread.Sleep(10);Send(socket,header.Substring(20)+status.Substring(0,30));
                    Thread.Sleep(10);Send(socket,status.Substring(30));
                    Check(Line(socket)==MineruResidentWire.Object("role",Q("service_runner"),"calls","1"),"fragmented_request_role_bound",checks);
                    Send(socket,controlHeader+status);
                    Check(Line(socket)==MineruResidentWire.Object("role",Q("controller"),"calls","2"),"persistent_channel_reauthenticates_each_request",checks);
                    socket.ReceiveBufferSize=1024;
                    Send(socket,header+status.Replace("request-status","large-reply"));
                    Check(Line(socket)==MineruResidentWire.Object("blob",Q(new string('z',60000))),"bounded_large_reply_not_truncated",checks);
                }
                using(Socket socket=Connect(port)) {Send(socket,header+"{");Check(Line(socket)==null,"partial_request_deadline",checks);}
                using(Socket socket=Connect(port)) {
                    Send(socket,controlHeader+Request("stop")+"\n");
                    Check(Line(socket)==MineruResidentWire.Object("role",Q("controller"),"calls","4"),"close_reply_delivered",checks);
                    Check(Line(socket)==null,"closed_owner_eof",checks);
                }
            } catch(Exception error) {state.Failure=error;state.Closed=true;}
        });
        client.IsBackground=true;
        using(MineruM6Credentials credentials=new MineruM6Credentials(tokens))
        using(MineruM6Endpoint endpoint=new MineruM6Endpoint(port,credentials,
            delegate(string raw,MineruM6Principal principal) {
                state.Calls++;
                if(MineruResidentWire.Parse(raw,65536).Get("request_id").String()=="large-reply")
                    return MineruResidentWire.Object("blob",Q(new string('z',60000)));
                if(MineruResidentWire.Parse(raw,65536).Get("command").Get("kind").String()=="stop") state.Closed=true;
                return MineruResidentWire.Object("role",Q(principal.Role),"calls",MineruResidentWire.Integer(state.Calls));
            },delegate {if(Stopwatch.GetTimestamp()>limit) throw new TimeoutException("Endpoint test exceeded 8 seconds");},
            delegate{return state.Closed;},
            delegate(string code,byte[] raw) {
                if(diagnostics.Count>=32) throw new IOException("Test diagnostic bound");
                if(raw!=null) {
                    string value=System.Text.Encoding.ASCII.GetString(raw);
                    if(value.Contains(runner) || value.Contains(controller) || value.Contains("M6-AUTH/1"))
                        throw new Exception("Credentials reached diagnostic sink");
                }
                diagnostics.Add(code);
            },1000,2000,4)) {
            try {endpoint.Run(delegate{client.Start();});}
            finally {if(!client.Join(2000)) throw new TimeoutException("Endpoint test client did not exit");}
        }
        if(state.Failure!=null) throw new Exception("Loopback client check failed",state.Failure);
        Check(state.Calls==4,"only_valid_authenticated_requests_dispatched",checks);
        Check(diagnostics.Contains("authentication_rejected") && diagnostics.Contains("partial_request_eof") &&
            diagnostics.Contains("request_timeout"),"bounded_diagnostics_cover_failures_without_credentials",checks);
        return MineruResidentWire.Object("status",Q("pass"),"scope",Q("synthetic_loopback_transport_only"),
            "checks","["+String.Join(",",checks.ToArray())+"]");
    }
}
