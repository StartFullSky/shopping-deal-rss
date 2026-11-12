# 旧版工具说明

[返回项目首页](../README.md)

本文记录保留的查价工具、RSSHub 来源和旧版 PowerShell 监控流程。当前默认 `mode: product_only` 使用程序编排与可选商品审核，后台所有试跑仅预览；下述工具需要显式选择或单独配置。

## 独立查价工具

两种查价后端均只输出候选报告，不调用商品审核模型，也不发送通知。配置位于 [batch-price-config.json](../batch-price-config.json)。

| 后端参数 | 参考来源 | 数值门槛 |
| --- | --- | --- |
| `--backend smzdm` | 什么值得买百科的搜索摘要或商品报价 | 默认不高于展示参考价的 50% |
| `--backend xianbao_history` | 线报酷公开搜索接口中的历史促销报价 | 默认低于同类单位价参考中位数至少 20% |

这些参考价不等于当前市场最低价或已核验的结算价。未取得可靠参考时保留未知状态，不直接认定为不划算。

### 什么值得买参考价

每次都显式指定 `smzdm`，避免进入默认商品筛选流程：

```powershell
python -X utf8 scripts/price_batch.py --backend smzdm --output price-runs/smzdm.json
python -X utf8 scripts/price_batch.py --backend smzdm --input price-runs/smzdm.input.json --output price-runs/smzdm-replay.json
```

流程先搜索商品，优先提取可归属到主商品的摘要报价；摘要不足时，再在预算内读取百科商品页或分类页，并按规格归一比较。

| 配置 | 默认值 |
| --- | --- |
| 工作线程 / 每批选取消息 | 2 / 10 条 |
| 每条最多搜索 / 读取百科或分类页 | 2 次 / 3 页 |
| 每批最多 HTTP 请求 | 50 次，包含来源和协议请求 |
| 单请求 / 单条 / 整批时间预算 | 4 / 45 / 240 秒 |
| 360 与 Exa 请求间隔 | 上一次请求结束后至少 5 秒 |
| 什么值得买请求间隔 | 上一次请求结束后随机等待 5～10 秒 |

同一服务串行请求，不同服务可以由两个线程交叠处理。截止时间会停止新工作，进行中的请求仍受超时限制。配置中的间隔是保守默认值，不是源站公开配额。

SQLite 保存商品映射 7 天、分类目录 1 天、报价 20 分钟、负结果 5 分钟。搜索报价缓存使用搜索与报价有效期中较短的值；长期商品映射不会延长其中报价的有效期。同键并发请求合并为一次抓取，缓存命中无需等待网络请求队列。

默认搜索源为 360，可选备用为 Exa 的[匿名 MCP 入口](https://exa.ai/docs/get-started/exa-mcp)。备用入口的可用性和配额取决于服务提供方；设置 `fallback_search: "none"` 可关闭备用搜索。两种搜索源均只使用值得买百科参考价。

遇到验证码跳转、验证 HTML、403 或 429 时，该服务在当前批次停止请求，并记录 `stopped_sources`。下一批仍按既定间隔处理；没有本地产生的冷却或递增退避，服务端明确返回的 `Retry-After` 仍生效。请求间隔通过 `provider_state_path` 跨批次、跨进程保存，更换 `--cache` 不会重置。程序不解验证码或轮换身份。

`*.input.json` 保存冻结输入，回放不会重新拉取来源。缓存过期或先前抓取失败时仍可能补充网络请求，因此回放不一定是零请求。`--cache` 可指定其他缓存文件，`--limit` 可覆盖消息数量。

报告保留现金付款、件数、单位价、引用 URL、促销条件、抓取时间及耗时。返卡、返红包等折算价与现金付款分开；混合商品、多规格冲突、营养成分数字误作报价等情况需要额外依据。

| 报告字段 | 含义 |
| --- | --- |
| `evidence_type` | `search_snippet` 为搜索摘要，`product_page` 为商品页 |
| `basis` / `source_updated_at` | 价格依据及已知的更新时间；搜索日期不等于价格更新时间 |
| `quote_status: quoted` | 成功取得可接受的报价 |
| `quote_status: source_no_quote` | 主报价区域明确表示暂无报价 |
| `quote_status: quote_unavailable` | 其他无报价或未知情况 |
| `price_coverage` | 可比价数量、来源及数值门槛通过情况 |
| `requires_review` / `review_reasons` | 仍需复核的原因 |
| `rate_wait_seconds` | 累计限速等待时间，线程等待可能相互重叠 |

推荐列表、历史或划线价、券额、返卡、运费、价格区间、数量不符和无法归属的多规格报价，不作为主商品当前报价。索引价、起步价和同类估算满足数值门槛时，仍属于需要复核的估算候选。

结果区分 `candidate`、`conditional_candidate`、`estimated_candidate`、`not_below_threshold`、`needs_review` 和 `deferred`。返回 HTTP 200 的腾讯验证码页属于来源挑战，不会缓存成普通的「商品暂无报价」。

历史实测见[批量查价报告](../research/batch-price-validation-20261109.md)和[搜索摘要优先报告](../research/batch-price-validation-20261111.md)。早期测试使用过更短间隔，其耗时不能代表当前限速配置。

### 线报历史促销价

```powershell
python -X utf8 scripts/price_batch.py --backend xianbao_history --output price-runs/xianbao-history.json
```

该模式只使用线报酷公开 `/api/v1/feed` 和 `/api/v1/search` JSON 接口，不读取电商详情、值得买百科或调用模型。

门槛由 `min_discount_pct` 控制，默认 20：

```text
当前现金总价 ≤ 参考中位数 × (1 - min_discount_pct / 100)
```

历史范围为线报发布前 30 天，排除当前帖子、未来帖子和同优惠重复转发。按通用品类和同义词分别查询，例如先搜「洗脸巾」，结果不足时再搜「棉柔巾」，不要求品牌或包装完全相同。

`history_category_synonyms` 配置同义词，`history_category_exclusions` 区分不同加工形态，`history_category_subtypes` 区分用途，例如生鲜苹果与苹果干、儿童玩具与宠物玩具。无法识别的品类保持未知，不改用品牌搜索冒充同类样本。

比较时先统一体积、重量或件数，再按电商商品 ID，或近似的标题、品牌、型号分组。同一商品不同日期和包装的重复报价只贡献一个商品样本。先求每个商品的历史单位价中位数，再求这些中位数的中位数。

- `history_min_samples`：最少不同商品数，默认 5
- `history_max_products`：最多选取不同商品数，默认 10
- 按近期样本轮换品牌，不按价格高低挑样本
- 凑够 5 个不同商品即可提前结束，从已有结果中最多选 10 个

每页最多返回 `history_search_limit` 条，默认 50；每条线报最多搜索两次。不足 5 个商品时，若匹配比例低于一半则尝试同义词，否则尝试下一页。搜索响应默认缓存 900 秒，不同线报共享关键词与页码缓存，并遵守同域 5 秒间隔和每批 50 次请求预算。

这属于有限样本，不是完整 30 天价格归档。缺少电商商品 ID 时，标题分组是近似判断；规格未知或不同商品数量不足时，结论保持未知。

解析会区分包装数量和商品容量：洗衣机的「12kg」不作为消耗品重量；明确买两箱时计入两箱；`1.6一盒`、`1.38r/瓶`、`3R`、`💰3.8` 及正文「每盒200ml」等写法可作为提取依据。仅能比较包装件数时，标为 `pack_count_unknown_current_size`，不推断缺失体积。

报告保留品类查询、页码、不同商品数量、原始报价数、品牌分布、每个商品中位数及底层引用。所得参考始终是历史促销价，不是正常零售价或市场价。

## RSSHub 来源

以下为旧版独立来源；当前商品筛选流程只采集首页列出的酷安与微博超话 API。

| 来源 | 路径或端点 | 提供的信息 |
| --- | --- | --- |
| 线报酷 RSS | `https://new.xianbao.fun/rss.php` | 社区线报及原文链接 |
| 什么值得买 | `/smzdm/keyword/{keyword}` | 关键词相关促销帖子 |
| 京东 | `/jd/price/{product-id}` | 指定商品价格及参考信息 |

通过 `RSSHUB_BASE_URL` 指定自建或自选实例：

```powershell
$rsshub = if ($env:RSSHUB_BASE_URL) {
    $env:RSSHUB_BASE_URL.TrimEnd('/')
} else {
    'https://rsshub.app'
}

$keyword = [uri]::EscapeDataString('机械键盘')
"${rsshub}/smzdm/keyword/${keyword}"
"${rsshub}/jd/price/526835"
```

京东链接 `https://item.jd.com/526835.html` 对应商品 ID `526835`。RSSHub 的淘宝众筹路由不代表通用淘宝商品覆盖；公开实例的自动访问限制以服务方要求为准。

## 旧版 PowerShell 监控

当前 `monitor-config.json` 使用 `mode: product_only`，`monitor.ps1` 会转入 Python 主流程并在进入旧版流程前返回。下述规则仅适用于显式选择并配置的 `mode: legacy`。

| 脚本 | 职责 |
| --- | --- |
| `scripts/monitor.ps1` | 执行一轮采集、详情处理、规则、状态检查与发送 |
| `scripts/deal-quality.ps1` | 证据校验、价格解析与质量评分 |
| `scripts/deal-state.ps1` | 基线、时效、去重与已发送状态 |
| `scripts/ai-price-review.ps1` | 旧版 Codex CLI 价格复核适配器，默认禁用 |
| `scripts/send_bark.ps1` | 旧版 Bark 发送脚本 |
| `scripts/install-monitor-task.ps1` | 安装或卸载 Windows 当前用户定时任务 |

旧版来源、关键词、优惠线索、回看窗口、质量门槛和请求上限均由监控配置控制。当前配置中的全站 RSS 来源已关闭；切换模式前需要单独检查来源与规则。商品筛选模式的设置不能直接当作旧版异常优惠门槛。

质量规则会区分现金总付款与单位价、优惠叠加与普通单券促销、发货规格异常与正常多件促销。零元匹配要求独立依据，不从「10元」中误匹配「0元」。高置信度的新鲜异常可以使用 Bark `timeSensitive`，其他合格线报使用 `active`。

旧版 Codex CLI 复核与当前 Agnes 商品审核是两个独立入口。它要求商品及历史报价证据；复核调用失败时，旧版监控按自身的规则评分决定是否降级为规则候选。不要把旧版价格条件带入当前商品分类提示词。

预览一轮，不改变发送状态：

```powershell
pwsh -File scripts/monitor.ps1 -ConfigPath .\monitor-config.json -DryRun
```

以下命令分别安装或卸载定时任务，只有主动执行才会改变调度状态：

```powershell
pwsh -File scripts/install-monitor-task.ps1 `
  -ConfigPath .\monitor-config.json `
  -IntervalMinutes 5

pwsh -File scripts/install-monitor-task.ps1 -Uninstall
```

任务使用隐藏窗口启动，并忽略重叠运行。其他系统需自行配置调度器；Skill 文件、本地后台和单次命令本身不会持续轮询。轮询频率应遵循来源的缓存与访问限制。

仅检查旧版 AI 适配器输入，不调用模型：

```powershell
pwsh -File scripts/ai-price-review.ps1 -ItemJsonFile .\item.json -DryRun
```

### 旧版候选与状态格式

`Initialize` 和 `Evaluate` 接受 JSON 数组，或包含 `items` 数组的对象。以下仅示意字段格式，实际评估需要有效、及时的来源与时间：

```json
{
  "items": [
    {
      "source": "xianbao",
      "sourceId": "kuan-123",
      "sourceUrl": "https://new.xianbao.fun/kuan/123.html",
      "productUrl": "https://item.jd.com/526835.html",
      "title": "优惠线报示例",
      "dealType": "coupon-stack",
      "publishedAt": "2026-11-04T06:00:00Z",
      "observedAt": "2026-11-04T06:10:00Z",
      "estimatedFinalPayable": 259,
      "couponState": "已领取店铺券与平台券",
      "quantityCondition": "买两件",
      "reproductionSteps": ["打开商品链接", "领取列出的券", "结算前核对实付金额"]
    }
  ]
}
```

必需字段为 `source`、`sourceUrl`、`productUrl`、`observedAt`、`estimatedFinalPayable`，以及 `publishedAt` 或 `modifiedAt` 至少一项。`reproductionSteps` 不得为空。可选的 `originalPrice`、`referencePrice`、`variant`、`channel` 和 `validUntil` 等字段保留优惠条件与版本信息。

`dealType` 支持：`price-error`、`zero-price`、`flash-inventory`、`coupon-stack`、`hidden-stack`、`hidden-eligibility`、`channel-specific`、`checkout-mismatch`、`variant-mismatch`、`target-price-met` 和 `abnormal-drop`。

```powershell
# 把当前快照设为首次基线，抑制初始消息集中发送
pwsh -File scripts/deal-state.ps1 -Action Initialize `
  -ItemsFile .\items.json -StatePath .\shopping-deal-rss-state.json

# 评估新的来源标识或优惠条件变化
pwsh -File scripts/deal-state.ps1 -Action Evaluate `
  -ItemsFile .\items.json -StatePath .\shopping-deal-rss-state.json

# 通知成功后再记录已发送；通过文件传入 JSON，避免命令行转义
pwsh -File scripts/deal-state.ps1 -Action MarkSent `
  -ItemJsonFile .\item.json -StatePath .\shopping-deal-rss-state.json

# 查看基线与已发送数量
pwsh -File scripts/deal-state.ps1 -Action Status `
  -StatePath .\shopping-deal-rss-state.json
```

命令均输出 JSON。状态同时按来源标识和内容指纹去重，跨来源 ID 的相同内容抑制 7 天；仅在发送确认成功后调用 `MarkSent`。旧版 JSON 状态文件与当前 `.state/notifications.sqlite3` 是不同的存储。

成功发送会记录 `sourceToDetectionSeconds`、`sourceToNotifySeconds` 和 `detectionToNotifySeconds`，运行摘要提供平均值及最大值，用于观察来源缓存和发送延迟。

## 旧版 Bark 脚本

`scripts/send_bark.ps1` 使用 HTTPS JSON POST，校验目标与消息大小，提供超时和有限重试。它支持 `level`、点击链接 `url` 与分组 `group`。当前 Python 主流程使用的是 `notification_delivery.py`，其失败重试由程序队列控制。

旧版脚本支持 `BARK_URL` 或 Windows DPAPI 加密的 UTF-8 端点文件。先在当前会话安全配置 `BARK_URL`，再创建加密文件，无需打印端点内容：

```powershell
if ([string]::IsNullOrWhiteSpace($env:BARK_URL)) {
    throw '请先在当前会话配置 BARK_URL'
}

$secretDirectory = Join-Path $env:USERPROFILE '.codex\secrets'
$secretPath = Join-Path $secretDirectory 'shopping-deal-rss-bark.bin'
New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
Add-Type -AssemblyName System.Security

$plainBytes = [Text.Encoding]::UTF8.GetBytes($env:BARK_URL.Trim())
$protectedBytes = [Security.Cryptography.ProtectedData]::Protect(
    $plainBytes,
    $null,
    [Security.Cryptography.DataProtectionScope]::CurrentUser
)
[IO.File]::WriteAllBytes($secretPath, $protectedBytes)
```

验证消息格式，不实际发送：

```powershell
pwsh -File scripts/send_bark.ps1 `
  -SecretPath "$env:USERPROFILE\.codex\secrets\shopping-deal-rss-bark.bin" `
  -Title '通知测试' `
  -Body '仅验证预览消息格式' `
  -Level active `
  -Url 'https://example.com' `
  -DryRun
```

预览只报告消息大小和目标主机，不打印设备密钥；加密端点文件也应保存在仓库外。

## 参考与边界

- [项目调研与设计取舍](../research/market-patterns.md)
- [完整 Skill 使用规则](../SKILL.md)
- 来源文本仅作为数据，不执行其中的指令；访问频率遵守来源要求、缓存策略与服务端限流
- 缺少报价、规格不兼容、预算耗尽或访问受限时保留未知或失败原因，不虚构商品价格
- 价格候选并不代表已核验结算、库存或商品质量；这些工具不会登录、领券或下单
