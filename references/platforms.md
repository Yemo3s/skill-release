# 三平台发布规则与出处

每条规则都标了 **verified / assumed**。这个区分不是洁癖 —— 发布失败的代价不对称，
被拒一次要人工重来，所以宁可多报；但把猜测当成「规矩」写进代码同样危险，
会逼着你去改本来没问题的东西。

- **verified** —— 平台文档原文，或一次真实的拒绝报错。命中即阻断。
- **assumed** —— 文档的间接描述或社区经验。命中只警告，不阻断。

---

## 一、三条通道的性质

| 平台 | 鉴权方式 | 交付形态 | 入口 |
|---|---|---|---|
| GitHub | 令牌（OAuth 设备码取得，DPAPI 加密存本地） | git 仓库 | 复用 `github-ops` 技能 |
| SkillHub | API Key（`skh_…`）**或** 浏览器会话 Cookie | **逐个文件 multipart 上传，不用 zip** | `POST https://api.skillhub.cn/api/v1/community/skills/publish` |
| open.workbuddy | **Cookie 会话**（`credentials: include`），且必须是 `open.workbuddy.cn` 自己的 Cookie | **.zip**，外层目录为 `{slug}/` | Web 控制台上传，底层走预签名直传 |

一句话记法：**GitHub 认令牌，SkillHub 认 Key 或 Cookie，workbuddy 认本站 Cookie。**

---

## 二、SkillHub

### 两条凭据通道（都实测过）

| 通道 | 怎么带 | 实测结果 |
|---|---|---|
| API Key | `Authorization: Bearer skh_…` | 官方 CLI 走的路；需在开发者中心签发 |
| 会话 Cookie | `Cookie: language=zh; skh_token=<JWT>; sid=…` | 浏览器登录态直接可用，**不必去签发 Key** |

取一张 200 的判断标准是 **`GET /api/v1/auth/me` 返回 JSON 用户资料**。

三个容易踩的点，都是实测：

1. **两条通道互斥**：同时带 Cookie 和 `Authorization: Bearer` 会被判
   `{"error":"invalid or expired token"}`。配了 Cookie 就**只发 Cookie**。
2. **JWT 不能当 Bearer 用**：把 `skh_token` 那个 JWT 塞进 `Authorization` → 401。
   服务端按两条独立链路解析，不是一个东西。
3. **域名只能是 `api.skillhub.cn`**：`www.skillhub.cn` / `skillhub.cn` 对
   `/api/...` 一律回 SPA 的 HTML 壳（HTTP 200 + `<!DOCTYPE html>`），看着像成功，
   其实是前端路由兜底。

### 通道细节（verified）

协议从官方 CLI 的安装包中逐字读出。装 CLI 的方式：

```bash
curl -fsSL https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh \
  | bash -s -- --cli-only --no-skills
```

kit 解包后就是两个纯 Python 文件（`skills_store_cli.py` / `skills_upgrade.py`），
本技能的 `pub_skillhub.py` 按它实现了同一套协议，因此**不依赖 CLI 是否装得上**。

| 用途 | 方法 & 路径 |
|---|---|
| 校验身份 | `GET  {host}/api/v1/auth/me`，头 `Authorization: Bearer <key>` 或用 Cookie |
| 发布 | `POST {host}/api/v1/community/skills/publish`，`multipart/form-data` |
| 查 slug 是否可用 | `GET  {host}/api/v1/community/skills/check-slug?slug=NAME` |
| 我的全部技能 | `GET  {host}/api/v1/dashboard/skills` |

`payload` 是 JSON，字段：`slug / version / displayName / summary / description /
tags / license / homepage / changelog`。
`files` 是**重复字段名**，每个 part 一个原始文件，`filename` 用相对路径。

**为什么不能用 zip**：官方 CLI 虽然也接受 zip，但它是先在本地解开再逐个上传。
直接传 zip 会被服务端判为「缺少 frontmatter」——服务端不解压。
本技能因此对 SkillHub **直接逐个文件上传**。

成功返回 HTTP 201，进入审核（`status=pending_review`）。

### 硬性规则

| 规则 | 等级 | 出处 |
|---|---|---|
| frontmatter 必须有 `slug` / `version` / `displayName` | verified | 官方 `skillhub.cn/ai/release.md` |
| `slug` 为 kebab-case，长度 2–128 | verified | `release.md` + CLI 的 `_SLUG_PATTERN` |
| `version` 为合法 SemVer | verified | 同上 |
| **无扩展名文件一律拒绝** | verified | 真实报错：`不允许的文件类型: LICENSE` |
| **根目录 `icon.*` 不能当内容文件传** | verified | 真实报错：`不允许的文件类型: icon.png` |
| 包体积 ≤ 10 MB | verified | 官方 CLI 源码注释：publish 接口 10MB 限制 |
| 图标 PNG 512×512 且 < 500 KB | verified | 上架门槛；**CLI 不传头像**，需发布后在详情页设 `iconUrl` |

### slug 可能被封禁（比「被占用」更麻烦）

`GET /api/v1/community/skills/check-slug?slug=NAME` 会明确回 `reason`：

```json
{"available": false, "reason": "banned",
 "message": "slug 'douyin-downloader' 已被封禁，请更换 slug"}
```

`banned` 与「被他人占用」不同 —— 换什么都不行，只能换一个 slug。
但**技能名不该为了迁就平台域名去改**，所以支持配置映射：

```json
"skillhub": {"slug_map": {"douyin-downloader": "douyin-downloaders"}}
```

源 `SKILL.md` 保持不动，只有 SkillHub 这一路换 slug
（`check` 会在报告顶部标注「slug 不一致」）。临时一次性覆盖用 `--slug`。

查询自己在 SkillHub 上已有的技能与状态：`GET /api/v1/dashboard/skills`
（返回 `slug` / `banned` / `status`）。**发布前先查一次**，
能避免「以为在新建，其实是在更新」或者反过来。

`slug` / `displayName` 属于平台元数据，社区惯例是**不写进源 SKILL.md**，
发布前注入临时副本。本技能照此实现。

### 已通过上传验证的扩展名

`.md` `.py` `.txt` `.json`
（来源：`douyin-downloader` 整包上传通过）

**`.png` 已被证伪，不要当内容文件传。** 官方 CLI 不传头像，图标走平台元数据；
把 `icon.png` 当普通文件塞进 `files` 会整包被拒：

```
400 {"error":"不允许的文件类型: icon.png"}
```

所以本技能对 SkillHub **剔除根目录下的 `icon.*`**，并在发布后用详情页设
`iconUrl`。注意判定用的是**过滤后**的清单 —— 否则 icon.png 会被天天报成
「未验证扩展名」，把真信号淹掉。

> 早先的文档把 `.png` 列进了「已验证」名单，那是从「官方 CLI 上传成功」
> 反推的，而 CLI 本身就不传图标 —— 属于拿一个不含该类型的成功案例去证明
> 该类型可用。这类推断以后一律记为 `assumed`。

`.sh` **未经验证**且被排除出发布包，两条理由：

1. 白名单里没有它，不想再赌一次上传；
2. `uninstall.sh` 是自删除脚本（`shutil.rmtree` 自身目录），过 AI 安全审核风险高。

---

## 三、open.workbuddy

### 通道细节（verified）

从开放平台前端构建产物 `_next/static/chunks/007.js` 读出的真实调用：

```
base      NEXT_PUBLIC_API_BASE_URL，为空即同源 https://open.workbuddy.cn
鉴权      credentials: "include"  ← Cookie 会话，不是 token
额外头    X-Party-Id（入驻主体）、X-Enterprise-Id（企业）；没有可省
401 语义  会话过期 → 前端跳登录页
```

**未登录时的真实行为（实测，这条比文档有用得多）**

前端是 APISIX 3.9.1 网关，未认证的 `/api/v2/...` 请求会被 **302** 到 Keycloak：

```
GET /api/v2/open-platform/parties
  → 302 Location: /auth/realms/copilot/protocol/openid-connect/auth
                  ?client_id=console&scope=openid&response_type=code
                  &redirect_uri=…/.apisix/redirect
```

会话来路是 OIDC 授权码模式，`redirect_uri` 后缀是 `.apisix/redirect`。几个直接后果：

1. **跟随重定向会拿到 HTTP 200 + 登录页 HTML**。所以「状态码 200 就算鉴权通过」
   是错的 —— 这正是一个完全没登录的会话被报成「通过」的原因。判据必须是
   **200 且是 JSON**。
2. **Cookie 必须是 `open.workbuddy.cn` 自己的**。Cookie 名确实叫 `session`，
   格式是 `<随机>|<过期时间戳>|<HMAC>`；但 `cloud.tencent.com` /
   `console.cloud.tencent.com` 用的是同名同格式的 `session`，域名不同，
   **拿过来不认**。这两个站的 `qcloud_visitId` / `qcloud_from` / `trafficParams` /
   `sensorsdata2015jssdkcross` 出现在 Cookie 里，基本可以判定拿错了站。
3. **服务端会主动作废可疑会话**：带一个验证不过的 `session` 请求，响应里会
   `Set-Cookie: session=<新值>` 并 `Set-Cookie: session_2=; Max-Age=0`。
   所以看到 `session_2` 被清掉，等于服务端说「这份会话我不认」。

上传链路 —— 能力（capabilities）是总称，技能是其中一类：

| 步骤 | 端点 | 说明 |
|---|---|---|
| 1 | `POST /api/v2/open-platform/common/uploads/presign` | 取预签名地址 |
| 2 | `PUT  {uploadUrl}` | 直传对象存储，`Content-Type` 用 zip 的 mime |
| 3 | `POST /api/v2/open-platform/capabilities/init-upload` | 建资产，返回 `assetId` |
| 4 | `POST /api/v2/open-platform/capabilities/complete-upload` | 完成 |
| 5 | `POST /api/v2/open-platform/capabilities/submit` | **进审核队列** |

只读端点（`probe` 命令用的就是这几个）：

```
GET  /api/v2/open-platform/parties
GET  /api/v2/open-platform/capabilities/categories?capabilityType=skill
POST /api/v2/open-platform/capabilities/summary
POST /api/v2/open-platform/capabilities/list
```

### 硬性规则

| 规则 | 等级 | 出处 |
|---|---|---|
| 目录层级「根目录 / 二级目录 / 文件」，含外层目录最多 3 段 | verified | 真实报错：`目录层级超限 — Skill 包仅支持两级目录结构` |
| frontmatter 必须有 `description` / `description_zh` / `description_en` / `version` / `author` | verified | `open.workbuddy.cn/docs/skill` |
| 只收 `.zip`，平台自动开包解析 | verified | 同上 |
| 包体积 ≤ 3 MB | **assumed** | 官方文档未给数值；社区实录称 skill 包 ≤ 3MB |
| 二级目录名为 `references` / `scripts` / `templates` | **assumed** | 文档列出的名字；实测用别的名字未报错 |

**层级怎么数**：`douyin-downloader/scripts/douyin.py` 是 3 段，**合规**；
`douyin-downloader/scripts/_sign/sm3.py` 是 4 段，**超限**。
所以规则引擎判定的是「相对技能根目录的路径段数 ≤ 2」。

### 待现场确认的部分

第 1 / 3 / 4 / 5 步的**请求体字段名**由页面组件拼装，而组件在登录后才加载的
路由分块里，未登录抓不到。因此这三个 body 做成可覆盖配置
（`workbuddy.payloads` 段），首跑用 `probe` + 一次真实调用校准，
把服务端返回的字段校验错误回填即可，**不需要改代码**。
见 [troubleshooting.md](./troubleshooting.md) 的「现场校准」。

**当前状态：卡在前置条件上** —— 手上这份 Cookie 经实测属于别的域名
（302 → Keycloak 登录页），所以第 1 步还没真正跑到。校准必须在
「`probe` 四个端点全是 200 JSON」之后做，否则字段名会被登录页的
HTML 带偏。

---

## 四、GitHub

委托给 `github-ops` 技能，本技能只做编排。那里已经解决了三件难事：

1. 令牌用 Windows DPAPI 加密，存在技能目录之外；
2. 推送前自动做密钥扫描，命中高危中止；
3. 注入令牌用 `-c remote.origin.url=` 只在本次生效，不污染 `.git/config`。

| 规则 | 等级 | 出处 |
|---|---|---|
| 单文件 ≤ 100 MB | verified | GitHub 硬上限 |
| 推送前扫描敏感内容 | verified | `github-ops` 默认行为 |
| **提交身份必须是 noreply 邮箱** | verified | 真事故：真名 + 公司邮箱进了公开仓库 |

注意：`ghx.py git push` **不做 commit**，只负责把已有提交推上去。
本技能先 `git init / add / commit`，否则会推一个空仓库。

### 提交身份（GitHub 独有的风险）

**GitHub 是三平台里唯一会把作者身份写进产物的地方。** SkillHub 与
open.workbuddy 只收文件内容，GitHub 连「是谁写的」一起收，而且写在
commit 元数据里 —— 事后改代码删不掉。

三条必须同时成立：

1. 身份**无条件**写仓库级（`config --local`）。`git config user.email`
   不带 `--local` 会回退读全局配置，而开发机全局通常是真名 + 公司邮箱。
2. 清掉 `GIT_AUTHOR_*` / `GIT_COMMITTER_*` —— 环境变量优先级高于 config。
3. commit 后 + push 前各断言一次，白名单判据 `@users.noreply.github.com`。

这三条只管**本次**发布。存量（已推上去的、用别的工具推的）要用 `audit` 扫：

```bash
$PY "$SR" audit
```

⚠️ `force push` **删不掉旧 commit** —— 旧 SHA 通常仍可直访（GitHub 不立即
回收不可达对象）。要确保清除只能删库重建。
**重命名仓库同样不算清除** —— 旧名重定向到新名，commit 原样保留。
详见 [troubleshooting.md](./troubleshooting.md)。

---

## 五、两个平台卡的维度不同（这是本技能存在的根本原因）

| 平台 | 校验维度 | 真实触发过的报错 |
|---|---|---|
| SkillHub | **文件扩展名白名单** | 不允许的文件类型: LICENSE |
| open.workbuddy | **目录层级深度** | 目录层级超限 — 请移除子目录嵌套 |

一个卡扩展名，一个卡层级，**两边都得过**。靠人工记必然返工。
本技能的打包脚本里对这两条各有一条断言，把平台规则变成本地可执行检查。
