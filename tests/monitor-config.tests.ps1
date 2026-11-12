$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$config = Get-Content -LiteralPath (Join-Path $repositoryRoot "monitor-config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$bookPattern = @($config.rules.excludePatterns | Where-Object { $_ -match "kindle" }) | Select-Object -First 1
$couponStackPattern = @($config.rules.dealTypes.'coupon-stack') | Select-Object -First 1
$checkoutMismatchPattern = @($config.rules.dealTypes.'checkout-mismatch') | Select-Object -First 1

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

Assert-True ([bool]$bookPattern) "A book-category exclusion pattern should be configured."
$bookTitles = @(
    "kindle book deal",
    ([char[]]@(0x7AE5, 0x4E66, 0x7ED8, 0x672C)) -join "",
    ([char[]]@(0x5C0F, 0x5B66, 0x6559, 0x8F85, 0x7EC3, 0x4E60, 0x518C)) -join ""
)
foreach ($title in $bookTitles) {
    Assert-True ($title -match $bookPattern) "Book-related title should be excluded: $title"
}
$nonBookTitles = @(
    ([char[]]@(0x513F, 0x7AE5, 0x4E66, 0x5305)) -join "",
    ([char[]]@(0x5E8A, 0x5934, 0x4E66, 0x67B6)) -join "",
    ([char[]]@(0x53EF, 0x6298, 0x53E0, 0x4E66, 0x684C)) -join ""
)
foreach ($title in $nonBookTitles) {
    Assert-True (-not ($title -match $bookPattern)) "Non-book product should not be excluded: $title"
}
if ($config.mode -eq "product_only") {
    Assert-True (-not [bool]$config.quality.requireHistoricalPrice) "Product-only mode must not require historical prices."
    Assert-True ([double]$config.quality.minHistoricalDiscountPct -eq 0) "Product-only mode must not gate on discounts."
    Assert-True (-not [bool]$config.delivery.enabled) "Product-only evaluation must keep delivery disabled."
} else {
    Assert-True ([double]$config.quality.minHistoricalDiscountPct -ge 30) "Historical discount threshold should be at least 30 percent."
    Assert-True ([bool]$config.quality.requireHistoricalPrice) "Historical price evidence should be required."
}
$couponStackText = @'
伯希和 山野2.0Pro 三合一冲锋衣
348亓❗高品质❗这次不用金帀❗
① 領顶部【消費劵】(没劵换呺)
②跳APP❗看图"进値播"拍卞❗
忦格不对的❗加 🛒底部湊消費劵
'@
Assert-True ($couponStackText -match $couponStackPattern) "Coupon, app, live-stream, checkout, and cart wording should classify as a coupon stack."
Assert-True ($couponStackText -match $checkoutMismatchPattern) "Variant price wording should classify as a checkout mismatch."
$ordinaryProductText = "斯凯奇云野户外徒步鞋运动鞋 60.76元"
Assert-True (-not ($ordinaryProductText -match $couponStackPattern)) "Ordinary product pricing should not classify as a coupon stack."

[pscustomobject]@{
    status = "passed"
    assertions = if ($config.mode -eq "product_only") { 13 } else { 12 }
} | ConvertTo-Json -Compress
