[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Title,

    [Parameter(Mandatory = $true)]
    [string]$Body,

    [string]$SecretPath = "C:\Users\X\.codex\secrets\shopping-deal-rss-bark.bin",

    [ValidateSet("passive", "active", "timeSensitive", "critical")]
    [string]$Level = "active",

    [string]$Url = "",

    [string]$Group = "shopping-deals",

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
if (-not [string]::IsNullOrWhiteSpace($Url)) {
    $Url = $Url.Trim()
    $linkUri = $null
    if ($Url.Length -gt 2000 -or -not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$linkUri) -or @("http", "https") -notcontains $linkUri.Scheme.ToLowerInvariant()) {
        throw "Url must be a valid HTTP(S) URL no longer than 2000 characters."
    }
}

if (Test-Path -LiteralPath $SecretPath) {
    $encrypted = [IO.File]::ReadAllBytes($SecretPath)
    $decrypted = [Security.Cryptography.ProtectedData]::Unprotect(
        $encrypted,
        $null,
        [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $baseUrl = [Text.Encoding]::UTF8.GetString($decrypted).Trim().TrimEnd("/")
}
elseif (-not [string]::IsNullOrWhiteSpace($env:BARK_URL)) {
    $baseUrl = $env:BARK_URL.Trim().TrimEnd("/")
}
else {
    throw "Bark secret file was not found and BARK_URL is not set."
}
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
    level = $Level
    group = $Group
}
if ($Url) {
    $payload.url = $Url
}
$payloadJson = $payload | ConvertTo-Json -Compress

if ($DryRun) {
    return [pscustomobject]@{
        status = "dry-run"
        transport = "POST JSON"
        destinationHost = $pushEndpoint.Host
        level = $Level
        urlHost = if ($Url) { ([Uri]$Url).Host } else { "" }
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
        $codeProperty = $response.PSObject.Properties["code"]
        $responseCode = if ($null -ne $codeProperty) { $codeProperty.Value } else { $null }
        if ($null -ne $responseCode -and $responseCode -ne 200) {
            $messageProperty = $response.PSObject.Properties["message"]
            $responseMessage = if ($null -ne $messageProperty) { $messageProperty.Value } else { "unknown error" }
            throw "Bark returned code ${responseCode}: ${responseMessage}"
        }
        $timestampProperty = $response.PSObject.Properties["timestamp"]
        $timestamp = if ($null -ne $timestampProperty) { $timestampProperty.Value } else { $null }
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
