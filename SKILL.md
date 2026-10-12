---
name: skill-release
display_name: 技能发布管家
display_name_en: Skill Release Manager
description: "把本地技能一句话发到 GitHub / SkillHub / open.workbuddy 三处，并把两个平台的校验规则前置成本地断言。TRIGGER: 用户说「发布这个技能」「上架 skill」「更新到 skillhub」「推到 open.workbuddy」「同步到 GitHub」「技能发布报解析失败」「目录层级超限」「不允许的文件类型」「slug 已被封禁」「slug 冲突」「图标不让传」，或给出技能目录要求分发。DO NOT TRIGGER: 只是新建技能骨架（属于 skill-scaffold）；只是查找/安装别人的技能；只是普通代码提交。"
description_zh: 把技能一次分发到三个平台：GitHub 仓库、SkillHub 市场、open.workbuddy。同一份内容派生三种形态，平台要的字段（slug / displayName / author）自动投影到临时副本而不污染源文件；发布前跑规则引擎，把「无扩展名文件」「根目录图标」「目录层级超限」「slug 被封禁」这类真实被拒过的坑拦在本地。SkillHub 支持 API Key 与会话 Cookie 两条凭据通道。默认演练，加 --yes 才真发。
description_en: "Release a skill to three platforms in one command: a GitHub repo, the SkillHub marketplace, and open.workbuddy. One source of content, three packaging shapes; platform-specific frontmatter (slug / displayName / author) is projected into a staging copy instead of polluting the source. A rule engine blocks locally the exact failures platforms rejected before — extensionless files, root-level icons, over-deep paths, banned slugs. SkillHub accepts either an API key or a browser session cookie. Dry-run by default; --yes to actually publish."
version: 1.4.5
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

### 凭据

```bash
$PY "$SR" creds                                            # 查看（脱敏）
$PY "$SR" creds --set skillhub.key=skh_xxxx
$PY "$SR" creds --set skillhub.cookie='skh_token=<JWT>; sid=<sid>'
$PY "$SR" creds --set workbuddy.cookie='<浏览器 Cookie>'
$PY "$SR" creds --set author=mo
$PY "$SR" creds --set github.owner=<GitHub 用户名>
$PY "$SR" creds --from-har ~/Downloads/wb.har   # open.workbuddy 用 HAR，别手抄
```

SkillHub 有 Key 和 Cookie 两条路，**配一条就够**；两条都配时以 Cookie 优先。
open.workbuddy 的 `session` 登录后约 4 KB，手工复制会被截断 ——
用 `--from-har` 从浏览器导出的 HAR 里抽，它会先自检、再验活，**通过才写盘**。
⚠️ 导 HAR 前**必须**在 DevTools 里勾上「允许生成包含敏感数据的 HAR」
（Chrome 130 起默认导出「已清理」档，Cookie 会被剥光，症状酷似「没登录」）。

### 探测 open.workbuddy

```bash
$PY "$SR" probe
```

只读：依次打 `/parties`、`/capabilities/categories`、`/capabilities/summary`、
`/capabilities/list`，报告会话是否有效、各端点是否可达。

打网络之前会先跑一遍 **Cookie 完整性自检** —— 「Cookie 是坏的（复制被截断）」
和「没登录」响应长得一模一样（都是 302 → Keycloak），但 Cookie 结构分得清：
本站 `session` 固定是 4 段 `<22位id>|<10位过期>|<payload>|<27位HMAC>`，
登录后约 4 KB，**手工复制几乎必然被截断**，末尾 HMAC 一丢就 302。
长值请走 `Network → 右键 → 复制 → 将所有内容另存为 HAR（包含敏感数据）`，
别走剪贴板；只点「导出 HAR」不勾上面那个开关的话，导出的 HAR 里**一个 Cookie 都没有**。
详见 [references/troubleshooting.md](./references/troubleshooting.md)。

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
→ `{iconUrl}` → publish payload 直带 `iconUrl`（**图标只能随 publish 更新**，
漏了详情页就一直裸奔）；open.workbuddy：
`POST /api/v2/open-platform/common/uploads/presign`（`scene: "capability_cover"`，
**无 complete 步骤**）→ objectKey → `fields.common.coverObjectKey`。
GitHub 不消费图标。两张图都不合规会在发布时报错（尺寸/大小前置断言）。

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
