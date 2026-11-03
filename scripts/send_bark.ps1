[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Title,

    [Parameter(Mandatory = $true)]
    [string]$Body,

    [string]$SecretPath = "C:\Users\X\.codex\secrets\shopping-deal-rss-bark.bin",

    [ValidateRange(1, 60)]
    [int]$TimeoutSec = 20,

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Security

$Title = $Title.Trim()
$Body = $Body.Trim()
if ([string]::IsNullOrWhiteSpace($Title) -or [string]::IsNullOrWhiteSpace($Body)) {
    throw "Title and Body are required."
}
if ($Title.Length -gt 128) {
    throw "Title must be 128 characters or fewer."
}
if ([Text.Encoding]::UTF8.GetByteCount($Body) -gt 6000) {
    throw "Body is too large for one Bark notification."
}
if (-not (Test-Path -LiteralPath $SecretPath)) {
    throw "Bark secret file was not found: $SecretPath"
}

$encrypted = [IO.File]::ReadAllBytes($SecretPath)
$decrypted = [Security.Cryptography.ProtectedData]::Unprotect(
    $encrypted,
    $null,
    [Security.Cryptography.DataProtectionScope]::CurrentUser
)
$baseUrl = [Text.Encoding]::UTF8.GetString($decrypted).Trim().TrimEnd("/")
$endpoint = [Uri]$baseUrl
if ($endpoint.Scheme -ne "https" -or $endpoint.Query -or $endpoint.Fragment) {
    throw "The decrypted Bark endpoint must be an HTTPS URL without a query or fragment."
}

$segments = @($endpoint.AbsolutePath.Trim("/").Split("/") | Where-Object { $_ })
if ($segments.Count -ne 1 -or $segments[0] -notmatch "^[A-Za-z0-9_-]+$") {
    throw "The decrypted Bark endpoint must contain exactly one device key."
}
$deviceKey = $segments[0]

$pushEndpoint = [UriBuilder]$endpoint
$pushEndpoint.Path = "/push"
$pushEndpoint.Query = ""
$pushEndpoint.Fragment = ""
$payload = [ordered]@{
    device_key = $deviceKey
    title = $Title
    body = $Body
}
$payloadJson = $payload | ConvertTo-Json -Compress

if ($DryRun) {
    return [pscustomobject]@{
        status = "dry-run"
        transport = "POST JSON"
        destinationHost = $pushEndpoint.Host
        titleCharacters = $Title.Length
        bodyUtf8Bytes = [Text.Encoding]::UTF8.GetByteCount($Body)
    }
}

$lastError = $null
for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
        $response = Invoke-RestMethod `
            -Uri $pushEndpoint.Uri.AbsoluteUri `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $payloadJson `
            -TimeoutSec $TimeoutSec
        $responseCode = $response.PSObject.Properties["code"].Value
        if ($null -ne $responseCode -and $responseCode -ne 200) {
            $responseMessage = $response.PSObject.Properties["message"].Value
            throw "Bark returned code ${responseCode}: ${responseMessage}"
        }
        $timestamp = $response.PSObject.Properties["timestamp"].Value
        return [pscustomobject]@{
            status = "sent"
            timestamp = $timestamp
        }
    }
    catch {
        $lastError = $_
        if ($attempt -lt 3) {
            Start-Sleep -Seconds ([Math]::Pow(2, $attempt - 1))
        }
    }
}
throw $lastError
