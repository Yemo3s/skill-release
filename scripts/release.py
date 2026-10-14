#!/usr/bin/env python3
"""skill-release —— 一句话把技能发到 GitHub / SkillHub / open.workbuddy。

设计原则
--------
1. **一份产物，三处分发**：同一个 staging 目录派生三种形态，内容一致。
   不会出现「GitHub 上是旧版、SkillHub 上是新版」这种对不上的情况。
2. **平台规则前置**：打包前跑规则引擎，阻断项不过就不发。
   把两个平台踩过的坑（无扩展名文件、目录层级）变成本地断言。
3. **默认演练**：不带 --yes 一律 dry-run。发布是有副作用的外部动作。
4. **失败可续**：某个平台失败不影响其他平台，逐个报告结果。

用法见 references/commands.md。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import creds                      # noqa: E402
import frontmatter as fm_mod      # noqa: E402
import pack                       # noqa: E402
import rules                      # noqa: E402

__version__ = "1.4.9"

DEFAULT_WORKDIR = Path.home() / ".workbuddy" / ".release"
VERSION_FILE = "VERSION.txt"


class ReleaseError(RuntimeError):
    pass


# ---------------------------------------------------------------- 通用选项


def add_common(parser: argparse.ArgumentParser, suffix: str) -> None:
    """给某一级解析器注册通用选项。

    每一级用独立的 dest 后缀：argparse 的子解析器默认值会覆盖上一级已解析的
    值，共用同一个 dest 会让「写在子命令前」的选项被静默吃掉。

    并且一律用 argparse.SUPPRESS 作默认值 —— 未设置的选项干脆不进 namespace，
    这样 opt() 用 hasattr 就能区分「没给」和「明确给了 False」。
    若用常规默认值，store_true 的 False 会和「未设置」混淆，
    导致写在子命令前的 --yes 被子命令的 False 覆盖（这是本技能首版真实踩到的 bug）。
    """
    S = argparse.SUPPRESS
    g = parser.add_argument_group("通用选项")
    g.add_argument("--workdir", dest=f"workdir_{suffix}", metavar="DIR", default=S,
                   help=f"暂存目录，默认 {DEFAULT_WORKDIR}")
    g.add_argument("--changelog", dest=f"changelog_{suffix}", metavar="STR", default=S,
                   help="本次变更说明，会送到平台审核侧")
    g.add_argument("-y", "--yes", action="store_true", dest=f"yes_{suffix}", default=S,
                   help="确认执行（不加则一律演练）")
    g.add_argument("--json", action="store_true", dest=f"json_{suffix}", default=S,
                   help="以 JSON 输出")
    g.add_argument("-q", "--quiet", action="store_true", dest=f"quiet_{suffix}", default=S,
                   help="只输出结果行")


def opt(args: argparse.Namespace, name: str, default=None):
    """取选项：子命令级优先，其次顶层，最后默认值。

    依赖 add_common 的 SUPPRESS 默认值 —— 只有真正出现过的选项才会带属性。
    """
    for suf in ("sub", "top"):
        key = f"{name}_{suf}"
        if hasattr(args, key):
            val = getattr(args, key)
            if val is not None:
                return val
    return default


def want_json(args) -> bool:
    return bool(opt(args, "json", False))


# ---------------------------------------------------------------- 载入技能


def load_skill(skill_dir: Path) -> dict:
    d = Path(skill_dir).expanduser().resolve()
    if not d.is_dir():
        raise ReleaseError(f"技能目录不存在：{d}")
    md = d / "SKILL.md"
    if not md.is_file():
        raise ReleaseError(f"缺少 SKILL.md：{md}")
    text = md.read_text(encoding="utf-8")
    try:
        data = fm_mod.parse_frontmatter(text)
    except fm_mod.FrontmatterError as exc:
        raise ReleaseError(f"SKILL.md frontmatter 不合法：{exc}") from exc
    slug = str(data.get("slug") or data.get("name") or d.name).strip()
    if not slug:
        raise ReleaseError("无法确定技能标识：frontmatter 的 slug / name 都为空")
    return {"dir": d, "slug": slug, "fm": data,
            "version": str(data.get("version") or "").strip()}


def bump_version(skill_dir: Path, kind: str) -> str:
    """原地升版本号。三处（SKILL.md / VERSION.txt / 脚本 __version__）一起改。

    之前吃过漏改的亏：升到 1.1.0 时只改了 VERSION 和 frontmatter，
    脚本里的 __version__ 还是 1.0.0，用户报 bug 时对不上号。
    """
    md = Path(skill_dir) / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    data = fm_mod.parse_frontmatter(text)
    cur = str(data.get("version") or "0.0.0").strip()
    parts = [int(x) for x in cur.split(".")[:3]] + [0, 0, 0]
    idx = {"major": 0, "minor": 1, "patch": 2}[kind]
    parts[idx] += 1
    for i in range(idx + 1, 3):
        parts[i] = 0
    new = ".".join(str(p) for p in parts[:3])

    md.write_text(fm_mod.build_skill_md(text, {"version": new}), encoding="utf-8")

    vf = Path(skill_dir) / VERSION_FILE
    if vf.is_file():
        vf.write_text(new + "\n", encoding="utf-8")

    # `_skillhub_meta.json` 里也有一份 version —— 漏掉它会和 VERSION.txt 静默漂移
    # （skill-release 1.4.5→1.4.6 就这么漂过一次）。能解析就同步，坏了不动它。
    meta_f = Path(skill_dir) / "_skillhub_meta.json"
    if meta_f.is_file():
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = None
        if isinstance(meta, dict):
            meta["version"] = new
            meta_f.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")

    touched: list[str] = []
    for py in (Path(skill_dir) / "scripts").glob("*.py"):
        t = py.read_text(encoding="utf-8")
        out = []
        changed = False
        for line in t.split("\n"):
            if line.startswith("__version__") and cur in line:
                out.append(f'__version__ = "{new}"')
                changed = True
            else:
                out.append(line)
        if changed:
            py.write_text("\n".join(out), encoding="utf-8")
            touched.append(py.name)
    return new


# ---------------------------------------------------------------- 暂存


def resolve_slugs(skill: dict, cfg: dict, cli_slug: str = "") -> dict[str, str]:
    """定出每个平台实际要用的 slug。

    默认三个平台同用一个 slug。但 SkillHub 的 slug 是**全网唯一、且会被封禁**的
    （`douyin-downloader` 就被封了），而技能名不该为了迁就平台去改。
    因此支持在配置里写映射：

        "skillhub": {"slug_map": {"douyin-downloader": "douyin-downloaders"}}

    这样源 SKILL.md 保持干净，`--slug` 可临时再覆盖全部平台。
    """
    base = str(cli_slug or skill["slug"]).strip() or skill["slug"]
    out = {t: base for t in rules.PLATFORMS}
    m = cfg.get("skillhub", {}).get("slug_map")
    if isinstance(m, dict):
        over = str(m.get(skill["slug"]) or m.get(base) or "").strip()
        if over:
            out["skillhub"] = over
    return out


def stage_all(skill_dir: Path, slugs: dict[str, str], workdir: Path, author: str,
              targets: tuple[str, ...]) -> dict[str, Path]:
    """为每个目标准备独立暂存目录，避免彼此的注入互相污染。"""
    out: dict[str, Path] = {}
    for t in targets:
        dest = workdir / slugs[t] / t
        out[t] = pack.stage(skill_dir, dest, platform=t, slug=slugs[t], author=author)
    return out


# ---------------------------------------------------------------- 各平台发布


def do_github(skill: dict, staging: Path, cfg: dict, changelog: str,
              realize: bool, slug: str = "") -> dict:
    import pub_github as gh

    if not gh.ghx_available():
        return {"target": "github", "ok": False, "status": "skipped",
                "message": f"未安装 github-ops（{gh.GHX_PY}），跳过 GitHub"}
    gsec = cfg.get("github", {})
    owner = str(gsec.get("owner") or "").strip()
    private = bool(gsec.get("private", True))
    slug = slug or skill["slug"]

    # 提交身份：显式配置优先，否则用 GitHub noreply 形态。
    # **绝不回退到全局 git 身份** —— 那是真名 + 公司邮箱，写进 commit 就删不掉了。
    ident_cfg = gsec.get("identity") or {}
    i_name = str(ident_cfg.get("name") or "").strip()
    i_mail = str(ident_cfg.get("email") or "").strip()
    identity = ((i_name, i_mail) if (i_name and i_mail)
                else gh.noreply_identity(owner, gsec.get("uid") or ""))

    commit = gh.prepare_repo(staging, changelog or f"release: {slug}", identity)
    if not realize:
        return {"target": "github", "ok": True, "status": "dry-run",
                "message": f"已建本地提交 {commit}；待推送至 "
                           f"{owner or '<未配置 owner>'}/{slug}"}

    if not owner:
        return {"target": "github", "ok": False, "status": "error",
                "message": "未配置 github.owner，无法确定推送到哪个账号"}

    ok, msg = gh.create_repo(slug, owner, private,
                             str(skill["fm"].get("description") or "")[:200])
    if not ok:
        return {"target": "github", "ok": False, "status": "error",
                "message": f"建仓失败：{msg[:300]}"}

    ok, out = gh.push(staging, f"{owner}/{slug}")
    if not ok and gh.is_network_failure(out):
        # github.com 不通（直连被墙 / 代理对 CONNECT 返 502）时，改走 api.github.com。
        # 两个域名可达性可以完全不同 —— 实测本机 github.com 超时而 api.github.com 正常，
        # 于是 git push 必失败。API 兜底产出同一棵树、同一个 noreply 身份。
        # 提交消息不传，让兜底复用暂存提交的原文，两条通道算出的 SHA 才会一致（幂等）。
        ok2, out2 = gh.push_via_api(staging, f"{owner}/{slug}")
        if ok2:
            last = [ln for ln in out.strip().splitlines() if ln.strip()]
            return {"target": "github", "ok": True, "status": "pushed",
                    "url": gh.repo_url(owner, slug),
                    "message": (f"git push 到 github.com 失败（{last[-1][:120] if last else ''}），"
                                f"已改走 API 兜底：{out2}")}
        return {"target": "github", "ok": False, "status": "error",
                "url": gh.repo_url(owner, slug),
                "message": (f"git push 失败：{out[-300:]}\n"
                            f"    API 兜底也失败：{out2[-300:]}")}
    return {"target": "github", "ok": ok, "status": "pushed" if ok else "error",
            "url": gh.repo_url(owner, slug),
            "message": (f"已推送到 {owner}/{slug}（本地提交 {commit}）" if ok
                        else out[-500:])}


def do_skillhub(skill: dict, staging: Path, cfg: dict, changelog: str,
                realize: bool, slug: str = "") -> dict:
    import pub_skillhub as sh

    sec = cfg.get("skillhub", {})
    host = str(sec.get("host") or sh.DEFAULT_HOST)
    key = str(sec.get("key") or "").strip()
    cookie = str(sec.get("cookie") or "").strip()
    md = (staging / "SKILL.md").read_text(encoding="utf-8")
    meta = fm_mod.project_for_skillhub(fm_mod.parse_frontmatter(md),
                                       slug_override=slug or skill["slug"])

    if not (key or cookie):
        return {"target": "skillhub", "ok": False, "status": "skipped",
                "message": "未配置 skillhub.key 或 skillhub.cookie，跳过 SkillHub"}
    # 图标：技能根目录 icon.png → skill-icons/upload → publish payload.iconUrl。
    # SkillHub 的 iconUrl **只能**在 publish 时带上（官网表单同一字段），
    # 漏了就会像 skill-release v1.4.3 那样在详情页裸奔。
    if realize:
        icon = skill["dir"] / "icon.png"
        if icon.is_file():
            try:
                meta["iconUrl"] = sh.upload_icon(host, key, icon, cookie=cookie)
            except sh.SkillHubError as exc:
                return {"target": "skillhub", "ok": False, "status": "error",
                        "message": f"图标上传失败（不影响包本身）：{exc}"}
    try:
        res = sh.publish(host, key, staging, meta, changelog or "版本更新",
                         dry_run=not realize, cookie=cookie)
    except sh.SkillHubError as exc:
        return {"target": "skillhub", "ok": False, "status": "error",
                "message": str(exc)}
    res["target"] = "skillhub"
    if res.get("ok"):
        res["url"] = sh.skill_url(host, meta.get("slug", ""))
    return res


def do_workbuddy(skill: dict, staging: Path, cfg: dict, changelog: str,
                 realize: bool, workdir: Path, submit: bool,
                 slug: str = "") -> dict:
    import pub_workbuddy as wb

    sec = cfg.get("workbuddy", {})
    base = str(sec.get("base") or wb.DEFAULT_BASE)
    cookie = str(sec.get("cookie") or "").strip()
    _apply_wb_ua(sec)      # 会话绑定 UA，必须与取 Cookie 时那条请求一致

    slug = slug or skill["slug"]
    version = str(skill["fm"].get("version") or "").strip()
    zip_path = workdir / f"{slug}-{version or 'x'}.zip"
    # 先打包再查凭据：演练时也要留下一份可人工核对的产物，
    # 否则「没配 cookie」会连带把打包环节也跳过，等于什么都没验证。
    pack.make_zip(staging, zip_path, prefix=slug)

    problems = pack.assert_package(zip_path, "workbuddy")
    if problems:
        return {"target": "workbuddy", "ok": False, "status": "error",
                "zip": str(zip_path),
                "message": "包未通过本地上传断言：" + "；".join(problems[:3])}

    if not cookie:
        return {"target": "workbuddy", "ok": False, "status": "skipped",
                "zip": str(zip_path),
                "message": f"已打好包（{zip_path.name}，"
                           f"{zip_path.stat().st_size / 1024:.1f} KB），"
                           f"但未配置 workbuddy.cookie，跳过上传"}

    try:
        # 封面：技能根目录 icon.png → presign(scene=capability_cover) → objectKey。
        # 显式配置 cover_object_key 优先（老配置不破坏），否则自动上传。
        cover_key = str(sec.get("cover_object_key") or "")
        icon = skill["dir"] / "icon.png"
        if realize and not cover_key and icon.is_file():
            cover = wb.upload_cover(base, cookie, icon,
                                    party_id=str(sec.get("party_id") or ""),
                                    enterprise_id=str(sec.get("enterprise_id") or ""))
            cover_key = cover["object_key"]
        res = wb.upload_and_submit(
            base, cookie, zip_path,
            name=slug,
            cover_object_key=cover_key,
            categories=sec.get("categories") or None,
            market_categories=sec.get("market_categories") or None,
            party_id=str(sec.get("party_id") or ""),
            enterprise_id=str(sec.get("enterprise_id") or ""),
            changelog=changelog, submit=submit, dry_run=not realize)
    except wb.WorkBuddyError as exc:
        return {"target": "workbuddy", "ok": False, "status": "error",
                "zip": str(zip_path), "message": str(exc)}
    res["target"] = "workbuddy"
    res["zip"] = str(zip_path)
    return res


# ---------------------------------------------------------------- 子命令


def cmd_check(args) -> int:
    skill = load_skill(Path(args.skill))
    targets = tuple(args.targets)
    cfg = creds.load()
    author = str(cfg.get("author") or "").strip()
    slugs = resolve_slugs(skill, cfg, getattr(args, "slug", "") or "")
    inv, findings = rules.analyze(skill["dir"], skill["fm"], targets, author,
                                  slugs["skillhub"])
    # open.workbuddy 的**资质类目**为空 = 提交后必被审核驳回
    #（「无资质类目字段，需重新选择对应类目」，2026-10-13 实测踩过）。
    # 这里前置成 warn（不阻断：本次可能压根不发 workbuddy）。
    if "workbuddy" in targets:
        sec = cfg.get("workbuddy", {}) or {}
        if not [c for c in (sec.get("categories") or []) if c]:
            findings.append(rules.Finding(
                "warn", "workbuddy", "no-category",
                "未配置 workbuddy.categories（资质类目），提交后会因「无资质类目字段」被驳回",
                "verified",
                "个人主体可选值见 https://open.workbuddy.cn/docs/service-categories-individual"
                "；技术类技能一般填 [\"tools:office\"]（工具-办公）；"
                "同时建议配 market_categories（如 [\"development-tools\"]）",
            ))
    if want_json(args):
        print(json.dumps({
            "skill": skill["slug"], "version": skill["version"], "dir": str(skill["dir"]),
            "slugs": slugs,
            "files": len(inv.files), "bytes": inv.total_bytes,
            "extensions": inv.extensions(),
            "publishable": rules.summarize(findings, targets),
            "findings": [f.__dict__ for f in findings],
        }, ensure_ascii=False, indent=2))
    else:
        print(rules.render_report(inv, findings, targets, slugs))
    ok = all(rules.summarize(findings, targets).values())
    return 0 if ok else 1


def _apply_wb_ua(sec: dict) -> None:
    """把凭据里存的 User-Agent 装进 pub_workbuddy。

    本站会话**绑定客户端 UA**（差一个字符就 302，见 pub_workbuddy.set_user_agent）。
    所以「Cookie + UA」是一对凭据，缺一不可 —— 只带 Cookie 会用兜底 UA 发出去，
    然后被 302 骗成「Cookie 是坏的」。
    """
    import pub_workbuddy as wb

    wb.set_user_agent(str(sec.get("user_agent") or ""))


def cmd_creds(args) -> int:
    # 注意：这是 creds 自己的选项，dest 不带 _sub/_top 后缀，
    # 不能走 opt()（那个是给 add_common 的通用选项用的）。
    from_har = getattr(args, "from_har", None)
    if from_har:
        import pub_workbuddy as wb

        cfg = creds.load()
        sec = cfg.get("workbuddy", {})
        try:
            cookie, info = wb.cookie_from_har(Path(from_har))
        except wb.WorkBuddyError as exc:
            print(f"从 HAR 提取失败：{exc}", file=sys.stderr)
            return 1
        print(f"从 HAR 抽到 {info['host']} 的 Cookie："
              f"{info['bytes']} 字符，session {info['session_len']} 字符，"
              f"项 {', '.join(info['names'])}")
        if info.get("user_agent"):
            print(f"  同一条请求的 User-Agent：{info['user_agent']}")
        else:
            print("  ⚠ 这条请求没有 User-Agent —— 本站会话绑定 UA，验活很可能失败。")
        for w in wb.sniff_cookie(cookie):
            print(f"  ⚠ {w}")
        # 验活才写盘 —— 未验证就落盘，下次 publish 会带着一份坏凭据去撞审核队列。
        # 验活必须带上同一个 UA，否则测的是「兜底 UA 能不能用」，等于没测。
        wb.set_user_agent(str(info.get("user_agent") or ""))
        try:
            wb.whoami(str(sec.get("base") or wb.DEFAULT_BASE), cookie,
                      str(sec.get("party_id") or ""), str(sec.get("enterprise_id") or ""))
        except wb.WorkBuddyError as exc:
            print(f"workbuddy 鉴权未通过，**未写盘**：{exc}", file=sys.stderr)
            return 1
        creds.set_value("workbuddy.cookie", cookie)
        creds.set_value("workbuddy.user_agent", str(info.get("user_agent") or ""))
        print("鉴权通过，已写入 workbuddy.cookie + workbuddy.user_agent")
        print(creds.describe())
        return 0

    if args.set_pairs:
        for pair in args.set_pairs:
            if "=" not in pair:
                print(f"格式应为 key=value，收到：{pair}", file=sys.stderr)
                return 2
            k, v = pair.split("=", 1)
            creds.set_value(k.strip(), v.strip())
        print("已写入配置")
    print(creds.describe())
    return 0


def cmd_doctor(args) -> int:
    import pub_github as gh
    import pub_skillhub as sh

    lines: list[str] = []
    fails = 0
    lines.append("运行环境")
    lines.append(f"  Python        ：{sys.version.split()[0]}")
    lines.append(f"  github-ops    ：{'就位' if gh.ghx_available() else '缺失 ' + str(gh.GHX_PY)}")
    if not gh.ghx_available():
        fails += 1

    cfg = creds.load()
    lines.append("")
    lines.append(creds.describe())
    lines.append("")

    lines.append("GitHub 提交身份")
    gsec = cfg.get("github", {})
    owner = str(gsec.get("owner") or "").strip()
    id_cfg = gsec.get("identity") or {}
    i_name = str(id_cfg.get("name") or "").strip()
    i_mail = str(id_cfg.get("email") or "").strip()
    if i_name and i_mail:
        lines.append(f"  本次将使用 ：{i_name} <{i_mail}>")
        if not i_mail.endswith("@" + gh.NOREPLY_DOMAIN):
            lines.append("  提醒       ：该邮箱会写进公开的 commit 元数据，可被任何人查到")
    else:
        n, e = gh.noreply_identity(owner, gsec.get("uid") or "")
        lines.append(f"  本次将使用 ：{n} <{e}>（noreply，不泄漏真名）")
    gname, gmail = gh.global_identity()
    if gmail:
        safe = gmail.endswith("@" + gh.NOREPLY_DOMAIN)
        lines.append(f"  全局 git   ：{gname} <{gmail}>"
                     + ("（安全）" if safe else "（真实身份；发布时不使用，已隔离）"))
    lines.append("")

    key = str(cfg.get("skillhub", {}).get("key") or "").strip()
    sh_cookie = str(cfg.get("skillhub", {}).get("cookie") or "").strip()
    host = str(cfg.get("skillhub", {}).get("host") or sh.DEFAULT_HOST)
    if key or sh_cookie:
        how = "会话 Cookie" if sh_cookie else "API Key"
        try:
            user = sh.whoami(host, key, cookie=sh_cookie)
            lines.append(f"SkillHub 鉴权 ：通过（{how}；"
                         f"{user.get('handle') or user.get('id') or '已登录'}）")
        except sh.SkillHubError as exc:
            lines.append(f"SkillHub 鉴权 ：失败 —— {exc}")
            fails += 1
    else:
        lines.append("SkillHub 鉴权 ：未配置 key / cookie，跳过")

    import pub_workbuddy as wb
    cookie = str(cfg.get("workbuddy", {}).get("cookie") or "").strip()
    if cookie:
        _apply_wb_ua(cfg.get("workbuddy", {}))
        for w in wb.sniff_cookie(cookie):
            lines.append(f"workbuddy Cookie：⚠ {w}")
        try:
            wb.whoami(str(cfg["workbuddy"].get("base") or wb.DEFAULT_BASE), cookie,
                      str(cfg["workbuddy"].get("party_id") or ""),
                      str(cfg["workbuddy"].get("enterprise_id") or ""))
            lines.append("workbuddy 鉴权：通过")
        except wb.WorkBuddyError as exc:
            lines.append(f"workbuddy 鉴权：失败 —— {exc}")
            fails += 1
    else:
        lines.append("workbuddy 鉴权：未配置 cookie，跳过")

    print("\n".join(lines))
    return 0 if fails == 0 else 1


def cmd_withdraw(args) -> int:
    """撤回 open.workbuddy 上**正在审核**的版本，退回 draft，便于修正后重提。

    为什么需要它：`upload_and_submit()` 发现资产在审核中会**直接跳过**，
    于是「提交后才发现字段填错」就没有补救路径 —— 只能干等驳回。
    2026-10-14 实测出 `POST /capabilities/{assetId}/withdraw` 可以主动退回。
    """
    import pub_workbuddy as wb

    cfg = creds.load()
    sec = cfg.get("workbuddy", {})
    cookie = str(sec.get("cookie") or "").strip()
    if not cookie:
        print("没有 Cookie。先执行：skill-release creds --set workbuddy.cookie=<值>",
              file=sys.stderr)
        return 2
    _apply_wb_ua(sec)
    base = str(sec.get("base") or wb.DEFAULT_BASE)
    party = str(sec.get("party_id") or "")
    ent = str(sec.get("enterprise_id") or "")

    target = str(args.target or "").strip()
    asset_id = target if target.startswith("os_") else ""
    name = str(args.name or "").strip()
    if not asset_id:
        skill = load_skill(Path(target or "."))
        name = name or skill["slug"]
        asset = wb.find_asset(base, cookie, name, party, ent)
        if not asset:
            print(f"没找到名为「{name}」的资产（GET /capabilities/list）", file=sys.stderr)
            return 1
        asset_id = str(asset.get("assetId") or "")
        print(f"命中资产：{name} → {asset_id}（状态 {asset.get('status')}）")
    if not asset_id:
        print("无法确定 assetId（既不是 os_ 开头，也没在列表里匹配到）", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"[演练] 将撤回 {asset_id}，不产生服务端副作用")
        return 0
    try:
        res = wb.withdraw(base, cookie, asset_id, party_id=party, enterprise_id=ent)
    except wb.WorkBuddyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(res["message"])
    return 0


def cmd_probe(args) -> int:
    import pub_workbuddy as wb
    cfg = creds.load()
    sec = cfg.get("workbuddy", {})
    cookie = str(args.cookie or sec.get("cookie") or "").strip()
    if not cookie:
        print("没有 Cookie。先执行：skill-release creds --set workbuddy.cookie=<值>",
              file=sys.stderr)
        return 2
    # 会话绑定 UA：不带上取 Cookie 时那个 UA，会拿到假的 302「Cookie 是坏的」。
    _apply_wb_ua(sec)
    # 再验身世 —— 只拦「根本没有 session」这一种必败情况。
    warns = wb.sniff_cookie(cookie)
    if warns:
        print("Cookie 自检：")
        for w in warns:
            print(f"  ⚠ {w}")
        print()
    base = str(sec.get("base") or wb.DEFAULT_BASE)
    for label, result, note in wb.probe(base, cookie,
                                        str(sec.get("party_id") or ""),
                                        str(sec.get("enterprise_id") or "")):
        print(f"  {label:<34} {result:<10} {note}")
    return 0


def cmd_publish(args) -> int:
    skill = load_skill(Path(args.skill))
    targets = tuple(args.targets)
    cfg = creds.load()
    author = str(cfg.get("author") or "").strip()
    workdir = Path(opt(args, "workdir") or DEFAULT_WORKDIR).expanduser()
    changelog = str(opt(args, "changelog") or "").strip()
    quiet = bool(opt(args, "quiet", False))

    if args.bump:
        new = bump_version(skill["dir"], args.bump)
        skill = load_skill(skill["dir"])
        if not quiet:
            print(f"版本号 {skill['version']} → {new}（已同步 SKILL.md / "
                  f"{VERSION_FILE} / 脚本 __version__）")

    slugs = resolve_slugs(skill, cfg, getattr(args, "slug", "") or "")

    # 提交策略：默认演练，--yes 才真发
    realize = bool(opt(args, "yes", False)) and not args.dry_run

    inv, findings = rules.analyze(skill["dir"], skill["fm"], targets, author,
                                  slugs["skillhub"])
    if not quiet:
        print(rules.render_report(inv, findings, targets, slugs))
    blocked = [f for f in findings if f.blocks and f.platform in targets + ("common",)]
    if blocked and not args.force:
        print("存在阻断项，未发布。加 --force 可强行继续（不建议）。", file=sys.stderr)
        return 1

    if not realize:
        print("演练模式：不会产生任何外部副作用（加 --yes 才真正发布）")

    staging = stage_all(skill["dir"], slugs, workdir, author, targets)
    if not quiet:
        print(f"暂存目录：{workdir / slugs[targets[0]]}")

    results: list[dict] = []
    for t in targets:
        st = staging[t]
        if t == "github":
            r = do_github(skill, st, cfg, changelog, realize, slugs["github"])
        elif t == "skillhub":
            r = do_skillhub(skill, st, cfg, changelog, realize, slugs["skillhub"])
        elif t == "workbuddy":
            r = do_workbuddy(skill, st, cfg, changelog, realize, workdir,
                             submit=not args.no_submit, slug=slugs["workbuddy"])
        else:
            r = {"target": t, "ok": False, "status": "unknown",
                 "message": f"未知目标 {t}"}
        results.append(r)

    if want_json(args):
        print(json.dumps({"skill": skill["slug"], "version": skill["version"],
                          "dry_run": not realize, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print("")
        print("发布结果")
        for r in results:
            mark = "✓" if r.get("ok") else "✗"
            label = rules.PLATFORM_LABEL.get(r["target"], r["target"])
            print(f"  {mark} {label:<15}{r.get('status', ''):<11}{r.get('message', '')}")
            if r.get("url"):
                print(f"      {r['url']}")
            if r.get("zip"):
                print(f"      包：{r['zip']}")

    bad = [r for r in results if not r.get("ok")]
    if not bad:
        return 0
    # 全部跳过（未配置）不算失败
    if all(r.get("status") == "skipped" for r in bad):
        return 0
    return 1


def cmd_audit(args) -> int:
    """只读审计 GitHub 账号下所有仓库的提交身份。

    发布链路的身份闸门只能拦「这一次」，拦不住已经推上去的、或用别的工具推的。
    这条命令补上那一半：把账号下每个仓库的提交元数据扫一遍。
    """
    import audit_identity as audit

    cfg = creds.load()
    owner = str(cfg.get("github", {}).get("owner") or "").strip()
    if not owner:
        raise ReleaseError("未配置 github.owner，无法确定审计哪个账号")

    depth = int(getattr(args, "depth", None) or audit.DEFAULT_DEPTH)
    only = str(getattr(args, "only_repo", "") or "").strip()
    watch, degraded = audit.build_watch(getattr(args, "watch", None) or [])

    try:
        findings, skipped, scanned, inventory = audit.scan(owner, watch, depth, only)
    except audit.AuditError as exc:
        raise ReleaseError(str(exc)) from exc

    if want_json(args):
        print(json.dumps({
            "owner": owner, "scanned_repos": scanned, "depth": depth,
            "watch": watch, "degraded": degraded, "skipped": skipped,
            "findings": [f.as_dict() for f in findings],
            "repos": inventory,
            "public_repos": sorted(r["full_name"] for r in inventory
                                   if not r["private"]),
            "public_repos_with_findings": sorted({f.repo for f in findings
                                                  if not f.private}),
        }, ensure_ascii=False, indent=2))
    else:
        print(audit.render(owner, findings, skipped, scanned, watch, degraded,
                           inventory))
    return 1 if findings else 0


# ---------------------------------------------------------------- 解析器


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="release.py",
        description="把技能一句话发到 GitHub / SkillHub / open.workbuddy",
    )
    p.add_argument("--version", action="version", version=__version__)
    add_common(p, "top")

    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="只跑规则引擎，不发布")
    c.add_argument("skill", nargs="?", default=".")
    c.add_argument("--targets", default=",".join(rules.PLATFORMS),
                   help="逗号分隔：github,skillhub,workbuddy")
    c.add_argument("--slug", help="临时覆盖 slug（不改源文件；也可写进配置 skillhub.slug_map）")
    add_common(c, "sub")
    c.set_defaults(func=cmd_check, targets_arg=True)

    d = sub.add_parser("doctor", help="体检：环境、凭据、各平台鉴权")
    add_common(d, "sub")
    d.set_defaults(func=cmd_doctor)

    cr = sub.add_parser("creds", help="查看 / 写入凭据")
    cr.add_argument("--set", dest="set_pairs", action="append", metavar="KEY=VALUE",
                    help="如 --set skillhub.key=skh_xxx（可重复）")
    cr.add_argument("--from-har", metavar="FILE.har",
                    help="从浏览器导出的 HAR 里抽 open.workbuddy 的 Cookie，"
                         "验活通过才写盘（避开约 4 KB session 被复制截断的问题）")
    add_common(cr, "sub")
    cr.set_defaults(func=cmd_creds)

    pr = sub.add_parser("probe", help="open.workbuddy 只读探测（不发任何东西）")
    pr.add_argument("--cookie", help="临时指定 Cookie，不写盘")
    add_common(pr, "sub")
    pr.set_defaults(func=cmd_probe)

    wd = sub.add_parser("withdraw",
                        help="撤回 open.workbuddy 待审版本（退回 draft，修正后可重提）")
    wd.add_argument("target", nargs="?", default=".",
                    help="技能目录，或直接给 assetId（os_...）")
    wd.add_argument("--name", help="按技能名找资产时覆盖名字（默认取 frontmatter 的 slug/name）")
    wd.add_argument("--dry-run", action="store_true", help="只打印，不真撤回")
    add_common(wd, "sub")
    wd.set_defaults(func=cmd_withdraw)

    pub = sub.add_parser("publish", help="发布（默认演练，加 --yes 才真发）")
    pub.add_argument("skill", nargs="?", default=".")
    pub.add_argument("--targets", default=",".join(rules.PLATFORMS),
                     help="逗号分隔：github,skillhub,workbuddy")
    pub.add_argument("--bump", choices=("patch", "minor", "major"),
                     help="发布前升版本号（会改源文件）")
    pub.add_argument("--slug", help="临时覆盖 slug（不改源文件；SkillHub 的 slug 可能被封禁）")
    pub.add_argument("--force", action="store_true", help="忽略阻断项（不建议）")
    pub.add_argument("--dry-run", action="store_true", help="强制演练")
    pub.add_argument("--no-submit", action="store_true",
                     help="open.workbuddy 只上传不提交审核")
    add_common(pub, "sub")
    pub.set_defaults(func=cmd_publish)

    au = sub.add_parser("audit", help="只读审计：扫账号下所有仓库的提交身份")
    au.add_argument("--repo", dest="only_repo", metavar="OWNER/NAME",
                    help="只查一个仓库，默认查账号下全部")
    au.add_argument("--depth", type=int, default=100, metavar="N",
                    help="每个仓库查最近 N 条提交（上限 100），默认 100")
    au.add_argument("--watch", action="append", metavar="STR",
                    help="追加盯防的身份片段（姓名或邮箱），可重复")
    add_common(au, "sub")
    au.set_defaults(func=cmd_audit)

    return p


def _split_targets(raw: str) -> list[str]:
    out = [t.strip() for t in raw.split(",") if t.strip()]
    bad = [t for t in out if t not in rules.PLATFORMS]
    if bad:
        raise SystemExit(f"未知目标：{', '.join(bad)}（可选 {', '.join(rules.PLATFORMS)}）")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "targets", None) is not None:
        args.targets = _split_targets(args.targets)
    try:
        return args.func(args)
    except ReleaseError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
