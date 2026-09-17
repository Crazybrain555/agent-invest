// Test-only fake Docker CLI for the installation owner integration tests.
// It never contacts a Docker daemon, engine, socket, registry or network, and
// it never reads C:\ProgramData. Every response is an explicit literal the test
// declared in a plan file; a call that matches no declared response is a
// visible failure, never a silent success.
//
// Each invocation appends exactly one record to the test-owned call log
// (UTC, plan index, exit code, base64 of the argv joined by U+001F) *before*
// it sleeps, so a test can prove that a caller reached Docker while it still
// holds the installation lock. The argv is recorded losslessly so the caller's
// Windows argument encoding can be compared against literal expectations.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading;

public static class MineruTestFakeDocker
{
    const string PlanContract = "mineru.test-fake-docker-plan.v1";
    const int MaximumDeclaredDelayMilliseconds = 600000;

    sealed class Response
    {
        public string Match;
        public int MaximumUses = int.MaxValue;
        public int DelayMilliseconds;
        public int ExitCode;
        public byte[] StandardOutput = new byte[0];
        public byte[] StandardError = new byte[0];
    }

    static UTF8Encoding Utf8() { return new UTF8Encoding(false); }
    static UTF8Encoding StrictUtf8() { return new UTF8Encoding(false, true); }

    static int Refuse(int code, string message)
    {
        byte[] bytes = Utf8().GetBytes("fake-docker-fixture-error: " + message);
        using (Stream error = Console.OpenStandardError())
        {
            error.Write(bytes, 0, bytes.Length);
            error.Flush();
        }
        return code;
    }

    static List<Response> ParsePlan(string path)
    {
        List<Response> responses = new List<Response>();
        Response current = null;
        bool contract = false;
        int number = 0;
        foreach (string raw in File.ReadAllLines(path, StrictUtf8()))
        {
            number++;
            string line = raw.TrimEnd('\r');
            if (line.Length == 0 || line[0] == '#') continue;
            if (line == "---") { current = null; continue; }
            int split = line.IndexOf('=');
            if (split < 1) throw new FormatException("plan line " + number + " is not key=value");
            string key = line.Substring(0, split);
            string value = line.Substring(split + 1);
            if (key == "contract_version")
            {
                if (value != PlanContract) throw new FormatException("unsupported plan contract: " + value);
                contract = true;
                continue;
            }
            if (current == null) { current = new Response(); responses.Add(current); }
            switch (key)
            {
                case "match": current.Match = value; break;
                case "maximum_uses": current.MaximumUses = int.Parse(value, CultureInfo.InvariantCulture); break;
                case "delay_ms": current.DelayMilliseconds = int.Parse(value, CultureInfo.InvariantCulture); break;
                case "exit_code": current.ExitCode = int.Parse(value, CultureInfo.InvariantCulture); break;
                case "stdout_base64": current.StandardOutput = Convert.FromBase64String(value); break;
                case "stderr_base64": current.StandardError = Convert.FromBase64String(value); break;
                default: throw new FormatException("plan line " + number + " has an unknown key: " + key);
            }
        }
        if (!contract) throw new FormatException("plan does not declare " + PlanContract);
        for (int index = 0; index < responses.Count; index++)
        {
            Response response = responses[index];
            if (response.Match == null || response.Match.Length == 0)
                throw new FormatException("plan response " + index + " declares no match");
            if (response.MaximumUses < 1)
                throw new FormatException("plan response " + index + " declares a non-positive use bound");
            if (response.DelayMilliseconds < 0 || response.DelayMilliseconds > MaximumDeclaredDelayMilliseconds)
                throw new FormatException("plan response " + index + " delay is outside the finite test bound");
        }
        if (responses.Count == 0) throw new FormatException("plan declares no response");
        return responses;
    }

    static int[] UsesPerResponse(string logPath, int count)
    {
        int[] uses = new int[count];
        if (!File.Exists(logPath)) return uses;
        using (FileStream stream = new FileStream(logPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        using (StreamReader reader = new StreamReader(stream, StrictUtf8()))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                string[] fields = line.Split('\t');
                if (fields.Length < 2) continue;
                int index;
                if (!int.TryParse(fields[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out index)) continue;
                if (index >= 0 && index < count) uses[index]++;
            }
        }
        return uses;
    }

    static void Append(string logPath, int index, int exitCode, string argv)
    {
        string record = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) + "\t" +
            index.ToString(CultureInfo.InvariantCulture) + "\t" +
            exitCode.ToString(CultureInfo.InvariantCulture) + "\t" +
            Convert.ToBase64String(Utf8().GetBytes(argv)) + "\r\n";
        byte[] bytes = Utf8().GetBytes(record);
        using (FileStream stream = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
        {
            stream.Write(bytes, 0, bytes.Length);
            stream.Flush(true);
        }
    }

    static void WriteStream(Stream stream, byte[] bytes)
    {
        using (stream)
        {
            if (bytes.Length != 0) stream.Write(bytes, 0, bytes.Length);
            stream.Flush();
        }
    }

    public static int Main(string[] arguments)
    {
        string planPath = Environment.GetEnvironmentVariable("MINERU_TEST_FAKE_DOCKER_PLAN");
        string logPath = Environment.GetEnvironmentVariable("MINERU_TEST_FAKE_DOCKER_LOG");
        if (string.IsNullOrEmpty(planPath) || string.IsNullOrEmpty(logPath))
            return Refuse(66, "MINERU_TEST_FAKE_DOCKER_PLAN and MINERU_TEST_FAKE_DOCKER_LOG are both required");
        string argv = string.Join("\u001f", arguments);
        try
        {
            List<Response> responses = ParsePlan(planPath);
            int[] uses = UsesPerResponse(logPath, responses.Count);
            for (int index = 0; index < responses.Count; index++)
            {
                Response response = responses[index];
                if (uses[index] >= response.MaximumUses) continue;
                if (argv.IndexOf(response.Match, StringComparison.Ordinal) < 0) continue;
                Append(logPath, index, response.ExitCode, argv);
                if (response.DelayMilliseconds > 0) Thread.Sleep(response.DelayMilliseconds);
                WriteStream(Console.OpenStandardOutput(), response.StandardOutput);
                WriteStream(Console.OpenStandardError(), response.StandardError);
                return response.ExitCode;
            }
            Append(logPath, -1, 67, argv);
            return Refuse(67, "no declared response matches this call: " + argv.Replace('\u001f', ' '));
        }
        catch (Exception failure)
        {
            // The original fixture failure is preserved verbatim; it must never
            // be reported to the caller as an ordinary Docker outcome.
            try { Append(logPath, -2, 68, argv); }
            catch (Exception logFailure) { return Refuse(68, failure.ToString() + "; log also failed: " + logFailure.ToString()); }
            return Refuse(68, failure.ToString());
        }
    }
}
