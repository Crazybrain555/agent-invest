// Independent root-authored controlled child for production parent regression.
// No GPU, network, services, databases or PDF operations.
using System;
using System.IO;
using System.Threading;
public static class MineruM6PrivateStore {
    public static void CreatePrivateDirectory(string path) { Directory.CreateDirectory(path); }
}
public static class LauncherFixture {
    public static int Main(string[] args) {
        string mode = File.ReadAllText(args[0]).Trim();
        if (mode == "no-ready") return 0;
        if (mode == "bad-ready") { Console.WriteLine("not json"); return 0; }
        if (mode == "oversize-ready") { Console.Write(new string('x', 262144)); return 0; }
        if (mode == "ready-write-failure") Directory.CreateDirectory("ready.json");
        if (mode == "exit-write-failure") Directory.CreateDirectory("process-exit.json");
        Console.WriteLine("{\"status\":\"ready\"}");
        Console.Out.Flush();
        if (mode == "flood") {
            string chunk = new string('x', 8192);
            for (int i = 0; i < 512; ++i) { Console.Out.Write(chunk); Console.Error.Write(chunk); }
            Console.Out.Flush(); Console.Error.Flush();
        }
        if (mode == "timeout") Thread.Sleep(120000);
        return mode == "nonzero" ? 7 : 0;
    }
}
