# 命令参考

六个子命令：`check`（只检查）、`doctor`（体检）、`creds`（凭据）、
`probe`（只读探测）、`audit`（提交身份审计）、`publish`（发布）。

```bash
# 把 <你> 换成你的 Windows 用户名
PY="C:/Users/<你>/.workbuddy/binaries/python/versions/3.13.12/python.exe"
SR="C:/Users/<你>/.workbuddy/skills/skill-release/scripts/release.py"
```

**为什么必须用绝对路径调 Python**：Git Bash 下 `~` 会展开成 `/c/Users/...`，
Windows 版 python.exe 不认这种路径，会把它当成 `C:\c\Users\...` 然后报
「文件不存在」—— 这个报错极具误导性。

---

## check —— 只检查，不发布

```bash
$PY "$SR" check <技能目录> [--targets github,skillhub,workbuddy] [--json]
```

最常用的命令。跑规则引擎，结论前置输出。

| 退出码 | 含义 |
|---|---|
| 0 | 指定的所有平台均可发布 |
| 1 | 有阻断项 |
| 2 | 参数或目录有问题（技能目录不存在、SKILL.md 缺失等） |

`--json` 输出结构：

```json
{
  "skill": "douyin-downloader",
  "version": "1.1.0",
  "publishable": {"github": true, "skillhub": true, "workbuddy": false},
  "files": 20,
  "bytes": 225692,
  "extensions": {".py": 8, ".md": 5, ".sh": 3},
  "findings": [
    {"level": "block", "platform": "workbuddy", "code": "fm-author",
     "message": "...", "source": "verified", "fix": "..."}
  ]
}
```

`level` 取 `block` / `warn` / `info`；`source` 取 `verified` / `assumed`。

---

## doctor —— 体检

```bash
$PY "$SR" doctor
```

一次看清：Python 版本、`github-ops` 是否就位、配置文件位置、
三平台凭据是否有效（**只输出脱敏值**）。

退出码 1 表示有平台鉴权失败。未配置的平台会标注「跳过」，不算失败。

---

## creds —— 凭据

```bash
$PY "$SR" creds                                    # 查看（脱敏）
$PY "$SR" creds --set skillhub.key=skh_xxxx
$PY "$SR" creds --set skillhub.cookie='skh_token=<JWT>; sid=<sid>'
$PY "$SR" creds --set workbuddy.cookie='<Cookie>'
$PY "$SR" creds --set workbuddy.party_id=<入驻主体 ID>
$PY "$SR" creds --set author=mo
$PY "$SR" creds --set github.owner=<GitHub 用户名>
$PY "$SR" creds --set github.private=false
$PY "$SR" creds --from-har ~/Downloads/wb.har # ★ open.workbuddy 用这个，别手抄
```

### `--from-har` —— open.workbuddy 的 Cookie 别手抄

登录后 `session` 约 **4 KB**，手工「选中-复制」会被截断（实测两次都卡在 4001 字符，
末尾 27 位 HMAC 整段丢失 → 302）。HAR 是落盘文件，读它零损耗。

1. 在 <https://open.workbuddy.cn> 登录，F12 → Network → 刷新页面
2. **先开敏感数据开关**：DevTools → 设置（F1）→ 偏好设置 → 网络 →
   勾选「**允许生成包含敏感数据的 HAR**」
   > Chrome 130 起 HAR **默认导出「已清理」档**，Cookie / Set-Cookie /
   > Authorization 全不含。没开这个开关导出的 HAR，本站请求一条不少、状态码都是
   > 200，就是一个 Cookie 都没有 —— 症状和「没登录」一模一样。
3. 点 Network 面板的**「导出」→「导出 HAR（包含敏感数据）」**
   （或右键请求 → 复制 → 「将所有内容另存为 HAR（包含敏感数据）」）
4. `$PY "$SR" creds --from-har <文件路径>`

它只挑**最后一条带 `session` 的本站请求**（不做跨条目合并 —— 那会把旧 `session`
和新 `session_2` 拼一起，比不取更糟），先跑 `sniff_cookie()` 自检，
再 `whoami()` **验活通过才写盘**。所以失败不会污染配置。
若整份 HAR 里本站请求全部无 Cookie，它会直接点名是导出档位的问题。

配置文件：`~/.workbuddy/.secrets/skill-release.json`，目录权限 0700、文件 0600。

**这个位置是刻意的** —— 技能目录会被打包上传到三个平台，凭据放里面就有被
连带打包的风险。`github-ops` 的 `doctor` 专门检查这一点。

环境变量优先级高于文件（便于 CI 或一次性调用）：

| 配置项 | 环境变量 |
|---|---|
| `skillhub.key` | `SKILLHUB_KEY` |
| `skillhub.cookie` | `SKILLHUB_COOKIE` |
| `workbuddy.cookie` | `WORKBUDDY_COOKIE` |
| `author` | `SKILL_RELEASE_AUTHOR` |

### 怎么取 SkillHub 的凭据（二选一）

**A. 会话 Cookie**（不用去签发 Key）

1. 浏览器登录 <https://skillhub.cn>
2. F12 → Network → 任一 `api.skillhub.cn` 请求 → Request Headers → `Cookie`
3. 真正用到的只有 `skh_token` 与 `sid`，其余可写可不写

**B. API Key** —— 开发者中心签发 `skh_…`，`--set skillhub.key=skh_xxxx`。

**两条通道互斥**：配了 Cookie 就不要再发 `Authorization`，同时带会被判
`invalid or expired token`。都配时本技能以 Cookie 优先。

> 注意域名只能是 `api.skillhub.cn`。`www.skillhub.cn` / `skillhub.cn` 对
> `/api/...` 会回 SPA 的 HTML 壳（200 + `<!DOCTYPE html>`），看着像成功其实不是。

### 怎么取 open.workbuddy 的 Cookie

1. **先在** <https://open.workbuddy.cn> **把登录走完** —— 该站前面是 APISIX，
   未登录时接口会 302 到 Keycloak，此时页面看着正常但会话是空的
2. F12 → Network → **在 `open.workbuddy.cn` 的页面里**找一个
   `/api/v2/open-platform/...` 请求 → Request Headers → `Cookie`
3. 复制请求头里的 `Cookie` 整串
4. `$PY "$SR" probe --cookie "<整串>"` 先验证，通过后再写盘

⚠️ 复制前确认那个请求的域名是 `open.workbuddy.cn`。
`cloud.tencent.com` / `console.cloud.tencent.com` 也用同名同格式的 `session`，
但域名不同，粘进来只会得到 302 → 登录页。

验证标准是 `probe` 四个端点全部 **200 JSON** —— 不是「200」就算过。

---

## probe —— open.workbuddy 只读探测

```bash
$PY "$SR" probe [--cookie "<Cookie>"]
```

依次打 `/parties`、`/capabilities/categories`、`/capabilities/summary`、
`/capabilities/list`，报告会话是否有效、各端点是否可达。

**只读。任何情况下不写入。** 用它验证 Cookie 最安全 —— 不会产生审核队列里的垃圾。

---

## audit —— 提交身份审计

```bash
$PY "$SR" audit [--repo OWNER/NAME] [--depth N] [--watch STR] [--json]
```

| 选项 | 说明 |
|---|---|
| `--repo` | 只查一个仓库，默认查账号下全部（含私有） |
| `--depth N` | 每个仓库查最近 N 条提交（上限 100），默认 100 |
| `--watch STR` | 追加盯防的身份片段（姓名或邮箱），可重复 |

扫的是**提交元数据**，不是文件内容 —— GitHub 是三平台里唯一会记录「谁写的」的。
文件内容可以随时改，commit 里的姓名 / 邮箱推上去就几乎删不掉。

### 判据：白名单 + 你的身份，不是域名黑名单

一条提交判为「干净」的条件是：邮箱以 `@users.noreply.github.com` 结尾。
其余的要再与盯防名单匹配才算命中，名单 = **本机全局 git 身份**
（`git config --global user.name` / `user.email`）+ `--watch` 追加项。

这样上游 fork 的原作者邮箱不会被误报 —— 用「公司域名黑名单」则必然漏：
换个公司、换个邮箱、用个人邮箱提交，黑名单全都拦不住。
名单为空（本机没配全局身份）时自动降级：所有非 noreply 身份都算可疑，宁可多报。

### 输出与退出码

三段：**命中清单**（按「公开 / 私有」分组 —— 公开的才是真正暴露的）、
**公开仓库清单**（哪些仓库匿名可读，公开 fork 会标注出来）、跳过说明。
退出码：`0` = 干净，`1` = 有命中。

公开仓库清单单列一节，是因为它和身份命中是同一件事的两半：
一条脏 commit 在私有仓库里是隐患，在公开仓库里是事故。
`--json` 里对应 `repos` 与 `public_repos` 两个字段。

### 有命中怎么办

1. **先止血**：仓库设置里把可见性改成私有，立即生效；
   **公开 fork 改不动**（`422 Public forks can't be made private`）——
   得先在 Danger Zone「Leave fork network」，约 1 分钟后 `fork` 标志变 `false`，
   再用 API 改可见性。详见 [troubleshooting.md](./troubleshooting.md)；
2. **再清除**：泄漏的 commit 只有**删库重建**清得掉。
   `force push` 只改分支指向，旧 SHA 仍可直访；
   删库需要令牌带 `delete_repo` scope，没有就走网页设置页（Settings → 最底部）；
3. **查本机**：`git log --all --format=%ae` 扫一遍本地历史 ——
   本地留着的那条，下次推送会再出去一次。
   本技能的暂存目录（`~/.workbuddy/.release/`）里也有 `.git`，
   发现脏提交直接删掉整个暂存目录即可（下次发布会重建）。

⚠️ **重命名仓库不会清除任何东西**：旧名重定向到新名，泄漏的 commit 原样保留。
它只是把「公开的泄漏」变成「私有的泄漏」。

---

## publish —— 发布

```bash
$PY "$SR" publish <技能目录> [选项]
```

| 选项 | 说明 |
|---|---|
| `--targets` | 逗号分隔，默认 `github,skillhub,workbuddy` |
| `--yes` / `-y` | 确认执行。**不加则一律演练** |
| `--dry-run` | 强制演练，即使带了 `--yes` |
| `--bump {patch,minor,major}` | 发布前升版本号（**会改源文件**） |
| `--changelog STR` | 变更说明，送到平台审核侧 |
| `--force` | 忽略阻断项（不建议） |
| `--no-submit` | open.workbuddy 只上传不提交审核 |
| `--workdir DIR` | 暂存目录，默认 `~/.workbuddy/.release` |
| `--json` | 结构化输出 |
| `--quiet` / `-q` | 只输出结果行 |

### 默认是演练

```
$PY "$SR" publish <技能目录>              # 演练，无任何外部副作用
$PY "$SR" publish <技能目录> --yes        # 真发
```

演练也会**真的打包**并落盘到暂存目录，方便人工核对产物。

### `--bump` 会改三个地方

`SKILL.md` 的 frontmatter `version`、`VERSION.txt`、以及 `scripts/*.py` 里的
`__version__`。三处必须同步 —— 之前吃过漏改的亏：升到 1.1.0 时只改了两处，
`__version__` 还是 1.0.0，用户报 bug 时对不上号。

### 结果按平台分列

```
发布结果
  ✓ GitHub         dry-run    已建本地提交 0790d01；待推送至 owner/douyin-downloader
  ✗ SkillHub       skipped    未配置 skillhub.key（skh_ 开头），跳过 SkillHub
  ✗ open.workbuddy skipped    已打好包（141.0 KB），但未配置 workbuddy.cookie，跳过上传
      包：C:\Users\<你>\.workbuddy\.release\douyin-downloader-1.1.0.zip
```

| 退出码 | 含义 |
|---|---|
| 0 | 全部成功，或全部跳过（未配置凭据） |
| 1 | 有阻断项，或有平台真的失败 |
| 2 | 参数/目录错误 |

**未配置凭据的平台跳过不算失败** —— 只想更新一个平台时不必配全三个。

---

## 暂存目录结构

`publish` 会在 `--workdir` 下生成：

```
~/.workbuddy/.release/
├── douyin-downloader/
│   ├── github/        # 含 .sh、自动补 README + .gitignore，frontmatter 原样
│   ├── skillhub/      # 剔除 .sh，注入 slug / displayName / summary
│   └── workbuddy/     # 剔除 .sh，注入 author
└── douyin-downloader-1.1.0.zip    # workbuddy 用，外层目录 {slug}/
```

三种形态的**内容一致**，差异只有两处，都是平台规则要求的：

1. `.sh` 只在 GitHub 保留（SkillHub 白名单未验证 + 自删除脚本过审风险）；
2. SKILL.md 的平台元数据字段注入到副本，**源文件不动**。

想清理直接删 `~/.workbuddy/.release/` 即可。

---

## 自检

```bash
$PY "$SR" ../selftest.py
```

92 项离线检查，全绿。覆盖 frontmatter 往返、平台字段投影、两条真实被拒过的硬规则、
暂存差异、打包断言、版本一致性、CLI 选项优先级。
规则或打包逻辑被改动后这里会立刻报红。
