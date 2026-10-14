"""GitHub 适配器 —— 委托给 github-ops，不重复实现认证与扫描。

**为什么不自己实现**
------------------
`github-ops` 已经解决了三件难事，重做一遍只会引入新的失败面：
  1. 令牌用 Windows DPAPI 加密存放在技能目录之外；
  2. 推送前自动做密钥扫描，命中高危会中止（避免把 cookie / token 推上公开仓库）；
  3. 注入令牌时用 `-c remote.origin.url=` 只在本次生效，不污染 .git/config。

所以这里只做「准备仓库 → 调用 ghx」这一段编排。

一个必须注意的点：ghx 的 `git push` **不做 commit**，只负责把已有提交推上去。
所以本模块要先 git init / add / commit，否则会推一个空仓库上去。

提交身份（血的教训）
-------------------
`git config user.email` **不带 `--local` 会回退读全局配置**。开发机全局配的
通常是真名 + 公司邮箱，一旦 commit 用它生成，真名就永久进了公开仓库的
commit 元数据 —— 改代码也删不掉，只能重写历史 + 强推 + 删库。

所以本模块的身份策略是**三条硬约束**，任何一条被绕过都不行：

  1. **无条件**写仓库级身份（`--local`），不做「缺失才补」的判断；
  2. 清掉 `GIT_AUTHOR_*` / `GIT_COMMITTER_*` 环境变量 —— 它们**优先级高于 config**；
  3. commit 之后、push 之前各断言一次：邮箱必须是 `@users.noreply.github.com`。

第 3 条用**白名单**而非黑名单：黑名单永远列不全（公司域名猜不到、用户换了
几家单位也不知道），白名单则天然覆盖所有情况。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

__all__ = ["GHX_PY", "GitHubError", "ghx_available", "run_ghx",
           "prepare_repo", "create_repo", "push", "push_via_api",
           "is_network_failure", "NETWORK_FAIL_MARKERS", "repo_url",
           "noreply_identity", "identity_of", "unsafe_identity",
           "assert_safe_identity", "global_identity",
           "NOREPLY_DOMAIN", "DEFAULT_IDENTITY",
           "is_non_fast_forward", "NON_FF_MARKERS", "remote_head",
           "reconcile_remote"]

GHX_PY = Path.home() / ".workbuddy" / "skills" / "github-ops" / "scripts" / "ghx.py"

NOREPLY_DOMAIN = "users.noreply.github.com"
_SAFE_EMAIL_SUFFIXES = (f"@{NOREPLY_DOMAIN}",)

DEFAULT_IDENTITY = ("skill-release", f"release-bot@{NOREPLY_DOMAIN}")

# 这几个环境变量的优先级高于 git config，必须清掉才讲得通「无条件设身份」
_IDENTITY_ENV = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                 "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


class GitHubError(RuntimeError):
    pass


def ghx_available() -> bool:
    return GHX_PY.is_file()


def run_ghx(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """调 ghx.py。用当前解释器，避开 Windows 上 `python` 不在 PATH 的问题。"""
    if not ghx_available():
        raise GitHubError(
            f"未找到 github-ops：{GHX_PY}\n"
            "GitHub 这一段依赖它的凭据管理与推送前扫描。"
        )
    cmd = [sys.executable, str(GHX_PY)] + args
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """跑 git。**清掉身份类环境变量** —— 它们优先级高于 config，
    不清就压不住外部注入的作者信息。"""
    env = {k: v for k, v in os.environ.items() if k not in _IDENTITY_ENV}
    r = subprocess.run(["git", "-C", str(repo)] + list(args),
                       capture_output=True, text=True, env=env,
                       encoding="utf-8", errors="replace", timeout=120)
    if check and r.returncode != 0:
        raise GitHubError(f"git {' '.join(args)} 失败：{(r.stderr or r.stdout).strip()[:400]}")
    return r


def _git_available() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def noreply_identity(login: str, uid: object = "") -> tuple[str, str]:
    """按 GitHub 官方格式生成 noreply 身份。

    带数字 id 的形态（`<id>+<login>@users.noreply.github.com`）会被 GitHub
    正确归属到账号；拿不到 id 时退化为不带 id 的形态，同样有效、同样不泄漏真名。
    """
    who = (str(login or "").strip() or "github-user")
    num = str(uid or "").strip()
    if num.isdigit():
        return who, f"{num}+{who}@{NOREPLY_DOMAIN}"
    return who, f"{who}@{NOREPLY_DOMAIN}"


def global_identity() -> tuple:
    """读全局 git 身份，**仅供 doctor 展示** —— 发布链路绝不使用它。

    存在的意义是让用户看见「你全局配的是真名，但发布时不会用到」，
    而不是等出了事才发现。
    """
    env = {k: v for k, v in os.environ.items() if k not in _IDENTITY_ENV}
    vals = []
    for key in ("user.name", "user.email"):
        try:
            r = subprocess.run(["git", "config", "--global", "--get", key],
                               capture_output=True, text=True, env=env, timeout=30)
            vals.append((r.stdout or "").strip())
        except (OSError, subprocess.SubprocessError):
            vals.append("")
    return vals[0], vals[1]


def identity_of(repo: Path, rev: str = "HEAD") -> dict:
    """读出某个提交的作者 / 提交者身份。取不到时返回空字典。"""
    r = _git(repo, "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce",
             rev, check=False)
    parts = (r.stdout or "").strip().split("\0")
    if r.returncode != 0 or len(parts) != 4:
        return {}
    return {"author_name": parts[0], "author_email": parts[1],
            "committer_name": parts[2], "committer_email": parts[3]}


def unsafe_identity(repo: Path, rev: str = "HEAD") -> list:
    """列出该提交里**不该公开**的身份项，空列表表示干净。

    白名单判据：邮箱后缀必须是 `@users.noreply.github.com`。
    """
    info = identity_of(repo, rev)
    bad: list = []
    for key in ("author_email", "committer_email"):
        val = str(info.get(key) or "").strip()
        if val and not val.endswith(_SAFE_EMAIL_SUFFIXES):
            bad.append(f"{key}={val}")
    return bad


def assert_safe_identity(repo: Path, rev: str = "HEAD") -> None:
    """硬闸门：身份含真实邮箱就拒绝继续。发布链路上调用两次（commit 后、push 前）。"""
    bad = unsafe_identity(repo, rev)
    if bad:
        raise GitHubError(
            "已中止：提交身份不是 GitHub noreply 邮箱，"
            "继续推送会把你真实的姓名 / 邮箱写进公开仓库元数据。\n"
            f"    检出：{'; '.join(bad)}\n"
            f"    期望：*@{NOREPLY_DOMAIN}\n"
            "    处理：本技能只写仓库级身份、且已清掉 GIT_AUTHOR_* 环境变量，"
            "出现此错说明暂存目录被外部改过。删掉该暂存目录后重跑即可。"
        )


def prepare_repo(staging: Path, commit_message: str = "",
                 identity: tuple | None = None) -> str:
    """在暂存目录里建/更新本地提交，返回 commit 短哈希。

    身份**无条件**写成仓库级（`--local`）—— 不做「缺失才补」的判断。
    那个判断是本技能首版真实踩到的坑：`git config user.email` 不带 `--local`
    会回退读全局配置，而全局往往正是真名 + 公司邮箱。
    """
    staging = Path(staging).resolve()
    if not _git_available():
        raise GitHubError("本机未找到 git，请先安装 Git")
    if not (staging / ".git").is_dir():
        _git(staging, "init", "-q")
        _git(staging, "checkout", "-q", "-b", "main", check=False)

    name, email = identity or DEFAULT_IDENTITY
    _git(staging, "config", "--local", "user.name", str(name))
    _git(staging, "config", "--local", "user.email", str(email))

    _git(staging, "add", "-A")
    staged = _git(staging, "diff", "--cached", "--name-only").stdout.strip()
    if not staged:
        assert_safe_identity(staging)
        return "(无改动)"
    msg = commit_message or "release: 更新技能内容"
    _git(staging, "commit", "-q", "-m", msg)

    # 兜底：暂存目录若沿用了上一次的 .git，顶端提交可能还带着旧身份。
    # `--reset-author` 用当前 config（即上面刚无条件写入的目标身份）重写作者。
    # 暂存目录是可丢弃的临时产物，重写它的历史没有副作用。
    if unsafe_identity(staging):
        _git(staging, "commit", "--amend", "--no-edit", "--reset-author", "-q")

    assert_safe_identity(staging)
    return _git(staging, "rev-parse", "--short", "HEAD").stdout.strip()


def create_repo(slug: str, owner: str, private: bool, description: str = "") -> tuple[bool, str]:
    """建仓库。已存在也算成功（幂等），避免重复发布时报错。"""
    args = ["repo", "create", slug, "--description", description or f"{slug} skill"]
    args.append("--private" if private else "--public")
    r = run_ghx(args)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0:
        return True, out.strip()
    lowered = out.lower()
    if "already exists" in lowered or "已存在" in out or "422" in out:
        return True, "仓库已存在，跳过创建"
    return False, out.strip()


def push(staging: Path, repo_slug: str, branch: str = "main",
         scan: bool = True) -> tuple[bool, str]:
    """推送。默认先扫描敏感内容 —— 命中高危会中止，这里如实上报。

    推送前再断言一次身份：ghx 的 `--noreply-identity` 是在 push 时才设
    local config，**改在了 commit 之后**，对已生成的提交毫无作用。
    所以真正的闸门必须在这里 —— 盯的是提交内容，不是 config。

    **非快进自动对齐**：staging 每次 publish 都重建（`git init` + 单条
    孤儿提交），而远端已有历史时 push 必被拒：
        ! [rejected] main -> main (fetch first)
    以前要人工 `reset --soft` 收尾（2026-10-13 手工干过一次），
    现在就地自动接上，见 `reconcile_remote()`。
    """
    assert_safe_identity(staging)
    args = ["git", "push", str(staging), "--repo", repo_slug, "--branch", branch,
            "--noreply-identity"]
    if not scan:
        args.append("--no-scan")
    r = run_ghx(args)
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode == 0 or not is_non_fast_forward(text):
        return r.returncode == 0, text

    try:
        changed, note = reconcile_remote(staging, repo_slug, branch)
    except Exception as exc:  # noqa: BLE001
        return False, f"{text}\n[x] 非快进自动对齐失败：{exc}"
    if not changed:
        return False, f"{text}\n[x] 非快进，且未能自动对齐：{note}"

    r = run_ghx(args)
    text2 = ((r.stdout or "") + (r.stderr or "")).strip()
    return r.returncode == 0, f"[i] {note}\n{text2}"


def repo_url(owner: str, slug: str) -> str:
    return f"https://github.com/{owner}/{slug}"


# ---------------------------------------------------------------- API 兜底
# 为什么需要这一层（2026-10-13 实测）：
#
#   `git push` 走的域名是 **github.com**，而 REST API 走 **api.github.com**。
#   这两个域名在本机网络里的可达性可以完全不同 —— 实测：
#
#     github.com:443      直连超时；沙箱代理对 CONNECT 返回 502   → git push 必失败
#     api.github.com:443  直连正常                                → API 全通
#
#   于是发布链会在最后一步断掉，前三步（打包 / 校验 / 身份隔离）全白做。
#   兜底做法：改用 Git Data API 把同一棵文件树写成一次提交。
#   走的是 api.github.com，因此不受 github.com 是否可达影响。
#
#   注意 **空仓库**：Git Data API 的 blobs / trees / commits 在「还没有任何提交」
#   的仓库上全返回 `409 Git Repository is empty`。所以要先用 Contents API
#   落一个文件把仓库 bootstrap 起来，再建完整树、写 root commit、force 更新 ref
#   —— 这样分支上只留下一条提交，bootstrap 那条成为不可达对象。
NETWORK_FAIL_MARKERS = (
    "connect tunnel failed", "failed to connect", "could not resolve host",
    "connection timed out", "operation timed out", "connection reset",
    "connection was reset", "empty reply from server", "proxy error",
    "response 502", "response 503",
)


def is_network_failure(text: str) -> bool:
    """这段报错是不是「网络到不了 github.com」，而不是认证/权限问题？

    只有前者才该走 API 兜底 —— 如果是 403 / 认证失败，换个通道照样失败。
    """
    low = (text or "").lower()
    return any(m in low for m in NETWORK_FAIL_MARKERS)


# ---------------------------------------------------------------- 非快进对齐
# 为什么需要（2026-10-14 第二次踩到）：
#
#   staging 是**每次 publish 都重建**的临时目录，所以本地永远是一条孤儿提交
#   （`git init` → 一条 commit，没有父），而远端已经有历史。两边各自成根，
#   push 必然被拒：
#
#     ! [rejected]  main -> main (fetch first)
#     error: failed to push some refs
#
#   它不是网络问题（连接是通的），所以 `is_network_failure` 兜底不会触发 ——
#   发布链就卡死在最后一步。第一次（2026-10-13）是人工 `git reset --soft`
#   收尾的，这次把它固化进来。
#
#   **别用 rebase**：两个根提交没有共同祖先，rebase 必冲突。
#   正确做法是 `reset --soft <远端SHA>` —— HEAD 挪到远端之后、索引与工作区
#   **不动**，于是我们整棵树天然变成「相对远端的改动」，重新 commit 一次
#   就把新提交接在了远端 HEAD 后面，push 立刻变成快进。

NON_FF_MARKERS = ("non-fast-forward", "[rejected]", "fetch first",
                  "updates were rejected", "failed to push some refs")


def is_non_fast_forward(text: str) -> bool:
    """这段报错是不是「远端有本地没有的提交」？"""
    low = (text or "").lower()
    return any(m in low for m in NON_FF_MARKERS)


def remote_head(repo_slug: str, branch: str = "main") -> str:
    """远端分支顶端的 SHA；仓库为空 / 分支不存在返回 ""。走 api.github.com。"""
    ghcore = _ghcore()
    try:
        token = ghcore.resolve_token()[0]
        ref = ghcore.api_request("GET", f"/repos/{repo_slug}/git/ref/heads/{branch}",
                                 token)
    except Exception:  # noqa: BLE001 —— 404 / 无令牌都当「没有远端历史」
        return ""
    if not isinstance(ref, dict):
        return ""
    return str((ref.get("object") or {}).get("sha") or "")


def reconcile_remote(staging: Path, repo_slug: str, branch: str = "main",
                     timeout: int = 600) -> tuple[bool, str]:
    """把本地那条孤儿提交改接到远端 HEAD 之后，让 push 变成快进。

    返回 (是否真的改动过, 说明文本)。任何一步失败都抛 GitHubError。
    """
    staging = Path(staging)
    head = remote_head(repo_slug, branch)
    if not head:
        return False, "远端没有该分支（空仓库或名字不对），无需对齐"
    cur = (_git(staging, "rev-parse", "HEAD").stdout or "").strip()
    if cur == head:
        return False, "本地 HEAD 已与远端一致"

    # 远端对象本地多半没有 —— 先 fetch。私有仓库要靠令牌，所以走 ghgit 的
    # _transfer（它按 askpass → helper 两条路注入令牌，不污染 .git/config）。
    ghcore, ghgit = _ghcore(), _ghgit()
    token = ghcore.resolve_token()[0]
    remotes = ghgit.list_remotes(str(staging))
    remote = "origin" if "origin" in remotes else (next(iter(remotes), "origin"))
    https_url, kind = ghgit.normalize_to_https(
        remotes.get(remote) or f"https://github.com/{repo_slug}.git")
    if kind == "other":
        raise GitHubError(f"远程 {remote} 不是 GitHub 地址，无法对齐")
    ok, text, _variant = ghgit._transfer("fetch", str(staging), token, remote,
                                         https_url, branch, [], timeout)
    if not ok:
        raise GitHubError(f"fetch {branch} 失败：{text[-300:]}")

    # 用远端 SHA 而不是 FETCH_HEAD —— 后者可能被别处覆盖。
    #
    # 身份必须**从我们自己那条提交里抄**，不能信赖 reset 之后的 local config：
    # push 走的是 `ghx git push --noreply-identity`，而 ghx 的
    # `set_noreply_identity()` 会把 local config 改写成**不带数字 id 的旧式
    # noreply**（`Yemo3s@users.noreply.github.com`），于是这里 commit 出来的
    # 提交会与历史的 `<id>+Yemo3s@users.noreply.github.com` 形态不一致
    # —— 2026-10-14 实测踩到过（0fed0914 就是被它污染的）。
    meta = (_git(staging, "log", "-1", "--format=%an%n%ae").stdout or "").split("\n")
    msg = (_git(staging, "log", "-1", "--format=%B").stdout or "").strip()
    a_name = meta[0].strip() if meta else ""
    a_mail = meta[1].strip() if len(meta) > 1 else ""

    _git(staging, "reset", "--soft", head)
    _git(staging, "add", "-A")
    pin = (["-c", f"user.name={a_name}", "-c", f"user.email={a_mail}"]
           if a_name and a_mail else [])
    _git(staging, *(pin + ["commit", "-q", "-m",
                           msg or "release: 更新技能内容"]))
    assert_safe_identity(staging)
    new = (_git(staging, "rev-parse", "--short", "HEAD").stdout or "").strip()
    who = f"{a_name} <{a_mail}>" if a_mail else "（沿用仓库 config）"
    return True, (f"远端 {branch} 已是 {head[:7]}，本地孤儿提交已 reset --soft "
                  f"接上并重提为 {new}（快进可推，身份 {who}）")


def _ghcore():
    """导入 github-ops 的 ghcore（令牌解析与 api_request 都在里面）。"""
    sys.path.insert(0, str(GHX_PY.parent))
    import ghcore  # noqa: PLC0415

    return ghcore


def _ghgit():
    """导入 github-ops 的 ghgit（仓库探测 / 带令牌的 fetch）。"""
    sys.path.insert(0, str(GHX_PY.parent))
    import ghgit  # noqa: PLC0415

    return ghgit


def _staging_meta(staging: Path):
    """读暂存仓库那条提交的消息 / 身份 / 日期。

    让 API 提交与原 git 提交**逐字一致** —— 输入相同，GitHub 算出的
    commit SHA 就相同，于是「git push 失败后改走 API」是幂等的，
    不会因为换了通道就多出一条提交。
    """
    r = _git(staging, "log", "-1", "--format=%an%n%ae%n%cn%n%ce%n%aI%n%cI%n%B")
    lines = (r.stdout or "").split("\n")
    if len(lines) < 7:
        raise GitHubError(f"读暂存提交元数据失败：{(r.stdout or r.stderr)[:200]}")
    an, ae, cn, ce, aI, cI = lines[:6]
    msg = "\n".join(lines[6:]).strip() + "\n"
    return (msg, {"name": an, "email": ae}, {"name": cn, "email": ce}, aI, cI)


def push_via_api(staging: Path, repo_slug: str, branch: str = "main",
                 message: str = "", scan: bool = True) -> tuple[bool, str]:
    """用 Git Data API 把暂存目录推成一个提交。返回 (ok, 报告)。

    与 `push()` 的分工：`push()` 走 git（github.com），本函数走 API
    （api.github.com）。两者产出的内容与提交身份一致 —— 都把身份显式写成
    noreply，都不碰账号主邮箱。

    分支已存在时按正常提交追加（`parents=[当前 HEAD]`），不存在时 bootstrap 后
    写一条 root commit 并 force 建分支 —— 绝不在有历史的仓库上砍历史。
    """
    staging = Path(staging).resolve()
    # 与 push() 同款身份闸门：盯提交内容，而不是 config
    assert_safe_identity(staging)

    if scan:
        r = run_ghx(["scan", str(staging), "--include-untracked"])
        if r.returncode != 0:
            return False, ("推送前密钥扫描命中高危，已中止（API 兜底同样会拦）：\n"
                           + ((r.stdout or "") + (r.stderr or ""))[-600:])

    msg, author, committer, aI, cI = _staging_meta(staging)
    author["date"], committer["date"] = aI, cI
    message = message or msg

    files = [p for p in sorted(staging.rglob("*"))
             if p.is_file() and ".git" not in p.relative_to(staging).parts]
    if not files:
        return False, "暂存目录里没有可发布的文件"

    g = _ghcore()
    tok, _src = g.resolve_token()

    try:
        repo = g.api_request("GET", f"/repos/{repo_slug}", tok)
    except g.ApiError as exc:
        return False, f"API 兜底失败：读仓库 {repo_slug} 出错（{exc.status}）{exc.message[:200]}"

    head_sha = ""
    try:
        ref = g.api_request("GET", f"/repos/{repo_slug}/git/ref/heads/{branch}", tok)
        head_sha = str(ref["object"]["sha"])
    except g.ApiError as exc:
        if exc.status != 404:
            return False, f"API 兜底失败：读分支出错（{exc.status}）{exc.message[:200]}"

    # 空仓库：先用 Contents API bootstrap（Git Data API 此时一律 409）
    if not head_sha:
        boot = next((p for p in files if p.name == "README.md"), files[0])
        rel = boot.relative_to(staging).as_posix()
        import base64  # noqa: PLC0415

        g.api_request("PUT", f"/repos/{repo_slug}/contents/{rel}", tok, body={
            "message": message, "branch": branch,
            "content": base64.b64encode(boot.read_bytes()).decode("ascii"),
            "author": author, "committer": committer,
        })

    import base64  # noqa: PLC0415

    entries = []
    for p in files:
        blob = g.api_request("POST", f"/repos/{repo_slug}/git/blobs", tok, body={
            "content": base64.b64encode(p.read_bytes()).decode("ascii"),
            "encoding": "base64"})
        entries.append({"path": p.relative_to(staging).as_posix(),
                        "mode": "100644", "type": "blob", "sha": blob["sha"]})

    tree = g.api_request("POST", f"/repos/{repo_slug}/git/trees", tok,
                         body={"tree": entries})
    commit_body = {"message": message, "tree": tree["sha"],
                   "author": author, "committer": committer}
    if head_sha:
        commit_body["parents"] = [head_sha]
    commit = g.api_request("POST", f"/repos/{repo_slug}/git/commits", tok,
                           body=commit_body)

    if head_sha:
        g.api_request("PATCH", f"/repos/{repo_slug}/git/refs/heads/{branch}", tok,
                      body={"sha": commit["sha"], "force": True})
    else:
        g.api_request("POST", f"/repos/{repo_slug}/git/refs", tok,
                      body={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})

    return True, (f"已通过 API 推送（{len(files)} 个文件，提交 {commit['sha'][:8]}，"
                  f"身份 {author['name']} <{author['email']}>）；"
                  f"仓库 {repo_url(*repo_slug.split('/'))}")
