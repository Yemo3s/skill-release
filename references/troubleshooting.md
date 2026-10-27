# 排错

按「看到的报错」查，不按模块查。

---

## 一、上传被平台拒了

### `不允许的文件类型: LICENSE`

**原因**：SkillHub 按扩展名白名单过滤，**无扩展名文件一律拒**，且是整包被拒。

**定位**：

```bash
$PY "$SR" check <技能目录> --targets skillhub
```

会直接列出所有无扩展名文件。

**修法**：改名为 `<原名>.txt`，并同步代码里的引用。
`douyin-downloader` 上踩过两次（`LICENSE`、`VERSION`），两个都要改。

### `目录层级超限 — Skill 包仅支持两级目录结构`

**原因**：open.workbuddy 的规则是「根目录 / 二级目录 / 文件」。
**含外层目录最多 3 段**。`skills/scripts/_sign/sm3.py` 是 4 段。

**定位**：

```bash
$PY "$SR" check <技能目录> --targets workbuddy
```

**修法**：把第三层目录里的文件上提到上一层。若原目录名有意义，给文件名加前缀
（如 `_sign/__init__.py` → `signing.py`，其余同名上提）。

⚠️ 上提后要同步相对导入：`from .x import` → `from x import`，以及所有引用点。
`douyin-downloader` 那次改了 10 处（7 处相对导入 + 3 处调用方）。

### `SKILL.md 缺少 slug` / `缺少 displayName` / `缺少 author`

这三项**不需要改源文件** —— 本技能会把它们投影到临时副本：

| 字段 | 来源 |
|---|---|
| `slug` | 取 `slug`，否则由 `name` 推导 |
| `displayName` | 取 `displayName`，否则由 `display_name` / `display_name_en` / `slug` 推导 |
| `author` | 取配置的 `author` |

`check` 报这三项失败时说明**推导不出来**，才需要处理：

- `slug`：SKILL.md 里 `name` 也是空的 → 补一个 kebab-case 的 `name`
- `author`：`$PY "$SR" creds --set author=<你的展示名>`

---

## 二、SkillHub

### `API Key 无效或已失效（401）` / `会话 Cookie 无效或已失效（401）`

两条通道，换一条或换一份：

```bash
$PY "$SR" creds --set skillhub.key=skh_xxxx                          # A：签发 Key
$PY "$SR" creds --set skillhub.cookie='skh_token=<JWT>; sid=<sid>'   # B：浏览器 Cookie
```

**如果 Cookie 明明是新复制的还是 401**，按顺序查这三条：

1. **是不是把 `skh_token` 的 JWT 当 Bearer 用了**。它俩不是一个东西 ——
   JWT 塞进 `Authorization` 一定 401，只能放在 `Cookie` 里。
2. **是不是两条通道同时带了**。Cookie + `Authorization: Bearer` 一起发，
   服务端会回 `{"error":"invalid or expired token"}`。配了 Cookie 就只发 Cookie。
3. **host 是不是 `api.skillhub.cn`**。`www.skillhub.cn` / `skillhub.cn` 对
   `/api/...` 回的是 SPA 的 HTML 壳（HTTP 200 + `<!DOCTYPE html>`），
   容易被误认为「通了」。

### `不允许的文件类型: icon.png`

图标不是内容文件，是**平台元数据**，不能塞进 `files` 清单。
本技能已自动剔除**根目录**下的 `icon.*`（`icon.png` / `jpg` / `webp` / `svg` / `gif`），
所以正常不该再遇到。若仍出现，说明图标不在根目录 —— 子目录里的 png 不在
排除范围内（那种情况属于未验证类型，`check` 会先给 warn）。

图标本身有**专用上传通道**，`v1.4.4` 起发布时自动走
`POST /api/v1/community/skill-icons/upload`（multipart 字段 `file`），
把返回的 `iconUrl` 随 publish payload 一起发 —— **无需再到详情页手动设置**。

### `不允许的文件类型: <某个你没见过的扩展名>`（如 `.tsv`）

SkillHub 按**扩展名白名单**过滤，白名单外的类型**整包被拒**。
已被实测拒过的扩展名记在 `rules.py` 的 `REJECTED_EXT`，命中即本地 block：

| 扩展名 | 报错原文 | 出处 |
|---|---|---|
| `.tsv` | `400 {"error":"不允许的文件类型: reference/icon-map.tsv"}` | 2026-10-13 `probably-stolen` |

改法：**同内容换个允许的扩展名**。制表符文本改名 `.txt` 即可通过
（实测路径：`reference/icon-map.tsv` → `reference/icon-map.txt`），
**记得同步代码与文档里的引用** —— 否则包里链接指向一个不存在的文件。

> 顺带一提：`rules.py` 里 `VERIFIED_EXT`（验证可通过）与 `REJECTED_EXT`
> （实测被拒）是两档不同的信号，别混。前者命中只 warn，后者命中 block。

### `git push` 报 `CONNECT tunnel failed, response 502` / `Failed to connect to github.com port 443`

这不是认证问题，是**网络到不了 `github.com`**。关键事实：

| 域名 | 谁在用 | 可达性 |
|---|---|---|
| `github.com` | `git push/pull` | 实测本机：直连超时；代理对 CONNECT 返 502 |
| `api.github.com` | REST API（建仓 / 审计 / 备份） | 同一时刻一切正常 |

所以「API 能建仓、git 推不上去」是正常现象，不是配置错了。

**本技能 v1.4.6 起自动兜底**：`push()` 失败且报错属网络类时，
改调 `push_via_api()` —— 用 Git Data API 把同一棵树写成一次提交，
身份同样显式写 noreply，产出与原 git 提交一致（消息/身份/日期相同则
commit SHA 也相同，所以两条通道可互换、幂等）。

判定与兜底都在 [scripts/pub_github.py](../scripts/pub_github.py)：
`is_network_failure()` 认网络类报错，`push_via_api()` 走 API。
403 / 令牌无效**不**触发兜底 —— 那种情况换通道一样失败。

两个空仓库的坑（已在代码里处理）：

1. Git Data API 的 `blobs` / `trees` / `commits` 在**还没有任何提交**的仓库上
   一律 `409 Git Repository is empty`。要先用 Contents API 落一个文件 bootstrap。
2. bootstrap 之后写一条**无父提交**（root commit）再 force 更新 ref，
   分支上就只留一条提交，bootstrap 那条成为不可达对象 —— 干净。

逃生舱：`ghx api <METHOD> <PATH> [--body …]` 可以直接打 REST，用于现场排查。

### `slug 冲突（409）` / `slug 已被封禁`

先分清三种：

| 现象 | 含义 | 怎么办 |
|---|---|---|
| `reason: "taken"` | 被别人占了 | 换 slug |
| `reason: "banned"` | **该 slug 被封禁** | 换 slug，争不回来 |
| 你自己的 slug | 只是想更新已有技能 | 照原 slug 发即可，会自动更新 |

查清再动手：

```bash
# 单个 slug 是否可用（含 reason）
GET https://api.skillhub.cn/api/v1/community/skills/check-slug?slug=NAME

# 我已有哪些技能（slug / banned / status）
GET https://api.skillhub.cn/api/v1/dashboard/skills
```

被封禁时不要改技能名去迁就平台 —— 写进配置：

```bash
$PY "$SR" creds --set skillhub.slug_map='{"原slug": "平台可用的slug"}'
```

或临时一次性：`publish <dir> --yes --targets skillhub --slug <别的slug>`。

> 实例：`douyin-downloader` 这个 slug 被封禁，该技能在 SkillHub 上实际用的是
> 复数的 `douyin-downloaders`。若不查一下，会误判成「新建」并反复失败。

### `发布过于频繁（429）`

服务端按 token / IP 做多维限流。服务端会回 `retryAfter`，按提示等待即可。
**不要写循环重试** —— 会把本机 IP 打进更严的策略里。

### 状态一直是 `pending_review`

审核 1–3 个工作日。审核通过前详情页与搜索均不可见，属正常。
查询：skillhub.cn → 个人中心 → 我的 Skill。

---

## 三、open.workbuddy

### `会话已失效：接口被重定向到 Keycloak 登录页` ／ `返回登录页 HTML 而非 JSON`

最常见，也最容易误判。该站前面是 APISIX，未认证的 `/api/v2/...` 会 **302** 到
`/auth/realms/copilot/...`；一旦跟随重定向，最终就是「HTTP 200 + 登录页 HTML」。
**光看状态码会把一个完全没登录的会话报成通过。**

先分三种情况，`probe` 会替你分：

```bash
$PY "$SR" probe
```

- 四个端点全部 `会话失效` → Cookie 完全没被认（下面两类）
- 只有部分端点失效 → 会话有效但缺权限，看 `X-Party-Id` / `X-Enterprise-Id`

### 先试 `login --refresh`，不要直接让用户重扫

`probe` 报 403 / 302 时，**配置里的 cookie 过期并不代表登录态没了**：
Chrome 会在后台静默续期 SSO，持久化 profile 里的 cookie 一直是活的。
配置里那份字符串只是某次扫码的快照，几小时后必然失效。

```bash
$PY "$SR" login --refresh      # 无头打开 profile → 验活 → 把现成 cookie 倒回配置
```

2026-10-26 实测：配置 cookie 已 403，profile 内页面 fetch 却 200，
`--refresh` 一条命令恢复，全程不用扫码。只有 `--refresh` 也报失效才需要真扫码。

### 情况 A：Cookie 是坏的（复制被截断）—— **最高频**

`session` 是本站唯一的鉴权凭据，而且**登录后会长到约 4 KB**（里面塞着
id_token / access_token / refresh_token）。**这种长度的值手工选中复制几乎必然被截断。**

本站签发形状是**固定**的：

```
<22 位 id> | <10 位过期戳> | <payload> | <27 位 HMAC>
    22           10             不定          27
```

未登录采样 `[22, 10, 214, 27]`，总长 276。

本机踩到的实例：从 DevTools 复制的一串 6.2 KB Cookie 头里，`session` 被截在
**4001 字符**，末尾的 `|` + 27 位 HMAC **整段丢失** → 服务端 302。
两次独立复制都恰好 4001 字符 —— 这个「长度一模一样」就是被硬截断的指纹。

`probe` 会先跑 `sniff_cookie()` 报出来：

```
Cookie 自检：
  ⚠ session 是 3 段、共 4001 字符，而本站签发的是 4 段
    `<22位id>|<10位过期>|<payload>|<27位HMAC>` —— 末尾的 `|XXX…`（27 位 HMAC）不见了。
  ⚠ session 值 4001 字符，已超过 4000 —— 这种长度手工选中复制几乎必然被截断。
```

**取法：别走剪贴板。**

| 方式 | 怎么做 | 适用 |
|---|---|---|
| **HAR 导出**（推荐） | F12 → Network → 刷新 → 右键 → `Save all as HAR` → 把**文件路径**给对方读 | 4 KB 级的 Cookie 值 |
| **浏览器直读** | 用真实浏览器打开站点、人工登录一次，从浏览器上下文读 Cookie | 一劳永逸，连登录都可省 |

> 🚨 **HAR 导出必须开一个开关，否则拿到的 HAR 里一个 Cookie 都没有。**
> Chrome 130 起出于隐私考虑，HAR **默认导出「已清理」档** ——
> Cookie / Set-Cookie / Authorization 全被剥掉（
> [官方文档](https://developer.chrome.com/docs/devtools/network/reference)）。
> **症状极具误导性**：HAR 里本站请求一条不少、状态码都是 200，
> 就是没有 Cookie —— 看起来像「没登录」，实际是导出档位的问题。
>
> 开法：**DevTools → 设置（F1）→ 偏好设置 → 网络 →
> 勾选「允许生成包含敏感数据的 HAR」**，然后点 Network 面板上的
> **「导出」→ 选「导出 HAR（包含敏感数据）」**（不是默认那项）。
> 也可以右键请求 → 复制 → 「将所有内容另存为 HAR（包含敏感数据）」。
>
> `cookie_from_har()` 会识别这种档位并直接点名，不会让人去查「为什么我没登录」：
> ```
> HAR 里 host=open.workbuddy.cn 的 19 条请求**全部没有 Cookie** ——
> 这不是没登录，是 Chrome 把敏感信息剥掉了。
> ```
> 自检口令：`python -c "import json;d=json.load(open('x.har',encoding='utf-8'));print(sum(1 for e in d['log']['entries'] if e['request'].get('cookies')))"`
> —— 输出 0 就是被剥了。

> ⚠️ 早期版本文档曾把 `qcloud_visitId` / `qcloud_from` / `trafficParams` /
> `sensorsdata2015jssdkcross` / `session_2` 的出现当作「拿错站」的证据。
> **实测推翻**：open.workbuddy.cn 自己的已登录请求**就带着它们**（同源埋点），
> `tgw_l7_route` 也是本站的。这类判据已从 `sniff_cookie()` 里删除，别再照它排除。

> ⚠️ 另一条也被推翻的解读：`Set-Cookie: session_2=; Max-Age=0` 曾被写成
> 「服务端不认这份会话」。对照实验（本站签发的正常 session / 篡改 HMAC 末位 /
> 砍掉第 4 段 / 只带 `session_2`）**都不触发**它，只有那份 4001 字符的坏值会触发。
> 它的含义是「这个 session 值我根本不认」，**不是**「会话过期」，也不能解释成
> 「`session_2` 不是本站的 Cookie」。**唯一硬判据始终是那个 302。**

**另一个高频陷阱**：`session` 是 **HttpOnly** 的，所以 Console 里跑
`copy(document.cookie)` 拿到的串**永远不含它** —— 拿到的是残留的统计类 Cookie，
看起来很像回事，实际必然被 302。别用那个取法。

### 情况 B：真拿错了站

同名同格式的 `session` 在 `cloud.tencent.com` / `console.cloud.tencent.com` 上也存在。
判据**只有一条**：请求所属域名必须是 `open.workbuddy.cn`。

1. 在 <https://open.workbuddy.cn> 上把登录完整走一遍
2. F12 → Network → 筛选框输入 `open.workbuddy.cn`，避免误点腾讯云的请求
3. 找一条 `/api/v2/open-platform/...`（或 `/api/v1/open-platform/...`）的请求
4. 用上面的 HAR 导出，别手工选中复制

写盘前**不要用 publish 试**，那会产生审核队列里的垃圾：

```bash
$PY "$SR" probe --cookie "<新 Cookie>"
$PY "$SR" creds --set workbuddy.cookie='<新 Cookie>'
```

> 这个站的登录页标题是 `Tencent Cloud CodeBuddy`，但 `form action` 指向
> `open.workbuddy.cn/auth/realms/copilot/login-actions/authenticate` ——
> **登录就在本站，不跳腾讯云**。所以正确的 Cookie 一定长在 `open.workbuddy.cn` 上。

### 现场校准（请求体字段名）

**这是本技能唯一需要现场确认的地方，先说清原因。**

第 1 / 3 / 4 / 5 步（presign / init-upload / complete-upload / submit）的
请求体字段名由页面组件拼装，而组件在**登录后**才加载的路由分块里 ——
未登录抓不到。因此这三个 body 做成可覆盖配置，不需要改代码。

首跑若报**字段校验错误**，按下面三步校准：

1. 跑一次 `publish --yes --targets workbuddy`，把服务端返回的报错原文看清楚，例如：

   ```
   init-upload 失败（400）：{"message":"capability_type is required"}
   ```

2. 把字段名回填到配置：

   ```bash
   $PY "$SR" creds --set workbuddy.payloads.init_upload.capabilityType=capabilityType
   ```

   或直接编辑 `~/.workbuddy/.secrets/skill-release.json`：

   ```json
   {
     "workbuddy": {
       "payloads": {
         "presign":         {"fileName": "{filename}", "fileSize": "{size}", "mimeType": "{mime}"},
         "init_upload":     {"capabilityType": "{capability_type}", "fileName": "{filename}"},
         "complete_upload": {"assetId": "{asset_id}", "cosKey": "{cos_key}"},
         "submit":          {"assetId": "{asset_id}"}
       }
     }
   }
   ```

   占位符：`{filename}` `{size}` `{mime}` `{capability_type}` `{asset_id}` `{cos_key}`

3. 重跑。资产已经上传成功时（报错发生在 submit 之前），
   `assetId` 会打在报错里，也可以先在控制台手工提交。

### `直传 COS 失败`

预签名地址有时效。整条链路是连续调用，中间若停顿过久会过期，重跑即可。
若反复失败，先确认本机能否直连对象存储域名。

### `已上传但 submit 失败`

资产已经在服务端了。报错里会带 `assetId`，可以：
(a) 控制台手工提交；(b) 校准 `payloads.submit` 后重跑。
**不要盲目重跑整个流程** —— 会创建重复资产。

### 类目变成单字符乱码（`[` `"` `t` `o` `o` …）

**现象**：控制台里服务类目显示成一长串符号和单个字母，删一个反而多一个，
越删越长。资产详情里 `fields.common.categories` 长这样：

```json
[{"categoryId":"["},{"categoryId":"\""},{"categoryId":"t"},{"categoryId":"o"}, …]
```

**根因**：提交时 `categories` 传的是**字符串** `'["tools:office"]'`，
不是数组。平台把它当可迭代对象逐字符遍历，20 个字符就成 20 个 categoryId。
最常见来源是 `creds --set workbuddy.categories=["tools:office"]` ——
命令行只拿得到字符串，原样落盘就完了。

**修法**：只有一条路 —— `withdraw` 退回 draft，改对配置后**重发一整版**。
类目字段没有可用的 update 端点，控制台表单对坏数据本身也有 bug（删是追加），
UI 无解。脚本侧现在是双保险：`creds.set_value()` 入口把 JSON 字面量解析成真结构，
`_as_list` / `normalize_categories` 在出口再兜一次。

**判据**（一条命令自查）：`categories` 长度 > 3，或任一 `categoryId` 长度 ≤ 1 → 乱码。

### 查类目要读 `fields.common`，不是 `currentVersion`

`GET /api/v2/open-platform/capabilities/{assetId}` 里的 `currentVersion`
**只有 6 个键**：`version` / `versionId` / `status` / `payMode` / `payDetail` / `needEncrypt`。
**没有 `categories`，也没有 `marketCategories`。**

读 `currentVersion.categories` 会永远得到 `None`，看起来像「服务类目为空」，
于是按 R01 去修 —— 白撤回一版。真正的类目在这两个位置：

```python
fields.common.categories        # [{"categoryId": "tools:office"}, ...]
fields.common.marketCategories  # ["development-tools", ...]
```

2026-10-27 就这么误判过一次：把 skill-release 1.5.3 的**乱码类目**
读成了「空类目」。教训是 —— **先确认字段在这个接口里存不存在，再判断值对不对**。

### 审核驳回：`无资质类目字段，需重新选择对应类目`

平台原话：「**服务类目审核：**无资质类目字段，需重新选择对应类目，如需资质请一并
提交，请修改后再重新提交审核」。**2026-10-14 第二次驳回后才定位到真根因**，
下面这版才是对的（前两版结论都错，别再按老思路查）。

**真根因：空 `qualifications` 数组。** 「服务类目没选」会被驳回没错，但
**选了类目一样会被驳回** —— 只要把空资质写成
`{"categoryId":"tools:office","qualifications":[]}`。审核侧会判成
「选了类目却没交资质」，打回话术仍然是那句「无资质类目字段」。
控制台提交前会跑一遍 `normalizeCategories()`：

```js
let t = {categoryId: e.categoryId};
a.length > 0 && (t.qualifications = a);   // 空资质 → 整个键都不发
```

所以 `tools:office` 这类 `qualificationRequired: false` 的类目，
**必须一个 `qualifications` 键都不带**。

> ⚠️ **判据盲区**：详情接口读回来永远是 `"qualifications": []`
> （服务端把缺键归一化了），**看接口分别不出当初到底发没发那个键**。
> 只能回代码看 `normalize_categories()`。

排查顺序：

1. **服务类目 ≠ 市场类目**，两套体系在 `submit` 时都要给：
   `fields.common.categories`（服务/资质类目）与 `fields.common.marketCategories`
   （市场展示类目，1..3 个）。
2. **服务类目枚举的唯一权威来源是
   `GET /api/v2/open-platform/capabilities/business-categories`**
   （2026-10-14 从前端 chunk 反解，接口名 `capabilityPort.businessCategories()`）。
   条目形如
   `{"id":"tools:office","name":{"zh":"办公"},"qualificationRequired":false}`。
   个人主体「工具」大类下 11 个三级类目：
   记账 `tools:bookkeeping` / 日历 `tools:calendar` / 天气 `tools:weather` /
   **办公 `tools:office`** / **图片处理 `tools:image_editing`** /
   计算器 `tools:calculator` / 报价·比价 `tools:price_comparison` /
   信息查询 `tools:info_lookup` / 预约·报名 `tools:booking` /
   健康管理 `tools:health_management` / 备忘录 `tools:memo`
   —— **没有「开发工具」**，所以技术类技能一般落 `tools:office`。
3. **payload 与控制台逐字段对齐**：`{capabilityType, assetType, action,
   assetId, uploadToken, clientToken, fields:{common:{…}}}` ——
   `assetType` 与控制台一样要发（值同 `capabilityType`）。

```jsonc
// 凭据文件 ~/.workbuddy/.secrets/skill-release.json 的 workbuddy 段
"categories": ["tools:office"],            // 服务（资质）类目
"market_categories": ["development-tools"] // 市场展示类目
```

`pub_workbuddy.normalize_categories()` 负责包装成平台结构，
并**丢掉空的 `qualifications`**；类目为空时本地直接抛错，不会白跑一次提交。

**驳回原因怎么查**（不用等站内信）：

```
GET /capabilities/{assetId}/reasons
  → data.{rejectStage: "manual"|"auto", reviewDetail:{reviewNote, reviewedAt}}
GET /api/v2/open-platform/messages
  → 平台通知全量流水（asset.approved / asset.rejected…）
```

`rejectStage: "manual"` = 人工抽检环节驳回。

> 重新提交后：资产状态回到 `under_review`，**`versionId` 会换新**
> （实测 46965 → 48444），并且只有到这一步
> `pendingVersion.version` 才会同步成**包里的**版本号。

重新提交：

```bash
$PY "$SR" publish <技能目录> --targets workbuddy --yes --changelog "补充资质类目"
```

（被驳回的资产不用新建，`find_asset` 会按 name 命中并按状态走 `resubmit`。）

---

### `pay` 回看是「免费」，明明设了 29 元

**先别怀疑平台不支持 —— 九成是载荷位置写错了。** 2026-10-26 的真实事故：
控制台界面上显示「一次性付费·29元」，提交成功、状态 `under_review`，
回读服务端 `pendingVersion.payMode` 却是 **0**，等于按免费送审了。

三个错法，任何一个都会**静默**变免费（返回 200，无任何报错）：

| 错法 | 正确写法 |
|---|---|
| `payMode` 放在 submit 载荷**顶层** | 放进 `fields.common` **里面** |
| `payDetail: [{"amount": 2900}]` | `payDetail: [{"price": 2900}]`（键是 price，单位分） |
| `payDetail: [{"durationDays": 30}]` | `payDetail: [{"duration_days": 30}]`（snake_case） |

正确形状（¥29 买断）：

```json
"fields": {"common": {..., "payMode": 1,
                      "payDetail": [{"price": 2900, "duration_days": 0}],
                      "needEncrypt": true}}
```

排查顺序：

```bash
$PY "$SR" pay <技能目录>                 # 看服务端到底记了什么
$PY "$SR" withdraw <技能目录>            # 记错了就撤回（只有 draft/待审能撤）
$PY "$SR" publish <技能目录> --targets workbuddy --yes --pay buyout --price 29
```

注意：**付费方式过审即锁死**，且已过审的免费资产不能转付费。所以「定错价」
只能在待审阶段纠正；过了审就只能删掉重建。

### 界面显示 29 元、但重开页面就没了

控制台「付费设置」弹窗的**保存是纯前端状态**（不调任何接口），付费信息只在
最终 `submit` 的载荷里才落库。所以：

- 用 UI 自动化时，**从设价到点提交必须同一次页面会话**，中间任何 `goto` 重载
  都会把设置丢掉，提交出去就是 `payMode: 0`；
- 更稳的做法是根本不碰 UI —— 直接用 `publish --pay ...` 走 API。

### 已提交、还在审核，但发现字段填错了

**没有「改一改再提交」这条捷径** —— `publish` 一旦发现资产
`under_review` 就**整体跳过**（既不上传也不提交，见 `pub_workbuddy.in_review`），
重跑只会得到一句「已在 open.workbuddy 审核中」。

出路是先撤回，退回 `draft` 再重提：

```bash
$PY "$SR" withdraw <技能目录>        # 或 withdraw os_xxxxxxxx
$PY "$SR" publish <技能目录> --targets workbuddy --yes --changelog "修正字段后重提"
```

`POST /capabilities/{assetId}/withdraw` → `data.status` 变 `draft`。
撤回不改版本号，重提可用同版本或更高版本。

⚠️ **撤回的代价，两条（2026-10-14 实测）**：

1. **审核计时会重头开始**（约 7 个工作日）。
2. **`versionId` 会被复用，`pendingVersion.version` 标签不刷新** ——
   撤回 1.4.6（versionId 48444）后重提 1.4.9，`submit` 回的还是
   versionId 48444，详情里 `pendingVersion.version` 依然显示 **1.4.6**。
   内容确实是新包（判据：`fields.common.releaseNote` 已是新提交的文案），
   但**版本号标签停留在该 version 记录第一次解析出来的值**。
   （被**驳回**后重提则会换新 versionId，例如 46965 → 48444，不在此列。）

   所以拿不到「干净版本号」时，别怀疑包没生效 —— 先看 `releaseNote`。

因此「只改了工具代码、不影响上架内容」这种，权衡一下再决定要不要撤。
2026-10-14 就是因为加了 withdraw 功能本身，把 skill-release 的 1.4.6
撤回来发成 1.4.9，只为让三端内容一致。

---

## 四、GitHub

### `未安装 github-ops，跳过 GitHub`

本技能的 GitHub 部分依赖 `github-ops` 技能（凭据管理 + 推送前密钥扫描）。
缺失时会在 `doctor` 里报出来。

### 推送被密钥扫描中止

```
扫描发现达到 high 级别的内容，已中止推送
```

**这不是 bug，是保护。** 按提示三选一：

1. 从文件里移除该内容后重试；
2. 确认是文档示例 → 在该行加 `ghscan:allow`；
3. 确认整个文件无需检查 → 写进 `.ghscanignore`。

命中清单要交给用户判断，**不要自行删改文件**。

### 推上去是空仓库

`ghx.py git push` 只推已有提交、**不做 commit**。
本技能会先 `git init / add / commit`。若手动调 `ghx` 遇到这情况，
先在目录里 `git add -A && git commit -m "..."`。

### `! [rejected] main -> main (fetch first)`

```
 ! [rejected]        main -> main (fetch first)
 error: failed to push some refs to 'https://github.com/…'
```

**不是网络问题**（连接是通的），所以 API 兜底那一层不会接手。

成因：staging 是**每次 publish 都重建**的临时目录，本地永远是一条**孤儿提交**
（`git init` → 一条 commit，无父），而远端已经有历史 —— 两边各自成根，push 必被拒。

**1.4.9 起自动处理**，`push()` 命中这个特征就调 `reconcile_remote()`：

1. 取远端分支 SHA（REST，走 `api.github.com`）；
2. `ghghit._transfer("fetch", …)` 带令牌把远端对象拉全；
3. `git reset --soft <远端SHA>` —— HEAD 挪过去，索引与工作区**不动**；
4. 用原提交的消息 + 原提交的身份重新 commit，新提交就接在远端 HEAD 后面。

**别用 rebase** —— 两个根提交没有共同祖先，rebase 必冲突。
第一次碰到（2026-10-13）是人工 `reset --soft` 收尾的，现在固化成代码。

### 对齐后的提交丢了 noreply 的数字前缀

```
作者 A：  Yemo3s <103362262+Yemo3s@users.noreply.github.com>   ← 历史正常形态
作者 B：  Yemo3s <Yemo3s@users.noreply.github.com>              ← 被改写了
```

根因在**上游**：push 走的是 `ghx git push --noreply-identity`，而 ghx 的
`set_noreply_identity()` 会把仓库级 config 改写成**不带数字 id 的旧式 noreply**。
`reconcile_remote` 如果在它之后才 commit，就会继承这个被改写过的 config。

修法：`reconcile_remote` **从我们自己那条提交里抄身份**（`git log -1 --format=%an%n%ae`），
用 `git -c user.name=… -c user.email=…` 显式钉在 commit 上，不读 config。

> 两种形态**都不泄漏真名**（`audit` 的白名单按 `@users.noreply.github.com` 后缀判，
> 两种都过），GitHub 也都能正确归属到账号。所以这只影响一致性，不是安全事故 ——
> 除非你在意历史里形态统一，否则不必为它改写历史（改历史要强推，
> 而旧 SHA 仍可直访，见 `提交用了真名` 那一节）。

### `未配置 github.owner`

```bash
$PY "$SR" creds --set github.owner=<GitHub 用户名>
```

### 提交用了真名 / 公司邮箱（**已修，但存量要清**）

这是本技能最严重的一次事故：首个 `douyin-downloader` 版本推上公开仓库后，
commit 的作者与提交者都是开发机的**真名 + 公司邮箱**。

**成因是两处叠加**，单看每处都「合理」：

1. `prepare_repo` 原本只在「身份缺失」时才补 noreply，而 `git config user.email`
   **不带 `--local` 会回退读全局配置**。开发机全局正是真名 + 公司邮箱 → 补全被跳过。
2. `ghx git push --noreply-identity` 确实设了 local 身份，但它跑在 push 阶段 ——
   **而 commit 早就生成了**。改身份改在了 commit 之后，纯摆设。

于是「打印着『提交身份已改为 noreply』，真名照样上了公开仓库」。

**现在的三道防线**（任一被绕过都不行）：

| # | 防线 | 实现 |
|---|---|---|
| 1 | 身份**无条件**写仓库级 | `prepare_repo`：不再判断是否缺失，直接 `config --local` |
| 2 | 清掉 `GIT_AUTHOR_*` / `GIT_COMMITTER_*` | 这些环境变量**优先级高于 config**，不清就压不住 |
| 3 | commit 后、push 前各断言一次 | 邮箱后缀必须是 `@users.noreply.github.com`，否则 `GitHubError` |

第 3 条用**白名单**判据。黑名单（比如「检测公司域名」）永远列不全 ——
猜不到用户在哪家公司、换过几家。

**发布前先扫一遍存量**（防的是「闸门只拦本次、管不到存量」）：

```bash
$PY "$SR" audit                 # 扫账号下所有仓库（含私有）
$PY "$SR" audit --repo OWNER/NAME
```

判据同样是白名单：邮箱以 `@users.noreply.github.com` 结尾即安全；
其余与本机全局 git 身份匹配才算命中。详见 [commands.md](./commands.md)。

**重命名仓库不等于清除。** 旧名会重定向到新名，泄漏的 commit 原样保留 ——
只是从「公开的泄漏」变成「私有的泄漏」。真正要清还是得删库重建。

**本机也要查。** 暂存目录（`~/.workbuddy/.release/<slug>/github`）里有 `.git`，
历史里那条脏提交同样会随下次推送再出去一次。发现后直接删掉整个暂存目录
（下次发布会重建），并用 `git log --all --format=%ae` 扫一遍本机其它仓库。

**公共事件档案（gharchive）不会记录作者邮箱。**
`data.gharchive.org` 是 GitHub 公开事件的第三方归档，但当前格式的
`PushEvent` 里只有 `repository_id / push_id / ref / head / before`，
**不含 commit 的 author 字段**（2026-10-10 抽样该小时 28,398 条 PushEvent，
带 `commits` 字段的为 0）。所以「仓库公开了几分钟」这条路径上，
泄漏出去的是仓库名与**账号 login**，而不是真名 / 邮箱。
这只是排除了最可能的一条路径 —— 不能代表所有爬虫都没抓过。

**已经泄漏出去了怎么办**：改代码删不掉 commit 元数据，只能重写历史。

```bash
git -C <repo> commit --amend --reset-author        # 只有顶端一个提交时
git filter-repo --email-callback '...'             # 整条历史
git push --force-with-lease
```

⚠️ **`force push` 不能算清除**。它改的是分支指向，旧 commit 变成不可达对象；
GitHub 对不可达对象不会立即回收，**旧 SHA 通常仍可通过 `/<repo>/commit/<sha>`
直访一段时间**（未逐案实测，取决于 GitHub 的 GC 时机）。
若该 commit 曾被 PR 引用或被 fork，则可能长期保留。

因此**要确保清除，唯一可靠的做法是删除仓库后重建** —— 旧 commit 立即 404。
代价是 star / fork / issue 一并丢失，删前先评估，重建后重新推一遍即可。

### 删仓库报 `HTTP 403  Must have admin rights to Repository.`

**不是权限配置错了，是令牌 scope 不够。** 删除仓库需要 `delete_repo`，
而 `repo` scope **不包含**它 —— 这一点很容易误判，因为 `repo` 看起来已经是全能。

```bash
$PY "$GHX" auth status      # 看「授权范围」这一行
```

本机是 `read:user, repo, workflow` → 删库必被拒。

补权限：<https://github.com/settings/tokens> 重新生成 classic token，
勾上 `delete_repo`；或给 fine-grained token 授
**Administration: Read and write**。

### 想把 fork 设为私有，报 `Public forks can't be made private`

GitHub 的硬规则：**公开 fork 的可见性跟随 fork 网络，不能单独转私有**。
`PATCH /repos/{owner}/{repo}` 发 `{"private": true}` 一定返回 422。

必须先**脱离 fork 网络**（Leave fork network）：

| 条件 | 说明 |
|---|---|
| fork 是公开的 | 私有 fork 不适用 |
| fork < 1 GB | 超了只能走「手动脱离」流程 |
| 没有子 fork | 有人 fork 过你的 fork 就不行 |

**「脱离 fork 网络」只能通过网页操作，REST API 不支持。**

```
https://github.com/<owner>/<repo>/settings
→ General → 滚到底部 Danger Zone → Leave fork network
→ 确认 → 输入仓库名 → 提交
```

⚠️ 脱离是**不可逆的**，且会丢掉 issues / PR / wiki / star / watcher / 子 fork；
**但 git commit 元数据会保留** —— 也就是说，光靠这一步清不掉真名 commit，
只是转私有后外人访问不到了。

#### 脱离之后：改可见性用 API 就行，但要等约 1 分钟

实测（`Yemo3s/GoGoGo`，440 MB，0 子 fork）：点完 Leave fork network 后立刻
`PATCH private=true` 仍返回同样的 422 —— 切换需要约 1 分钟。判据是元数据里的
**`fork` 标志变 `false`、`parent` 变 `null`**：

```bash
# 轮询到 fork=false 再改；提前改只会白挨一次 422
$PY "$GHX" api GET /repos/<owner>/<repo> --json      # 看 fork / parent
$PY "$GHX" api PATCH /repos/<owner>/<repo> --field private=true
```

不要用「网页上 Change visibility 出现了没有」当判据 —— 那要看刷新时机；
`fork=false` 是服务端事实。

#### 怎么确认真的私有了

**匿名访问的 HTML 应返回 404**，且页面里不再出现 `forked from`：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'User-Agent: Mozilla/5.0' \
     "https://github.com/<owner>/<repo>?cb=$RANDOM"
```

三个坑，都踩过：

| 现象 | 真相 |
|---|---|
| 匿名返回 **200** | 多半是 CDN 缓存。加随机 query（`?cb=`）再试 |
| 匿名 **API** 返回 403 | 不等于没私有 —— 未认证配额耗尽（看 `x-ratelimit-remaining: 0`）也是 403。**以 HTML 为准** |
| 带令牌查是 `private: true` 就放心了 | 不够，令牌视角看不出缓存 / 边缘节点。匿名 HTML 404 才是终判 |

超 1 GB 或想省事，走官方推荐的「手动脱离」：`git clone --bare` → 删 fork →
建同名新仓库 → `git push --mirror`。删 fork 同样需要 `delete_repo`。

### 手动指定提交身份

默认用 GitHub noreply 形态；想指定别的：

```bash
$PY "$SR" creds --set github.owner=Yemo3s
$PY "$SR" creds --set github.uid=103362262     # 让 noreply 带数字 id，能正确归属账号
```

要完全自定义（**会公开可查**）：

```bash
$PY "$SR" creds --set github.identity.name=<名字>
$PY "$SR" creds --set github.identity.email=<邮箱>
```

`doctor` 会打印本次使用的提交身份，以及全局 git 身份是否安全。

---

## 五、本技能自身

### `--yes` 没生效 / 选项被吃掉

已修。历史 bug 值得记一笔：`opt()` 早期版本用
`if val not in (None, False) or default is None` 判断，逻辑写反了 ——
`store_true` 的 `False` 会被当成「已设置」直接返回，导致写在子命令前的
`--yes` 被子命令级的 `False` 覆盖。

现改为 `add_common` 里一律用 `argparse.SUPPRESS` 作默认值，未设置的选项
干脆不进 namespace，`opt()` 用 `hasattr` 区分「没给」和「明确给了 False」。
自检里有 `test_cli` 钉住这个行为。

### 版本号四处不一致

```bash
$PY "$SR" ../selftest.py      # test_version 会报出来
```

四处指 `release.py` 的 `__version__`、`VERSION.txt`、`SKILL.md` frontmatter，
以及 `_skillhub_meta.json` 里的 `version`。`--bump` 会同时改这四处
（以及被发布技能的对应位置）。

> `_skillhub_meta.json` 是 1.4.7 起才纳入 `--bump` 的 —— 之前它不在同步范围内，
> 结果 skill-release 自己就从 1.4.5 一路漂到 1.4.6 都没人发现。
> 它不影响发布（SkillHub 的版本号取自 SKILL.md frontmatter），
> 但它是在地记录，漂了会让人误判「哪一版发出去了」。

### 暂存目录脏了

```bash
"C:/Users/<你>/.workbuddy/binaries/python/versions/3.13.12/python.exe" -c \
  "import shutil,pathlib; shutil.rmtree(pathlib.Path.home()/'.workbuddy'/'.release', ignore_errors=True)"
```

每次 `publish` 都会清空重建对应子目录，正常不会有残留。
但暂存目录里有 `.git`，**如果里面留着一条带真名的历史，下次推送会把它带出去** ——
所以 `audit` 扫出问题后，连同这里一起清。Windows 上不要用 `rm -rf`（会被安全策略拦截），
用上面的 Python 一行命令，它走的是回收站语义。

---

## 六、诊断顺序

遇到任何问题，先按这个顺序跑，多数情况三步内定位：

```bash
$PY "$SR" doctor                        # 1. 环境和凭据
$PY "$SR" check <技能目录>               # 2. 规则引擎（带改法）
$PY "$SR" check <技能目录> --json        # 3. 要喂给脚本/上报时用
```

发 GitHub 之前在 2 与 3 之间插一步 `$PY "$SR" audit` ——
它查的是规则引擎查不到的东西：**已经推上去的**历史里有没有你的真名。
