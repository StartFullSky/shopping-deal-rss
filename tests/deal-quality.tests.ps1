$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

. (Join-Path (Split-Path -Parent $PSScriptRoot) "scripts\deal-quality.ps1")

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw "Assertion failed: $Message"
    }
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Message)
    if ([string]$Expected -ne [string]$Actual) {
        throw "Assertion failed: $Message. Expected '$Expected', got '$Actual'."
    }
}

$sampleTitle = "立白 洗衣液 1.3亓/斤"
$sampleText = @"
领弹出的券后 等3秒才能退出
跳到APP，领2亓叠加券
跳APP领6亓券+在此处加车
从下面链接再次加车一件
购物车商品数量改1件拍下
共16斤21亓
"@
$sampleSteps = @(
    "领弹出的券后 等3秒才能退出",
    "跳到APP，领2亓叠加券",
    "跳APP领6亓券+在此处加车",
    "从下面链接再次加车一件",
    "购物车商品数量改1件拍下"
)
$samplePrice = Get-DealPriceInfo $sampleText $sampleTitle
Assert-Equal 21 $samplePrice.estimatedFinalPayable "Total price should parse from 共16斤21亓."
Assert-Equal 1.3 $samplePrice.unitPrice "Unit price should parse from 1.3亓/斤."
Assert-Equal "斤" $samplePrice.unit "Unit should parse from the unit-price expression."
Assert-Equal "total" $samplePrice.priceBasis "Total price should take precedence over unit price."

$sampleQuality = Get-DealQualityAssessment `
    -Text $sampleText `
    -Title $sampleTitle `
    -DealType "coupon-stack" `
    -PriceInfo $samplePrice `
    -ReproductionSteps $sampleSteps `
    -ProductUrl "https://item.example/product/123" `
    -SourceAgeMinutes 3
Assert-True $sampleQuality.qualified "A reproducible coupon stack should qualify."
Assert-True ($sampleQuality.score -ge 88) "A strong coupon stack should have a high score."
Assert-Equal "timeSensitive" $sampleQuality.urgency "A fresh high-score stack should use time-sensitive delivery."

$ordinaryCoupon = Get-DealQualityAssessment `
    -Text "领券并叠加优惠，39元" `
    -Title "普通商品 39元" `
    -DealType "coupon-stack" `
    -PriceInfo (Get-DealPriceInfo "39元" "普通商品 39元") `
    -ReproductionSteps @() `
    -SourceAgeMinutes 5
Assert-True (-not $ordinaryCoupon.qualified) "A coupon claim without reproducible steps should not qualify."

$falseZero = Get-DealQualityAssessment `
    -Text "10元" `
    -Title "普通商品 10元" `
    -DealType "zero-price" `
    -PriceInfo (Get-DealPriceInfo "10元" "普通商品 10元") `
    -ReproductionSteps @() `
    -SourceAgeMinutes 5
Assert-True (-not $falseZero.qualified) "A substring like 0元 inside 10元 must not qualify as zero-price."

$promotionQuantity = Get-DealQualityAssessment `
    -Text "拍5发150粒，45元" `
    -Title "商品 拍5发150粒 45元" `
    -DealType "variant-mismatch" `
    -PriceInfo (Get-DealPriceInfo "拍5发150粒，45元" "商品 拍5发150粒 45元") `
    -ReproductionSteps @("拍5发150粒") `
    -SourceAgeMinutes 5
Assert-True (-not $promotionQuantity.qualified) "A normal quantity promotion is not a variant mismatch."

[pscustomobject]@{
    status = "passed"
    assertions = 11
} | ConvertTo-Json -Compress
