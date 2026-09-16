# Bounded stdin receive for Windows PowerShell 5.1 (.NET Framework) commands started over Win32-OpenSSH.
#
# .NET Framework's ConsoleStream.Read waits in WaitForAvailableConsoleInput before ReadFile; on the
# FILE_FLAG_OVERLAPPED stdio pipes sshd creates for its child that wait can block forever, so
# [Console]::In / Console.OpenStandardInput() stall while the client has already sent everything.
# A FileStream over the raw GetStdHandle(-10) handle bypasses that wait. Recipes embed this function
# verbatim (pin its SHA-256) and call it with the exact byte count and digest the sender computed;
# the receiver reads exactly that many bytes within a deadline, never waits for EOF, and returns the
# bytes only when the digest matches. Every other outcome is a thrown terminal error and the caller
# must exit non-zero without executing or writing anything derived from the payload.
#
# Terminal error messages (stable, for callers and tests):
#   stdin_receive_timeout        the deadline passed before ExpectedBytes arrived (bytes so far appended)
#   stdin_receive_short          the pipe reached EOF before ExpectedBytes arrived
#   stdin_receive_hash_differs   ExpectedBytes arrived but SHA-256 differs from ExpectedSha256
#   stdin_receive_invalid_args   ExpectedBytes/ExpectedSha256/DeadlineMilliseconds outside their bounds
function Receive-MineruBoundedStdin {
    param(
        [Parameter(Mandatory = $true)][long]$ExpectedBytes,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][int]$DeadlineMilliseconds
    )
    if ($ExpectedBytes -lt 1 -or $ExpectedBytes -gt 268435456 -or $ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $DeadlineMilliseconds -lt 100 -or $DeadlineMilliseconds -gt 3600000) {
        throw 'stdin_receive_invalid_args'
    }
    if (-not ('MineruBoundedStdin.Native' -as [type])) {
        Add-Type -Namespace MineruBoundedStdin -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
public static extern System.IntPtr GetStdHandle(int nStdHandle);
'@
    }
    $handle = [MineruBoundedStdin.Native]::GetStdHandle(-10)
    if ($handle -eq [IntPtr]::Zero -or $handle -eq [IntPtr]::new(-1)) { throw 'stdin_receive_invalid_args' }
    $safe = New-Object Microsoft.Win32.SafeHandles.SafeFileHandle($handle, $false)
    $stream = New-Object IO.FileStream($safe, [IO.FileAccess]::Read, 8192, $false)
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $buffer = New-Object byte[] 8192
    $memory = New-Object IO.MemoryStream
    try {
        while ($memory.Length -lt $ExpectedBytes) {
            $remaining = $DeadlineMilliseconds - [int]$clock.ElapsedMilliseconds
            if ($remaining -le 0) { throw ('stdin_receive_timeout:' + $memory.Length) }
            $want = [int][Math]::Min($buffer.Length, $ExpectedBytes - $memory.Length)
            # A synchronous pipe read cannot be cancelled; the read runs on the pool and the deadline
            # decides. A timed-out read leaves this process to exit; nothing is written on that path.
            $task = $stream.ReadAsync($buffer, 0, $want)
            if (-not $task.Wait($remaining)) { throw ('stdin_receive_timeout:' + $memory.Length) }
            $count = $task.GetAwaiter().GetResult()
            if ($count -le 0) { throw ('stdin_receive_short:' + $memory.Length) }
            $memory.Write($buffer, 0, $count)
        }
        $data = $memory.ToArray()
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $digest = ([BitConverter]::ToString($sha.ComputeHash($data))).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
        if ($digest -cne $ExpectedSha256) { throw 'stdin_receive_hash_differs' }
        return ,$data
    } finally {
        $memory.Dispose()
        $stream.Dispose()
        $clock.Stop()
    }
}
