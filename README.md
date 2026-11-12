# Shopping Deal RSS · 线报工作台

采集线报、按规则筛选、可选 AI 复核，并通过本地后台查看结果和调整配置。

采集、去重、队列、重试和发送由程序负责；AI 只在发送前判断消息是否介绍了具体实物商品。项目也可以作为 Codex Skill 使用。

**当前计划任务已开启实际推送；后台的手动试跑仍只做预览。**

[快速开始](#快速开始) · [后台使用](#后台使用) · [处理流程](#处理流程) · [配置说明](#配置说明) · [命令行运行](#命令行运行) · [验证与测试](#验证与测试)

## 快速开始

### 1. 安装依赖

需要 Python 3.10 或更高版本。以下命令均在项目根目录执行：

```powershell
git clone https://github.com/StartFullSky/shopping-deal-rss.git
cd shopping-deal-rss
python -m pip install -r requirements-price-batch.txt
```

后台使用 Python 服务和静态页面，无需 Node.js 或前端构建。安装依赖和启动服务请使用同一个 Python 环境。只有运行 PowerShell 监控脚本时，才需要 PowerShell 7 或 Windows PowerShell 5.1。

### 2. 启动本地后台

Windows 下双击 [start-admin.cmd](start-admin.cmd)，即可启动服务并打开浏览器。也可以运行：

```powershell
python -X utf8 scripts/admin_server.py --open-browser
```

访问 [本地后台](http://127.0.0.1:8765)。服务仅监听本机回环地址，不部署网站或创建公网隧道；按 `Ctrl+C` 停止服务。

端口被占用时，可以指定其他端口：

```powershell
python -X utf8 scripts/admin_server.py --port 8766 --open-browser
```

### 3. 开始第一次试跑

点击「开始试跑」，选择「获取最新线报」。首次可以关闭「本次启用 AI 复核」，先检查采集和本地规则，无需配置模型密钥。

当前仅采集以下两个公开 API 来源，每个来源默认最多 20 条：

| 来源 | 分类别名 | 分类 ID |
| --- | --- | --- |
| [酷安](https://new.xianbao.fun/category-kuan/) | `kuan` | 17 |
| [微博超话](https://new.xianbao.fun/category-weibo-chaohua/) | `weibo-chaohua` | 14 |

程序会校验返回的分类 ID。来源失败时记录错误，不回退到全站、好单或普通微博来源。

## 后台使用

| 页面或操作 | 用途 |
| --- | --- |
| 线报列表 | 搜索标题和正文，按来源、保留、程序过滤、AI 排除或暂缓状态筛选 |
| 消息详情 | 查看原文、程序筛选原因、AI 结论和发送阶段状态 |
| 规则配置 | 编辑关键词、消息时效、批次上限、请求间隔，以及模型和异常处理策略 |
| 用当前批次试算 | 比较已保存规则与未保存草稿，列出新增保留和新增排除的消息 |
| 开始试跑 | 获取最新线报，或复用当前批次；可单独选择本次是否启用 AI |
| 试跑记录 | 回看每批输入、配置版本、筛选结果、耗时、Token 用量和缓存命中情况 |

关键词草稿试算只执行本地规则和批内去重，使用该批数据的原始采集时间；不会请求线报或模型，也不会保存配置或修改发送标记。

完整的历史批次重跑保留原始时间戳，按当前时间执行消息有效期检查。打开、刷新页面只读取本地记录；获取最新数据需要主动开始试跑。

配置保存后从下一轮生效，正在运行的任务继续使用启动时的配置。保存采用版本检查和原子替换，遇到编辑冲突时需要重新加载。

## 处理流程

```text
采集指定来源 → 来源与时效检查 → 关键词过滤 → 去重 → 可选 AI 复核 → 程序发送并记录状态
```

关键词和去重在 AI 之前执行；需要审核时，优先读取缓存，未命中才调用模型。AI 不负责拉取数据、修改发送状态或发消息。

### 关键词规则

关键词匹配标题和正文，采用字面子串匹配：

- `include_keywords`：命中任意一个才保留；为空则不限制品类
- `exclude_keywords`：命中任意一个即排除；同时命中包含词时仍会排除

当前两个列表均为空，可在后台填写，每行一个关键词。以下是局部配置示例，应合并到现有配置中：

```json
{
  "include_keywords": ["沐浴露", "洗发水"],
  "exclude_keywords": ["求购", "二手", "抽奖"]
}
```

建议保存前先试算。例如排除「会员」，也可能过滤「会员价洗发水」。

### 可选 AI 复核

默认通过 Xiaomi Responses API 直连小米模型 `mimo-v2.6-pro`，每批 20 条、每轮最多两批。当前只判断是否为实物商品，不比较价格或计算优惠分数。

- 具体实物商品即使缺少价格、规格或链接，也可以保留；普通促销、凑单和会员价不影响商品身份
- 纯红包、纯优惠券、抽奖、虚拟权益、服务及讨论等消息判为非商品
- 无法识别商品或活动对象时，保留「无法判断」结论

| 审核情况 | 程序行为 |
| --- | --- |
| 关闭 AI，或传入 `--no-ai` | 跳过模型调用、凭据读取和审核缓存，按本地规则处理 |
| AI 明确判为商品 | 继续进入程序的发送判断 |
| AI 明确判为非商品 | 拦截该消息 |
| 模型异常或无法判断 | 默认按本地规则放行；可配置为暂缓 |

关闭 AI 后，通过本地规则的消息只表示满足程序条件，不表示已由模型确认商品身份。模型错误单独记录，不会缓存为非商品；关闭 AI 时也不会沿用之前的缓存拒绝。

## 配置说明

### 配置文件与覆盖关系

| 文件 | 管理内容 |
| --- | --- |
| [product-filter-config.json](product-filter-config.json) | 当前主流程：来源、关键词、时效、AI、限速、缓存和发送策略 |
| [monitor-config.json](monitor-config.json) | PowerShell 入口与后台读取的发送开关、发送上限及路径覆盖；同时保留旧版配置 |
| [batch-price-config.json](batch-price-config.json) | 独立查价模式；默认入口仍转入商品筛选主流程 |

后台和 `monitor.ps1` 使用相同的生效配置读取逻辑。直接运行 Python 主流程时，默认只读取商品筛选配置；传入 `--monitor-config` 后才应用监控配置中的覆盖项。

监控配置会覆盖发送开关、`monitor.maxAlertsPerRun`、`delivery.barkSecretPath` 和 `delivery.notificationStatePath`。其中顶层 `ai` 属于旧版 Codex CLI 价格复核器；当前小米商品审核开关位于 `product-filter-config.json` 的 `ai.enabled`。

### 常用设置

以下默认值来自 `product-filter-config.json`：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `feed_limit` | 20 | 每个来源最多采集条数 |
| `max_items` | 40 | 每轮去重后进入处理的消息上限 |
| `max_age_minutes` | 120 | 消息有效期，单位为分钟 |
| `search_interval_seconds` | 5 | 同一来源请求之间的间隔，单位为秒 |
| `ai.enabled` | `true` | 是否启用可选商品审核 |
| `ai.on_error` / `ai.on_unknown` | `bypass` | `bypass` 按本地规则放行，`defer` 暂缓 |
| `classification_cache_seconds` | 604800 | 商品审核结果缓存 7 天 |
| `unknown_cache_seconds` | 300 | 无法判断的结论缓存 5 分钟 |
| `delivery.enabled` | `true` | 计划任务实际发送开关；后台试跑仍强制预览 |
| `delivery.max_per_run` | 2 | 每轮最多发送尝试次数，含失败尝试 |

来源和模型请求保留各自的限速与预算。限速状态跨批次、跨进程保存；更换审核缓存文件不会重置请求间隔。

### 模型凭据

| 环境变量 | 用途 |
| --- | --- |
| `XIAOMI_API_KEY` | 模型 API 密钥，优先于本地 Codex provider 配置 |
| `XIAOMI_BASE_URL` | 覆盖模型 API Base URL |
| `XIAOMI_MODEL` | 覆盖模型名称，默认保持 `mimo-v2.6-pro` |

Windows 默认从本地 Codex provider 配置读取小米凭证；其他系统使用环境变量。密钥保存在仓库外，后台仅显示是否已配置，不回显内容。环境变量覆盖生效时，后台会显示实际使用的模型与地址。

## 命令行运行

关闭 AI，采集并预览最新线报：

```powershell
python -X utf8 scripts/deal_pipeline.py --no-ai --dry-run --output price-runs/latest.json
```

按配置启用可选 AI，但仍只预览：

```powershell
python -X utf8 scripts/deal_pipeline.py --dry-run --output price-runs/latest.json
```

复用冻结输入，跳过重新采集：

```powershell
python -X utf8 scripts/deal_pipeline.py --input price-runs/latest.input.json --no-ai --dry-run --output price-runs/replay.json
```

输出报告保存在 `--output`，原始快照保存在同名的 `*.input.json`。CLI 与后台都会把运行记录写入历史数据库。

| 参数 | 用途 |
| --- | --- |
| `--config` | 指定商品筛选配置文件 |
| `--monitor-config` | 应用监控入口的配置覆盖 |
| `--no-ai` | 本轮跳过 AI，不修改已保存配置 |
| `--dry-run` | 强制预览，不实际发送或修改通知标记 |
| `--input` | 使用已有快照，保留消息原始时间 |
| `--limit` | 覆盖本轮处理上限 |
| `--cache` / `--state` | 分别指定审核缓存与通知状态文件 |
| `--json-stdout` | 在标准输出中返回完整 JSON 报告 |

`scripts/product_filter.py` 和默认的 `scripts/price_batch.py` 也是同一主流程的兼容入口。Windows 下可通过 `scripts/monitor.ps1 -DryRun` 进入相同流程；创建后台页面或运行一次命令不会自动安装定时任务。

## 推送与本地状态

程序支持 Bark 单设备推送、WxPusher Topic 群发，以及 WxPusher 全关注者广播。计划任务可以实际发送；后台手动试跑仍只做预览。

WxPusher 的多人接收方式如下：

1. 登录 [WxPusher 管理后台](https://wxpusher.zjiecode.com/admin/)创建应用，取得以 `AT_` 开头的 `appToken`。
2. 将 `appToken` 保存为 `WXPUSHER_APP_TOKEN` 环境变量，或保存到 `delivery.wxpusher.app_token_secret_path` 指向的 Windows DPAPI 文件。不要把 Token 提交到 Git。
3. 全关注者广播使用 `delivery.wxpusher.recipient_mode: "all_followers"`。程序通过官方用户列表接口读取当前关注者，再用 `uids` 分批发送；后续新关注者会在下一轮自动进入接收名单。
4. Topic 群发可改为 `recipient_mode: "topic"`，把 Topic 的数字 ID 填入 `delivery.wxpusher.topic_ids`。发送到该 Topic 的消息只发给已订阅该 Topic 的用户。
5. 两份配置中的 `delivery.channel` 设为 `wxpusher`，并保持 `delivery.enabled: true`。发送调用 `POST https://wxpusher.zjiecode.com/api/send/message`。

WxPusher 业务码 `1000` 只表示发送任务已受理，不代表每台设备已经送达；官方发送状态查询接口已停用。为避免重复轰炸，本项目在任务受理成功后记录为已发送。`uids` 单次最多 2000 个，`topicIds` 单次最多 5 个，程序会自动分批。

发送状态与审核缓存分开保存：相同消息版本已发送后不会再发，跨帖相同内容在去重窗口内抑制；标题或正文变化会形成新版本。

明确失败按 `retry_delay_seconds` 等待后重试，最多 `max_attempts` 次；送达不确定或发送中进程中断时，暂停自动重发，等待核实。首次开启发送默认只建立基线，避免集中推送已有消息。

| 本地文件或目录 | 用途 |
| --- | --- |
| `.state/run-history.sqlite3` | 运行输入、配置快照、过滤原因和统计 |
| `.state/notifications.sqlite3` | 通知队列、发送结果、去重和处理占用状态 |
| `.price-cache/` | 审核、限速及旧版查价缓存 |
| `price-runs/` | CLI 输出报告与冻结输入 |

这些运行产物均由 Git 忽略。预览模式可以写入运行历史和审核缓存，但不会创建或修改通知标记。

## 验证与测试

运行 Python 单元与接口测试：

```powershell
python -X utf8 -m unittest discover -s tests -p "test_*.py" -q
```

Windows 下验证 PowerShell 配置与旧版状态规则：

```powershell
powershell -NoProfile -File tests/monitor-config.tests.ps1
powershell -NoProfile -File tests/deal-state.tests.ps1
powershell -NoProfile -File tests/deal-quality.tests.ps1
```

可选浏览器检查需要本机已安装 Playwright 和 Chrome，并先启动后台：

```powershell
python -X utf8 tests/browser_admin_smoke.py --live-feed --artifacts price-runs/browser-smoke
```

浏览器检查会采集一批最新线报，验证搜索、筛选、详情、规则试算、保存与重新加载、批次重跑和手机布局。测试时关闭 AI、恢复原关键词配置，并保持仅预览。

## 项目结构

| 路径 | 职责 |
| --- | --- |
| [web/admin/](web/admin/) | 本地后台页面、样式和交互 |
| [scripts/admin_server.py](scripts/admin_server.py) | 后台 HTTP 接口与手动预览任务 |
| [scripts/deal_pipeline.py](scripts/deal_pipeline.py) | 采集、规则、去重、可选审核和发送编排 |
| [scripts/product_filter.py](scripts/product_filter.py) | 商品分类提示词、模型接口和审核缓存 |
| [scripts/deal_config.py](scripts/deal_config.py) | 生效配置读取、校验和版本化保存 |
| [scripts/run_history.py](scripts/run_history.py) | 历史记录与筛选原因 |
| [scripts/notification_state.py](scripts/notification_state.py) | 持久化通知队列与去重 |
| [scripts/notification_delivery.py](scripts/notification_delivery.py) | 程序控制的 Bark 发送器 |
| [tests/](tests/) | 单元、接口、PowerShell 和可选浏览器检查 |
| [SKILL.md](SKILL.md) | Codex Skill 的使用规则；接口元数据位于 `agents/openai.yaml` |

## 旧版功能与参考

旧版 RSSHub、PowerShell 异常优惠监控，以及值得买和线报历史查价后端仍保留，但不进入当前默认流程。详见[旧版工具说明](docs/legacy-workflows.md)，其中包含显式查价命令、旧版状态格式和 Bark 配置方法。

- [监控项目调研](research/market-patterns.md)
- [早期批量查价实测](research/batch-price-validation-20261109.md)
- [搜索摘要优先查价实测](research/batch-price-validation-20261111.md)
- 参考项目：[wool-hunter](https://github.com/irito045/wool-hunter)、[yangmao-radar](https://github.com/zaiji100/yangmao-radar)

当前商品识别不代表价格优势、库存或结算条件已经核验。项目不执行登录、领券或下单；来源失败、审核异常和缺失信息会保留在报告中。
