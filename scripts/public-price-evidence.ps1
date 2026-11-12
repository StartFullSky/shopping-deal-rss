[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Title,

    [string]$ProductUrl = "",

    [string]$SourceUrl = "",

    [string]$RsshubBaseUrl = "",

    [ValidateRange(1, 8)]
    [int]$MaxResultsPerQuery = 4,

    [ValidateRange(5, 30)]
    [int]$TimeoutSec = 15,

    [ValidateRange(5, 120)]
    [int]$BudgetSec = 45
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$researchDeadline = [DateTimeOffset]::UtcNow.AddSeconds($BudgetSec)

function Get-RemainingResearchSeconds {
    return [Math]::Max(0, [Math]::Ceiling(($researchDeadline - [DateTimeOffset]::UtcNow).TotalSeconds))
}

function Get-RequestTimeoutSec {
    $remainingSeconds = Get-RemainingResearchSeconds
    if ($remainingSeconds -le 0) {
        throw "Public research budget of $BudgetSec seconds was exhausted."
    }
    return [Math]::Min($TimeoutSec, [Math]::Max(1, [int]$remainingSeconds))
}

function Assert-PublicHttpUrl {
    param([string]$Url)
    $uri = [Uri]::new($Url, [UriKind]::Absolute)
    if (@("http", "https") -notcontains $uri.Scheme.ToLowerInvariant()) {
        throw "Only public HTTP and HTTPS URLs can be researched."
    }
    if ($uri.IsLoopback -or ($uri.HostNameType -eq [UriHostNameType]::Dns -and $uri.Host -in @("localhost", "localhost.localdomain"))) {
        throw "Local URLs cannot be used as public price evidence."
    }
    return $uri
}

function ConvertTo-PublicEvidenceText {
    param([string]$Value)
    $text = [string]$Value
    $text = [regex]::Replace($text, "(?is)<script\b.*?</script>", " ")
    $text = [regex]::Replace($text, "(?is)<style\b.*?</style>", " ")
    $text = [regex]::Replace($text, "(?i)<br\s*/?>", " ")
    $text = [regex]::Replace($text, "<[^>]+>", " ")
    $text = [Net.WebUtility]::HtmlDecode($text)
    return [regex]::Replace($text, "\s+", " ").Trim()
}

function Get-PriceMentions {
    param([string]$Value)
    return @([regex]::Matches([string]$Value, "(?i)(?:[¥￥]\s*)?\d+(?:\.\d{1,2})?\s*(?:元|亓)") |
        ForEach-Object { $_.Value } |
        Select-Object -Unique)
}

function Get-SearchFeedItems {
    param(
        [string]$Uri,
        [string]$Provider,
        [string]$Query
    )
    $requestTimeoutSec = Get-RequestTimeoutSec
    $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec $requestTimeoutSec -Headers @{
        "User-Agent" = "ShoppingDealRSS/1.1 (+https://github.com/StartFullSky/shopping-deal-rss)"
    }
    [xml]$document = $response.Content
    $items = @($document.rss.channel.item)
    $output = foreach ($item in $items | Select-Object -First $MaxResultsPerQuery) {
        $title = ConvertTo-PublicEvidenceText ([string]$item.title)
        $snippet = ConvertTo-PublicEvidenceText ([string]$item.description)
        $link = [string]$item.link
        if ([string]::IsNullOrWhiteSpace($link)) {
            continue
        }
        [ordered]@{
            provider = $Provider
            query = $Query
            title = $title
            url = $link
            snippet = $snippet
            publishedAt = [string]$item.pubDate
            priceMentions = @(Get-PriceMentions "$title $snippet")
        }
    }
    return @($output)
}

function Get-PublicPageEvidence {
    param([string]$Url)
    try {
        $publicUri = Assert-PublicHttpUrl $Url
        $requestTimeoutSec = Get-RequestTimeoutSec
        $response = Invoke-WebRequest -UseBasicParsing -Uri $publicUri.AbsoluteUri -TimeoutSec $requestTimeoutSec -MaximumRedirection 5 -Headers @{
            "User-Agent" = "ShoppingDealRSS/1.1 (+https://github.com/StartFullSky/shopping-deal-rss)"
        }
        $html = [string]$response.Content
        $titleMatch = [regex]::Match($html, "(?is)<title\b[^>]*>(.*?)</title>")
        $title = if ($titleMatch.Success) { ConvertTo-PublicEvidenceText $titleMatch.Groups[1].Value } else { "" }
        $text = ConvertTo-PublicEvidenceText $html
        if ($text.Length -gt 1200) {
            $text = $text.Substring(0, 1200)
        }
        $finalUrl = $publicUri.AbsoluteUri
        $responseUriProperty = $response.BaseResponse.PSObject.Properties["ResponseUri"]
        $requestUriProperty = $response.BaseResponse.PSObject.Properties["RequestMessage"]
        if ($null -ne $responseUriProperty -and $null -ne $responseUriProperty.Value) {
            $finalUrl = [string]$responseUriProperty.Value.AbsoluteUri
        }
        elseif ($null -ne $requestUriProperty -and $null -ne $requestUriProperty.Value -and $null -ne $requestUriProperty.Value.RequestUri) {
            $finalUrl = [string]$requestUriProperty.Value.RequestUri.AbsoluteUri
        }
        return [ordered]@{
            requestedUrl = $Url
            finalUrl = $finalUrl
            statusCode = [int]$response.StatusCode
            title = $title
            textExcerpt = $text
            priceMentions = @(Get-PriceMentions "$title $text")
        }
    }
    catch {
        return [ordered]@{
            requestedUrl = $Url
            error = $_.Exception.Message
        }
    }
}

if ([string]::IsNullOrWhiteSpace($RsshubBaseUrl)) {
    $RsshubBaseUrl = [Environment]::GetEnvironmentVariable("RSSHUB_BASE_URL")
}

$searchTitle = [regex]::Replace($Title, "\s*\d+(?:\.\d{1,2})?\s*(?:亓|元).*$", "").Trim()
if ([string]::IsNullOrWhiteSpace($searchTitle)) {
    $searchTitle = $Title.Trim()
}

$queries = [Collections.Generic.List[object]]::new()
$queries.Add([ordered]@{ provider = "bing-rss"; query = "$searchTitle 历史价格" })
$queries.Add([ordered]@{ provider = "bing-rss"; query = "$searchTitle 价格" })
$queries.Add([ordered]@{ provider = "bing-rss"; query = "site:smzdm.com $searchTitle" })
if (-not [string]::IsNullOrWhiteSpace($RsshubBaseUrl)) {
    $encodedKeyword = [uri]::EscapeDataString($searchTitle)
    $queries.Add([ordered]@{
        provider = "smzdm-rsshub"
        query = $searchTitle
        uri = $RsshubBaseUrl.TrimEnd("/") + "/smzdm/keyword/" + $encodedKeyword
    })
}

$results = [Collections.Generic.List[object]]::new()
$seenLinks = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$searchErrors = [Collections.Generic.List[object]]::new()
foreach ($searchQuery in $queries) {
    if ((Get-RemainingResearchSeconds) -le 0) {
        $searchErrors.Add([ordered]@{
            provider = [string]$searchQuery.provider
            query = [string]$searchQuery.query
            error = "Public research budget of $BudgetSec seconds was exhausted."
        })
        break
    }
    try {
        $uri = if ($searchQuery.Contains("uri")) { [string]$searchQuery.uri } else {
            "https://www.bing.com/search?format=rss&q=" + [uri]::EscapeDataString([string]$searchQuery.query)
        }
        foreach ($result in @(Get-SearchFeedItems -Uri $uri -Provider ([string]$searchQuery.provider) -Query ([string]$searchQuery.query))) {
            if ($seenLinks.Add([string]$result.url)) {
                $results.Add($result)
            }
        }
    }
    catch {
        $searchErrors.Add([ordered]@{
            provider = [string]$searchQuery.provider
            query = [string]$searchQuery.query
            error = $_.Exception.Message
        })
    }
    Start-Sleep -Milliseconds 250
}

$sourcePages = [Collections.Generic.List[object]]::new()
foreach ($url in @($ProductUrl, $SourceUrl) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique) {
    if ((Get-RemainingResearchSeconds) -le 0) {
        $sourcePages.Add([ordered]@{
            requestedUrl = [string]$url
            error = "Public research budget of $BudgetSec seconds was exhausted."
        })
        continue
    }
    $sourcePages.Add((Get-PublicPageEvidence ([string]$url)))
}

return [ordered]@{
    observedAt = [DateTimeOffset]::UtcNow.ToString("o")
    budgetSec = $BudgetSec
    remainingBudgetSec = Get-RemainingResearchSeconds
    productTitle = $Title
    searchTitle = $searchTitle
    results = @($results)
    sourcePages = @($sourcePages)
    searchErrors = @($searchErrors)
}
