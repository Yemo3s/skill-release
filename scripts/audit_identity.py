#!/usr/bin/env python3
"""跨仓库身份泄漏审计 —— 扫一遍 GitHub 账号下所有仓库的提交元数据。

为什么需要它
------------
发布链路里的 `assert_safe_identity` 只能拦住**这一次**发布。
已经推上去的提交、以及用别的方式（手工 clone 后 commit、其他工具）推的提交，
它管不到 —— 而 commit 里的姓名 / 邮箱写进去就几乎删不掉。

真实事故：一条 commit 带着 `真名 <公司邮箱>` 被推到公开仓库，事后
`force push` 删不掉（旧 SHA 仍可直访），删库又需要 `delete_repo` scope。
所以正确的顺序是**先审计、后发布**。

判据为什么是白名单
------------------
只认「不是 noreply 就算可疑」+「命中你本机全局 git 身份才算你」。
不用「检测公司域名」这类黑名单 —— 黑名单永远列不全，换个公司、换个邮箱就漏。
反过来，上游 fork 的原作者邮箱不在你的盯防名单里，也就不会被误报。

本模块只做只读请求，令牌始终由 `github-ops` 托管，这里不碰也不存。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

__all__ = ["GHX_PY", "NOREPLY_DOMAIN", "AuditError", "Finding",
           "local_global_identity", "build_watch", "match_identity",
           "scan", "render"]

GHX_PY = Path.home() / ".workbuddy" / "skills" / "github-ops" / "scripts" / "ghx.py"

NOREPLY_DOMAIN = "users.noreply.github.com"
_SAFE_EMAIL_SUFFIXES = ("@" + NOREPLY_DOMAIN,)

DEFAULT_DEPTH = 100


class AuditError(RuntimeError):
    pass


# ------------------------------------------------------------------ 盯防名单


def local_global_identity() -> list[str]:
    """读本机全局 git 身份 —— 这是最可能被误用的那份真名 / 真实邮箱。

    读不到就返回空列表，由 build_watch 决定降级行为。
    """
    out: list[str] = []
    for key in ("user.name", "user.email"):
        try:
            r = subprocess.run(["git", "config", "--global", "--get", key],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
        except (OSError, subprocess.SubprocessError):
            return out
        val = (r.stdout or "").strip()
        if val:
            out.append(val.lower())
    return out


def build_watch(extra: list[str] | None = None) -> tuple[list[str], bool]:
    """组装盯防名单。

    返回 (名单, 是否降级)。降级 = 名单为空 —— 此时 match_identity 会把
    **所有**非 noreply 身份都当作可疑，宁可多报也不漏报。
    """
    seen: list[str] = []
    for item in local_global_identity() + [str(x) for x in (extra or [])]:
        low = item.strip().lower()
        if low and low not in seen:
            seen.append(low)
    return seen, not seen


# ------------------------------------------------------------------ 判据


def match_identity(author: dict, committer: dict,
                   watch: list[str]) -> list[str]:
    """纯函数：返回这条提交里命中的身份项，空列表表示干净。

    - 邮箱以 `@users.noreply.github.com` 结尾 → 安全，直接跳过；
    - 否则要求姓名或邮箱命中盯防名单才算「你的泄漏」；
    - watch 为空（降级）时，任何非 noreply 身份都算命中。
    """
    hits: list[str] = []
    for role, who in (("author", author or {}), ("committer", committer or {})):
        name = str(who.get("name") or "").strip()
        mail = str(who.get("email") or "").strip()
        if not mail or mail.lower().endswith(_SAFE_EMAIL_SUFFIXES):
            continue
        hay = f"{name} {mail}".lower()
        if not watch or any(w in hay for w in watch):
            hits.append(f"{role}: {name} <{mail}>")
    return hits


# ------------------------------------------------------------------ 取数


def _api(path: str, limit: int = 200, soft: bool = False) -> object:
    """通过 github-ops 的 ghx api 发只读请求。

    soft=True 时，拿不到内容不抛错，而是返回 {'_skip': 原因} —— 用于
    「空仓库（409）」「无权限（404）」这类不该中断整轮审计的情况。
    """
    if not GHX_PY.is_file():
        raise AuditError(f"未找到 github-ops：{GHX_PY}")
    r = subprocess.run([sys.executable, str(GHX_PY), "api", "GET", path,
                        "--json", "--limit", str(limit)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    body = (r.stdout or "").strip()
    if not body:
        why = _first_line(r.stderr)
        if soft:
            return {"_skip": why or "无输出"}
        raise AuditError(f"ghx api 无输出（{path}）：{why}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        if soft:
            return {"_skip": _first_line(body)}
        raise AuditError(f"ghx api 返回非 JSON（{path}）：{body[:200]}") from exc


def _first_line(text: str) -> str:
    """把 ghx 的多行错误压成一行，去掉它自带的提示尾巴。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith(("提示：", "文档：")):
            return line[:120]
    return ""


class Finding:
    """一条命中。"""

    def __init__(self, repo: str, private: bool, fork: bool, sha: str,
                 branch: str, is_head: bool, identity: str,
                 when: str, message: str, url: str) -> None:
        self.repo = repo
        self.private = private
        self.fork = fork
        self.sha = sha
        self.branch = branch
        self.is_head = is_head
        self.identity = identity
        self.when = when
        self.message = message
        self.url = url

    def as_dict(self) -> dict:
        return dict(repo=self.repo, private=self.private, fork=self.fork,
                    sha=self.sha, branch=self.branch, is_head=self.is_head,
                    identity=self.identity, date=self.when,
                    message=self.message, url=self.url)


def scan(owner: str, watch: list[str], depth: int = DEFAULT_DEPTH,
         only_repo: str = "") -> tuple[list[Finding], list[str], int, list[dict]]:
    """扫描。返回 (命中列表, 跳过说明, 扫过的仓库数, 仓库可见性清单)。

    带上可见性清单，是因为「哪个仓库外人能读」和「谁写的」是同一件事的两半：
    一条脏 commit 在私有仓库里是隐患，在公开仓库里是事故。
    """
    if only_repo:
        repos = [_api(f"/repos/{only_repo}")]
    else:
        repos = _api("/user/repos?per_page=100&visibility=all&affiliation=owner")
    if isinstance(repos, dict):          # 单仓库或错误对象
        if "full_name" not in repos:
            raise AuditError(f"取仓库列表失败：{json.dumps(repos, ensure_ascii=False)[:200]}")
        repos = [repos]

    findings: list[Finding] = []
    skipped: list[str] = []
    inventory: list[dict] = []
    scanned = 0
    for r in sorted(repos, key=lambda x: str(x.get("full_name", ""))):
        full = str(r.get("full_name") or "")
        branch = str(r.get("default_branch") or "main")
        inventory.append({
            "full_name": full,
            "private": bool(r.get("private")),
            "fork": bool(r.get("fork")),
            "size": int(r.get("size") or 0),
            "pushed_at": str(r.get("pushed_at") or ""),
        })
        commits = _api(f"/repos/{full}/commits?per_page={max(1, min(depth, 100))}"
                       f"&sha={branch}", soft=True)
        if isinstance(commits, dict):
            skipped.append(f"{full}：{commits.get('_skip') or '取不到提交'}")
            continue
        scanned += 1
        for i, c in enumerate(commits):
            cm = c.get("commit") or {}
            hits = match_identity(cm.get("author") or {},
                                  cm.get("committer") or {}, watch)
            if not hits:
                continue
            who = cm.get("committer") or cm.get("author") or {}
            findings.append(Finding(
                repo=full,
                private=bool(r.get("private")),
                fork=bool(r.get("fork")),
                sha=str(c.get("sha") or "")[:10],
                branch=branch,
                is_head=(i == 0),
                identity="；".join(hits),
                when=str(who.get("date") or ""),
                message=str(cm.get("message") or "").splitlines()[0][:70],
                url=str(c.get("html_url") or ""),
            ))
    return findings, skipped, scanned, inventory


# ------------------------------------------------------------------ 呈现


def public_overview(inventory: list[dict]) -> list[str]:
    """公开仓库清单。

    为什么单列一节：公开仓库的**提交元数据**对匿名访客是可读的，
    私有仓库不是。所以「有几个公开仓库」直接决定泄漏的暴露面。
    fork 单独标注 —— 公开 fork 转私有会被 GitHub 挡下（`422 Public forks
    can't be made private`），必须先 Leave fork network。
    """
    pub = [r for r in inventory if not r.get("private")]
    if not pub:
        return [f"公开仓库：无（{len(inventory)} 个仓库全部私有）"]
    lines = [f"公开仓库：{len(pub)} 个 / 共 {len(inventory)} 个"
             " —— 匿名访客可读其提交元数据"]
    for r in sorted(pub, key=lambda x: str(x.get("full_name"))):
        tail = ("  ← fork：想转私有得先 Leave fork network"
                if r.get("fork") else "")
        lines.append(f"  ▸ {r.get('full_name')}{tail}")
    return lines


def render(owner: str, findings: list[Finding], skipped: list[str],
           scanned: int, watch: list[str], degraded: bool,
           inventory: list[dict] | None = None) -> str:
    """结论先行，再上清单。"""
    public = [f for f in findings if not f.private]
    private = [f for f in findings if f.private]
    repos_hit = sorted({f.repo for f in findings})

    lines: list[str] = []
    lines.append(f"身份审计（GitHub 账号：{owner}）")
    lines.append(f"  扫过 {scanned} 个仓库，盯防 {len(watch)} 个身份片段"
                 + ("（名单为空 → 所有非 noreply 身份都算可疑）" if degraded else ""))
    lines.append("")

    if not findings:
        lines.append("结论：干净。没有任何仓库的提交里出现你的真名 / 真实邮箱。")
    else:
        lines.append(f"结论：{len(repos_hit)} 个仓库有命中 —— "
                     f"公开 {len({f.repo for f in public})} 个，"
                     f"私有 {len({f.repo for f in private})} 个。")
        lines.append("")
        for f in sorted(findings, key=lambda x: (x.private, x.repo, not x.is_head)):
            vis = "私有" if f.private else "公开"
            lines.append(f"  ▸ [{vis}] {f.repo}"
                         + ("（fork）" if f.fork else "")
                         + f"  {f.branch} @ {f.sha}" + ("（HEAD）" if f.is_head else ""))
            lines.append(f"      {f.identity}")
            lines.append(f"      {f.when}  {f.message}")
            if f.url:
                lines.append(f"      {f.url}")

    if inventory:
        lines.append("")
        lines.extend(public_overview(inventory))

    if skipped:
        lines.append("")
        lines.append("跳过（空仓库或无权限）：")
        for s in skipped:
            lines.append(f"  · {s}")

    if findings:
        lines.append("")
        lines.append("处理顺序：")
        lines.append("  ① 先去仓库设置里改可见性止血（公开 → 私有，立即生效）；")
        lines.append("     公开 fork 改不动，得先 Leave fork network，约 1 分钟后")
        lines.append("     生效，再用 API 改可见性；")
        lines.append("  ② 泄漏的 commit 只有「删库重建」才清得掉 ——")
        lines.append("     force push 只改分支指向，旧 SHA 仍可直访；")
        lines.append("     删库需要令牌带 delete_repo scope，没有就走网页设置页；")
        lines.append("  ③ 本机也要查：`git log --all --format=%ae` 扫一遍，")
        lines.append("     本地历史里留着的那条，下次推送还会再出去一次。")
    return "\n".join(lines)
