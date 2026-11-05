function Get-DealPriceInfo {
    param(
        [string]$Text,
        [string]$Title = ""
    )

    $combinedText = "$Title`n$Text"
    $unitMatch = [regex]::Match(
        $combinedText,
        "(?i)(?<![\d.])(?<price>\d+(?:\.\d{1,2})?)\s*(?:元|亓)\s*/\s*(?<unit>斤|件|个|盒|瓶|双|包|袋|升|L|ml|毫升|克|g|千克|kg)"
    )
    $totalPatterns = @(
        "(?i)(?:到手|实付|最终(?:实付|到手|价)?|券后|凑后|付款|下单)\s*(?:价|价格)?\s*(?:约|仅|低至)?\s*[:：=]?\s*[¥￥]?\s*(?<price>\d+(?:\.\d{1,2})?)\s*(?:元|亓)",
        "(?i)(?:共|合计|总价)\s*(?:(?<quantity>\d+(?:\.\d+)?)\s*(?:斤|件|个|盒|瓶|双|包|袋|升|L|ml|毫升|克|g|千克|kg)\s*)?(?<price>\d+(?:\.\d{1,2})?)\s*(?:元|亓)"
    )
    $totalMatch = $null
    foreach ($pattern in $totalPatterns) {
        $totalMatch = [regex]::Match($combinedText, $pattern)
        if ($totalMatch.Success) {
            break
        }
    }
    if (-not $totalMatch.Success) {
        $totalMatch = [regex]::Match(
            $combinedText,
            "(?i)(?<![\d.])(?<price>\d+(?:\.\d{1,2})?)\s*(?:元|亓)(?!\s*/)"
        )
    }

    $value = $null
    $basis = ""
    $unit = ""
    $unitPrice = $null
    if ($unitMatch.Success) {
        $unitPrice = [decimal]$unitMatch.Groups["price"].Value
        $unit = [string]$unitMatch.Groups["unit"].Value.ToLowerInvariant()
    }
    if ($totalMatch.Success) {
        $value = [decimal]$totalMatch.Groups["price"].Value
        $basis = "total"
    }
    elseif ($null -ne $unitPrice) {
        $value = $unitPrice
        $basis = "unit"
    }

    return [pscustomobject]@{
        estimatedFinalPayable = $value
        unitPrice = $unitPrice
        unit = $unit
        priceBasis = $basis
    }
}

function Test-DealActionStep {
    param([string]$Step)
    $value = ($Step -replace "\s+", " ").Trim()
    if (-not $value -or $value -match "^<img>.*</img>$" -or $value -match "^[a-fA-F0-9]{16,64}$") {
        return $false
    }
    if ($value -match "^(?:https?://|￥|<img>)") {
        return $false
    }
    return $value -match "领取|领券|领.{0,8}(?:券|红包)|跳到|跳至|进入|进#|加车|加入购物车|下单|拍下|拍卞|改.{0,6}件|退出|复制|打开|点击|结算|付款|凑单|再次|扫码|搜索"
}

function Get-DealQualityAssessment {
    param(
        [string]$Text,
        [string]$Title,
        [string]$DealType,
        $PriceInfo,
        [object[]]$ReproductionSteps = @(),
        [string]$ProductUrl = "",
        [double]$SourceAgeMinutes = 0,
        [double]$MinScore = 72,
        $HistoricalPrice = $null,
        [double]$MinHistoricalDiscountPct = 30,
        [bool]$RequireHistoricalPrice = $true
    )

    $combinedText = "$Title`n$Text"
    $actionStepCount = @($ReproductionSteps | Where-Object { Test-DealActionStep ([string]$_) }).Count
    $couponClaim = $combinedText -match "领.{0,12}(?:券|红包)|(?:使用|兑换).{0,12}(?:券|红包)|券后|支付券|店铺券|平台券"
    $stackAction = $combinedText -match "叠加|再领|再次.{0,8}(?:领|加车)|加车|加入购物车|凑单|满\d+减\d+|支付券|平台券|店铺券"
    $checkoutEvidence = $combinedText -match "到手|实付|券后|结算|付款|下单|最终价"
    $priceAnomaly = $combinedText -match "错价|价格错误|标价错误|价格漏洞|漏洞价|价格BUG|价格bug|BUG价|白嫖"
    $zeroPrice = $combinedText -match "(?<![¥￥\d.])0(?:\.0+)?\s*(?:元|亓)(?:购|拍|下单|到手)|零元购|一元购|1元购|白嫖|(?:实付|到手|最终|下单|付款)\s*0(?:\.0+)?\s*(?:元|亓)"
    $inventoryAnomaly = $combinedText -match "库存漏洞|库存错误|超卖|无货下单|秒罄"
    $checkoutMismatch = $combinedText -match "结算.{0,12}(?:低于|不一致|价格)|购物车.{0,12}(?:低于|不一致)|实付.{0,12}低于|下单.{0,12}低于"
    $variantMismatch = $combinedText -match "发错|收到.{0,12}(?:规格|型号|颜色).{0,8}(?:错|不一致)|SKU.{0,12}(?:错|bug|不一致)|规格.{0,12}(?:错|bug|不一致)|选项.{0,12}(?:错|bug|不一致)"
    $fulfillmentEvidence = $combinedText -match "拍\s*\d+\s*(?:件|个|盒|瓶|双)?.{0,16}(?:发|收到|实际)|收到|发货|实发"
    $eligibilityEvidence = $combinedText -match "隐藏入口|受邀|仅限.{0,12}(?:地区|账号|会员|学生|新客|老客)|地区专享|渠道专享|小程序|线下|门店"
    $sequenceEvidence = $combinedText -match "[①②③④⑤⑥⑦⑧⑨⑩]|步骤|第一步|再次|然后|接着"
    $ordinaryPromotion = $combinedText -match "今日.{0,6}(?:特价|好价)|限时.{0,6}(?:特价|秒杀)|聚划算|团购|拼团"
    $complicatedReward = $combinedText -match "返[¥￥]?\d+|返(?:E卡|积分|豆|金)|晒单|评论.{0,8}(?:奖励|返)|抽奖"
    $currentPayable = if ($null -ne $PriceInfo) { $PriceInfo.estimatedFinalPayable } else { $null }
    $historicalPriceValue = if ($null -ne $HistoricalPrice) { [double]$HistoricalPrice } else { $null }
    $historicalDiscountPct = $null
    $priceAdvantage = $false
    if ($null -ne $currentPayable -and $null -ne $historicalPriceValue -and $historicalPriceValue -gt 0 -and $currentPayable -ge 0) {
        $historicalDiscountPct = [Math]::Round((($historicalPriceValue - [double]$currentPayable) / $historicalPriceValue) * 100, 1)
        $priceAdvantage = $historicalDiscountPct -ge $MinHistoricalDiscountPct
    }

    $baseScores = @{
        "price-error" = 82
        "zero-price" = 86
        "flash-inventory" = 78
        "hidden-stack" = 82
        "checkout-mismatch" = 80
        "variant-mismatch" = 72
        "hidden-eligibility" = 70
        "coupon-stack" = 66
        "channel-specific" = 60
        "target-price-met" = 58
        "abnormal-drop" = 56
    }
    $score = if ($baseScores.ContainsKey($DealType)) { [double]$baseScores[$DealType] } else { 45 }
    $reasons = [Collections.Generic.List[string]]::new()
    $riskFlags = [Collections.Generic.List[string]]::new()

    if ($null -ne $PriceInfo -and $null -ne $PriceInfo.estimatedFinalPayable) {
        $score += 5
        $reasons.Add("price")
        if ($PriceInfo.priceBasis -eq "total") {
            $score += 5
            $reasons.Add("total-price")
        }
        else {
            $score -= 5
            $riskFlags.Add("unit-price-only")
        }
    }
    if ($ProductUrl) {
        $score += 5
        $reasons.Add("product-link")
    }
    if ($actionStepCount -ge 4) {
        $score += 12
        $reasons.Add("steps-4plus")
    }
    elseif ($actionStepCount -ge 2) {
        $score += 8
        $reasons.Add("steps-2plus")
    }
    elseif ($actionStepCount -eq 1) {
        $score += 3
        $reasons.Add("steps-1")
    }
    if ($checkoutEvidence) {
        $score += 5
        $reasons.Add("checkout-evidence")
    }
    if ($couponClaim -and $stackAction) {
        $score += 8
        $reasons.Add("coupon-stack-evidence")
    }
    if ($sequenceEvidence) {
        $score += 4
        $reasons.Add("sequence")
    }
    if ($SourceAgeMinutes -le 10) {
        $score += 10
        $reasons.Add("fresh-10m")
    }
    elseif ($SourceAgeMinutes -le 30) {
        $score += 6
        $reasons.Add("fresh-30m")
    }
    elseif ($SourceAgeMinutes -le 120) {
        $score += 2
        $reasons.Add("fresh-2h")
    }
    else {
        $score -= 10
        $riskFlags.Add("older-lead")
    }
    if ($ordinaryPromotion -and -not ($priceAnomaly -or $zeroPrice -or $inventoryAnomaly -or $checkoutMismatch -or $variantMismatch)) {
        $score -= 18
        $riskFlags.Add("ordinary-promotion")
    }
    if ($complicatedReward) {
        $score -= 6
        $riskFlags.Add("complicated-reward")
    }
    if ($combinedText -match "88VIP|PLUS会员|需买|限购|仅限|受邀|白号|黑号") {
        $score -= 4
        $riskFlags.Add("eligibility-or-quantity")
    }
    if ($RequireHistoricalPrice) {
        if ($null -eq $historicalPriceValue) {
            $score -= 20
            $riskFlags.Add("missing-historical-price")
        }
        elseif (-not $priceAdvantage) {
            $score -= 25
            $riskFlags.Add("discount-below-threshold")
        }
        else {
            $score += 10
            $reasons.Add("historical-price-advantage")
        }
    }

    $hardGate = $true
    switch ($DealType) {
        "coupon-stack" {
            $hardGate = $couponClaim -and $stackAction -and ($actionStepCount -ge 2 -or $sequenceEvidence)
        }
        "hidden-stack" {
            $hardGate = $couponClaim -and $stackAction -and ($priceAnomaly -or $combinedText -match "隐藏|未显示|漏洞")
        }
        "price-error" { $hardGate = $priceAnomaly }
        "zero-price" { $hardGate = $zeroPrice }
        "flash-inventory" { $hardGate = $inventoryAnomaly }
        "checkout-mismatch" { $hardGate = $checkoutMismatch -and $checkoutEvidence }
        "variant-mismatch" { $hardGate = $variantMismatch -and $fulfillmentEvidence }
        "hidden-eligibility" { $hardGate = $eligibilityEvidence }
        "channel-specific" { $hardGate = $eligibilityEvidence }
        "target-price-met" { $hardGate = $null -ne $PriceInfo -and $null -ne $PriceInfo.estimatedFinalPayable }
    }
    if (-not $hardGate) {
        $riskFlags.Add("insufficient-independent-evidence")
    }

    $score = [Math]::Max(0, [Math]::Min(100, $score))
    $priceGate = -not $RequireHistoricalPrice -or $priceAdvantage
    $qualified = $hardGate -and $priceGate -and $score -ge $MinScore
    $confidence = if ($score -ge 88) { "high" } elseif ($score -ge 72) { "medium" } else { "low" }
    $urgentTypes = @("price-error", "zero-price", "flash-inventory", "checkout-mismatch")
    $urgency = if ($qualified -and (($urgentTypes -contains $DealType -and $SourceAgeMinutes -le 30) -or $score -ge 88)) {
        "timeSensitive"
    }
    elseif ($qualified) {
        "active"
    }
    else {
        "passive"
    }

    return [pscustomobject]@{
        qualified = $qualified
        score = $score
        confidence = $confidence
        urgency = $urgency
        actionStepCount = $actionStepCount
        historicalPrice = $historicalPriceValue
        historicalDiscountPct = $historicalDiscountPct
        priceAdvantage = $priceAdvantage
        reasons = @($reasons)
        riskFlags = @($riskFlags)
    }
}
