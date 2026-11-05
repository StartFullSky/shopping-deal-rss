[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ItemJsonFile,

    [double]$MinHistoricalDiscountPct = 30,

    [string]$Model = "",

    [string]$CodexCommandPath = "",

    [string]$SchemaPath = "",

    [ValidateRange(30, 600)]
    [int]$TimeoutSec = 180,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $ItemJsonFile)) {
    throw "Item JSON file was not found: $ItemJsonFile"
}
$itemJson = [IO.File]::ReadAllText($ItemJsonFile, [Text.UTF8Encoding]::new($false)).Trim()
if ([string]::IsNullOrWhiteSpace($itemJson)) {
    throw "Item JSON is empty."
}
if ([string]::IsNullOrWhiteSpace($SchemaPath)) {
    $SchemaPath = Join-Path (Split-Path -Parent $PSScriptRoot) "assets\ai-price-review.schema.json"
}
$SchemaPath = (Resolve-Path -LiteralPath $SchemaPath).Path

if ([string]::IsNullOrWhiteSpace($CodexCommandPath)) {
    $codexCommand = Get-Command "codex.cmd" -ErrorAction SilentlyContinue
    if ($null -eq $codexCommand) {
        $codexCommand = Get-Command "codex.exe" -ErrorAction SilentlyContinue
    }
    if ($null -eq $codexCommand) {
        $codexCommand = Get-Command "codex.ps1" -ErrorAction SilentlyContinue
    }
    if ($null -eq $codexCommand) {
        if ($DryRun) {
            return [pscustomobject]@{
                status = "dry-run"
                codexAvailable = $false
                schemaPath = $SchemaPath
                itemCharacters = $itemJson.Length
                minHistoricalDiscountPct = $MinHistoricalDiscountPct
            }
        }
        throw "Codex CLI was not found."
    }
    $CodexCommandPath = [string]$codexCommand.Source
}
elseif (-not (Test-Path -LiteralPath $CodexCommandPath)) {
    throw "Codex command was not found: $CodexCommandPath"
}

if ($DryRun) {
    return [pscustomobject]@{
        status = "dry-run"
        codexAvailable = $true
        codexCommandPath = $CodexCommandPath
        schemaPath = $SchemaPath
        itemCharacters = $itemJson.Length
        minHistoricalDiscountPct = $MinHistoricalDiscountPct
    }
}

$prompt = @"
You are a strict shopping price-history reviewer.
Use only public web research. Do not log in, open carts, claim coupons, place orders, or bypass anti-bot controls.
Match the exact product, pack size, variant, quantity, seller, and channel before comparing prices.
The current payable price is the candidate's estimatedFinalPayable.
A historical price must be a real recent price observation from the last 180 days. A list price, crossed-out price, coupon face value, or unsupported model memory is not historical evidence.
Approve only when the current payable price is at least $MinHistoricalDiscountPct percent below a verified comparable historical price and the deal still appears actionable.
If the evidence is missing, stale, mismatched, or ambiguous, set approved to false.
Return only one JSON object matching the supplied schema.

CANDIDATE_JSON
$itemJson
"@

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("shopping-deal-ai-review-" + [guid]::NewGuid().ToString("N"))
[void][IO.Directory]::CreateDirectory($temporaryDirectory)
$outputPath = Join-Path $temporaryDirectory "result.json"
$stdoutPath = Join-Path $temporaryDirectory "stdout.txt"
$stderrPath = Join-Path $temporaryDirectory "stderr.txt"

function Quote-Argument {
    param([string]$Value)
    return '"' + ($Value -replace '(\\*)"', '$1$1\"') + '"'
}

$argumentValues = @(
    "exec",
    "--skip-git-repo-check",
    "--ephemeral",
    "--ignore-user-config",
    "--ignore-rules",
    "--sandbox", "read-only",
    "--color", "never",
    "--output-schema", $SchemaPath,
    "--output-last-message", $outputPath
)
if (-not [string]::IsNullOrWhiteSpace($Model)) {
    $argumentValues += @("--model", $Model)
}
$argumentText = (@($argumentValues | ForEach-Object { Quote-Argument $_ }) -join " ")
$extension = [IO.Path]::GetExtension($CodexCommandPath).ToLowerInvariant()
$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
if (@(".cmd", ".bat") -contains $extension) {
    $startInfo.FileName = $env:ComSpec
    $startInfo.Arguments = '/d /s /c ""' + $CodexCommandPath + '" ' + $argumentText + '"'
}
elseif ($extension -eq ".ps1") {
    $startInfo.FileName = (Get-Command "powershell.exe").Source
    $startInfo.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + (Quote-Argument $CodexCommandPath) + " " + $argumentText
}
else {
    $startInfo.FileName = $CodexCommandPath
    $startInfo.Arguments = $argumentText
}

$process = [Diagnostics.Process]::Start($startInfo)
try {
    $process.StandardInput.Write($prompt)
    $process.StandardInput.Close()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutSec * 1000)) {
        & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
        throw "AI price review timed out after $TimeoutSec seconds."
    }
    $stdout = $stdoutTask.Result
    $stderr = $stderrTask.Result
    [IO.File]::WriteAllText($stdoutPath, $stdout, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($stderrPath, $stderr, [Text.UTF8Encoding]::new($false))
    if ($process.ExitCode -ne 0) {
        throw "AI price review failed with exit code $($process.ExitCode)."
    }
    if (-not (Test-Path -LiteralPath $outputPath)) {
        throw "AI price review did not produce a final JSON result."
    }
    $resultText = [IO.File]::ReadAllText($outputPath, [Text.UTF8Encoding]::new($false)).Trim()
    $resultText = $resultText -replace '^```(?:json)?\s*', '' -replace '\s*```$', ''
    $result = $resultText | ConvertFrom-Json
    $requiredProperties = @(
        "approved", "decision", "confidence", "currentPrice",
        "historicalPrice", "discountPct", "historicalSourceUrl",
        "evidence", "reason"
    )
    foreach ($property in $requiredProperties) {
        if ($null -eq $result.PSObject.Properties[$property]) {
            throw "AI price review result is missing '$property'."
        }
    }
    return $result
}
finally {
    if (-not $process.HasExited) {
        & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
    }
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}
