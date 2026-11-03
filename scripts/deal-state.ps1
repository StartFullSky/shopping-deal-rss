[CmdletBinding()]
param(
    [ValidateSet("Initialize", "Evaluate", "MarkSent", "Status")]
    [string]$Action = "Status",
    [string]$StatePath = "C:\Users\X\.codex\state\shopping-deal-rss-state.json",
    [string]$ItemsFile,
    [string]$ItemJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-TextValue {
    param($Item, [string[]]$Names)
    foreach ($name in $Names) {
        $property = $Item.PSObject.Properties[$name]
        if ($null -ne $property -and $null -ne $property.Value) {
            $value = ([string]$property.Value).Trim()
            if ($value) {
                return $value
            }
        }
    }
    return $null
}

function Get-ArrayValue {
    param($Item, [string]$Name)
    $property = $Item.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        return @()
    }
    if ($property.Value -is [System.Collections.IEnumerable] -and $property.Value -isnot [string]) {
        return @($property.Value | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    }
    $value = ([string]$property.Value).Trim()
    if ($value) {
        return @($value)
    }
    return @()
}

function Normalize-Url {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    try {
        $uri = [Uri]$Value.Trim()
        if (-not $uri.IsAbsoluteUri) {
            return $Value.Trim().TrimEnd("/")
        }
        $builder = [UriBuilder]$uri
        $builder.Scheme = $uri.Scheme.ToLowerInvariant()
        $builder.Host = $uri.Host.ToLowerInvariant()
        $builder.Fragment = ""
        if ($builder.Path.Length -gt 1) {
            $builder.Path = $builder.Path.TrimEnd("/")
        }
        $trackingNames = @(
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "spm", "scm", "pvid", "jd_ref", "from", "scene", "clickid"
        )
        $queryParts = @()
        if (-not [string]::IsNullOrWhiteSpace($uri.Query)) {
            foreach ($part in $uri.Query.TrimStart("?").Split("&")) {
                if ([string]::IsNullOrWhiteSpace($part)) {
                    continue
                }
                $name = [Uri]::UnescapeDataString($part.Split("=", 2)[0]).ToLowerInvariant()
                if ($trackingNames -notcontains $name) {
                    $queryParts += $part
                }
            }
        }
        $builder.Query = ($queryParts | Sort-Object) -join "&"
        return $builder.Uri.AbsoluteUri
    }
    catch {
        return $Value.Trim().TrimEnd("/")
    }
}

function ConvertTo-Timestamp {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    $styles = [Globalization.DateTimeStyles]::AllowWhiteSpaces -bor
        [Globalization.DateTimeStyles]::AssumeLocal -bor
        [Globalization.DateTimeStyles]::AdjustToUniversal
    return [DateTimeOffset]::Parse($Value, [Globalization.CultureInfo]::InvariantCulture, $styles)
}

function ConvertTo-InvariantNumber {
    param($Value)
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        return $null
    }
    $number = 0.0
    $styles = [Globalization.NumberStyles]::Float -bor [Globalization.NumberStyles]::AllowThousands
    if ([double]::TryParse([string]$Value, $styles, [Globalization.CultureInfo]::InvariantCulture, [ref]$number)) {
        return [Math]::Round($number, 2)
    }
    return ([string]$Value).Trim()
}

function Get-Sha256 {
    param([string]$Value)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
        return (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join "")
    }
    finally {
        $sha.Dispose()
    }
}

function Get-CanonicalText {
    param($Value)
    if ($null -eq $Value) {
        return ""
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        return (@($Value | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ }) -join "|")
    }
    return ([string]$Value).Trim()
}

function ConvertTo-DealView {
    param($Item)
    $source = Get-TextValue $Item @("source")
    $sourceId = Get-TextValue $Item @("sourceId", "source_id", "id")
    $sourceUrl = Get-TextValue $Item @("sourceUrl", "source_url", "articleUrl")
    $productUrl = Normalize-Url (Get-TextValue $Item @("productUrl", "product_url", "itemUrl"))
    $dealType = ([string](Get-TextValue $Item @("dealType", "deal_type", "informationAsymmetryType"))).ToLowerInvariant()
    $publishedAt = ConvertTo-Timestamp (Get-TextValue $Item @("publishedAt", "datePublished"))
    $modifiedAt = ConvertTo-Timestamp (Get-TextValue $Item @("modifiedAt", "dateModified"))
    $observedAt = ConvertTo-Timestamp (Get-TextValue $Item @("observedAt", "observed_at"))
    $sourceTimestamp = if ($null -eq $publishedAt) {
        $modifiedAt
    }
    elseif ($null -eq $modifiedAt) {
        $publishedAt
    }
    elseif ($modifiedAt -gt $publishedAt) {
        $modifiedAt
    }
    else {
        $publishedAt
    }

    $identitySource = if ($productUrl) {
        $productUrl
    }
    elseif ($sourceId) {
        "$source/$sourceId"
    }
    else {
        $sourceUrl
    }
    $identityText = @(
        ([string]$source).ToLowerInvariant(),
        ([string]$identitySource).ToLowerInvariant(),
        $dealType
    ) -join "|"

    $fingerprintText = @(
        ([string]$identitySource).ToLowerInvariant(),
        $dealType,
        (Get-CanonicalText (Get-TextValue $Item @("estimatedFinalPayable", "estimated_final_payable", "finalPayable"))),
        (Get-CanonicalText (Get-TextValue $Item @("originalPrice", "original_price", "listPrice"))),
        (Get-CanonicalText (Get-TextValue $Item @("referencePrice", "reference_price"))),
        (Get-CanonicalText (Get-TextValue $Item @("couponState", "coupon_state"))),
        (Get-CanonicalText (Get-TextValue $Item @("quantityCondition", "quantity_condition"))),
        (Get-CanonicalText (Get-TextValue $Item @("variant", "sku"))),
        (Get-CanonicalText (Get-TextValue $Item @("channel", "eligibility"))),
        (Get-CanonicalText (Get-TextValue $Item @("validUntil", "valid_until"))),
        (Get-CanonicalText (Get-ArrayValue $Item "reproductionSteps"))
    ) -join "`n"

    $missing = [Collections.Generic.List[string]]::new()
    $requiredValues = [ordered]@{
        source = $source
        sourceUrl = $sourceUrl
        productUrl = $productUrl
        dealType = $dealType
        observedAt = $observedAt
        sourceTimestamp = $sourceTimestamp
    }
    foreach ($entry in $requiredValues.GetEnumerator()) {
        if ($null -eq $entry.Value -or [string]::IsNullOrWhiteSpace([string]$entry.Value)) {
            $missing.Add($entry.Key)
        }
    }

    $estimatedFinalPayable = ConvertTo-InvariantNumber (Get-TextValue $Item @(
        "estimatedFinalPayable", "estimated_final_payable", "finalPayable"
    ))
    if ($null -eq $estimatedFinalPayable) {
        $missing.Add("estimatedFinalPayable")
    }
    $reproductionSteps = @(Get-ArrayValue $Item "reproductionSteps")
    if ($reproductionSteps.Count -eq 0) {
        $missing.Add("reproductionSteps")
    }

    $allowedDealTypes = @(
        "price-error", "zero-price", "flash-inventory", "coupon-stack", "hidden-stack",
        "hidden-eligibility", "channel-specific", "checkout-mismatch", "variant-mismatch"
    )
    if ($dealType -and $allowedDealTypes -notcontains $dealType) {
        $missing.Add("unsupportedDealType")
    }

    foreach ($linkName in @("sourceUrl", "productUrl")) {
        $linkValue = if ($linkName -eq "sourceUrl") { $sourceUrl } else { $productUrl }
        if ($linkValue) {
            try {
                $linkUri = [Uri]$linkValue
                if (-not $linkUri.IsAbsoluteUri -or @("http", "https") -notcontains $linkUri.Scheme.ToLowerInvariant()) {
                    $missing.Add("${linkName}Invalid")
                }
            }
            catch {
                $missing.Add("${linkName}Invalid")
            }
        }
    }

    return [ordered]@{
        stateKey = Get-Sha256 $identityText
        offerFingerprint = Get-Sha256 $fingerprintText
        source = $source
        sourceId = $sourceId
        sourceUrl = $sourceUrl
        productUrl = $productUrl
        title = Get-TextValue $Item @("title")
        dealType = $dealType
        publishedAt = if ($publishedAt) { $publishedAt.ToString("o") } else { $null }
        modifiedAt = if ($modifiedAt) { $modifiedAt.ToString("o") } else { $null }
        sourceTimestamp = if ($sourceTimestamp) { $sourceTimestamp.ToString("o") } else { $null }
        observedAt = if ($observedAt) { $observedAt.ToString("o") } else { $null }
        originalPrice = ConvertTo-InvariantNumber (Get-TextValue $Item @("originalPrice", "original_price", "listPrice"))
        referencePrice = ConvertTo-InvariantNumber (Get-TextValue $Item @("referencePrice", "reference_price"))
        estimatedFinalPayable = $estimatedFinalPayable
        couponState = Get-TextValue $Item @("couponState", "coupon_state")
        quantityCondition = Get-TextValue $Item @("quantityCondition", "quantity_condition")
        variant = Get-TextValue $Item @("variant", "sku")
        channel = Get-TextValue $Item @("channel", "eligibility")
        validUntil = Get-TextValue $Item @("validUntil", "valid_until")
        reproductionSteps = $reproductionSteps
        missingFields = @($missing)
    }
}

function Read-Items {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "ItemsFile is required for this action."
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Items file was not found: $Path"
    }
    $raw = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $itemsProperty = $raw.PSObject.Properties["items"]
    $items = if ($null -ne $itemsProperty) { $itemsProperty.Value } else { $raw }
    return @($items)
}

function Read-State {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
}

function New-State {
    param([DateTimeOffset]$Now)
    return [ordered]@{
        schemaVersion = 1
        initializedAt = $Now.ToString("o")
        updatedAt = $Now.ToString("o")
        items = @()
    }
}

function Write-State {
    param($State, [string]$Path)
    $directory = Split-Path -Parent $Path
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        $null = New-Item -ItemType Directory -Path $directory -Force
    }
    $temporaryPath = "$Path.$PID.tmp"
    $json = $State | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText($temporaryPath, $json, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
}

function Invoke-StateMutation {
    param([scriptblock]$ScriptBlock)
    $directory = Split-Path -Parent $StatePath
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        $null = New-Item -ItemType Directory -Path $directory -Force
    }
    $lockPath = "$StatePath.lock"
    $lock = [IO.File]::Open(
        $lockPath,
        [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
    try {
        return & $ScriptBlock
    }
    finally {
        $lock.Dispose()
        Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
    }
}

function Test-Eligible {
    param($View, [DateTimeOffset]$Now)
    if (@($View.missingFields).Count -gt 0) {
        return [ordered]@{
            eligible = $false
            reason = "incomplete"
            details = @($View.missingFields)
        }
    }
    $sourceTimestamp = ConvertTo-Timestamp $View.sourceTimestamp
    $observedAt = ConvertTo-Timestamp $View.observedAt
    $ageMinutes = ($Now - $sourceTimestamp).TotalMinutes
    $futureMinutes = ($sourceTimestamp - $Now).TotalMinutes
    $freshnessMinutes = if (@("price-error", "zero-price", "flash-inventory") -contains $View.dealType) {
        30
    }
    else {
        360
    }
    if ($futureMinutes -gt 10) {
        return [ordered]@{ eligible = $false; reason = "future_source_timestamp"; details = @() }
    }
    if ($ageMinutes -lt 0 -or $ageMinutes -gt $freshnessMinutes) {
        return [ordered]@{ eligible = $false; reason = "stale"; details = @("limitMinutes=$freshnessMinutes") }
    }
    if (($Now - $observedAt).TotalMinutes -gt 24 * 60) {
        return [ordered]@{ eligible = $false; reason = "observation_too_old"; details = @() }
    }
    return [ordered]@{ eligible = $true; reason = "fresh_and_complete"; details = @() }
}

function Get-ItemRecord {
    param($State, [string]$StateKey)
    foreach ($item in @($State.items)) {
        if ($item.stateKey -eq $StateKey) {
            return $item
        }
    }
    return $null
}

function Set-ItemRecord {
    param($State, $Record)
    $items = [Collections.Generic.List[object]]::new()
    $replaced = $false
    foreach ($item in @($State.items)) {
        if ($item.stateKey -eq $Record.stateKey) {
            $items.Add($Record)
            $replaced = $true
        }
        else {
            $items.Add($item)
        }
    }
    if (-not $replaced) {
        $items.Add($Record)
    }
    $State.items = @($items)
}

$now = [DateTimeOffset]::Now

switch ($Action) {
    "Status" {
        $state = Read-State $StatePath
        if ($null -eq $state) {
            [ordered]@{
                action = "status"
                statePath = $StatePath
                stateExists = $false
                initialized = $false
                itemCount = 0
            } | ConvertTo-Json -Depth 6
            return
        }
        [ordered]@{
            action = "status"
            statePath = $StatePath
            stateExists = $true
            initialized = $true
            schemaVersion = $state.schemaVersion
            initializedAt = $state.initializedAt
            updatedAt = $state.updatedAt
            itemCount = @($state.items).Count
            sentCount = @($state.items | Where-Object { $_.status -eq "sent" }).Count
            baselineCount = @($state.items | Where-Object { $_.status -eq "baseline" }).Count
        } | ConvertTo-Json -Depth 6
        return
    }

    "Initialize" {
        $result = Invoke-StateMutation {
            if (Test-Path -LiteralPath $StatePath) {
                throw "State is already initialized at $StatePath"
            }
            $state = New-State $now
            $initialized = [Collections.Generic.List[object]]::new()
            $rejected = [Collections.Generic.List[object]]::new()
            foreach ($item in (Read-Items $ItemsFile)) {
                $view = ConvertTo-DealView $item
                if (-not $view.sourceUrl -and -not $view.productUrl -and -not $view.sourceId) {
                    $rejected.Add([ordered]@{ reason = "missing_identity"; item = $view })
                    continue
                }
                $record = [ordered]@{
                    stateKey = $view.stateKey
                    offerFingerprint = $view.offerFingerprint
                    source = $view.source
                    sourceId = $view.sourceId
                    sourceUrl = $view.sourceUrl
                    productUrl = $view.productUrl
                    dealType = $view.dealType
                    sourceTimestamp = $view.sourceTimestamp
                    firstSeenAt = $now.ToString("o")
                    lastSeenAt = $view.observedAt
                    status = "baseline"
                    notifiedAt = $null
                }
                Set-ItemRecord $state $record
                $initialized.Add([ordered]@{
                    stateKey = $view.stateKey
                    offerFingerprint = $view.offerFingerprint
                })
            }
            $state.updatedAt = $now.ToString("o")
            Write-State $state $StatePath
            return [ordered]@{
                action = "initialize"
                statePath = $StatePath
                baselineCount = $initialized.Count
                rejectedCount = $rejected.Count
                initialized = @($initialized)
                rejected = @($rejected)
            }
        }
        $result | ConvertTo-Json -Depth 10
        return
    }

    "Evaluate" {
        $state = Read-State $StatePath
        $eligible = [Collections.Generic.List[object]]::new()
        $rejected = [Collections.Generic.List[object]]::new()
        foreach ($item in (Read-Items $ItemsFile)) {
            $view = ConvertTo-DealView $item
            $policy = Test-Eligible $view $now
            $existing = if ($null -ne $state) { Get-ItemRecord $state $view.stateKey } else { $null }
            if ($null -eq $state) {
                $rejected.Add([ordered]@{
                    stateKey = $view.stateKey
                    offerFingerprint = $view.offerFingerprint
                    reason = "baseline_required"
                    details = @()
                })
                continue
            }
            if (-not $policy.eligible) {
                $rejected.Add([ordered]@{
                    stateKey = $view.stateKey
                    offerFingerprint = $view.offerFingerprint
                    reason = $policy.reason
                    details = $policy.details
                })
                continue
            }
            if ($null -ne $existing -and $existing.offerFingerprint -eq $view.offerFingerprint) {
                $reason = if ($existing.status -eq "sent") { "duplicate_sent" } else { "baseline_suppressed" }
                $rejected.Add([ordered]@{
                    stateKey = $view.stateKey
                    offerFingerprint = $view.offerFingerprint
                    reason = $reason
                    details = @()
                })
                continue
            }
            $decision = if ($null -eq $existing) { "new_item" } else { "material_change" }
            $eligible.Add([ordered]@{
                decision = $decision
                stateKey = $view.stateKey
                offerFingerprint = $view.offerFingerprint
                item = $view
            })
        }
        [ordered]@{
            action = "evaluate"
            statePath = $StatePath
            stateExists = $true
            evaluatedAt = $now.ToString("o")
            eligible = @($eligible)
            rejected = @($rejected)
        } | ConvertTo-Json -Depth 12
        return
    }

    "MarkSent" {
        if ([string]::IsNullOrWhiteSpace($ItemJson)) {
            throw "ItemJson is required for MarkSent."
        }
        $item = $ItemJson | ConvertFrom-Json
        $view = ConvertTo-DealView $item
        $policy = Test-Eligible $view $now
        if (-not $policy.eligible) {
            throw "Item is no longer eligible: $($policy.reason)"
        }
        $result = Invoke-StateMutation {
            $state = Read-State $StatePath
            if ($null -eq $state) {
                throw "State is not initialized at $StatePath"
            }
            $existing = Get-ItemRecord $state $view.stateKey
            if ($null -ne $existing -and
                $existing.status -eq "sent" -and
                $existing.offerFingerprint -eq $view.offerFingerprint) {
                return [ordered]@{
                    action = "mark_sent"
                    status = "already_sent"
                    stateKey = $view.stateKey
                    offerFingerprint = $view.offerFingerprint
                    notifiedAt = $existing.notifiedAt
                }
            }
            $firstSeenAt = if ($null -ne $existing -and $existing.firstSeenAt) {
                $existing.firstSeenAt
            }
            else {
                $now.ToString("o")
            }
            $record = [ordered]@{
                stateKey = $view.stateKey
                offerFingerprint = $view.offerFingerprint
                source = $view.source
                sourceId = $view.sourceId
                sourceUrl = $view.sourceUrl
                productUrl = $view.productUrl
                dealType = $view.dealType
                sourceTimestamp = $view.sourceTimestamp
                firstSeenAt = $firstSeenAt
                lastSeenAt = $view.observedAt
                status = "sent"
                notifiedAt = $now.ToString("o")
            }
            Set-ItemRecord $state $record
            $state.updatedAt = $now.ToString("o")
            Write-State $state $StatePath
            return [ordered]@{
                action = "mark_sent"
                status = "sent"
                stateKey = $view.stateKey
                offerFingerprint = $view.offerFingerprint
                notifiedAt = $record.notifiedAt
            }
        }
        $result | ConvertTo-Json -Depth 8
        return
    }
}
