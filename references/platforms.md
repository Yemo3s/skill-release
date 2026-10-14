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
| 图标 PNG 512×512 且 < 500 KB | verified | 上架门槛；官方 CLI 不传头像，图标走**专用通道**（见下） |

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

### 已被实测拒绝的扩展名（命中即本地 block）

| 扩展名 | 报错原文 | 出处 |
|---|---|---|
| `.tsv` | `400 {"error":"不允许的文件类型: reference/icon-map.tsv"}` | 2026-10-13 `probably-stolen` 发布实测 |

`.tsv` 不在白名单里。同内容的制表符文本**改名为 `.txt` 即可通过** ——
实测路径：把 `reference/icon-map.tsv` 改成 `reference/icon-map.txt`
（并同步代码/文档里的引用）后重发即成功。规则见 `rules.py` 的 `REJECTED_EXT`。

**`.png` 已被证伪，不要当内容文件传。** 官方 CLI 不传头像，图标走平台元数据；
把 `icon.png` 当普通文件塞进 `files` 会整包被拒：

```
400 {"error":"不允许的文件类型: icon.png"}
```

所以本技能对 SkillHub **剔除根目录下的 `icon.*`**（不当内容文件传），
但图标本身有**专用上传通道**，v1.4.4 起发布时自动走：

```
POST /api/v1/community/skill-icons/upload    multipart，字段名 file
  → 200 {"iconUrl": "https://skillhub-….cos.accelerate.myqcloud.com/skill-icons/uploads/<uid>/<hash>.png"}
```

出处：skillhub.cn 前端 bundle（`uploadSkillIcon(file)`，2026-10-12 反解）。
拿到 iconUrl 后**随 publish payload 一起发**（payload 支持 `iconUrl` 字段，
官网表单同一来源）—— 已实测 v1.4.4 发布成功（skillId 277816）。
两个实测事实：① 官方 CLI 从不传 iconUrl，所以 CLI 发的技能全部无图标；
② **已上架技能补图标的唯一办法是再 publish 一版**（列表/详情接口均无
单独更新图标的端点）。

> ⚠️ 2026-10-12 复核实测修正：**「审核中列表仍显示 NULL」并不成立** ——
> v1.4.5 提交后，公开接口 `GET /api/v1/users/<handle>/skills` 的 `iconUrl`
> **立即**就是新图 URL（md5 与本地一致），且审核通过后 `iconAuditStatus`
> 字段直接消失。所以「前台看着还是旧图」**基本是浏览器/CDN 缓存**，
> 不要据此判定「没传上」—— **判据是下载远端图比 md5**，不是看接口有没有 URL。

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
   **拿过来不认**。
   ⚠️ 但**别用「Cookie 里有没有 `qcloud_visitId` / `sensorsdata2015jssdkcross` /
   `session_2`」来判断拿错站** —— 这些是**同源埋点**，出现在本站请求里完全正常
   （实测有效会话里就有）。唯一可信判据是**请求能否过鉴权**：`probe` 四端点
   全 `200 且 JSON` 才算通。
3. **会话约两天过期**：过期后请求 302 到 Keycloak，此时重新导 HAR 走
   `creds --from-har` 续期即可，不必怀疑 Cookie 结构有问题。

上传链路 —— 能力（capabilities）是总称，技能是其中一类。
**2026-10-10 从前端 bundle 反解 + 真机实测确认的真实契约**：

| 步骤 | 端点 | 说明 |
|---|---|---|
| 1 | `POST …/capabilities/init-upload` | `{capabilityType,fileName,fileSize,mimeType}` → `data.{uploadId,objectKey,uploadUrl,expireAt}` |
| 2 | `PUT  {uploadUrl}` | 直传 COS，`Content-Type` **必带**（少了签名不匹配） |
| 3 | `POST …/capabilities/complete-upload` | `{uploadId, assetId?}` → `data.{uploadToken,…}` |
| 4 | `POST …/capabilities/submit` | `{capabilityType,action,assetId,uploadToken,clientToken,fields}` → 进审核 |

> ⚠️ 早前文档把第 1 步写成「`/common/uploads/presign` 取预签名」—— **这是错的**：
> presign 是**封面/头像**的通道（要 `scene` 字段），拿它传能力包只会得到
> `invalid request parameters`。反过来，封面**只能**走 presign，见下节。
> 另：响应统一是 `{code,data,msg}` 信封，**必须剥 `data`**，否则取不到 `uploadUrl`。

只读端点（`probe` 命令用的就是这几个）：

```
GET  /api/v2/open-platform/parties
GET  /api/v2/open-platform/capabilities/categories?capabilityType=skill   （市场类目）
GET  /api/v2/open-platform/capabilities/business-categories               （服务类目树）
GET  /api/v2/open-platform/capabilities/{assetId}                         （详情，含 fields）
GET  /api/v2/open-platform/capabilities/{assetId}/reasons                 （驳回原因）
GET  /api/v2/open-platform/messages                                       （通知流水）
POST /api/v2/open-platform/capabilities/summary
POST /api/v2/open-platform/capabilities/list
```

**状态变更端点**（2026-10-14 枚举，**非只读**）：

```
POST /api/v2/open-platform/capabilities/{assetId}/withdraw   （撤回待审 → draft，body 可为空）
```

- `withdraw` 是把「已提交待审」退回 `draft` 的**唯一**通路。存在的价值：
  `publish` 遇到资产在审核中会整体跳过，于是在「提交后才发现字段填错」时
  原本只能干等驳回；现在可以 `release.py withdraw <目录>` 主动退回改完再提。
  **撤回不改版本号**，重提可用同版本或更高版本。
- 路径别写错：实测 `/capabilities/{id}/versions/{versionId}/withdraw`、
  `/capabilities/{id}/cancel-review`、`DELETE /capabilities/{id}/versions/{id}`
  **全是 404**，只有顶层的 `/capabilities/{id}/withdraw` 通。
- 同级还有 `update` / `delist` / `publish`（从前端 controller 的动作枚举读出，
  与 withdraw 并列）；`update` 能给已存在资产补字段，**未实测**，
  别在审核中的资产上赌它。

**封面/图标通道**（与能力包完全分开，2026-10-12 从前端 bundle 反解）：

```
POST /api/v2/open-platform/common/uploads/presign
     {"scene":"capability_cover","fileName":"icon.png","fileSize":N,"mimeType":"image/png"}
  → data.{uploadUrl, objectKey, previewUrl}
PUT  <uploadUrl>   body=PNG 字节，Content-Type: image/png
  → objectKey 填进 submit / update 的 fields.common.coverObjectKey
```

要点：① `scene` 来自前端 imageType 枚举（chunk 79908，另有 avatar /
buddy_app_icon 等）；② **没有 complete 步骤**，PUT 成功即生效；
③ 平台硬约束 512×512 正方形、≤ 512000 字节（前端 CapabilityCover 规则）；
④ 资产已存在时可走 `POST /capabilities/{assetId}/update` 补字段
（controller 里与 delist/withdraw/publish 并列），body 结构按 submit 的
fields 推断，**未实测** —— 别在审核中的资产上赌它。
⑤ **没有真封面时，列表接口的 `coverUrl` 返回字面量 `letter:auto`**
（平台用它生成占位字母图）—— 这才是「没有封面」的判据，
不是「URL 还没刷新」。2026-10-12 实测 `upload_cover()` 一次跑通，
`coverUrl` 随即变成 `public/common/capability_cover/<uid>/<uuid>.png`。
⑥ 两端的图标是否**真的生效**，唯一可靠判据是**把远端图下载下来比 md5**，
不要只看接口有没有返回 URL。

**类目（两套体系，`submit` 时都要给）** —— 2026-10-14 定稿（被驳回两次换来的）：

| 字段 | 体系 | 取值 | 空着/写错会怎样 |
|---|---|---|---|
| `fields.common.categories` | **服务（资质）类目** | `[{"categoryId":"tools:office"}]` ← **资质为空时不要带这个键** | **审核驳回**：「服务类目审核：无资质类目字段，需重新选择对应类目」 |
| `fields.common.marketCategories` | 市场展示类目 | `["development-tools"]`（连字符 id） | 前台分类缺失；控制台要求 1..3 个 |

- ⚠️ **空资质写成 `qualifications: []` 同样会被驳回**，话术与「没选类目」一模一样。
  控制台源码就是 `a.length > 0 && (t.qualifications = a)` —— 空资质时**整键不发**。
  详情接口读回来永远归一化成 `"qualifications": []`，**看不出当初发了什么**。
- **服务类目枚举的唯一权威来源**：`GET /capabilities/business-categories`
  （前端 `capabilityPort.businessCategories()` 用的就是它）。
  条目形如 `{"id":"tools:office","name":{"zh":"办公"},"qualificationRequired":false}`。
  文档站 <https://open.workbuddy.cn/docs/service-categories-individual>
  只有中文名，没有 id；`GET /capabilities/categories` 给的是**市场类目**，另一套。
- 个人主体「**工具**」大类下的三级类目（`qualificationRequired` 全为 false）：
  `tools:bookkeeping` 记账 / `tools:calendar` 日历 / `tools:weather` 天气 /
  **`tools:office` 办公** / **`tools:image_editing` 图片处理** /
  `tools:calculator` 计算器 / `tools:price_comparison` 报价·比价 /
  `tools:info_lookup` 信息查询 / `tools:booking` 预约·报名 /
  `tools:health_management` 健康管理 / `tools:memo` 备忘录 ——
  **没有「开发工具」**，所以技术类技能一般落 `tools:office`。
  全部大类：logistics / education / transportation / daily_life / catering /
  travel / tools / business_service / sports。
- 配置写法（凭据文件 `workbuddy` 段）：`"categories": ["tools:office"]`
  （纯字符串会自动包装）、`"market_categories": ["development-tools"]`。
  代码侧见 `pub_workbuddy.normalize_categories()`；**空类目会在本地直接抛错**，
  不会等平台驳回。
- 想抄填法：`GET /capabilities/{assetId}` 的 `data.fields.common`
  能看到已过审资产的真实写法。
- 驳回原因：`GET /capabilities/{assetId}/reasons`
  → `data.{rejectStage, reviewDetail:{reviewNote, reviewedAt}}`；
  平台通知流水见 `GET /messages`。

> 三个易误判点：① `GET /capabilities/categories` 的 13 个是**市场类目**，
> 不是服务类目，拿它填 `categories` 过不了审；② 空资质发成 `qualifications: []`
> 照样被打回（见上）；③ 提交后要看 `pendingVersion.version` 才同步成包里的
> 版本号，draft/rejected 阶段详情里显示的是旧版本号（别误会成「包没生效」）。

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

第 1 / 3 / 4 / 5 步的请求体字段名**已于 2026-10-10 全部实测确认**
（见 `upload_and_submit()` docstring），`payloads` 覆盖配置保留但已不需要。
「校准必须在 probe 全 200 之后做」这条纪律仍然有效 —— 会话失效时拿到的
是登录页 HTML，照着它回填字段只会全错。

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
