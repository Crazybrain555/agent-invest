// Deterministic decoding/identity checks; the physical capture runs separately
// once per fresh Windows process under its finite self-Job.
using System;
using System.IO;
using Microsoft.Win32;

public static class MineruM6OwnerIdentityChecks {
    static int checks;
    static void Check(bool value) {
        if(!value) throw new Exception("M6 boot identity check failed");
        checks++;
    }
    static void Reject(object value,RegistryValueKind kind) {
        try { MineruM6OwnerIdentity.DecodeBootCounter(value,kind); }
        catch(IOException) { checks++;return; }
        throw new Exception("Malformed M6 boot counter accepted");
    }
    public static int Run() {
        checks=0;
        Check(MineruM6OwnerIdentity.DecodeBootCounter(0,RegistryValueKind.DWord)==0U);
        Check(MineruM6OwnerIdentity.DecodeBootCounter(68,RegistryValueKind.DWord)==68U);
        Check(MineruM6OwnerIdentity.DecodeBootCounter(-1,RegistryValueKind.DWord)==UInt32.MaxValue);
        Reject(null,RegistryValueKind.DWord);
        Reject("68",RegistryValueKind.String);
        Reject(68L,RegistryValueKind.QWord);
        Reject(68,RegistryValueKind.Unknown);
        Reject(68L,RegistryValueKind.DWord);
        string node="sha256:"+new string('a',64);
        string first=MineruM6OwnerIdentity.BootIdentity(node,68);
        Check(first==MineruM6OwnerIdentity.BootIdentity(node,68));
        Check(first!=MineruM6OwnerIdentity.BootIdentity(node,69));
        Check(first!=MineruM6OwnerIdentity.BootIdentity("sha256:"+new string('b',64),68));
        string raw=MineruResidentWire.Object("contract_version",MineruResidentWire.Quote("m6.windows-boot-counter.v1"),
            "windows_node_identity_sha256",MineruResidentWire.Quote(node),"boot_counter","68");
        Check(first==MineruResidentWire.Hash(MineruResidentWire.Utf8.GetBytes(raw)));
        return checks;
    }
}
