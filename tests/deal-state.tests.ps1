$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$stateScript = Join-Path $repositoryRoot "scripts\deal-state.ps1"
$testDirectory = Join-Path ([IO.Path]::GetTempPath()) ("shopping-deal-rss-state-tests-" + [guid]::NewGuid())
[IO.Directory]::CreateDirectory($testDirectory) | Out-Null
$itemsPath = Join-Path $testDirectory "items.json"
$itemJsonPath = Join-Path $testDirectory "item.json"
$statePath = Join-Path $testDirectory "state.json"
$now = [DateTimeOffset]::UtcNow

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

function New-TestItem {
    param([string]$SourceId, [string]$SourceUrl, [decimal]$Price)
    return [ordered]@{
        source = "test"
        sourceId = $SourceId
        sourceUrl = $SourceUrl
        productUrl = "https://example.com/item/same-product"
        title = "Same coupon stack"
        dealType = "coupon-stack"
        publishedAt = $now.AddMinutes(-3).ToString("o")
        observedAt = $now.AddMinutes(-1).ToString("o")
        estimatedFinalPayable = $Price
        priceBasis = "total"
        historicalPrice = 30
        historicalDiscountPct = 30
        historicalSourceUrl = "https://example.com/history"
        aiDecision = "verified-good-deal"
        aiConfidence = 0.9
        qualityScore = 92
        qualityConfidence = "high"
        urgency = "timeSensitive"
        reproductionSteps = @("Claim coupon", "Add item to cart")
    }
}

function Write-TestItems {
    param([object[]]$Items, [string]$Path)
    $json = [ordered]@{ items = @($Items) } | ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($Path, $json, [Text.UTF8Encoding]::new($false))
}

function Invoke-StateAction {
    param([string[]]$Arguments)
    $raw = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $stateScript @Arguments | Out-String
    return $raw | ConvertFrom-Json
}

try {
    $first = New-TestItem "source-1" "https://example.com/post/1" 21
    Write-TestItems @($first) $itemsPath
    $initialized = Invoke-StateAction @("-Action", "Initialize", "-ItemsFile", $itemsPath, "-StatePath", $statePath)
    Assert-True ($initialized.baselineCount -eq 1) "Initialize should store one baseline item."

    $firstJson = $first | ConvertTo-Json -Compress -Depth 10
    [IO.File]::WriteAllText($itemJsonPath, $firstJson, [Text.UTF8Encoding]::new($false))
    $marked = Invoke-StateAction @("-Action", "MarkSent", "-StatePath", $statePath, "-ItemJsonFile", $itemJsonPath)
    Assert-True ($marked.status -eq "sent") "MarkSent should persist successful delivery."
    Assert-True ($null -ne $marked.sourceToNotifySeconds) "MarkSent should report source-to-notify latency."
    $savedState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    Assert-True ([string]$savedState.items[0].historicalPrice -eq "30") "Historical price should be persisted."
    Assert-True ([string]$savedState.items[0].aiDecision -eq "verified-good-deal") "AI decision should be persisted."

    $duplicate = New-TestItem "source-2" "https://example.com/post/2" 21
    Write-TestItems @($duplicate) $itemsPath
    $evaluation = Invoke-StateAction @("-Action", "Evaluate", "-ItemsFile", $itemsPath, "-StatePath", $statePath)
    $duplicateRejected = @($evaluation.rejected | Where-Object { $_.reason -eq "duplicate_content" })
    Assert-True ($duplicateRejected.Count -eq 1) "Same content under another source ID should be suppressed."

    $changed = New-TestItem "source-3" "https://example.com/post/3" 22
    Write-TestItems @($changed) $itemsPath
    $changedEvaluation = Invoke-StateAction @("-Action", "Evaluate", "-ItemsFile", $itemsPath, "-StatePath", $statePath)
    Assert-True (@($changedEvaluation.eligible).Count -eq 1) "A materially changed price should remain eligible."

    [pscustomobject]@{
        status = "passed"
    assertions = 7
    } | ConvertTo-Json -Compress
}
finally {
    foreach ($file in @($itemsPath, $itemJsonPath, $statePath, "$statePath.lock")) {
        if ([IO.File]::Exists($file)) {
            [IO.File]::Delete($file)
        }
    }
    if ([IO.Directory]::Exists($testDirectory)) {
        [IO.Directory]::Delete($testDirectory, $false)
    }
}
