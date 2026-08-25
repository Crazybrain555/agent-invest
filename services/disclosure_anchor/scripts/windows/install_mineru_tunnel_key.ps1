[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicKeySource,
    [string]$TunnelUser = "agentinvest_mineru",
    [string]$SshdConfig = "C:\ProgramData\ssh\sshd_config",
    [string]$ReceiptTarget = "C:\ProgramData\agent-invest\mineru\tunnel-key-receipt.json"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$SshdBinary = "C:\Windows\System32\OpenSSH\sshd.exe"
$SshKeygenBinary = "C:\Windows\System32\OpenSSH\ssh-keygen.exe"
$Marker = "# agent-invest MinerU tunnel: $TunnelUser"
$UserCreated = $false
$ConfigMutated = $false
$Timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$ConfigBackup = "$SshdConfig.pre-mineru-tunnel-$Timestamp.bak"
$UserRoot = "C:\Users\$TunnelUser"
$SshDirectory = Join-Path $UserRoot ".ssh"
$AuthorizedKeysPath = Join-Path $SshDirectory "authorized_keys"

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Value, $encoding)
}

function Set-PrivateAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$UserSid
    )
    & icacls.exe $Path "/inheritance:r" "/grant:r" "*$UserSid`:(F)" "*S-1-5-18:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot set private SSH ACL on $Path" }
    & icacls.exe $Path "/setowner" "*$UserSid" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cannot set private SSH owner on $Path" }
    $acl = Get-Acl -LiteralPath $Path
    $access = @($acl.Access)
    $actualSids = @($access | ForEach-Object {
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    } | Sort-Object -Unique)
    $expectedSids = @($UserSid, "S-1-5-18") | Sort-Object -Unique
    if (($actualSids -join ",") -ne ($expectedSids -join ",")) {
        throw "private SSH ACL contains unexpected principals"
    }
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -ne $UserSid) { throw "private SSH owner is not the tunnel user" }
    foreach ($rule in $access) {
        if (
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
            $rule.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl
        ) {
            throw "private SSH ACL is not exact FullControl"
        }
    }
    return $acl.Sddl
}

if ($TunnelUser -notmatch '^[a-z][a-z0-9_]{2,31}$') {
    throw "tunnel user name is invalid"
}
foreach ($path in @($PublicKeySource, $SshdConfig, $SshdBinary, $SshKeygenBinary)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "required tunnel installer input is missing: $path"
    }
}
if ($null -ne (Get-LocalUser -Name $TunnelUser -ErrorAction SilentlyContinue)) {
    throw "dedicated tunnel user already exists; refusing an ambiguous reinstall"
}
$configText = [string](Get-Content -LiteralPath $SshdConfig -Raw)
if ($configText.Contains($Marker)) {
    throw "dedicated tunnel Match block already exists"
}

$publicKey = ([string](Get-Content -LiteralPath $PublicKeySource -Raw)).Trim()
if ($publicKey -notmatch '^ssh-ed25519 ([A-Za-z0-9+/]+={0,2})( [^\r\n]+)?$') {
    throw "dedicated tunnel public key must be one canonical Ed25519 line"
}
$keyBase64 = $Matches[1]
$keyBlob = [Convert]::FromBase64String($keyBase64)
& $SshKeygenBinary -l -f $PublicKeySource | Out-Null
if ($LASTEXITCODE -ne 0) { throw "ssh-keygen rejected the Ed25519 public key" }
$algorithm = [Security.Cryptography.SHA256]::Create()
try { $keyHash = $algorithm.ComputeHash($keyBlob) }
finally { $algorithm.Dispose() }
$keySha256 = "sha256:" + (-join ($keyHash | ForEach-Object { $_.ToString("x2") }))

$passwordBytes = New-Object byte[] 48
$random = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $random.GetBytes($passwordBytes) }
finally { $random.Dispose() }
$password = ConvertTo-SecureString ([Convert]::ToBase64String($passwordBytes)) -AsPlainText -Force
Copy-Item -LiteralPath $SshdConfig -Destination $ConfigBackup

try {
    New-LocalUser -Name $TunnelUser -Password $password -AccountNeverExpires `
        -PasswordNeverExpires -UserMayNotChangePassword -Description `
        "agent-invest MinerU forward-only SSH account" | Out-Null
    $UserCreated = $true
    $user = Get-LocalUser -Name $TunnelUser
    $userSid = [string]$user.SID.Value
    if (
        $user.Enabled -ne $true -or
        (Get-LocalGroup -SID "S-1-5-32-544" | Get-LocalGroupMember |
            Where-Object { $_.SID.Value -eq $userSid })
    ) {
        throw "dedicated tunnel account identity or group membership is invalid"
    }

    New-Item -ItemType Directory -Path $SshDirectory -Force | Out-Null
    $forcedCommand = "powershell.exe -NoProfile -NonInteractive -Command Start-Sleep -Seconds 2147483"
    $restrictedLine = (
        'command="' + $forcedCommand + '",restrict,port-forwarding,' +
        'permitopen="127.0.0.1:30001",permitopen="127.0.0.1:30003",' +
        'permitopen="127.0.0.1:9835" ' + $publicKey
    )
    Write-Utf8NoBom -Path $AuthorizedKeysPath -Value ($restrictedLine + "`n")
    $directorySddl = Set-PrivateAcl -Path $UserRoot -UserSid $userSid
    Set-PrivateAcl -Path $SshDirectory -UserSid $userSid | Out-Null
    $authorizedKeysSddl = Set-PrivateAcl -Path $AuthorizedKeysPath -UserSid $userSid

    $matchBlock = @(
        "",
        $Marker,
        "Match User $TunnelUser",
        "    AuthorizedKeysFile `"$AuthorizedKeysPath`"",
        "    AuthenticationMethods publickey",
        "    PasswordAuthentication no",
        "    AllowAgentForwarding no",
        "    AllowTcpForwarding local",
        "    AllowStreamLocalForwarding no",
        "    PermitOpen 127.0.0.1:30001 127.0.0.1:30003 127.0.0.1:9835",
        "    PermitListen none",
        "    PermitTTY no",
        "    MaxSessions 0",
        ""
    ) -join "`r`n"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::AppendAllText($SshdConfig, $matchBlock, $encoding)
    $ConfigMutated = $true
    & $SshdBinary -t -f $SshdConfig
    if ($LASTEXITCODE -ne 0) { throw "sshd rejected the dedicated tunnel Match block" }

    Restart-Service -Name sshd -Force
    $service = Get-Service -Name sshd
    if ($service.Status -ne [ServiceProcess.ServiceControllerStatus]::Running) {
        throw "sshd did not return to Running"
    }
    $receipt = [ordered]@{
        schema = "mineru-tunnel-key-install-receipt.v2"
        installed_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        tunnel_user = $TunnelUser
        tunnel_user_sid = $userSid
        authorized_keys_path = $AuthorizedKeysPath
        authorized_keys_config = $AuthorizedKeysPath
        dedicated_key_sha256 = $keySha256
        sshd_config_path = $SshdConfig
        sshd_config_backup = $ConfigBackup
        forwarding_direction = "local-only"
        allow_streamlocal_forwarding = "no"
        permitopen = @("127.0.0.1:30001", "127.0.0.1:30003", "127.0.0.1:9835")
        permitlisten = "none"
        max_sessions = 0
        user_root_acl_sddl = $directorySddl
        authorized_keys_acl_sddl = $authorizedKeysSddl
    }
    $receiptDirectory = Split-Path -Parent $ReceiptTarget
    if (-not (Test-Path -LiteralPath $receiptDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $receiptDirectory -Force | Out-Null
    }
    Write-Utf8NoBom -Path $ReceiptTarget -Value (
        ($receipt | ConvertTo-Json -Depth 20) + "`n"
    )
    $receipt | ConvertTo-Json -Depth 20
}
catch {
    $originalError = $_.Exception.Message
    if ($ConfigMutated) {
        Copy-Item -LiteralPath $ConfigBackup -Destination $SshdConfig -Force
        & $SshdBinary -t -f $SshdConfig
        if ($LASTEXITCODE -eq 0) { Restart-Service -Name sshd -Force }
    }
    if ($UserCreated) {
        Remove-LocalUser -Name $TunnelUser -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $UserRoot) {
        Remove-Item -LiteralPath $UserRoot -Recurse -Force
    }
    throw "tunnel account installation failed and was rolled back: $originalError"
}
