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
import watchlist                  # noqa: E402

__version__ = "1.5.7"

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
                realize: bool, slug: str = "",
                price_per_call: float | None = None) -> dict:
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
    # 付费：命令行 --pay per_call --price X 优先；其次配置 skillhub.price_per_call。
    # SkillHub 只有按次计费一种付费形态（x402），买断/订阅是 open.workbuddy 的档位。
    if price_per_call is None:
        cfg_price = sec.get("price_per_call")
        try:
            price_per_call = float(cfg_price) if cfg_price else None
        except (TypeError, ValueError):
            price_per_call = None
    if price_per_call is not None:
        if not 0.01 <= float(price_per_call) <= 100:
            return {"target": "skillhub", "ok": False, "status": "error",
                    "message": f"per_call 单价 {price_per_call} 越界：0.01–100 元/次"}
        print(f"  SkillHub 付费发布：{price_per_call:.2f} 元/次（per_call）")
        # 入驻前置检查：没入驻的账号发付费只会白挨一次 403
        blockers: list[str] = []
        try:
            st = sh.aipay_status(host, token=key, cookie=cookie)
            if not st.get("onboarded"):
                blockers.append("SkillPay 未入驻（/api/v1/aipay/status onboarded=false）。"
                                "先到商户中心完成「SkillPay 个人入驻」（手机号验证码）")
        except sh.SkillHubError as exc:
            print(f"  ⚠ 入驻状态查询失败（{exc}）")
        # 官方 X402 预检：SKILL.md 必须描述真实 402 收费链路
        try:
            pc = sh.precheck_paid_skill(host, staging / "SKILL.md",
                                        token=key, cookie=cookie)
            compliance = str(pc.get("compliance") or "")
            if compliance != "pass":
                blockers.append(f"官方预检未过（compliance={compliance or 'unknown'}）："
                                f"{json.dumps(pc, ensure_ascii=False)[:300]}。"
                                "付费 Skill 需要真实 402 收费后端，纯知识包发不了")
            else:
                print("  官方预检：pass")
        except sh.SkillHubError as exc:
            print(f"  ⚠ 预检请求失败（{exc}）")
        if blockers:
            if realize:
                return {"target": "skillhub", "ok": False, "status": "error",
                        "message": "；".join(blockers)}
            for b in blockers:
                print(f"  ⚠ [演练，不阻断] {b}")
            price_per_call = None    # 演练时降级为免费形态，只看不发
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
                         dry_run=not realize, cookie=cookie,
                         price_per_call=price_per_call)
    except sh.SkillHubError as exc:
        return {"target": "skillhub", "ok": False, "status": "error",
                "message": str(exc)}
    res["target"] = "skillhub"
    if res.get("ok"):
        res["url"] = sh.skill_url(host, meta.get("slug", ""))
    return res


def do_workbuddy(skill: dict, staging: Path, cfg: dict, changelog: str,
                 realize: bool, workdir: Path, submit: bool,
                 slug: str = "", pay_mode: int | None = None,
                 pay_amount: float | None = None,
                 pay_days: int | None = None) -> dict:
    import pub_workbuddy as wb

    sec = cfg.get("workbuddy", {})
    base = str(sec.get("base") or wb.DEFAULT_BASE)
    cookie = str(sec.get("cookie") or "").strip()
    _apply_wb_ua(sec)      # 会话绑定 UA，必须与取 Cookie 时那条请求一致

    # 付费参数：命令行优先，其次配置，最后免费。金额对外用「元」，提交时换算成分。
    if pay_mode is None:
        pay_mode = int(sec.get("pay_mode") or 0)
    if pay_amount is None:
        pay_amount = float(sec.get("pay_amount") or 0)
    if pay_days is None:
        pay_days = int(sec.get("pay_days") or 0)
    pay_mode = int(pay_mode or 0)
    pay_cents = int(round(float(pay_amount or 0) * 100))
    if pay_mode and pay_cents <= 0:
        return {"target": "workbuddy", "ok": False, "status": "error",
                "message": "设了付费却没给价格（--price，单位：元）"}
    if pay_mode == 2 and not pay_days:
        pay_days = 30       # 月订阅的默认周期

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
            categories=_as_list(sec.get("categories")),
            market_categories=_as_list(sec.get("market_categories")),
            party_id=str(sec.get("party_id") or ""),
            enterprise_id=str(sec.get("enterprise_id") or ""),
            changelog=changelog, submit=submit, dry_run=not realize,
            pay_mode=pay_mode, pay_amount=pay_cents, pay_days=pay_days)
    except wb.WorkBuddyError as exc:
        return {"target": "workbuddy", "ok": False, "status": "error",
                "zip": str(zip_path), "message": str(exc)}
    res["target"] = "workbuddy"
    res["zip"] = str(zip_path)

    # 提交成功就把资产登记进巡航列表 —— 这是「发完就不用管」的关键一步。
    # 资产状态机是 draft → under_review → approved → published，
    # 过审之后**还要再点一次发布**才真正上架；这一步人一定会忘（实测忘过）。
    # 登记之后 `watch --check --ship` 每天扫一遍，过审就自动上线。
    if res.get("status") == "submitted" and res.get("asset_id"):
        try:
            watchlist.add({
                "assetId": res["asset_id"],
                "name": slug,
                "version": version,
                "versionId": res.get("version_id"),
                "payMode": pay_mode,
                "price": round(pay_cents / 100, 2) if pay_mode else 0,
                "payDays": pay_days if pay_mode == 2 else 0,
                "dir": str(skill["dir"]),
                "changelog": changelog,
                "target": "workbuddy",
                "lastStatus": "under_review",
                "action": "waiting",
            }, workdir)
            res["watched"] = True
        except (OSError, ValueError):
            # 巡航登记失败不该让「已经提交成功」这件事看起来像失败
            res["watched"] = False
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
        # 付费配置自检：付费是版本级属性、过审即锁死、免费资产不能转付费，
        # 所以「打算收费却配置不完整」必须在提交前拦下，而不是等驳回后再撤回。
        pm = int(sec.get("pay_mode") or 0)
        if pm not in (0, 1, 2):
            findings.append(rules.Finding(
                "error", "workbuddy", "bad-pay-mode",
                f"workbuddy.pay_mode={pm} 非法，只能是 0免费 / 1买断 / 2订阅",
                "verified", "改配置：release.py creds --set workbuddy.pay_mode=1"))
        elif pm and float(sec.get("pay_amount") or 0) <= 0:
            findings.append(rules.Finding(
                "error", "workbuddy", "pay-without-price",
                "配置了付费方式但 workbuddy.pay_amount 为空（单位：元）",
                "verified",
                "补价格：release.py creds --set workbuddy.pay_amount=29；"
                "或发布时用 --pay buyout --price 29"))
        elif pm == 2 and not int(sec.get("pay_days") or 0):
            findings.append(rules.Finding(
                "warn", "workbuddy", "sub-without-days",
                "配置了月订阅但没写 workbuddy.pay_days，提交时会默认 30 天",
                "verified", "如需自定义周期：creds --set workbuddy.pay_days=30"))
        # 收入天花板门槛：付费资产在提交前必须能算出「值得做」的理由。
        # 2026-10-27 launch-guard 事故 → 这条从「事后复盘文字」变成「提交前阻塞项」。
        if pm:
            price = float(sec.get("pay_amount") or 0)
            aud = sec.get("audience")
            if aud in (None, ""):
                findings.append(rules.Finding(
                    "warn", "workbuddy", "pay-no-audience",
                    "付费发布但没提供可触达人群估算，无法验证是否值得做",
                    "verified",
                    "先看同类免费资产的安装量：release.py metrics --days 90；"
                    "再 creds --set workbuddy.audience=<人数>。"
                    "天花板 = 人群 × 10% × 价格，低于 ¥300 就别做"))
            else:
                rc = rules.revenue_ceiling(int(aud), price)
                findings.append(rules.Finding(
                    "info" if rc["pass"] else "warn",
                    "workbuddy", "pay-ceiling",
                    rc["verdict"], "verified",
                    "这只是估算（转化率按 10% 拍的）。人群要取「已证明会装这类东西的人」，"
                    "不是全平台用户数。要不要做你定 —— 拿它当参考，别当判决"))
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


def cmd_pay(args) -> int:
    """只读回看某个资产的付费状态（发布前定档 / 发布后核验都用它）。

    存在的意义：付费方式是**版本级**属性，过审即锁死、免费资产不能转付费，
    而且「界面上显示 29 元」不代表服务端记的是 29 元 —— 2026-10-26 实测过
    控制台弹窗只是前端状态、submit 载荷没带上就静默变免费。所以宁可多问一句
    服务端，也不要在驳回/上线后才发现定错了价。
    """
    import pub_workbuddy as wb

    cfg = creds.load()
    sec = cfg.get("workbuddy", {})
    cookie = str(sec.get("cookie") or "").strip()
    if not cookie:
        print("没有 Cookie。先执行：skill-release login 或 creds --set workbuddy.cookie=<值>",
              file=sys.stderr)
        return 2
    _apply_wb_ua(sec)
    base = str(sec.get("base") or wb.DEFAULT_BASE)
    party = str(sec.get("party_id") or "")
    ent = str(sec.get("enterprise_id") or "")

    target = str(getattr(args, "target", "") or "").strip()
    asset_id = target if target.startswith("os_") else ""
    if not asset_id:
        name = str(getattr(args, "name", "") or "").strip()
        if not name:
            try:
                name = load_skill(Path(target or "."))["slug"]
            except Exception:
                name = ""
        if not name:
            print("给一个 assetId（os_...）、技能目录或 --name。", file=sys.stderr)
            return 2
        asset = wb.find_asset(base, cookie, name, party, ent)
        if not asset:
            print(f"没找到名为「{name}」的资产", file=sys.stderr)
            return 1
        asset_id = str(asset.get("assetId") or "")
    try:
        st = wb.pay_state(base, cookie, asset_id, party_id=party, enterprise_id=ent)
    except wb.WorkBuddyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if want_json(args):
        print(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        price = ""
        if st["prices"] and st["pay_mode"] == 1:
            price = f" ¥{st['prices'][0]:g}"
        elif st["prices"] and st["pay_mode"] == 2:
            price = f" ¥{st['prices'][0]:g}/{st['days'][0] if st['days'] else '?'} 天"
        print(f"资产 {st['asset_id']}")
        print(f"  状态      ：{st['status']}（版本 {st['version'] or '-'} / {st['version_status'] or '-'}）")
        print(f"  付费方式  ：{st['pay_mode_label']}{price}")
        print(f"  原始字段  ：{json.dumps(st['pay_detail'], ensure_ascii=False)}")
        if st["pay_mode"] == 0:
            print("  提示      ：当前是免费。付费只能在 submit 时带上，且过审后不可改。")
    return 0


def _playwright_python() -> str:
    """找装了 playwright 的解释器。

    release.py 自己跑在托管 Python 上（不带 playwright），所以扫码登录这步
    要借隔离 venv 的解释器 —— 不在主环境里装重依赖，避免污染发布链路。
    """
    cands = [
        Path.home() / ".workbuddy/binaries/python/envs/default/Scripts/python.exe",
        Path.home() / ".workbuddy/binaries/python/envs/default/bin/python",
    ]
    for c in cands:
        if c.is_file():
            return str(c)
    return ""


def cmd_login(args) -> int:
    """扫码登录 open.workbuddy.cn，自动把会话写进凭据。

    比导 HAR 少三步：不用开 DevTools、不用勾「含敏感数据」、不用手抄 4 KB 的
    session（手抄必被截断）。同时会把浏览器真实 UA 一起写进去 ——
    平台会话与 UA 绑定，cookie 对了 UA 不对照样 302。
    """
    import subprocess

    target = getattr(args, "target", "workbuddy") or "workbuddy"
    script = _HERE / ("login_skillhub.py" if target == "skillhub"
                      else "login_workbuddy.py")
    if not script.is_file():
        print(f"缺少登录脚本：{script}", file=sys.stderr)
        return 2

    py = _playwright_python()
    if not py:
        print("未找到装了 playwright 的解释器。先建隔离环境：", file=sys.stderr)
        print("  <托管python> -m venv ~/.workbuddy/binaries/python/envs/default", file=sys.stderr)
        print("  ~/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m pip install playwright",
              file=sys.stderr)
        return 2

    cmd = [py, str(script)]
    if getattr(args, "check_only", False):
        cmd.append("--check-only")
    elif getattr(args, "refresh", False):
        # 只对 workbuddy 有意义：把活着的浏览器 profile 会话重新导回配置
        cmd.append("--refresh")
    else:
        cmd += ["--timeout", str(int(getattr(args, "timeout", 300) or 300))]
        if getattr(args, "fresh", False):
            cmd.append("--fresh")

    return subprocess.call(cmd)


def cmd_ship(args) -> int:
    """把「过审待发布」的资产全部上线 —— 过审 ≠ 上线，中间还隔一个发布按钮。

    资产状态机里有 `approved`（过审待发布）与 `published`（已上线）两态；
    长期不登控制台的话，过审的版本就会一直卡在前者。本命令扫一遍资产列表，
    把所有 approved 的直接调发布接口上线，平时挂自动化跑，人就不用记了。
    """
    import pub_workbuddy as wb

    cfg = creds.load()
    sec = cfg.get("workbuddy", {})
    cookie = str(sec.get("cookie") or "").strip()
    if not cookie:
        print("没有 Cookie。先执行：release.py login（扫码）", file=sys.stderr)
        return 2
    _apply_wb_ua(sec)
    base = str(sec.get("base") or wb.DEFAULT_BASE)
    party = str(sec.get("party_id") or "")
    ent = str(sec.get("enterprise_id") or "")

    try:
        pending = wb.pending_release(base, cookie, party_id=party, enterprise_id=ent)
    except wb.WorkBuddyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not pending:
        print("没有过审待发布的资产，全部在线 ✓")
        return 0
    print(f"过审待发布 {len(pending)} 个：")
    for it in pending:
        print(f"  - {it['name']}  {it.get('version') or '?'}  ({it['asset_id']})")
    if args.dry_run or not args.yes:
        print("[演练] 未真正发布。确认无误后加 --yes 执行。")
        return 0

    failed = 0
    for it in pending:
        try:
            res = wb.publish_asset(base, cookie, it["asset_id"],
                                   visible_scope=[args.scope],
                                   party_id=party, enterprise_id=ent)
            print(f"  ✓ {it['name']}: {res['message']}")
        except wb.WorkBuddyError as exc:
            failed += 1
            print(f"  ✗ {it['name']}: {exc}", file=sys.stderr)
    if failed:
        print(f"{failed} 个发布失败，见上方报错", file=sys.stderr)
        return 1
    print(f"全部上线完成（{len(pending)} 个）")
    return 0


def cmd_watch(args) -> int:
    """审核巡航：把「等审核」这件事从人脑里挪到列表里。

    三种用法：
      watch                  看列表（谁在等、等多久、查过几次）
      watch --check --ship   跑一次巡检，过审的自动上线，落地的自动出列
      watch --stop           停止巡航（清空列表）

    **为什么要有 --ship**：过审（approved）不等于上线（published），
    中间还隔着一次「发布」调用。大家都以为审核通过就完事了，于是资产
    在 approved 状态里躺到下次想起来为止 —— 这才是最常亏的一段时间。

    **为什么列表空了就算「巡航结束」**：列表只装「在等结果」的资产。
    被驳回、被撤回、已上线都不需要再等，出列即完结。所以
    `--json` 输出里 `done: true` 就是自动化任务停掉自己的信号。
    """
    import pub_workbuddy as wb

    workdir = Path(opt(args, "workdir") or DEFAULT_WORKDIR).expanduser()
    json_out = want_json(args)
    quiet = bool(opt(args, "quiet", False))

    # ---- 停止巡航
    if args.stop:
        if args.asset:
            ok = watchlist.remove(args.asset, workdir)
            if json_out:
                print(json.dumps({"stopped": ok, "assetId": args.asset},
                                 ensure_ascii=False))
            else:
                print(f"{'已移出巡航' if ok else '列表里没有这个资产'}：{args.asset}")
            return 0
        n = watchlist.clear(workdir)
        if json_out:
            print(json.dumps({"stopped": True, "removed": n}, ensure_ascii=False))
        else:
            print(f"巡航已停止，清空 {n} 个待审登记" if n else "巡航列表本来就是空的")
        return 0

    data = watchlist.load(workdir)
    items = data["items"]
    if args.asset:
        items = [it for it in items if it.get("assetId") == args.asset]

    # ---- 只看列表
    if not args.check:
        if json_out:
            print(json.dumps({"items": items, "summary": watchlist.summary(items)},
                             ensure_ascii=False, indent=2))
            return 0
        if not items:
            print("巡航列表为空 —— 没有在等的资产（发版时会自动登记）")
            return 0
        print(f"待审 {len(items)} 个：")
        for it in items:
            pay = (f"¥{it.get('price')} 买断" if int(it.get("payMode") or 0) == 1
                   else f"¥{it.get('price')}/月" if int(it.get("payMode") or 0) == 2
                   else "免费")
            print(f"  {it.get('assetId')}  {str(it.get('name') or ''):<22}"
                  f"v{str(it.get('version') or '?'):<8}{pay:<12}"
                  f"{str(it.get('lastStatus') or '?'):<13}"
                  f"查过 {int(it.get('checks') or 0)} 次")
            if it.get("lastCheckedAt"):
                print(f"      上次检查 {it['lastCheckedAt']}")
        print("\n巡检一次：release.py watch --check --ship")
        return 0

    # ---- 巡检
    if not items:
        if json_out:
            print(json.dumps({"checked": 0, "results": [],
                              "summary": watchlist.summary([]), "done": True},
                             ensure_ascii=False))
        else:
            print("巡航列表为空，无需巡检 ✓")
        return 0

    cfg = creds.load()
    sec = cfg.get("workbuddy", {})
    cookie = str(sec.get("cookie") or "").strip()
    if not cookie:
        print("没有 Cookie。先执行：release.py login（扫码）", file=sys.stderr)
        return 2
    _apply_wb_ua(sec)
    base = str(sec.get("base") or wb.DEFAULT_BASE)
    party = str(sec.get("party_id") or "")
    ent = str(sec.get("enterprise_id") or "")

    results: list[dict] = []
    remaining: list[str] = []
    for it in items:
        aid = str(it["assetId"])
        row = {"assetId": aid, "name": it.get("name"),
               "version": it.get("version"), "payMode": int(it.get("payMode") or 0),
               "price": it.get("price")}
        try:
            st = wb.review_state(base, cookie, aid, party_id=party, enterprise_id=ent)
        except wb.WorkBuddyError as exc:
            msg = str(exc)
            row.update({"action": "error", "message": msg})
            results.append(row)
            remaining.append(aid)
            # 会话失效是全局问题，后面几条也必然失败，直接中断更省事
            if "会话失效" in msg or "403" in msg:
                break
            continue

        row["remote_status"] = st["status"]
        row["remote_version"] = st["version"]
        row["pending_status"] = st["pending"]["status"]
        row["current_version"] = st["current"]["version"]
        action = watchlist.classify(st["status"], st["pending"]["status"],
                                    st["current"]["status"])
        row["action"] = action

        if action == "ship":
            if not args.ship:
                row["note"] = "已过审待上线（加 --ship 自动发）"
                remaining.append(aid)
            else:
                try:
                    r = wb.publish_asset(base, cookie, aid,
                                         visible_scope=[args.scope],
                                         party_id=party, enterprise_id=ent)
                    row["action"] = "shipped"
                    row["note"] = r["message"]
                except wb.WorkBuddyError as exc:
                    row["action"] = "ship_failed"
                    row["note"] = str(exc)
                    remaining.append(aid)
        elif action in watchlist.RESOLVED:
            if action == "rejected":
                row["note"] = (st["pending"]["reject_reason"]
                               or st["current"]["reject_reason"]
                               or "被驳回，原文见控制台")
        else:
            # waiting / unknown：留在列表里
            remaining.append(aid)
            if action == "unknown":
                row["note"] = f"状态无法识别：{st['status']!r}，请人工看一眼"

        results.append(row)
        watchlist.touch(aid, status=st["status"], workdir=workdir)

    # 出列：已落地的不必再等
    for row in results:
        if row.get("action") in ("shipped", "done", "rejected", "withdrawn"):
            watchlist.remove(row["assetId"], workdir)

    left = [it for it in watchlist.load(workdir)["items"]]
    done = not left

    if json_out:
        print(json.dumps({"checked": len(results), "results": results,
                          "remaining": left,
                          "summary": watchlist.summary(left),
                          "done": done}, ensure_ascii=False, indent=2))
        return 0

    mark = {"shipped": "✓ 已上线", "done": "✓ 已在线", "waiting": "… 审核中",
            "rejected": "✗ 被驳回", "withdrawn": "✗ 已撤回",
            "ship_failed": "✗ 上线失败", "unknown": "? 状态未知",
            "error": "✗ 读不到"}
    if not quiet:
        print(f"巡航巡检（{len(results)} 个）")
    for row in results:
        label = mark.get(row.get("action"), row.get("action"))
        ver = row.get("remote_version") or row.get("version") or "?"
        print(f"  {label:<10}{str(row.get('name') or ''):<22}v{ver}")
        if row.get("note"):
            print(f"      {row['note']}")
    print("")
    if done:
        print("全部落地，巡航列表已清空 —— 可以停止巡航了 ✓")
    else:
        print(f"仍有 {len(left)} 个待审，巡航继续")
    bad = [r for r in results if r.get("action") in ("error", "ship_failed")]
    return 1 if bad else 0


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


def cmd_metrics(args) -> int:
    """看自己已发资产到底有多少人在用 —— 定价与选题之前先看这张表。

    比凭感觉判断可靠的地方：留存是含真话的指标。装机高但周留存个位数，
    说明是「一次性工具」，用户情绪里没有复购理由，硬转订阅必失败。
    """
    script = Path(__file__).resolve().parent / "metrics_workbuddy.py"
    if not script.is_file():
        print(f"缺少指标脚本：{script}", file=sys.stderr)
        return 2
    import subprocess
    cmd = [sys.executable, str(script), "--days", str(int(args.days))]
    if getattr(args, "out", None):
        cmd += ["--json", args.out]
    if args.only:
        cmd += ["--only", args.only]
    return subprocess.call(cmd)


def _as_list(v):
    """creds 里 JSON 数组经常以字符串形态存进来（--set categories=["a"] 落盘成字符串）。

    直接把字符串塞进 fields.common.categories 会触发平台端「逐字符遍历」——
    2026-10-26 实测把 '["tools:office"]' 拆成了 16 个单字符 categoryId（乱码类目），
    且类目字段 update 端点只读、改不回来，只能重传包修复。这里必须在出口处转成真数组。
    """
    if v is None or v == "":
        return None
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else [v]
        except (ValueError, TypeError):
            return [x.strip() for x in v.split(",") if x.strip()]
    return [v]


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

    # 付费：命令行 > 配置。--pay free 可显式覆盖配置里的付费值（做免费版时很有用）。
    # 档位按平台分家（2026-10-27）：
    #   workbuddy：buyout 买断 / subscription 订阅（版本级、过审锁死）
    #   skillhub ：per_call 按次调用（x402，0.01–100 元/次，需 SkillPay 入驻 + 收费后端）
    pay_mode = getattr(args, "pay", None)
    pay_map = {"free": 0, "buyout": 1, "subscription": 2}
    price = getattr(args, "price", None)
    sh_price_per_call: float | None = None
    if pay_mode == "per_call":
        if "skillhub" not in targets:
            print("--pay per_call 只对 SkillHub 生效，本次目标里没有 skillhub。",
                  file=sys.stderr)
            return 1
        if not price:
            print("付费发布必须给 --price（单位：元/次，如 --price 0.10）。", file=sys.stderr)
            return 1
        sh_price_per_call = float(price)
        pay_mode = 0          # wb 侧不带付费档
    else:
        if pay_mode is not None:
            pay_mode = pay_map[pay_mode] if isinstance(pay_mode, str) else int(pay_mode)
        if pay_mode and not price:
            print("付费发布必须给 --price（单位：元，如 --price 29）。", file=sys.stderr)
            return 1
        if pay_mode == 2 and not args.pay_days:
            print("订阅请给 --pay-days（周期天数，如 30）。", file=sys.stderr)
            return 1
        if pay_mode and "workbuddy" not in targets:
            print("提示：buyout/subscription 只对 open.workbuddy 生效，"
                  "本次目标里没有 workbuddy。", file=sys.stderr)
        if pay_mode and "skillhub" in targets:
            print("提示：SkillHub 没有买断/订阅档（只有 per_call 按次），"
                  "本次 SkillHub 仍按免费发布。", file=sys.stderr)

    # 收入天花板：**摆在台面上，不替人做决定**。
    #
    # 2026-10-27 的教训是「根本没算就做完了整套」，所以要算。
    # 但「算出来 ¥26 所以别做」是我的处境（一天工钱 ¥300），不是所有人的标准：
    # 别人可能外站有粉丝、或只是想立个价格锚、或用付费筛人。
    # 通用工具一旦替人做价值判断，就和这次犯的错同源 —— 拿自己的处境当别人的标准。
    # 所以：给了 --audience 就把算式和结论打出来；不给就提一句；只有显式 --strict 才拦。
    audience = getattr(args, "audience", None)
    if audience in (None, ""):
        audience = (cfg.get("workbuddy") or {}).get("audience")
    strict = bool(getattr(args, "strict", False)) or bool(
        (cfg.get("workbuddy") or {}).get("strict_ceiling"))
    if pay_mode:
        if audience in (None, "", 0):
            msg = ("提示：未提供 --audience，跳过收入天花板估算。\n"
                   "  想知道值不值得做，先看同类免费资产的安装量：\n"
                   "    release.py metrics --days 90\n"
                   "  再带上人群数：--audience 9（天花板 = 人群 × 转化率 × 价格）")
            if strict:
                print("\n--strict 模式下必须给 --audience。\n" + msg, file=sys.stderr)
                return 1
            print(msg, file=sys.stderr)
        else:
            rc = rules.revenue_ceiling(int(audience), args.price or 0)
            mark = "✓" if rc["pass"] else "!"
            print(f"\n{mark} 收入天花板：{rc['verdict']}", file=sys.stderr)
            if not rc["pass"]:
                if strict:
                    print("  --strict 模式下不过线不提交。要去掉 --strict 或调高人群/价格估算。",
                          file=sys.stderr)
                    return 1
                print("  这只是估算（转化率按 10% 拍的），要不要做你定；"
                      "加 --strict 可让工具替你拦。", file=sys.stderr)

    results: list[dict] = []
    for t in targets:
        st = staging[t]
        if t == "github":
            r = do_github(skill, st, cfg, changelog, realize, slugs["github"])
        elif t == "skillhub":
            r = do_skillhub(skill, st, cfg, changelog, realize, slugs["skillhub"],
                            price_per_call=sh_price_per_call)
        elif t == "workbuddy":
            r = do_workbuddy(skill, st, cfg, changelog, realize, workdir,
                             submit=not args.no_submit, slug=slugs["workbuddy"],
                             pay_mode=pay_mode, pay_amount=args.price,
                             pay_days=args.pay_days)
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

    # 提交成功就明说下一步，否则「登记了但没人开巡航」等于没登记
    watched = [r for r in results if r.get("watched")]
    if watched and not want_json(args):
        names = "、".join(str(r.get("name") or r.get("asset_id") or "?") for r in watched)
        print("")
        print(f"已登记进巡航（{len(watched)} 个：{names}）—— release.py watch 可查看")
        print("把「技能审核巡航（过审自动上线，落地自停）」任务置为 ACTIVE，"
              "就会每天盯到上线，全部落地后它自己暂停")

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

    mt = sub.add_parser("metrics",
                        help="看已发资产的真实运营指标（安装/活跃/调用/留存），只读")
    mt.add_argument("--days", type=int, default=90, help="回溯天数，默认 90")
    mt.add_argument("--out", metavar="FILE", help="把汇总另存为 JSON 文件")
    mt.add_argument("--only", metavar="NAME", help="只看名称包含该串的资产")
    add_common(mt, "sub")
    mt.set_defaults(func=cmd_metrics)

    lg = sub.add_parser("login",
                        help="浏览器登录（workbuddy 扫码 / skillhub 手动登录），自动抓 Cookie，不用导 HAR")
    lg.add_argument("--target", choices=["workbuddy", "skillhub"], default="workbuddy",
                    help="登录哪个平台，默认 workbuddy（扫码）；skillhub 为手动登录后自动收尾")
    lg.add_argument("--timeout", type=int, default=300, help="等待扫码/登录的秒数，默认 300")
    lg.add_argument("--check-only", action="store_true", help="只验活，不打开浏览器")
    lg.add_argument("--refresh", action="store_true",
                    help="不开窗口：从浏览器 profile 里把当前有效会话重新导出（cookie 过期但 profile 还活着时用）")
    lg.add_argument("--fresh", action="store_true", help="清空旧浏览器 profile 再登录")
    add_common(lg, "sub")
    lg.set_defaults(func=cmd_login)

    wd = sub.add_parser("withdraw",
                        help="撤回 open.workbuddy 待审版本（退回 draft，修正后可重提）")
    wd.add_argument("target", nargs="?", default=".",
                    help="技能目录，或直接给 assetId（os_...）")
    wd.add_argument("--name", help="按技能名找资产时覆盖名字（默认取 frontmatter 的 slug/name）")
    wd.add_argument("--dry-run", action="store_true", help="只打印，不真撤回")
    add_common(wd, "sub")
    wd.set_defaults(func=cmd_withdraw)

    pyp = sub.add_parser("pay",
                         help="回看某资产的付费状态（免费/买断/订阅 + 金额），只读")
    pyp.add_argument("target", nargs="?", default=".",
                     help="技能目录，或直接给 assetId（os_...）")
    pyp.add_argument("--name", help="按技能名找资产时覆盖名字（默认取 frontmatter 的 slug/name）")
    add_common(pyp, "sub")
    pyp.set_defaults(func=cmd_pay)

    sh = sub.add_parser("ship",
                        help="把所有「过审待发布」(approved) 的资产一键上线 —— 解决过审后忘点发布按钮")
    sh.add_argument("--dry-run", action="store_true", help="只列出待发布资产，不真发布")
    sh.add_argument("--scope", default="ALL", choices=["ALL", "BUDDY_APP_ONLY"],
                    help="visibleScope，个人主体只能 ALL（默认）")
    add_common(sh, "sub")
    sh.set_defaults(func=cmd_ship)

    wt = sub.add_parser("watch",
                        help="审核巡航：发布后自动登记，过审自动上线，全部落地即结束")
    wt.add_argument("--check", action="store_true",
                    help="跑一次巡检（不加则只列出待审列表）")
    wt.add_argument("--ship", action="store_true",
                    help="巡检时把「已过审」(approved) 的自动上线")
    wt.add_argument("--stop", action="store_true", help="停止巡航（清空待审登记）")
    wt.add_argument("--asset", metavar="ID",
                    help="只针对某个 assetId（配合 --check / --stop）")
    wt.add_argument("--scope", default="ALL", choices=["ALL", "BUDDY_APP_ONLY"],
                    help="自动上线时的 visibleScope，个人主体只能 ALL（默认）")
    add_common(wt, "sub")
    wt.set_defaults(func=cmd_watch)

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
    pub.add_argument("--pay", choices=("free", "buyout", "subscription", "per_call"),
                     help="付费方式（按平台分家）：buyout 一次性买断 / subscription 月订阅"
                          "（open.workbuddy，版本级、过审锁死）；per_call 按次调用"
                          "（SkillHub，0.01–100 元/次，需 SkillPay 入驻 + 真实 402 收费后端）。"
                          "不填 buyout/subscription 则读配置 workbuddy.pay_mode")
    pub.add_argument("--price", type=float, metavar="元",
                     help="付费金额：buyout/subscription 是总价（元），per_call 是单次调用价"
                          "（元/次，两位小数）")
    pub.add_argument("--pay-days", type=int, metavar="N",
                     help="订阅周期天数（仅 --pay subscription，默认 30）")
    pub.add_argument("--audience", type=int, metavar="N",
                     help="可触达人群估算（可选）。取「已证明会装这类东西的人」，"
                          "例如同类免费资产的安装量，不是全平台用户数。"
                          "给了就按 人群 × 10%% × 价格 打印天花板估算")
    pub.add_argument("--strict", action="store_true",
                     help="开启硬门槛：付费发布时必须给 --audience，且天花板低于 ¥300 就拒绝提交。"
                          "默认只提示不拦 —— ¥300 是价值判断，不该由工具替所有人定")
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
