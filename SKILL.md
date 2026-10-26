---
name: skill-release
display_name: 技能发布管家
display_name_en: Skill Release Manager
description: "把本地技能一句话发到 GitHub / SkillHub / open.workbuddy 三处，并把两个平台的校验规则前置成本地断言。TRIGGER: 用户说「发布这个技能」「上架 skill」「更新到 skillhub」「推到 open.workbuddy」「同步到 GitHub」「技能发布报解析失败」「目录层级超限」「不允许的文件类型」「slug 已被封禁」「slug 冲突」「图标不让传」「撤回待审版本」「改完再重新提交」，或给出技能目录要求分发。DO NOT TRIGGER: 只是新建技能骨架（属于 skill-scaffold）；只是查找/安装别人的技能；只是普通代码提交。"
description_zh: 把技能一次分发到三个平台：GitHub 仓库、SkillHub 市场、open.workbuddy。同一份内容派生三种形态，平台要的字段（slug / displayName / author）自动投影到临时副本而不污染源文件；发布前跑规则引擎，把「无扩展名文件」「根目录图标」「目录层级超限」「slug 被封禁」这类真实被拒过的坑拦在本地。SkillHub 支持 API Key 与会话 Cookie 两条凭据通道。默认演练，加 --yes 才真发。
description_en: "Release a skill to three platforms in one command: a GitHub repo, the SkillHub marketplace, and open.workbuddy. One source of content, three packaging shapes; platform-specific frontmatter (slug / displayName / author) is projected into a staging copy instead of polluting the source. A rule engine blocks locally the exact failures platforms rejected before — extensionless files, root-level icons, over-deep paths, banned slugs. SkillHub accepts either an API key or a browser session cookie. Dry-run by default; --yes to actually publish."
version: 1.5.3
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

**额外规则：**

- **发 GitHub 之前先跑 `audit`** —— 提交身份一旦推上去就删不掉，
  事后补救的成本远高于事前扫一遍。有命中时先报给用户，不自行删库
- GitHub 推送前 `github-ops` 会做密钥扫描，命中高危会中止 →
  **把命中清单交给用户判断，不自行删改文件**
- `probe` / `audit` 只做只读探测，任何情况下不写入
- 用户说「算了」「先不发」 → 立即中止

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

### 登录（推荐：扫码，不用导 HAR）

```bash
$PY "$SR" login                       # 弹出浏览器 → 你扫码 → 自动收尾
$PY "$SR" login --timeout 600         # 慢慢扫
$PY "$SR" login --fresh               # 换一个全新 profile（旧会话残留时用）
$PY "$SR" login --check-only          # 只验活，不开浏览器
```

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

### 付费资产的铁律（2026-10-26 实测）

payMode 是**版本级**属性（`currentVersion.payMode`：0=免费 1=买断 2=订阅），
金额单位是**分**。三条死路 + 一条活路：

1. **API `submit` 会静默丢弃 payMode** —— 载荷里带上也不收，回读 `pendingVersion.payMode` 恒为 0
2. **`POST /capabilities/{assetId}/pay`（控制台"付费设置"通道）**：
   对 published+payMode=0 返回 `update goods failed`（免费转付费被服务端拒绝）；
   对 draft / pending 同样 `update goods failed`，载荷变体（durationDays/goodsType/goods 数组）全试过，均不收
3. **已过审的免费资产永远变不成付费** —— 与官方 FAQ 一致
4. **活路只有一条：在控制台 UI 里提交时设置付费方式** —— 自动化能做的是把包、
   类目、封面全部放进草稿，最后一步「设置付费 + 提交」必须人点

所以 `upload_and_submit()` 的行为是：带 `pay_mode` 提交 → 回读验证 →
没带上就自动 withdraw 撤回并报错，**绝不留下一个「本应收费却免费」的待审版本**。
草稿已就位时，引导用户去控制台完成最后一步（设付费 → 提交）。

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
代理：release.py login --timeout 600
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
| [troubleshooting.md](./references/troubleshooting.md) | 命令报错、上传被拒、open.workbuddy 请求体需要现场校准 |

## 输出约定

- 结论前置：第一行先说「XX 可发 / 不可发」
- 阻断项必须带**改法**，不能只报错
- 阻断项与警告分开列，警告不阻断
- 收尾一句话，不复述表格
