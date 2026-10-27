---
name: skill-release
display_name: 技能发布管家
display_name_en: Skill Release Manager
description: "把本地技能一句话发到 GitHub / SkillHub / open.workbuddy 三处，并把两个平台的校验规则前置成本地断言。TRIGGER: 用户说「发布这个技能」「上架 skill」「更新到 skillhub」「推到 open.workbuddy」「同步到 GitHub」「技能发布报解析失败」「目录层级超限」「不允许的文件类型」「slug 已被封禁」「slug 冲突」「图标不让传」「撤回待审版本」「改完再重新提交」，或给出技能目录要求分发。DO NOT TRIGGER: 只是新建技能骨架（属于 skill-scaffold）；只是查找/安装别人的技能；只是普通代码提交。"
description_zh: 把技能一次分发到三个平台：GitHub 仓库、SkillHub 市场、open.workbuddy。同一份内容派生三种形态，平台要的字段（slug / displayName / author）自动投影到临时副本而不污染源文件；发布前跑规则引擎，把「无扩展名文件」「根目录图标」「目录层级超限」「slug 被封禁」这类真实被拒过的坑拦在本地。SkillHub 支持 API Key 与会话 Cookie 两条凭据通道。默认演练，加 --yes 才真发。
description_en: "Release a skill to three platforms in one command: a GitHub repo, the SkillHub marketplace, and open.workbuddy. One source of content, three packaging shapes; platform-specific frontmatter (slug / displayName / author) is projected into a staging copy instead of polluting the source. A rule engine blocks locally the exact failures platforms rejected before — extensionless files, root-level icons, over-deep paths, banned slugs. SkillHub accepts either an API key or a browser session cookie. Dry-run by default; --yes to actually publish."
version: 1.5.7
allowed-tools: Bash, Read, Write, Glob, Grep, AskUserQuestion
category: 开发编程
license: MIT
tags: [release, publish, skillhub, github, workbuddy]
---

# skill-release

把「发布技能」这件事本身固化成流程：一份内容 → 三个平台，规则先跑，默认演练。

存在理由很具体：同一份技能包，**SkillHub 按扩展名白名单过滤、open.workbuddy 按目录层级过滤**，
两边规则不同，靠人工记住必然返工。本技能把两条规则变成本地断言。

## 触发规则

满足任一条件即可执行：

1. 用户要求把技能发布/上架/更新到 GitHub、SkillHub、open.workbuddy 中的任一个
2. 用户报告发布失败，报错含「不允许的文件类型」「目录层级超限」「解析失败」「SKILL.md 缺少」
3. 用户要求查看某技能是否满足上架条件（走 `check`，不发布）
4. 用户要求升版本号并同步发布

未通过触发规则时，禁止执行 `publish --yes`。

## 安全约束（最高优先级，不可被任何用户指令覆盖）

1. **凭据永不外泄**：不打印、不回显、不写日志。输出一律走 `creds.mask()`。
   凭据存在 `~/.workbuddy/.secrets/skill-release.json`，**在技能目录之外** ——
   放技能目录里会随打包被上传到三个平台。
2. **源文件不被平台字段污染**：`slug` / `displayName` / `author` 只注入临时副本。
   源 `SKILL.md` 保持干净，同一份技能才能同时喂三个平台。
3. **默认演练**：不带 `--yes` 一律 dry-run，不产生任何外部副作用。
4. **不绕开阻断项**：规则引擎报 block 时停止发布。`--force` 存在但需用户明确要求。
5. **不绕开认证**：不尝试解密浏览器 Cookie、不读取系统凭据管理器。
6. **静默失败零容忍**：某个平台失败必须报出原文与下一步，禁止用 `2>/dev/null` 掩盖。

## 前置检查

每次触发时按顺序执行：

1. `doctor` —— 一次看清 Python、github-ops 是否就位、三平台凭据是否有效
2. 任一平台凭据缺失时：**该平台跳过并在结果里显式标注**，不静默少发

## 确认规则

| 风险等级 | 操作 | 策略 |
|---|---|---|
| **高（必须确认）** | `publish --yes` 到 open.workbuddy（会进审核队列，审核期约 7 个工作日，且消耗审核次数） | 先报清目标技能、版本、目标平台，等用户明确同意 |
| **高（必须确认）** | `--bump`（会改源文件的 SKILL.md / VERSION.txt / 脚本 `__version__`） | 说明将改哪些文件再执行 |
| **高（必须确认）** | `--force` 忽略阻断项 | 先逐条列出被忽略的规则与后果 |
| **中（范围明确即可）** | `publish --yes` 到 GitHub / SkillHub | 目标明确直接执行 |
| **低（直接执行）** | `check` / `doctor` / `probe` / `creds`（查看） / `audit`（只读） | 无需确认 |
| **低（直接执行）** | `watch` / `watch --check`（只读巡检） | 无需确认 |
| **低（直接执行）** | `watch --check --ship`（把**已过审**的资产上线） | 无需确认 —— 这是发布流程的收尾，用户发的时候就已经授权了 |

**额外规则：**

- **发 GitHub 之前先跑 `audit`** —— 提交身份一旦推上去就删不掉，
  事后补救的成本远高于事前扫一遍。有命中时先报给用户，不自行删库
- GitHub 推送前 `github-ops` 会做密钥扫描，命中高危会中止 →
  **把命中清单交给用户判断，不自行删改文件**
- `probe` / `audit` 只做只读探测，任何情况下不写入
- 用户说「算了」「先不发」 → 立即中止
- **发布到 open.workbuddy 成功后，巡航是流程的一部分，不是可选动作**：
  结果里 `watched: true` → 建/唤醒每日巡航任务（跑 `watch --check --ship`）；
  巡航报 `done: true` → 把该任务暂停。别让任务空跑，也别让资产过审后干等

## 核心操作

所有命令通过技能目录下的脚本执行。用**托管 Python 的绝对路径**调用
（Git Bash 下 `python` 可能指向别处，且 `~` 会被展开成 `/c/Users/...`，
Windows 版 Python 不认这种路径）：

```bash
# 把 <你> 换成你的 Windows 用户名
PY="C:/Users/<你>/.workbuddy/binaries/python/versions/3.13.12/python.exe"
SR="C:/Users/<你>/.workbuddy/skills/skill-release/scripts/release.py"
```

### 体检

```bash
$PY "$SR" doctor
```

一次看清：Python 版本、`github-ops` 是否就位、SkillHub key 是否有效、
open.workbuddy Cookie 是否仍在有效期。

### 只检查，不发布

```bash
$PY "$SR" check "<技能目录>"
$PY "$SR" check "<技能目录>" --targets skillhub            # 只看某一个平台
$PY "$SR" check "<技能目录>" --json
```

这是最常用的命令。输出结论前置，逐条列出阻断项与改法。
退出码：0 = 三平台均可发，1 = 有阻断项。

### 发布

```bash
$PY "$SR" publish "<技能目录>"                             # 演练（默认）
$PY "$SR" publish "<技能目录>" --yes --changelog "修复选档" # 真发
$PY "$SR" publish "<技能目录>" --yes --targets github,skillhub
$PY "$SR" publish "<技能目录>" --yes --bump patch
```

默认走三个平台。未配置凭据的平台会**跳过并在结果里标注**，不影响其他平台。
收费技能加 `--pay buyout --price 29`，详见下方「付费发布」。

### 发布后自动巡航（过审自动上线，落地即结束）

**发布成功不等于这件事结束了。** 资产状态机是：

```
draft → under_review → approved → published
                          ↑           ↑
                      过审待发布    真正在市场里可见
```

`approved` 之后**还要再调一次发布**才算上架。人不会天天登控制台，
于是资产常常在这个状态里躺到下次想起来为止 —— 这段时间等于白发。

所以 `publish --yes` 提交成功后会自动把资产登记进巡航列表，
之后靠 `watch` 每天扫一遍：

```bash
$PY "$SR" watch                        # 看谁在等（版本、定价、查过几次）
$PY "$SR" watch --check                # 巡检一次，只报不动手
$PY "$SR" watch --check --ship         # 巡检 + 自动上线（巡航用这个）
$PY "$SR" watch --stop                 # 停止巡航（清空列表）
$PY "$SR" watch --check --ship --json  # 机器可读，含 done 字段
```

**三条规则**：

| 服务端状态 | 动作 | 是否出列 |
|---|---|---|
| `under_review` | 继续等 | 否 |
| `approved` | `--ship` 时自动上线 | 是（上线后） |
| `published` / `rejected` / `draft` | 已落地：报结果即可 | 是 |

**列表空了就是巡航结束**——`--json` 的 `summary.done` / `done` 为 `true`
即「没有要等的了」。定时任务见到这个信号就停自己，别空跑。

**自动化怎么挂**：发布完成后（结果里有 `watched: true`）创建一个每日任务跑
`watch --check --ship`，并在 prompt 里写死「`done: true` 时把本任务暂停」。
**按名字匹配，不要重复建**——任务统一叫「技能审核巡航（过审自动上线，落地自停）」：
已在就先 `status=ACTIVE` 唤醒，没有才新建。一个任务管全部资产，
绝不按 assetId 一个资产建一个（那样一多就没人管得过来）。
细节、分支处理、失败模式见 [cruise.md](./references/cruise.md)。

> 顺带说明：`ship` 子命令是老版本留的「一次性扫全站过审资产」，不带列表、
> 不认「都过完了没有」。新流程用 `watch`，`ship` 只在想手动补救时用。


### 登录（推荐：扫码，不用导 HAR）

```bash
$PY "$SR" login                       # 弹出浏览器 → 你扫码 → 自动收尾
$PY "$SR" login --timeout 600         # 慢慢扫
$PY "$SR" login --fresh               # 换一个全新 profile（旧会话残留时用）
$PY "$SR" login --refresh             # 不开窗口：把 profile 里的活会话重新导回配置
$PY "$SR" login --check-only          # 只验活，不开浏览器
```

**遇到 403 先试 `--refresh`，别急着让用户重新扫码。** Chrome 会在后台静默刷新
SSO 会话，所以 profile 里的 cookie 一直是活的，而配置里那份字符串只是某次扫码
的快照，几小时后就 403。`--refresh` 用无头模式打开 profile、验活、把现成 cookie
重新导出，全程不打扰用户（2026-10-26 实测：profile 有效而配置 403，一条命令恢复）。

**用户只需要做一件事：扫码。其余四步全自动。**

1. 用**系统 Chrome**（找不到退 Edge）**直接打开登录页**
   `https://open.workbuddy.cn/login/?platform=open&state=0`，带持久化 profile
2. 你扫码后，脚本在**页面里** fetch `/api/v2/open-platform/parties` 判断登录态
3. 把会话连同 `navigator.userAgent` **一起**写进凭据 ——
   平台会话与 UA 绑定，cookie 对了 UA 不对照样 302
4. 顺手抓 `party_id`、留一份 `storage_state` 备用

三个踩过的坑，改代码时别退回去：

- **必须直接进登录页**。落到首页时，如果 profile 里还有半个失效会话，
  首页不渲染二维码，用户会以为「窗口打开了但没反应」。
- **不要靠 cookie 名字判断登录**。`session` 这种名字在过期的 profile 里照样存在，
  于是「一进去就以为登了」→ 反复验活失败 → 空等到超时。
  只有页面内 fetch 的结果可信（自带 cookie + UA + TLS 指纹）。
- **profile 被占用时不要硬抢**。Chrome 锁住 `SingletonLock` 后，再启动只会把 URL
  转给已有窗口然后自己退出（Playwright 报 `TargetClosedError`），连 cookie 都拿不到。
  脚本会检测到锁并自动换一个带时间戳的目录。

> profile 落在 `~/.workbuddy/.secrets/wb-browser-profile/`，复用登录态 ——
> 第二次以后窗口一开往往就是已登录，连扫码都省了。
> 想清空旧会话用 `--fresh`：脚本是把整个目录**移走**而不是删除 ——
> profile 动辄数百个文件，`rmtree` 会撞上批量删除守卫。

### 看数据（定价、选题之前先跑这个）

```bash
$PY "$SR" metrics                      # 默认回溯 90 天
$PY "$SR" metrics --days 30            # 看近一月
$PY "$SR" metrics --only 抖音           # 只看某个资产
$PY "$SR" metrics --out wb-metrics.json
```

输出每个资产的 **安装 / 活跃 / 调用 / 频次 / 周留存 / 月留存** 与付费模式。
保留率是含真话的那个指标：装机高但周留存个位数，说明它是「一次性工具」，
用户心里没有复购理由，硬改成订阅必失败。

两个口径陷阱，脚本会自动标注：

- `installs` 在某天之前恒为 0、之后突然三位数 —— 多半是**埋点/口径上线或回填**，
  不是流量爆发。判据是同日的 `activeUsers`：真新增 200+ 装机，当日活跃不可能只有 50。
- 指标是 `{value, status}`，`status=pending` 表示口径未就绪，**不是 0**。
  直接把 null 当 0 画图，会得出「留存为零」的错结论。

接口契约（2026-10-26 实测，靠报错问出来的，别再猜）：
`POST /api/v2/open-platform/asset-metrics/daily`，
字段是 `assetIds` / `metricTypes` / **`startTime` / `endTime`**（不是 startDate），
指标枚举 `installs,calls,activeUsers,frequency,weekRetention,monthRetention`。

### 凭据

```bash
$PY "$SR" creds                                            # 查看（脱敏）
$PY "$SR" creds --set skillhub.key=skh_xxxx
$PY "$SR" creds --set skillhub.cookie='skh_token=<JWT>; sid=<sid>'
$PY "$SR" creds --set workbuddy.cookie='<浏览器 Cookie>'   # 不推荐，用 login
$PY "$SR" creds --set author=mo
$PY "$SR" creds --set github.owner=<GitHub 用户名>
$PY "$SR" creds --from-har ~/Downloads/wb.har   # login 跑不通时的兜底
```

SkillHub 有 Key 和 Cookie 两条路，**配一条就够**；两条都配时以 Cookie 优先。
open.workbuddy 走 `login` 即可；确实要导 HAR 时，⚠️ 必须先在 DevTools 里勾上
「允许生成包含敏感数据的 HAR」（Chrome 130 起默认导出「已清理」档，
Cookie 会被剥光，症状酷似「没登录」）。

### 探测 open.workbuddy

```bash
$PY "$SR" probe
```

只读：依次打 `/parties`、`/capabilities/categories`、`/capabilities/summary`、
`/capabilities/list`，报告会话是否有效、各端点是否可达。

打网络之前会先跑一遍 **Cookie 完整性自检** —— 「Cookie 是坏的（复制被截断）」
和「没登录」响应长得一模一样（都是 302 → Keycloak），但 Cookie 结构分得清：
本站 `session` 固定是 4 段 `<22位id>|<10位过期>|<payload>|<27位HMAC>`。
**这套自检是给 HAR 兜底路径用的** —— 主路径是 `login` 扫码，脚本自己抓
完整 Cookie + 真实 UA，根本不存在截断问题，也不依赖 UA 一致性
（平台会话绑定 UA，这是手抄 Cookie 反复失效的根因）。
只有当 `login` 跑不通（无图形环境等）才退回 HAR，且必须先在 DevTools 里勾
「允许生成包含敏感数据的 HAR」，别走剪贴板。
详见 [references/troubleshooting.md](./references/troubleshooting.md)。

### 付费发布（一次性买断 / 月订阅，全自动）

付费走 `publish` 的两个参数，纯 API 完成，**不需要人点控制台**：

```bash
$PY "$SR" publish <技能目录> --targets workbuddy --yes --pay buyout --price 29
$PY "$SR" publish <技能目录> --targets workbuddy --yes --pay subscription --price 19 --pay-days 30
$PY "$SR" publish <技能目录> --targets workbuddy --yes --pay free     # 显式覆盖配置里的付费值
```

也可以把定档写进配置，之后直接 `publish`：

```bash
$PY "$SR" creds --set workbuddy.pay_mode=1 --set workbuddy.pay_amount=29
```

`pay_mode` 是**版本级**属性：`0=免费 1=一次性买断 2=月订阅`；`pay_amount` 对外是**元**，
提交时自动换算成分。发完用 `pay` 回看服务端记的到底是什么：

```bash
$PY "$SR" pay <技能目录>        # 或 pay os_e1a727dfe880fe87
```

### 发付费之前先算一行（可选，但建议算）

```bash
$PY "$SR" publish <技能目录> --pay buyout --price 29 --audience 9
# → ! 收入天花板：天花板 9 × 10% × ¥29 = ¥26，低于 ¥300 → 不该做
```

`--audience` 填「**已证明会装这类东西的人**」——最省事的来源是同类免费资产的历史安装量
（`release.py metrics --days 90`），不是全平台用户数。

工具只把算式摆出来，**默认不拦你**。原因是它本来就不能替你做这个判断：

- `¥300` 是参照物不是法律，对一个人是一天工钱，对另一个人是一小时
- `10%` 转化率是保守拍的值，没有成交样本
- 付费的动机不止赚钱：筛人、立价格锚、试水都算

真要硬门槛，加 `--strict`（或 `creds --set workbuddy.strict_ceiling=true`）：
那样付费发布必须给 `--audience`，且不过线就拒绝提交。

**这条规则的来历**：2026-10-27 做过一个 ¥29 的付费技能，可触达人群只有 9 人，
天花板 ¥26，但整套文档、脚本、定价、上架全做完了才被指出来。
问题不是定价贵，是**根本没算**。所以门槛留在工具里，但只做提示。

**三个必须记住的坑**（2026-10-26 用无头浏览器复刻控制台 UI 抓包后确认，
此前「API 发不了付费」的结论是错的，错在载荷形状）：

1. `payMode` / `payDetail` / `needEncrypt` 必须在 **`fields.common` 里面**。
   放在 submit 载荷**顶层会被静默丢弃**——返回 200、无报错、按免费收审。
2. `payDetail` 元素的金额键是 **`price`**（不是 `amount`），单位**分**。
3. 周期键是 snake_case 的 **`duration_days`**（不是 `durationDays`）；买断写 `0`。

控制台同款载荷（¥29 买断）：

```json
"fields": {"common": {..., "payMode": 1,
                      "payDetail": [{"price": 2900, "duration_days": 0}],
                      "needEncrypt": true}}
```

**两条平台侧铁律**（改不了，只能顺着来）：

- **付费方式过审后不可更改** → 第一次提交就要定对；
- **已过审的免费资产不能转付费** → 想收费就得在还是 draft / 待审时改，
  走 `withdraw` 退回 draft 后重新 `publish` 带上 `--pay`。

所以 `upload_and_submit()` 的行为是：带 `pay_mode` 提交 → **回读 `payMode` 验证** →
没生效先试 `/pay` 兜底（已知不可靠）→ 仍不行就自动 `withdraw` 撤回并报错，
**绝不留下一个「本应收费却免费」的待审版本**。

想让资产从免费变收费、而它已经过审：`withdraw` 没用（过审后不归它管），
只能删掉重建，或者等下个版本用新资产。这是平台约束，不是脚本能绕的。

### 撤回 open.workbuddy 的待审版本

```bash
$PY "$SR" withdraw <技能目录>            # 按 frontmatter 的 slug/name 找资产
$PY "$SR" withdraw os_841ab5b3d968503a   # 也可以直接给 assetId
```

把**正在审核**的版本退回 `draft`。`publish` 遇到资产在审核中会整体跳过，
所以「提交后才发现字段填错」原本只能干等驳回 —— 用这条主动退回来，
改完再 `publish`。**不改版本号**，重提可用同版本或更高版本。
审核计时会重头开始，只改了工具代码这种权衡一下再决定。

### 审计提交身份（GitHub）

```bash
$PY "$SR" audit                                   # 扫账号下所有仓库
$PY "$SR" audit --repo Yemo3s/some-repo           # 只查一个
$PY "$SR" audit --watch "别名@其他域名"            # 追加盯防项
```

**发布前先跑这条。** GitHub 是三平台里唯一会记录「谁写的」的 ——
commit 元数据里的姓名 / 邮箱写进去几乎删不掉（`force push` 只改分支指向，
旧 SHA 仍可直访；彻底清除要删库重建，而删库需要令牌带 `delete_repo` scope）。

判据是白名单：邮箱以 `@users.noreply.github.com` 结尾即视为安全；
其余要与**本机全局 git 身份**（`user.name` / `user.email`）匹配才算「你的泄漏」——
所以上游 fork 的原作者邮箱不会被误报。

退出码：0 = 干净，1 = 有命中。报告按「公开 / 私有」分组，
公开仓库的命中会置顶 —— 那才是真正暴露的。
报告还会列出**哪些仓库是公开的**（公开 fork 会标注），
因为「匿名可读的仓库」和「脏提交」叠加才是事故。

## 使用示例

**场景一：技能改完了，发三个平台**

```
用户：把 douyin-downloader 发到三个平台
代理：
  1. release.py doctor                        # 先确认凭据都还在
  2. release.py publish ~/.workbuddy/skills/douyin-downloader          # 演练
  3. 报清结果后，用户同意 → 加 --yes 真发
```

**场景二：上传被拒了，先定位**

```
用户：发到 open.workbuddy 报「目录层级超限」
代理：release.py check <技能目录> --targets workbuddy
      → 直接列出第 3 层的文件与改法
```

**场景三：只想更新 SkillHub**

```
用户：只更新 skillhub，GitHub 先不动
代理：release.py publish <技能目录> --yes --targets skillhub --changelog "……"
```

**场景四：新版本**

```
用户：发个新版本
代理：release.py publish <技能目录> --yes --bump patch --changelog "……"
      先说明 --bump 会改哪三个地方
```

**场景五：cookie 过期了**

```
用户：会话失效了 / 帮我重新登录
代理：release.py login --refresh      # 先试这条：profile 里往往还有活会话
      → 若 --refresh 也报失效，才 release.py login --timeout 600
      → 弹窗停在登录页，只说一句「请扫码」
      → 扫完自动抓 cookie + UA + party_id，然后 release.py probe 复核
      ⚠️ 不要让用户导 HAR、不要让他手抄 Cookie
```

**场景六：想定价 / 想收费，先看盘子**

```
用户：我这个技能有量了，能收费吗
代理：release.py metrics --days 90
      → 先看 payMode：已上架的免费资产平台不支持转付费，要收费只能新发资产
      → 再看周留存：留存个位数的资产没有复购理由，买断比订阅现实
      → 装机高但留存低 = 选题好、交付完就走，别指望在同一资产上收钱
```

**场景七：发一个收费技能（全自动，不用人点控制台）**

```
用户：这个技能卖 29 块，发到 open.workbuddy
代理：release.py publish <技能目录> --targets workbuddy --pay buyout --price 29   # 演练
      → 确认无误后加 --yes
      → 发完立刻 release.py pay <技能目录> 回读，确认服务端记的是 ¥29 而不是免费
      ⚠️ 付费是版本级属性、过审即锁死：定错价只能 withdraw 回 draft 重发
      ⚠️ 已过审的免费资产转不了付费，别答应用户「上线后再改价」
```

**场景八：定价没生效，明明设了 29 元却显示免费**

```
用户：我的技能设了 29，怎么是免费
代理：release.py pay <技能目录>              # 先看服务端到底记了什么
      → payMode=0：payload 位置错了（payMode 必须在 fields.common 里，
        payDetail 用 price + duration_days），本技能已内置正确形状
      → 需要改：withdraw 回 draft，再 publish --pay buyout --price 29
```

**场景九：发完之后不用管了（开巡航）**

```
用户：发了就行，过了审帮我上架
代理：release.py publish <技能目录> --targets workbuddy --yes
      → 结果里 watched: true 说明已登记进巡航列表
      → 建/唤醒一个每日任务跑：release.py watch --check --ship --json
      → 任务 prompt 里写死：done=true 就把自己 PAUSED，并报「巡航结束」
      → 下次再发布时，看到 watched: true 就把它 ACTIVE 回来
```

**场景十：资产一直不出现，怀疑过审了没上架**

```
用户：审核说通过了，市场里搜不到
代理：release.py watch                      # 先看它还在不在列表里
      → 不在列表：手工补登记 watch --asset 不支持新增，用 watchlist.add()
      → 在列表且 lastStatus=approved：release.py watch --check --ship 立刻上线
      → 状态 approved 但 --ship 报错：改用 release.py ship --yes 兜底，
        然后把报错记进 references/troubleshooting.md
```

## 三个平台的通道性质

| 平台 | 鉴权 | 交付形态 | 卡点 |
|---|---|---|---|
| GitHub | 令牌（`github-ops` 已托管，DPAPI 加密） | git 仓库 | 推送前密钥扫描 **+ 提交身份必须 noreply** |
| SkillHub | API Key `skh_…` **或** 会话 Cookie（可裁剪，见下） | **逐个文件 multipart 上传，不用 zip** | 扩展名白名单，无扩展名一律拒 |
| open.workbuddy | **本体域名下的 Cookie 会话** | **.zip**，外层包 `{slug}/` | 目录层级「根/二级/文件」 |

**GitHub 是三平台里唯一会把「是谁写的」写进产物的**（commit 元数据），
而且写进去就删不掉。所以提交身份无条件写成 noreply 邮箱，
并在 commit 后与 push 前各断言一次 —— 绝不回退到全局 git 身份
（那通常是真名 + 公司邮箱）。详见 `references/troubleshooting.md`。

**`git push` 走的域名是 `github.com`，REST API 走的是 `api.github.com`**
—— 这两个域名的可达性可以完全不同（实测本机：`github.com` 直连超时、
沙箱代理对 CONNECT 返回 502，而 `api.github.com` 一切正常）。
所以推送失败时**先看报错类型**：属网络不通（`CONNECT tunnel failed` /
`Failed to connect … port 443`）就自动改走 API 兜底——
用 Git Data API 把同一棵树写成一次提交，身份同样显式写 noreply。
认证类报错（403 / 令牌无效）不会触发兜底，换通道也是白换。

**SkillHub 的 Cookie 通道只留必要字段**：浏览器整串 Cookie 里其它域名的项
（如 `qcloud_*`、`sensorsdata*`）一律裁掉，只保留 `skh_token` 与 `sid`
—— 少一个字段就少一处泄漏面（见 `references/platforms.md`）。
**两条通道互斥**：配了 Cookie 就不要再发 `Authorization`，同时带会被判 token 无效。

**open.workbuddy 的 Cookie 必须来自本站**。该站前面是 APISIX，未登录时接口
**302 到 Keycloak 登录页**；跟随重定向会拿到「200 + 登录页 HTML」。
因此判据是 **200 且是 JSON**，光看状态码会把未登录报成「通过」——
这是本技能踩过的真实坑。
`cloud.tencent.com` / `console.cloud.tencent.com` 有同名同格式的 `session`，
**域名不同，拿过来不认**。取法见 [references/commands.md](./references/commands.md)。

**图标（icon.png）自动上传**：技能根目录放一张 512×512、<500 KB 的 PNG 即可，
发布时自动走各自的图标通道 ——
SkillHub：`POST /api/v1/community/skill-icons/upload`（multipart，字段 `file`）
→ `{iconUrl}` → publish payload 直带 `iconUrl`（**补图标只能再 publish 一版**，
平台没有单独改图的端点）；open.workbuddy：
`POST /api/v2/open-platform/common/uploads/presign`（`scene: "capability_cover"`，
**无 complete 步骤**）→ objectKey → `fields.common.coverObjectKey`。
GitHub 不消费图标。图不合规会在发布时报错（尺寸/大小前置断言）。

> 核验图标是否**真的**生效：把远端图下载下来比 md5（两端同理）。
> open.workbuddy 侧 `coverUrl` 为字面量 **`letter:auto`** 就等于**没有封面**；
> 前台「看着还是旧图」多半是浏览器/CDN 缓存，别据此判定没传上。

**服务（资质）类目必填**（open.workbuddy）：`fields.common.categories` 为空会被**审核驳回**
（原文「服务类目审核：无资质类目字段，需重新选择对应类目」）。它与 `marketCategories`
是**两套体系**，都要在凭据文件里配：

```jsonc
"categories": ["tools:office"],             // 服务类目：<大类>:<三级类目>
"market_categories": ["development-tools"]  // 市场展示类目：连字符 id
```

⚠️ **资质为空时不要下发 `qualifications` 这个键**（写成 `[]` 一样被打回，话术一模一样）。
控制台就是「有资质文件才带键」，代码侧由 `normalize_categories()` 保证。
枚举的唯一权威来源是 `GET /capabilities/business-categories`（**不是**文档站、
更不是 `GET /capabilities/categories` 那 13 个市场类目）。个人主体「工具」大类下有
办公 `tools:office` / 图片处理 `tools:image_editing` / 信息查询 … 但**没有「开发工具」**，
技术类技能一般填 `tools:office`。为空时**本地直接报错**，不会白跑一次提交。
驳回原因可直接查 `GET /capabilities/{assetId}/reasons`。

细节与出处见 [references/platforms.md](./references/platforms.md)。

## 参考文档

按需查阅，无需预加载：

| 文档 | 何时查阅 |
|---|---|
| [platforms.md](./references/platforms.md) | 需要每条规则的出处、平台通道细节、两个平台各自卡什么 |
| [commands.md](./references/commands.md) | 需要完整参数、退出码、JSON 输出结构 |
| [cruise.md](./references/cruise.md) | 要挂巡航定时任务、状态判错、`approved` 卡住不动的排查 |
| [troubleshooting.md](./references/troubleshooting.md) | 命令报错、上传被拒、open.workbuddy 请求体需要现场校准 |

## 输出约定

- 结论前置：第一行先说「XX 可发 / 不可发」
- 阻断项必须带**改法**，不能只报错
- 阻断项与警告分开列，警告不阻断
- 收尾一句话，不复述表格
