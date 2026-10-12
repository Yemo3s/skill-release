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
           "prepare_repo", "create_repo", "push", "repo_url",
           "noreply_identity", "identity_of", "unsafe_identity",
           "assert_safe_identity", "global_identity",
           "NOREPLY_DOMAIN", "DEFAULT_IDENTITY"]

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
    """
    assert_safe_identity(staging)
    args = ["git", "push", str(staging), "--repo", repo_slug, "--branch", branch,
            "--noreply-identity"]
    if not scan:
        args.append("--no-scan")
    r = run_ghx(args)
    return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()


def repo_url(owner: str, slug: str) -> str:
    return f"https://github.com/{owner}/{slug}"
