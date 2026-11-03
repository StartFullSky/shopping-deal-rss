param(
    [Parameter(Mandatory = $true)]
    [string]$Title,

    [Parameter(Mandatory = $true)]
    [string]$Body,

    [string]$SecretPath = "C:\Users\X\.codex\secrets\shopping-deal-rss-bark.bin"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security

if ([string]::IsNullOrWhiteSpace($Title) -or [string]::IsNullOrWhiteSpace($Body)) {
    throw "Title and Body are required."
}

if (-not (Test-Path -LiteralPath $SecretPath)) {
    throw "Bark secret file was not found: $SecretPath"
}

$encrypted = [System.IO.File]::ReadAllBytes($SecretPath)
$decrypted = [System.Security.Cryptography.ProtectedData]::Unprotect(
    $encrypted,
    $null,
    [System.Security.Cryptography.DataProtectionScope]::CurrentUser
)
$baseUrl = [System.Text.Encoding]::UTF8.GetString($decrypted).TrimEnd("/")

if ($baseUrl -notmatch "^https://api\.day\.app/[^/?#]+$") {
    throw "The decrypted Bark endpoint is invalid."
}

$uri = $baseUrl + "/" +
    [System.Uri]::EscapeDataString($Title) + "/" +
    [System.Uri]::EscapeDataString($Body)

$response = Invoke-RestMethod -Uri $uri -Method Get -TimeoutSec 20
if ($response.code -ne 200) {
    throw "Bark returned code $($response.code): $($response.message)"
}

[pscustomobject]@{
    status = "sent"
    timestamp = $response.timestamp
}
