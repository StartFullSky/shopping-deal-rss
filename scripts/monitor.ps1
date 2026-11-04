[CmdletBinding()]
param(
    [string]$ConfigPath = "",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-PropertyValue {
    param($InputObject, [string[]]$Names, $Default = $null)
    if ($null -eq $InputObject) {
        return $Default
    }
    foreach ($name in $Names) {
        $property = $InputObject.PSObject.Properties[$name]
        if ($null -ne $property -and $null -ne $property.Value) {
            return $property.Value
        }
    }
    return $Default
}

function Get-ArrayValue {
    param($InputObject, [string]$Name)
    $value = Get-PropertyValue $InputObject @($Name) @()
    if ($null -eq $value) {
        return @()
    }
    if ($value -is [System.Collections.IEnumerable] -and $value -isnot [string]) {
        return @($value | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    }
    $text = ([string]$value).Trim()
    if ($text) {
        return @($text)
    }
    return @()
}

function Expand-ConfigValue {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return ""
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($Value.Trim())
    if ($expanded -match "%[^%]+%") {
        return ""
    }
    return $expanded
}

function Get-Sha256Text {
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

function Get-DealStateKeyFromItem {
    param($Item)
    $source = ([string](Get-PropertyValue $Item @("source") "")).ToLowerInvariant()
    $sourceId = [string](Get-PropertyValue $Item @("sourceId") "")
    $productUrl = [string](Get-PropertyValue $Item @("productUrl") "")
    $sourceUrl = [string](Get-PropertyValue $Item @("sourceUrl") "")
    $identitySource = if ($sourceId) {
        "$source/$sourceId"
    }
    elseif ($productUrl) {
        $productUrl
    }
    else {
        $sourceUrl
    }
    return Get-Sha256Text ("$source|$($identitySource.ToLowerInvariant())")
}

function ConvertTo-DateTimeOffset {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }
    $styles = [Globalization.DateTimeStyles]::AllowWhiteSpaces -bor
        [Globalization.DateTimeStyles]::AssumeLocal -bor
        [Globalization.DateTimeStyles]::AdjustToUniversal
    $parsed = [DateTimeOffset]::Now
    if ([DateTimeOffset]::TryParse(
        $Value,
        [Globalization.CultureInfo]::InvariantCulture,
        $styles,
        [ref]$parsed
    )) {
        return $parsed
    }
    return $null
}

function New-HttpClientBundle {
    param([string]$ProxyUrl, [int]$TimeoutSec, [string]$UserAgent)
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.AutomaticDecompression = [Net.DecompressionMethods]::GZip -bor
        [Net.DecompressionMethods]::Deflate
    if (-not [string]::IsNullOrWhiteSpace($ProxyUrl)) {
        $handler.Proxy = New-Object System.Net.WebProxy($ProxyUrl)
        $handler.UseProxy = $true
    }
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSec)
    if (-not [string]::IsNullOrWhiteSpace($UserAgent)) {
        $client.DefaultRequestHeaders.UserAgent.ParseAdd($UserAgent)
    }
    return [pscustomobject]@{
        Client = $client
        Handler = $handler
    }
}

function Invoke-HttpText {
    param($Client, [string]$Url)
    $response = $null
    try {
        $response = $Client.GetAsync($Url).GetAwaiter().GetResult()
        $statusCode = [int]$response.StatusCode
        if ($statusCode -eq 429 -or $statusCode -eq 503) {
            throw "Rate limited by ${Url}: HTTP ${statusCode}"
        }
        if (-not $response.IsSuccessStatusCode) {
            throw "Request failed for ${Url}: HTTP ${statusCode}"
        }
        return $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    }
    finally {
        if ($null -ne $response) {
            $response.Dispose()
        }
    }
}

function Get-XmlNodeText {
    param($Parent, [string]$Name)
    $node = $Parent.SelectSingleNode("./*[local-name()='$Name']")
    if ($null -eq $node) {
        return ""
    }
    return ([string]$node.InnerText).Trim()
}

function ConvertFrom-RssText {
    param([string]$Text)
    [xml]$document = $Text
    $entries = @($document.SelectNodes("//*[local-name()='item' or local-name()='entry']"))
    $items = foreach ($entry in $entries) {
        $link = Get-XmlNodeText $entry "link"
        if (-not $link) {
            $linkNode = $entry.SelectSingleNode("./*[local-name()='link']")
            if ($null -ne $linkNode) {
                $attribute = $linkNode.Attributes["href"]
                if ($null -ne $attribute) {
                    $link = ([string]$attribute.Value).Trim()
                }
            }
        }
        $identifier = Get-XmlNodeText $entry "guid"
        if (-not $identifier) {
            $identifier = Get-XmlNodeText $entry "id"
        }
        $description = Get-XmlNodeText $entry "description"
        if (-not $description) {
            $description = Get-XmlNodeText $entry "summary"
        }
        if (-not $description) {
            $description = Get-XmlNodeText $entry "content"
        }
        $published = Get-XmlNodeText $entry "pubDate"
        if (-not $published) {
            $published = Get-XmlNodeText $entry "published"
        }
        $modified = Get-XmlNodeText $entry "updated"
        [pscustomobject]@{
            Title = Get-XmlNodeText $entry "title"
            Link = $link
            Identifier = $identifier
            Description = $description
            PublishedAt = $published
            ModifiedAt = $modified
        }
    }
    return @($items)
}

function ConvertFrom-HtmlText {
    param([string]$Html)
    if ([string]::IsNullOrWhiteSpace($Html)) {
        return ""
    }
    $text = [regex]::Replace($Html, "(?is)<img\b.*?(?:/>|</img>)", " ")
    $text = [regex]::Replace($text, "(?is)<image\b.*?</image>", " ")
    $text = [regex]::Replace($text, "(?is)<(script|style)\b.*?</\1>", " ")
    $text = [regex]::Replace($text, "(?i)<br\s*/?>", "`n")
    $text = [regex]::Replace($text, "(?i)</(p|div|li|h[1-6]|tr)>", "`n")
    $text = [regex]::Replace($text, "<[^>]+>", " ")
    $text = [Net.WebUtility]::HtmlDecode($text)
    $text = [regex]::Replace($text, "[ \t\u00a0]+", " ")
    $text = [regex]::Replace($text, "(\r?\n[ \t]*){2,}", "`n")
    return $text.Trim()
}

function Get-HtmlLinks {
    param([string]$Html)
    $links = foreach ($match in [regex]::Matches($Html, "href\s*=\s*[""'](?<url>[^""']+)[""']", "IgnoreCase")) {
        [Net.WebUtility]::HtmlDecode($match.Groups["url"].Value).Trim()
    }
    return @($links | Where-Object { $_ -match "^https?://" })
}

function Find-JsonLdArticle {
    param($Value)
    if ($null -eq $Value) {
        return $null
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        foreach ($entry in $Value) {
            $found = Find-JsonLdArticle $entry
            if ($null -ne $found) {
                return $found
            }
        }
        return $null
    }
    $typeProperty = $Value.PSObject.Properties["@type"]
    if ($null -ne $typeProperty) {
        $types = @($typeProperty.Value | ForEach-Object { ([string]$_) })
        if (@("Article", "NewsArticle", "BlogPosting") | Where-Object { $types -contains $_ }) {
            return $Value
        }
    }
    foreach ($property in $Value.PSObject.Properties) {
        $found = Find-JsonLdArticle $property.Value
        if ($null -ne $found) {
            return $found
        }
    }
    return $null
}

function Get-XianbaoDetail {
    param($Client, [string]$Url)
    $html = Invoke-HttpText $Client $Url
    $contentHtml = ""
    $contentMatch = [regex]::Match(
        $html,
        "(?is)<div[^>]+class=[""'][^""']*(?:article-content|post-content)[^""']*[""'][^>]*>(?<content>.*?)</div>"
    )
    if ($contentMatch.Success) {
        $contentHtml = $contentMatch.Groups["content"].Value
    }
    else {
        $articleMatch = [regex]::Match($html, "(?is)<article\b[^>]*>(?<content>.*?)</article>")
        if ($articleMatch.Success) {
            $contentHtml = $articleMatch.Groups["content"].Value
        }
    }
    $article = $null
    foreach ($match in [regex]::Matches($html, "<script[^>]+type=[""']application/ld\+json[""'][^>]*>(?<json>.*?)</script>", "IgnoreCase")) {
        try {
            $jsonValue = $match.Groups["json"].Value | ConvertFrom-Json
            $article = Find-JsonLdArticle $jsonValue
            if ($null -ne $article) {
                break
            }
        }
        catch {
        }
    }
    $plainText = ConvertFrom-HtmlText $contentHtml
    $lines = @($plainText -split "\r?\n" |
        ForEach-Object { $_.Trim() } |
        Where-Object {
            $_ -and
            $_ -notmatch "版权说明|临时存储|未经核实" -and
            $_ -notmatch "^<img>.*</img>$" -and
            $_ -notmatch "^[a-fA-F0-9]{16,64}$"
        } |
        Select-Object -Unique)
    return [pscustomobject]@{
        ContentText = $plainText
        ReproductionSteps = @($lines | Select-Object -First 10)
        Links = @(Get-HtmlLinks $contentHtml)
        PublishedAt = if ($null -ne $article) { [string](Get-PropertyValue $article @("datePublished") "") } else { "" }
        ModifiedAt = if ($null -ne $article) { [string](Get-PropertyValue $article @("dateModified") "") } else { "" }
    }
}

function Test-AnyPattern {
    param([string]$Text, [string[]]$Patterns)
    foreach ($pattern in $Patterns) {
        if (-not [string]::IsNullOrWhiteSpace($pattern) -and $Text -match $pattern) {
            return $true
        }
    }
    return $false
}

function Get-FirstPatternMatch {
    param([string]$Text, [string[]]$Patterns)
    foreach ($pattern in $Patterns) {
        if (-not [string]::IsNullOrWhiteSpace($pattern)) {
            $match = [regex]::Match($Text, $pattern, "IgnoreCase")
            if ($match.Success) {
                return $match
            }
        }
    }
    return $null
}

function Test-KeywordFilters {
    param([string]$Text, [string[]]$IncludeKeywords, [string[]]$ExcludeKeywords)
    $includeKeywords = @($IncludeKeywords | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $excludeKeywords = @($ExcludeKeywords | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($includeKeywords.Count -gt 0) {
        $included = $false
        foreach ($keyword in $includeKeywords) {
            if ($Text.IndexOf($keyword, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $included = $true
                break
            }
        }
        if (-not $included) {
            return $false
        }
    }
    foreach ($keyword in $excludeKeywords) {
        if ($Text.IndexOf($keyword, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            return $false
        }
    }
    return $true
}

function Get-DealType {
    param([string]$Text, $Rules)
    $dealTypes = Get-PropertyValue $Rules @("dealTypes") $null
    if ($null -eq $dealTypes) {
        return $null
    }
    foreach ($property in $dealTypes.PSObject.Properties) {
        $patterns = @($property.Value | ForEach-Object { ([string]$_) })
        if (Test-AnyPattern $Text $patterns) {
            return [string]$property.Name
        }
    }
    return $null
}

function Get-PriceAfterLabels {
    param([string]$Text, [string[]]$Labels)
    if ([string]::IsNullOrWhiteSpace($Text) -or @($Labels).Count -eq 0) {
        return $null
    }
    $labelPattern = (@($Labels | ForEach-Object { [regex]::Escape($_) }) -join "|")
    $pattern = "(?i)(?<![\p{L}\p{N}])(?:$labelPattern)\s*(?:价|价格)?\s*(?:仅约|约|仅|低至)?\s*[:：=]?\s*[¥￥]?\s*(?<price>\d+(?:\.\d{1,2})?)"
    $match = [regex]::Match($Text, $pattern)
    if (-not $match.Success) {
        return $null
    }
    return [decimal]$match.Groups["price"].Value
}

function Get-FinalPayable {
    param([string]$Text, [string]$Title)
    $value = Get-PriceAfterLabels $Text @(
        "到手价", "到手", "实付价", "实付", "券后价", "券后", "凑后价", "凑后",
        "最终实付", "最终到手", "最终价", "低至", "每件"
    )
    if ($null -ne $value) {
        return $value
    }
    $matches = [regex]::Matches($Title, "(?<price>\d+(?:\.\d{1,2})?)\s*元")
    if ($matches.Count -gt 0) {
        return [decimal]$matches[$matches.Count - 1].Groups["price"].Value
    }
    return $null
}

function Get-CurrentPrice {
    param([string]$Text)
    return Get-PriceAfterLabels $Text @("当前价", "当前售价", "现价", "京东价", "价格")
}

function Get-TextExcerpt {
    param([string]$Text, $Match, [int]$Radius = 70)
    if ($null -eq $Match) {
        return ""
    }
    $start = [Math]::Max(0, $Match.Index - $Radius)
    $length = [Math]::Min($Text.Length - $start, $Match.Length + (2 * $Radius))
    return ($Text.Substring($start, $length) -replace "\s+", " ").Trim()
}

function Get-MatchingLine {
    param([string]$Text, [string]$Pattern, [int]$MaxLength = 180)
    foreach ($line in @($Text -split "\r?\n")) {
        if ($line -match $Pattern) {
            $value = ($line -replace "\s+", " ").Trim()
            if ($value.Length -gt $MaxLength) {
                return $value.Substring(0, $MaxLength)
            }
            return $value
        }
    }
    return ""
}

function Get-SourceIdentity {
    param([string]$Url, [string]$Fallback)
    $path = $Url
    try {
        $path = ([Uri]$Url).AbsolutePath
    }
    catch {
    }
    $match = [regex]::Match($path, "(?<id>\d+)(?:\.html)?/?$")
    if ($match.Success) {
        return $match.Groups["id"].Value
    }
    if (-not [string]::IsNullOrWhiteSpace($Fallback)) {
        return $Fallback
    }
    return Get-Sha256Text $Url
}

function Get-BestProductLink {
    param([string[]]$Links, [string]$SourceUrl)
    $candidates = @($Links | Where-Object {
        $value = $_
        $uri = $null
        [Uri]::TryCreate($value, [UriKind]::Absolute, [ref]$uri) -and
            @("http", "https") -contains $uri.Scheme.ToLowerInvariant() -and
            $uri.Host -ne ([Uri]$SourceUrl).Host -and
            $value -notmatch "\.(png|jpe?g|gif|webp)(?:[?#]|$)"
    })
    foreach ($pattern in @(
        "item\.jd\.com|goods\.jd\.com|item\.m\.jd\.com",
        "detail\.tmall\.com|detail\.tm\.hk",
        "mobile\.yangkeduo\.com|p\.dd",
        "item\.m\.vip\.com|mall\.vip\.com",
        "u\.jd\.com|s\.click\.taobao\.com"
    )) {
        $match = $candidates | Where-Object { $_ -match $pattern } | Select-Object -First 1
        if ($match) {
            return [string]$match
        }
    }
    if ($candidates.Count -gt 0) {
        return [string]$candidates[0]
    }
    return ""
}

function Get-QuantityCondition {
    param([string]$Text)
    $match = [regex]::Match($Text, "(?i)(?:买|拍|购|下)\s*\d+\s*(?:件|个|盒|瓶|双)|限购\s*\d+|1\s*件|2\s*件")
    if ($match.Success) {
        return $match.Value
    }
    return ""
}

function Get-ValidUntil {
    param([string]$Text)
    $match = [regex]::Match($Text, "(?:截止|有效期|结束|至)\s*[:：]?\s*[^\r\n]{1,80}")
    if ($match.Success) {
        return ($match.Value -replace "\s+", " ").Trim()
    }
    return ""
}

function Get-Variant {
    param([string]$Text)
    $match = [regex]::Match($Text, "(?:规格|SKU|型号|颜色|尺寸|选项)\s*[:：]?\s*[^\r\n]{1,60}")
    if ($match.Success) {
        return ($match.Value -replace "\s+", " ").Trim()
    }
    return ""
}

function Get-Channel {
    param([string]$Text)
    foreach ($channel in @("小程序", "京东", "天猫", "淘宝", "拼多多", "抖音", "唯品会", "线下门店", "APP")) {
        if ($Text.IndexOf($channel, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            return $channel
        }
    }
    return ""
}

function New-DealItem {
    param(
        $Source,
        $FeedItem,
        $Detail,
        [string]$DealType,
        [decimal]$EstimatedFinalPayable,
        [string]$Evidence,
        [DateTimeOffset]$ObservedAt
    )
    $sourceUrl = [string]$FeedItem.Link
    $links = @($Detail.Links)
    $productUrl = Get-BestProductLink $links $sourceUrl
    if (-not $productUrl) {
        $productUrl = $sourceUrl
    }
    $publishedValue = if ($Detail.PublishedAt) { [string]$Detail.PublishedAt } else { [string]$FeedItem.PublishedAt }
    $modifiedValue = if ($Detail.ModifiedAt) { [string]$Detail.ModifiedAt } else { [string]$FeedItem.ModifiedAt }
    $publishedDate = ConvertTo-DateTimeOffset $publishedValue
    $modifiedDate = ConvertTo-DateTimeOffset $modifiedValue
    $publishedAt = if ($publishedDate) { $publishedDate.ToString("o") } else { $publishedValue }
    $modifiedAt = if ($modifiedDate) { $modifiedDate.ToString("o") } else { $modifiedValue }
    $combinedText = "$($FeedItem.Title)`n$($FeedItem.Description)`n$($Detail.ContentText)"
    $originalPrice = Get-PriceAfterLabels $combinedText @("原价", "标价", "日常价", "吊牌价")
    $referencePrice = Get-PriceAfterLabels $combinedText @("参考价", "活动价", "优惠前", "划线价")
    $steps = @($Detail.ReproductionSteps)
    if ($steps.Count -eq 0) {
        $steps = @((ConvertFrom-HtmlText $FeedItem.Description).Trim() | Where-Object { $_ })
    }
    if ($steps.Count -eq 0) {
        $steps = @([string]$FeedItem.Title)
    }
    return [pscustomobject]@{
        source = [string](Get-PropertyValue $Source @("sourceName") (Get-PropertyValue $Source @("id") "rss"))
        sourceId = Get-SourceIdentity $sourceUrl ([string]$FeedItem.Identifier)
        sourceUrl = $sourceUrl
        productUrl = $productUrl
        title = [string]$FeedItem.Title
        dealType = $DealType
        publishedAt = $publishedAt
        modifiedAt = $modifiedAt
        observedAt = $ObservedAt.ToString("o")
        originalPrice = $originalPrice
        referencePrice = $referencePrice
        estimatedFinalPayable = $EstimatedFinalPayable
        couponState = Get-MatchingLine $combinedText "券|补贴|红包|Plus|满减"
        quantityCondition = Get-QuantityCondition $combinedText
        variant = Get-Variant $combinedText
        channel = Get-Channel $combinedText
        validUntil = Get-ValidUntil $combinedText
        reproductionSteps = @($steps | Select-Object -First 8)
        evidence = $Evidence
        allLinks = @($links | Select-Object -Unique | Select-Object -First 8)
        sourceKind = [string](Get-PropertyValue $Source @("kind") "rss")
    }
}

function Get-SourceCandidates {
    param($Client, $Source, $Config, [DateTimeOffset]$Now)
    $rules = Get-PropertyValue $Config @("rules") $null
    $monitor = Get-PropertyValue $Config @("monitor") $null
    $network = Get-PropertyValue $Config @("network") $null
    $url = [string](Get-PropertyValue $Source @("url") "")
    if ([string]::IsNullOrWhiteSpace($url)) {
        throw "Source URL is required."
    }
    $feedText = Invoke-HttpText $Client $url
    $feedItems = @(ConvertFrom-RssText $feedText)
    $sourceLookback = [int](Get-PropertyValue $Source @("maxLookbackMinutes") (Get-PropertyValue $monitor @("maxLookbackMinutes") 720))
    $maxDetailFetches = [int](Get-PropertyValue $Source @("maxDetailFetchesPerRun") (Get-PropertyValue $monitor @("maxDetailFetchesPerRun") 8))
    $requestDelayMs = [int](Get-PropertyValue $network @("requestDelayMs") 500)
    $includeKeywords = @((Get-ArrayValue $rules "includeKeywords") + (Get-ArrayValue $Source "includeKeywords"))
    $excludeKeywords = @((Get-ArrayValue $rules "excludeKeywords") + (Get-ArrayValue $Source "excludeKeywords"))
    $alwaysKeepPatterns = Get-ArrayValue $rules "alwaysKeepPatterns"
    $excludePatterns = Get-ArrayValue $rules "excludePatterns"
    $kind = [string](Get-PropertyValue $Source @("kind") "rss")
    $detailFetches = 0
    Write-Verbose "Source $(Get-PropertyValue $Source @('id') '') returned $(@($feedItems).Count) feed items."
    $candidates = foreach ($feedItem in $feedItems) {
        $sourceTimestamp = ConvertTo-DateTimeOffset ([string](Get-PropertyValue $FeedItem @("ModifiedAt") ""))
        if ($null -eq $sourceTimestamp) {
            $sourceTimestamp = ConvertTo-DateTimeOffset ([string](Get-PropertyValue $FeedItem @("PublishedAt") ""))
        }
        if ($null -ne $sourceTimestamp -and ($Now - $sourceTimestamp).TotalMinutes -gt $sourceLookback) {
            Write-Verbose "Skipping stale feed item: $($FeedItem.Title)"
            continue
        }
        $preliminaryText = "$($FeedItem.Title)`n$($FeedItem.Description)"
        if (-not (Test-KeywordFilters $preliminaryText $includeKeywords $excludeKeywords)) {
            Write-Verbose "Skipping keyword-filtered feed item: $($FeedItem.Title)"
            continue
        }
        $detail = [pscustomobject]@{
            ContentText = [string]$FeedItem.Description
            ReproductionSteps = @((ConvertFrom-HtmlText ([string]$FeedItem.Description)).Trim() | Where-Object { $_ })
            Links = @($FeedItem.Link | Where-Object { $_ })
            PublishedAt = [string]$FeedItem.PublishedAt
            ModifiedAt = [string]$FeedItem.ModifiedAt
        }
        if ($kind -eq "xianbao-detail" -and $detailFetches -lt $maxDetailFetches -and $FeedItem.Link) {
            try {
                $detail = Get-XianbaoDetail $Client ([string]$FeedItem.Link)
                $detailFetches++
                if ($requestDelayMs -gt 0) {
                    Start-Sleep -Milliseconds $requestDelayMs
                }
            }
            catch {
                $detail = [pscustomobject]@{
                    ContentText = [string]$FeedItem.Description
                    ReproductionSteps = @((ConvertFrom-HtmlText ([string]$FeedItem.Description)).Trim() | Where-Object { $_ })
                    Links = @($FeedItem.Link | Where-Object { $_ })
                    PublishedAt = [string]$FeedItem.PublishedAt
                    ModifiedAt = [string]$FeedItem.ModifiedAt
                }
            }
        }
        $combinedText = "$($FeedItem.Title)`n$($FeedItem.Description)`n$($Detail.ContentText)"
        if (-not (Test-KeywordFilters $combinedText $includeKeywords $excludeKeywords)) {
            Write-Verbose "Skipping keyword-filtered detail: $($FeedItem.Title)"
            continue
        }
        $dealType = Get-DealType $combinedText $rules
        $estimatedFinalPayable = Get-FinalPayable $combinedText ([string]$FeedItem.Title)
        $targetPrice = Get-PropertyValue $Source @("targetPrice") $null
        if ($null -ne $targetPrice) {
            $currentPrice = Get-CurrentPrice $combinedText
            if ($null -ne $currentPrice -and [decimal]$currentPrice -le [decimal]$targetPrice) {
                $dealType = "target-price-met"
                $estimatedFinalPayable = $currentPrice
            }
        }
        if ([string]::IsNullOrWhiteSpace([string]$dealType)) {
            Write-Verbose "Skipping item without a configured deal cue: $($FeedItem.Title)"
            continue
        }
        $alwaysKeep = Test-AnyPattern $combinedText $alwaysKeepPatterns
        if ((Test-AnyPattern $combinedText $excludePatterns) -and -not $alwaysKeep) {
            Write-Verbose "Skipping ordinary promotion: $($FeedItem.Title)"
            continue
        }
        if ($null -eq $estimatedFinalPayable) {
            Write-Verbose "Skipping item without an explicit payable price: $($FeedItem.Title)"
            continue
        }
        $cueMatch = Get-FirstPatternMatch $combinedText @(
            (Get-ArrayValue (Get-PropertyValue $rules @("dealTypes") $null) ([string]$dealType))
        )
        $evidence = Get-TextExcerpt $combinedText $cueMatch 80
        if (-not $evidence) {
            $evidence = "Source feed explicitly reported this deal type."
        }
        Write-Verbose "Qualified $dealType item: $($FeedItem.Title)"
        $item = New-DealItem $Source $FeedItem $detail ([string]$dealType) ([decimal]$estimatedFinalPayable) $evidence $Now
        if ($dealType -eq "target-price-met" -and $null -ne $targetPrice) {
            $item.referencePrice = [decimal]$targetPrice
        }
        $item
    }
    return @($candidates)
}

function Write-StateItemsFile {
    param([object[]]$Items, [string]$Prefix)
    $path = Join-Path $env:TEMP ("$Prefix-" + [guid]::NewGuid() + ".json")
    $payload = [ordered]@{ items = @($Items) }
    $payload | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $path -Encoding UTF8
    return $path
}

function Invoke-DealStateAction {
    param(
        [string]$Action,
        [string]$StatePath,
        [object[]]$Items,
        $Item,
        [string]$DealStateScript
    )
    $itemsFile = $null
    try {
        if ($Action -eq "MarkSent") {
            $itemJson = $Item | ConvertTo-Json -Compress -Depth 20
            $raw = & $DealStateScript -Action MarkSent -StatePath $StatePath -ItemJson $itemJson | Out-String
        }
        else {
            $itemsFile = Write-StateItemsFile @($Items) "shopping-deal-rss-items"
            $raw = & $DealStateScript -Action $Action -StatePath $StatePath -ItemsFile $itemsFile | Out-String
        }
        return $raw | ConvertFrom-Json
    }
    finally {
        if ($itemsFile -and (Test-Path -LiteralPath $itemsFile)) {
            Remove-Item -LiteralPath $itemsFile -Force
        }
    }
}

function Get-DealTypeLabel {
    param([string]$DealType)
    switch ($DealType) {
        "price-error" { return "速度bug❗" }
        "zero-price" { return "零元bug❗" }
        "flash-inventory" { return "库存bug❗" }
        "coupon-stack" { return "叠券好价❗" }
        "hidden-stack" { return "叠券bug❗" }
        "hidden-eligibility" { return "隐藏入口❗" }
        "channel-specific" { return "渠道专享❗" }
        "checkout-mismatch" { return "结算bug❗" }
        "variant-mismatch" { return "拍发bug❗" }
        "target-price-met" { return "到目标价❗" }
        "abnormal-drop" { return "异常降价❗" }
        default { return "好价❗" }
    }
}

function Test-CompactActionStep {
    param([string]$Step)
    $value = ($Step -replace "\s+", " ").Trim()
    if ($value -match "^[①②③④⑤⑥⑦⑧⑨⑩]\s*|^\d+\s*[.、)]\s*") {
        return $true
    }
    if ($value -match "^(?:https?://|￥|<img>|【?\s*(?:图片|链接))") {
        return $false
    }
    if ($value -match "^[（(].*(?:元|亓).*[）)]$") {
        return $false
    }
    return $value -match "领取|领券|领.{0,8}券|跳到|跳至|进入|进#|加车|加入购物车|下单|拍下|拍卞|改.{0,6}件|退出|复制|打开|点击|结算|付款|凑单|再次"
}

function New-BarkTitle {
    param($Item)
    $sourceTitle = ([string]$Item.title).Trim()
    $title = "$(Get-DealTypeLabel ([string]$Item.dealType))$sourceTitle"
    if ($sourceTitle -notmatch "[¥￥]?\d+(?:\.\d+)?\s*(?:元|亓|/斤|/件)" -and $null -ne $Item.estimatedFinalPayable) {
        $title += "｜到手 ¥$(([decimal]$Item.estimatedFinalPayable).ToString('0.##'))"
    }
    if ($title.Length -gt 128) {
        $title = $title.Substring(0, 128)
    }
    return $title
}

function Format-CompactStep {
    param([string]$Step, [int]$Index)
    $glyphs = @("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩")
    $value = ($Step -replace "\s+", " ").Trim()
    if (-not (Test-CompactActionStep $value)) {
        return $value
    }
    if ($value -match "^[①②③④⑤⑥⑦⑧⑨⑩]\s*") {
        return $value
    }
    if ($value -match "^\d+\s*[.、)]\s*(?<content>.+)$") {
        $value = $Matches["content"].Trim()
    }
    $glyph = if ($Index -lt $glyphs.Count) { $glyphs[$Index] } else { "$($Index + 1)." }
    return "$glyph$value"
}

function New-BarkBody {
    param($Item)
    $lines = [Collections.Generic.List[string]]::new()
    $lines.Add("——————")
    $steps = @($Item.reproductionSteps | Where-Object {
        $value = ([string]$_).Trim()
        $value -and
        $value -notmatch "^<img>.*</img>$" -and
        $value -notmatch "^[a-fA-F0-9]{16,64}$"
    })
    if ($steps.Count -eq 0) {
        $steps = @([string]$Item.title)
    }
    $index = 0
    foreach ($step in $steps) {
        $lines.Add((Format-CompactStep ([string]$step) $index))
        if (Test-CompactActionStep ([string]$step)) {
            $index++
        }
    }
    $body = $lines -join "`n"
    if ($Item.productUrl -and $body.IndexOf([string]$Item.productUrl, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        $lines.Add("商品 $($Item.productUrl)")
    }
    if ($Item.sourceUrl -and $body.IndexOf([string]$Item.sourceUrl, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        $sourceTime = if ($Item.modifiedAt) { $Item.modifiedAt } else { $Item.publishedAt }
        $sourceNature = if ($Item.sourceKind -eq "xianbao-detail") { "线报酷未核验" } else { "RSS未核验" }
        $lines.Add("来源 $sourceTime｜观测 $($Item.observedAt)｜$sourceNature $($Item.sourceUrl)")
    }
    $body = $lines -join "`n"
    if ([Text.Encoding]::UTF8.GetByteCount($body) -gt 5800) {
        $body = $body.Substring(0, [Math]::Min($body.Length, 1800)) + "`n…"
    }
    return $body
}

function Write-RunResult {
    param($Result, [string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return
    }
    $directory = Split-Path -Parent $Path
    if ($directory -and -not (Test-Path -LiteralPath $directory)) {
        $null = New-Item -ItemType Directory -Path $directory -Force
    }
    $Result | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $Path -Encoding UTF8
}

$configPathValue = $ConfigPath
if ([string]::IsNullOrWhiteSpace($configPathValue)) {
    $configPathValue = Join-Path (Split-Path -Parent $PSScriptRoot) "monitor-config.json"
}
$resolvedConfigPath = (Resolve-Path -LiteralPath $configPathValue).Path
$config = Get-Content -LiteralPath $resolvedConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$network = Get-PropertyValue $config @("network") $null
$monitor = Get-PropertyValue $config @("monitor") $null
$delivery = Get-PropertyValue $config @("delivery") $null
$rules = Get-PropertyValue $config @("rules") $null
$proxyUrl = Expand-ConfigValue ([string](Get-PropertyValue $network @("proxyUrl") ""))
$proxyDisabled = [bool](Get-PropertyValue $network @("proxyDisabled") $false)
if (-not $proxyUrl -and -not $proxyDisabled) {
    $proxyUrl = Expand-ConfigValue ([string]$env:HTTPS_PROXY)
}
if (-not $proxyUrl -and -not $proxyDisabled) {
    $proxyUrl = Expand-ConfigValue ([string]$env:HTTP_PROXY)
}
$timeoutSec = [int](Get-PropertyValue $network @("timeoutSec") 20)
$userAgent = [string](Get-PropertyValue $network @("userAgent") "ShoppingDealRSS/1.0")
$barkSecretPath = Expand-ConfigValue ([string](Get-PropertyValue $delivery @("barkSecretPath") ""))
$statePath = Expand-ConfigValue ([string](Get-PropertyValue $delivery @("statePath") ""))
$resultPath = Expand-ConfigValue ([string](Get-PropertyValue $delivery @("resultPath") ""))
$dealStateScript = Join-Path $PSScriptRoot "deal-state.ps1"
$sendBarkScript = Join-Path $PSScriptRoot "send_bark.ps1"
$maxAlertsPerRun = [int](Get-PropertyValue $monitor @("maxAlertsPerRun") 5)
$bundle = New-HttpClientBundle $proxyUrl $timeoutSec $userAgent

try {
    $now = [DateTimeOffset]::UtcNow
    $candidates = [Collections.Generic.List[object]]::new()
    $sourceErrors = [Collections.Generic.List[object]]::new()
    foreach ($source in @((Get-PropertyValue $config @("sources") @()))) {
        if (-not [bool](Get-PropertyValue $Source @("enabled") $true)) {
            continue
        }
        try {
            foreach ($candidate in @(Get-SourceCandidates $bundle.Client $source $config $now)) {
                $candidates.Add($candidate)
            }
        }
        catch {
            $sourceErrors.Add([ordered]@{
                sourceId = [string](Get-PropertyValue $Source @("id") "")
                url = [string](Get-PropertyValue $Source @("url") "")
                error = $_.Exception.Message
            })
        }
    }
    $candidateItems = @($candidates)
    $stateExists = -not [string]::IsNullOrWhiteSpace($statePath) -and (Test-Path -LiteralPath $statePath)
    if (-not $stateExists) {
        $result = [ordered]@{
            status = if ($DryRun) { "would_initialize_baseline" } else { "initialized_baseline" }
            runAt = $now.ToString("o")
            configPath = $resolvedConfigPath
            sourceCount = @((Get-PropertyValue $config @("sources") @())).Count
            candidateCount = $candidateItems.Count
            baselineCount = $candidateItems.Count
            sentCount = 0
            sourceErrors = @($sourceErrors)
            candidates = $candidateItems
        }
        if (-not $DryRun) {
            $initializeResult = Invoke-DealStateAction "Initialize" $statePath $candidateItems $null $dealStateScript
            $result.baselineCount = $initializeResult.baselineCount
        }
        Write-RunResult $result $resultPath
        $result | ConvertTo-Json -Depth 20
        return
    }
    $evaluation = Invoke-DealStateAction "Evaluate" $statePath $candidateItems $null $dealStateScript
    $candidateByStateKey = @{}
    foreach ($candidate in $candidateItems) {
        $stateKey = Get-DealStateKeyFromItem $candidate
        $candidateByStateKey[$stateKey] = $candidate
    }
    $eligible = @($evaluation.eligible)
    $wouldSend = @()
    $sent = [Collections.Generic.List[object]]::new()
    $deliveryErrors = [Collections.Generic.List[object]]::new()
    $alertCount = 0
    foreach ($entry in $eligible) {
        if ($alertCount -ge $maxAlertsPerRun) {
            break
        }
        $stateKey = [string](Get-PropertyValue $entry @("stateKey") "")
        $candidate = if ($candidateByStateKey.ContainsKey($stateKey)) {
            $candidateByStateKey[$stateKey]
        }
        else {
            Get-PropertyValue $entry @("item") $null
        }
        if ($DryRun) {
            $wouldSend += [ordered]@{
                stateKey = $stateKey
                title = New-BarkTitle $candidate
                body = New-BarkBody $candidate
            }
            $alertCount++
            continue
        }
        try {
            & $sendBarkScript `
                -SecretPath $barkSecretPath `
                -Title (New-BarkTitle $candidate) `
                -Body (New-BarkBody $candidate) | Out-Null
            $markResult = Invoke-DealStateAction "MarkSent" $statePath @() $candidate $dealStateScript
            $sent.Add([ordered]@{
                stateKey = $stateKey
                status = $markResult.status
                title = [string]$candidate.title
            })
            $alertCount++
        }
        catch {
            $deliveryErrors.Add([ordered]@{
                stateKey = $stateKey
                title = [string]$candidate.title
                error = $_.Exception.Message
            })
        }
    }
    $result = [ordered]@{
        status = if ($DryRun) { "dry_run" } else { "completed" }
        runAt = $now.ToString("o")
        configPath = $resolvedConfigPath
        statePath = $statePath
        candidateCount = $candidateItems.Count
        eligibleCount = $eligible.Count
        rejectedCount = @($evaluation.rejected).Count
        sentCount = $sent.Count
        sourceErrors = @($sourceErrors)
        deliveryErrors = @($deliveryErrors)
        sent = @($sent)
        wouldSend = @($wouldSend)
    }
    Write-RunResult $result $resultPath
    $result | ConvertTo-Json -Depth 20
}
catch {
    $result = [ordered]@{
        status = "error"
        runAt = [DateTimeOffset]::UtcNow.ToString("o")
        configPath = $resolvedConfigPath
        error = $_.Exception.Message
    }
    Write-RunResult $result $resultPath
    $result | ConvertTo-Json -Depth 8
    exit 1
}
finally {
    $bundle.Client.Dispose()
    $bundle.Handler.Dispose()
}
