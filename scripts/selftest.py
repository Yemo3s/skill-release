#!/usr/bin/env python3
"""skill-release 离线自检。

覆盖重点是**那些会悄悄坏掉的地方**：
  - frontmatter 解析/渲染往返（含转义引号、块标量、行内列表）
  - 平台字段投影（slug 从 name 推导、displayName 从 display_name 推导、author 从配置注入）
  - 两个平台的两条硬规则（无扩展名文件 / 目录层级超限）—— 都是真实被拒过的
  - 暂存时的平台差异（.sh 只在 github 保留）
  - 打包后的断言能抓出违规
  - 版本号三处一致
  - CLI 顶层/子命令选项优先级

不联网、不写用户目录（除一个临时配置）。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import creds          # noqa: E402
import frontmatter as fm_mod  # noqa: E402
import pack           # noqa: E402
import release        # noqa: E402
import rules          # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f"  ← {detail}" if detail else ""))


def _raises(fn, exc) -> bool:
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _message(fn) -> str:
    """跑 fn，把抛出的异常文案拿回来（用于断言「报错说的对不对」）。"""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            fn()
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return ""


SKILL_MD = '''---
name: demo-skill
display_name: 演示技能
display_name_en: Demo Skill
description: "第一句描述。TRIGGER: 用户说 \\"跑一下 demo\\"。DO NOT TRIGGER: 其他。"
description_zh: "中文简介"
description_en: "English intro"
version: 2.3.4
allowed-tools: Bash, Read
license: MIT
tags: [demo, test]
---

# 演示技能

正文第一行。
'''


def make_fixture(root: Path) -> Path:
    d = root / "demo-skill"
    (d / "scripts" / "deep").mkdir(parents=True)
    (d / "reference").mkdir(parents=True)
    (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (d / "VERSION.txt").write_text("2.3.4\n", encoding="utf-8")
    (d / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
                                 + (512).to_bytes(4, "big") + (512).to_bytes(4, "big")
                                 + b"\x00" * 32)
    (d / "scripts" / "run.py").write_text("__version__ = \"2.3.4\"\n", encoding="utf-8")
    (d / "scripts" / "install.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (d / "scripts" / "deep" / "nested.py").write_text("x = 1\n", encoding="utf-8")
    (d / "reference" / "guide.md").write_text("# 指南\n", encoding="utf-8")
    (d / "LICENSE").write_text("MIT\n", encoding="utf-8")          # 无扩展名
    return d


def test_frontmatter() -> None:
    print("[frontmatter 解析与渲染]")
    fm = fm_mod.parse_frontmatter(SKILL_MD)
    check("name 解析", fm.get("name") == "demo-skill")
    check("双引号内转义引号还原", '"跑一下 demo"' in str(fm.get("description")))
    check("行内列表解析", fm.get("tags") == ["demo", "test"])
    check("version 解析", fm.get("version") == "2.3.4")

    # 往返
    text = fm_mod.build_skill_md(SKILL_MD, {"slug": "demo-skill", "author": "mo"})
    fm2 = fm_mod.parse_frontmatter(text)
    check("往返后 slug 写入", fm2.get("slug") == "demo-skill")
    check("往返后 author 写入", fm2.get("author") == "mo")
    check("往返后原字段未丢", fm2.get("license") == "MIT" and fm2.get("tags") == ["demo", "test"])
    check("往返后正文完整", "# 演示技能" in text and "正文第一行。" in text)
    check("往返后描述中的引号仍在", '"跑一下 demo"' in str(fm2.get("description")))

    # 块标量
    block = '---\na: |\n  第一行\n  第二行\nb: >\n  折叠\n  成一行\n---\n正文\n'
    bf = fm_mod.parse_frontmatter(block)
    check("字面块标量保留换行", bf.get("a") == "第一行\n第二行", repr(bf.get("a")))
    check("折叠块标量折成一行", bf.get("b") == "折叠 成一行", repr(bf.get("b")))

    # 多行值渲染回块标量
    rendered = fm_mod.build_skill_md('---\nname: x\n---\nb\n', {"c": "甲\n乙"})
    check("多行值渲染为块标量", "c: |" in rendered and "甲" in rendered)

    check("缺结束标记报错",
          _raises(lambda: fm_mod.split_frontmatter("---\nname: x\n"), fm_mod.FrontmatterError))
    check("不以 --- 开头报错",
          _raises(lambda: fm_mod.split_frontmatter("# 没有 frontmatter\n"),
                  fm_mod.FrontmatterError))


def test_projection() -> None:
    print("[平台字段投影]")
    fm = fm_mod.parse_frontmatter(SKILL_MD)

    sh = fm_mod.project_for_skillhub(fm)
    check("slug 由 name 推导", sh["slug"] == "demo-skill")
    check("displayName 由 display_name 推导", sh["displayName"] == "演示技能")
    check("summary 由 description 首句截取", str(sh["summary"]).startswith("第一句描述。"),
          str(sh["summary"]))
    check("tags 透传为列表", sh["tags"] == ["demo", "test"])
    check("version 透传", sh["version"] == "2.3.4")

    wb = fm_mod.project_for_workbuddy(fm, author="mo")
    check("workbuddy author 从配置注入", wb.get("author") == "mo")
    check("workbuddy 保留中文简介", wb.get("description_zh") == "中文简介")
    wb2 = fm_mod.project_for_workbuddy(fm, author="")
    check("未配 author 时不注入空值", "author" not in wb2)

    eff, auto = rules.effective_frontmatter(fm, "skillhub")
    check("投影后 skillhub 三项齐备",
          all(str(eff.get(k) or "").strip() for k in ("slug", "version", "displayName")))
    check("自动补全项被记录", "slug" in auto and "displayName" in auto)
    eff_wb, auto_wb = rules.effective_frontmatter(fm, "workbuddy", "mo")
    check("投影后 workbuddy 五项齐备",
          all(str(eff_wb.get(k) or "").strip()
              for k in ("description", "description_zh", "description_en", "version", "author")))

    # slug 覆盖：SkillHub 的 slug 可能被封禁（douyin-downloader 就是），
    # 得能不改源文件就换一个。
    sh_o = fm_mod.project_for_skillhub(fm, slug_override="demo-skill-v2")
    check("slug 可被覆盖", sh_o["slug"] == "demo-skill-v2", str(sh_o["slug"]))
    check("覆盖 slug 不影响 displayName", sh_o["displayName"] == "演示技能")
    eff_o, auto_o = rules.effective_frontmatter(fm, "skillhub", slug_override="demo-skill-v2")
    check("规则引擎看到覆盖后的 slug", eff_o["slug"] == "demo-skill-v2")
    check("覆盖来源被标注", "覆盖" in auto_o.get("slug", ""), str(auto_o.get("slug")))
    check("覆盖后的非法 slug 会被规则引擎判掉",
          not fm_mod.SLUG_RE.match(str(
              rules.effective_frontmatter(fm, "skillhub",
                                          slug_override="BAD Slug")[0]["slug"])))

    slugs = release.resolve_slugs({"slug": "demo-skill"},
                                  {"skillhub": {"slug_map": {"demo-skill": "demo-skill-v2"}}})
    check("配置里的 slug_map 生效", slugs["skillhub"] == "demo-skill-v2", str(slugs))
    check("slug_map 只影响 skillhub",
          slugs["github"] == "demo-skill" and slugs["workbuddy"] == "demo-skill")
    slugs2 = release.resolve_slugs({"slug": "demo-skill"}, {}, cli_slug="forced-name")
    check("--slug 覆盖全部平台", set(slugs2.values()) == {"forced-name"}, str(slugs2))
    slugs3 = release.resolve_slugs({"slug": "demo-skill"},
                                   {"skillhub": {"slug_map": {"other": "x"}}})
    check("slug_map 无匹配时回落原名", slugs3["skillhub"] == "demo-skill", str(slugs3))


def test_rules(tmp_root: Path) -> None:
    print("[规则引擎：两条真实被拒过的硬规则]")
    fx = make_fixture(tmp_root)
    fm = fm_mod.parse_frontmatter(SKILL_MD)
    inv, findings = rules.analyze(fx, fm, rules.PLATFORMS, author="mo")

    codes = {f.code for f in findings if f.blocks}
    check("无扩展名文件被 SkillHub 拦下", "extensionless" in codes, str(codes))
    check("目录层级超限被 open.workbuddy 拦下", "depth" in codes, str(codes))

    depth = inv.depth_violations()
    check("深度违规只报深层那个", depth == ["scripts/deep/nested.py"], str(depth))
    check("extensionless 只报 LICENSE", inv.extensionless() == ["LICENSE"], str(inv.extensionless()))

    status = rules.summarize(findings, rules.PLATFORMS)
    # GitHub 不受这两条规则约束 —— 「无扩展名」是 SkillHub 的白名单问题，
    # 「目录层级」是 open.workbuddy 的解析问题，都不影响 git 仓库。
    check("GitHub 不受这两条规则约束", status["github"] is True, str(status))
    check("SkillHub 与 open.workbuddy 被判不可发",
          not status["skillhub"] and not status["workbuddy"], str(status))

    # 修掉两个问题后应当全绿
    (fx / "LICENSE").rename(fx / "LICENSE.txt")
    shutil.move(str(fx / "scripts" / "deep" / "nested.py"), str(fx / "scripts" / "deep_nested.py"))
    (fx / "scripts" / "deep").rmdir()
    inv2, f2 = rules.analyze(fx, fm, rules.PLATFORMS, author="mo")
    st2 = rules.summarize(f2, rules.PLATFORMS)
    check("修复后三平台均可发", all(st2.values()), str([f.message for f in f2 if f.blocks]))

    # author 未配置时 workbuddy 仍不可发
    _, f3 = rules.analyze(fx, fm, rules.PLATFORMS, author="")
    st3 = rules.summarize(f3, rules.PLATFORMS)
    check("缺 author 时仅 workbuddy 不可发",
          st3["skillhub"] and st3["github"] and not st3["workbuddy"], str(st3))

    # .sh 只在 github 保留
    _, f4 = rules.analyze(fx, fm, rules.PLATFORMS, author="mo")
    excl = [f for f in f4 if f.code == "publish-excluded"]
    check("发布包剔除 .sh 会被显式告知", bool(excl), str([f.code for f in f4]))
    return fx


def test_pack(fx: Path, tmp_root: Path) -> None:
    print("[打包与暂存]")
    out = tmp_root / "stage"
    gh = pack.stage(fx, out / "github", platform="github", slug="demo-skill")
    sh = pack.stage(fx, out / "skillhub", platform="skillhub", slug="demo-skill")
    wb = pack.stage(fx, out / "workbuddy", platform="workbuddy", slug="demo-skill",
                    author="mo")

    check("github 保留 .sh", (gh / "scripts" / "install.sh").is_file())
    check("skillhub 剔除 .sh", not (sh / "scripts" / "install.sh").exists())
    check("workbuddy 剔除 .sh", not (wb / "scripts" / "install.sh").exists())
    check("github 自动补 README", (gh / "README.md").is_file())
    check("github 自动补 .gitignore", (gh / ".gitignore").is_file())
    check("github 未改动 frontmatter",
          "slug:" not in fm_mod.split_frontmatter((gh / "SKILL.md").read_text("utf-8"))[0])

    sh_fm = fm_mod.parse_frontmatter((sh / "SKILL.md").read_text("utf-8"))
    check("skillhub 副本注入 slug", sh_fm.get("slug") == "demo-skill")
    check("skillhub 副本注入 displayName", sh_fm.get("displayName") == "演示技能")
    wb_fm = fm_mod.parse_frontmatter((wb / "SKILL.md").read_text("utf-8"))
    check("workbuddy 副本注入 author", wb_fm.get("author") == "mo")
    check("源 SKILL.md 未被改动", "author" not in fm_mod.parse_frontmatter(
        (fx / "SKILL.md").read_text("utf-8")))

    check("暂存目录不允许等于源目录",
          _raises(lambda: pack.stage(fx, fx, platform="github", slug="x"),
                  pack.PackageError))

    # zip + 断言
    z = pack.make_zip(wb, tmp_root / "demo.zip", prefix="demo-skill")
    with zipfile.ZipFile(z) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
    check("zip 有外层目录", all(n.startswith("demo-skill/") for n in names))
    check("zip 内层级不超过 3 段", max(len(n.split("/")) for n in names) <= 3)
    check("合法包断言通过", not pack.assert_package(z, "workbuddy"),
          str(pack.assert_package(z, "workbuddy")))

    # 造一个违规包，验证断言真的能抓
    bad = tmp_root / "bad"
    (bad / "a" / "b").mkdir(parents=True)
    (bad / "a" / "b" / "c.py").write_text("x=1\n", encoding="utf-8")
    (bad / "LICENSE").write_text("x\n", encoding="utf-8")
    zb = pack.make_zip(bad, tmp_root / "bad.zip", prefix="bad")
    probs = pack.assert_package(zb, "workbuddy")
    check("断言能抓出层级超限", any("层级超限" in p for p in probs), str(probs))
    check("断言能抓出无扩展名", any("无扩展名" in p for p in probs), str(probs))


def test_rejected_ext(tmp_root: Path) -> None:
    """被平台**实测拒绝**过的扩展名要在本地就 block，别浪费一次真实上传。

    出处：2026-10-13 probably-stolen 发布时
        400 {"error":"不允许的文件类型: reference/icon-map.tsv"}
    """
    print("[扩展名：实测被拒的一档]")
    fx = tmp_root / "rejected-ext"
    (fx / "reference").mkdir(parents=True)
    (fx / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    (fx / "reference" / "icon-map.tsv").write_text("a\tb\n", encoding="utf-8")

    _, findings = rules.analyze(fx, fm_mod.parse_frontmatter(SKILL_MD),
                                rules.PLATFORMS, author="mo")
    codes = {f.code for f in findings}
    check("REJECTED_EXT 含 .tsv", ".tsv" in rules.REJECTED_EXT,
          str(sorted(rules.REJECTED_EXT)))
    check(".tsv 被本地 block", "rejected-ext" in codes, str(sorted(codes)))
    blocked = [f for f in findings if f.code == "rejected-ext" and f.blocks]
    check("报错里点名了具体文件",
          blocked and "icon-map.tsv" in blocked[0].message,
          blocked[0].message if blocked else "(无)")
    check(".tsv 不再落进「未验证扩展名」警告",
          "unverified-ext" not in codes, str(sorted(codes)))
    check("改名 .txt 后不再被拦",
          "rejected-ext" not in
          {f.code for f in rules.analyze(
              _rename_tsv_to_txt(fx) or fx, fm_mod.parse_frontmatter(SKILL_MD),
              rules.PLATFORMS, author="mo")[1]})


def _rename_tsv_to_txt(root: Path) -> Path:
    src = root / "reference" / "icon-map.tsv"
    if src.is_file():
        src.rename(root / "reference" / "icon-map.txt")
    return root


def test_network_fallback() -> None:
    """github.com 不通时该走 API 兜底；认证类错误不该误判成网络问题。"""
    print("[GitHub 网络兜底判定]")
    import pub_github as gh
    check("CONNECT tunnel 502 判为网络问题",
          gh.is_network_failure("fatal: ... CONNECT tunnel failed, response 502"))
    check("端口连不上判为网络问题",
          gh.is_network_failure("Failed to connect to github.com port 443 after 21 s"))
    check("403 不判为网络问题",
          not gh.is_network_failure("remote: Permission to X denied (403)"))
    check("认证失败不判为网络问题",
          not gh.is_network_failure("认证失败：令牌无效"))


def test_skillhub_local(fx: Path) -> None:
    print("[SkillHub 本地校验]")
    import pub_skillhub as sh
    files = sh.collect_files(fx)
    check("收集文件时剔除 .sh",
          not any(r.endswith(".sh") for r, _ in files), str([r for r, _ in files]))
    check("SKILL.md 在列", any(r.lower() == "skill.md" for r, _ in files))
    # 实测：icon.png 当普通文件上传会被 SkillHub 整包拒绝
    # （400 不允许的文件类型: icon.png）。图标属平台元数据，不进文件清单。
    check("根目录 icon.png 不进 SkillHub 清单",
          not any(r.lower() == "icon.png" for r, _ in files),
          str([r for r, _ in files]))
    check("根目录 icon.png 仍在 GitHub 清单里",
          any(r.lower() == "icon.png"
              for r, _ in rules.collect_files(fx, for_platform="github").files))
    check(".png 不再被当成「已验证可通过」",
          ".png" not in rules.VERIFIED_EXT, str(sorted(rules.VERIFIED_EXT)))
    check("发布包统计用过滤后的清单（不含 icon / .sh）",
          not any(r.lower().endswith((".sh", "icon.png"))
                  for r, _ in rules.collect_files(fx, for_platform="skillhub").files))

    ok = sh.validate({"slug": "demo-skill", "version": "1.0.0", "displayName": "演示"})
    check("合法元数据通过", ok == [], str(ok))
    for meta, why in [
        ({"slug": "", "version": "1.0.0", "displayName": "x"}, "缺 slug"),
        ({"slug": "Bad_Slug", "version": "1.0.0", "displayName": "x"}, "slug 非法字符"),
        ({"slug": "a", "version": "1.0.0", "displayName": "x"}, "slug 过短"),
        ({"slug": "ok-name", "version": "1.0", "displayName": "x"}, "非 SemVer"),
        ({"slug": "ok-name", "version": "1.0.0", "displayName": ""}, "缺 displayName"),
    ]:
        check(f"拒绝：{why}", sh.validate(meta) != [])

    check("错误码 401 提示换 key", "401" in sh.format_error(401, {}))
    check("错误码 409 提示 slug 冲突", "冲突" in sh.format_error(409, {"error": "dup"}))
    check("错误码 429 提示稍后重试", "频繁" in sh.format_error(429, {"error": "slow"}))

    r = sh.publish("https://api.skillhub.cn", "skh_fake", fx,
                   {"slug": "demo-skill", "version": "1.0.0", "displayName": "演示"},
                   "首次", dry_run=True)
    check("dry-run 不发网络请求且校验通过", r["ok"] and r["status"] == "dry-run", str(r))
    r2 = sh.publish("https://api.skillhub.cn", "skh_fake", fx,
                    {"slug": "BAD!", "version": "1.0.0", "displayName": "演示"},
                    "x", dry_run=True)
    check("dry-run 也能拦下非法 slug", not r2["ok"], str(r2))


def test_creds(tmp_root: Path) -> None:
    print("[凭据]")
    check("脱敏：短值打全遮", creds.mask("abcd") == "****")
    check("脱敏：长值留头尾", creds.mask("skh_abcdefghijklmn").startswith("skh_"))
    check("脱敏：不外泄中段", "efghijklmn" not in creds.mask("skh_abcdefghijklmn"))
    check("空值标注为（空）", creds.mask("") == "(空)")

    p = tmp_root / "creds.json"
    creds.set_value("skillhub.key", "skh_test123456", p)
    creds.set_value("author", "mo", p)
    d = creds.load(p)
    check("点号路径写入", d["skillhub"]["key"] == "skh_test123456")
    check("顶层写入", d["author"] == "mo")
    check("默认值仍在", d["skillhub"]["host"] == "https://api.skillhub.cn")
    check("落盘文件存在", p.is_file())
    check("describe 不含完整 key", "skh_test123456" not in creds.describe(p))

    os.environ["SKILLHUB_KEY"] = "skh_from_env"
    try:
        check("环境变量覆盖文件", creds.load(p)["skillhub"]["key"] == "skh_from_env")
    finally:
        del os.environ["SKILLHUB_KEY"]
    check("路径可注入，不碰用户目录", creds.SECRETS_PATH.name == "skill-release.json")


def test_version() -> None:
    print("[版本一致性]")
    root = HERE.parent
    vf = root / "VERSION.txt"
    check("VERSION.txt 存在", vf.is_file(), str(vf))
    if not vf.is_file():
        return
    ver = vf.read_text(encoding="utf-8").strip()
    check("VERSION.txt 形如 x.y.z", bool(re.fullmatch(r"\d+\.\d+\.\d+", ver)), ver)
    check("release.__version__ 一致", release.__version__ == ver,
          f"{release.__version__} != {ver}")
    sk = root / "SKILL.md"
    if sk.is_file():
        text = sk.read_text(encoding="utf-8")
        fm = text[4:].split("\n---\n")[0] if text.startswith("---\n") else ""
        m = re.search(r"^version:\s*(\S+)\s*$", fm, re.M)
        check("frontmatter version 一致", bool(m) and m.group(1) == ver,
              m.group(1) if m else "frontmatter 无 version")


def test_cli() -> None:
    print("[CLI 选项位置]")
    parser = release.build_parser()
    a = parser.parse_args(["--yes", "check", "."])
    b = parser.parse_args(["check", ".", "--yes"])
    c = parser.parse_args(["check", "."])
    check("--yes 写在子命令前生效", release.opt(a, "yes") is True)
    check("--yes 写在子命令后生效", release.opt(b, "yes") is True)
    check("不传时为 False", not release.opt(c, "yes"))
    check("顶层 workdir 能取到",
          release.opt(parser.parse_args(["--workdir", "/tmp/x", "check", "."]),
                      "workdir") == "/tmp/x")
    check("子命令 workdir 优先",
          release.opt(parser.parse_args(["--workdir", "/tmp/a", "check", ".",
                                         "--workdir", "/tmp/b"]), "workdir") == "/tmp/b")
    check("targets 拆分",
          release._split_targets("github, skillhub") == ["github", "skillhub"])
    check("未知 target 报错",
          _raises(lambda: release._split_targets("github,nope"), SystemExit))
    check("缺子命令即报错", _raises(lambda: parser.parse_args([]), SystemExit))


def test_bump(tmp_root: Path) -> None:
    print("[版本自增]")
    d = make_fixture(tmp_root / "bumpcase")
    check("patch 自增", release.bump_version(d, "patch") == "2.3.5")
    check("SKILL.md 已更新",
          fm_mod.parse_frontmatter((d / "SKILL.md").read_text("utf-8")).get("version") == "2.3.5")
    check("VERSION.txt 已更新", (d / "VERSION.txt").read_text("utf-8").strip() == "2.3.5")
    check("脚本 __version__ 已更新",
          '__version__ = "2.3.5"' in (d / "scripts" / "run.py").read_text("utf-8"))
    check("minor 清零低位", release.bump_version(d, "minor") == "2.4.0")
    check("major 清零低位", release.bump_version(d, "major") == "3.0.0")


def test_auth_detection() -> None:
    """两个平台各自的凭据形态与「未登录」判据 —— 全是纯函数，不打网络。"""
    print("[鉴权判据]")
    import pub_skillhub as sh
    import pub_workbuddy as wb

    # workbuddy：关键教训是「200 + 登录页 HTML」绝不能被当成通过。
    # 真实形态：APISIX 把未登录的接口 302 到 Keycloak，
    # 跟随重定向后拿到 200 + <!DOCTYPE html>…class="login-pf"。
    login_page = {"raw": '<!DOCTYPE html><html class="login-pf"><head>'}
    check("302 跳 Keycloak 判为会话失效",
          bool(wb.auth_problem(302, {"_location":
               "https://open.workbuddy.cn/auth/realms/copilot/protocol/openid-connect/auth?x=1"})))
    check("200 + 登录页 HTML 判为会话失效", bool(wb.auth_problem(200, login_page)))
    check("200 + 其它非 JSON 也判为异常", bool(wb.auth_problem(200, {"raw": "oops"})))
    check("401 判为会话失效", bool(wb.auth_problem(401, {})))
    check("403 判为鉴权失败", bool(wb.auth_problem(403, {"message": "no"})))
    check("200 + 真 JSON 视为正常", wb.auth_problem(200, {"items": []}) is None)
    check("200 + 空 JSON 对象视为正常", wb.auth_problem(200, {}) is None)
    check("whoami 空 Cookie 直接报错",
          _raises(lambda: wb.whoami("https://open.workbuddy.cn", "  "), wb.WorkBuddyError))

    # skillhub：Cookie 通道与 Bearer 通道互斥（实测同时带会被判 invalid token）。
    seen: dict = {}

    class _Fake:
        def __init__(self, req):  # noqa: ANN001
            self._req = req

        def read(self):
            return b'{"user":{"handle":"probe"}}'

        def getcode(self):
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = urllib.request.urlopen

    def fake_open(req, *a, **kw):  # noqa: ANN001
        seen["h"] = {k.lower(): v for k, v in req.header_items()}
        return _Fake(req)

    urllib.request.urlopen = fake_open
    try:
        sh.whoami("https://api.skillhub.cn", "skh_key", cookie="skh_token=abc; sid=xyz")
        check("SkillHub 配 cookie 时带 Cookie 头", "cookie" in seen["h"], str(seen["h"]))
        check("SkillHub 配 cookie 时不带 Authorization",
              "authorization" not in seen["h"], str(seen["h"]))
        sh.whoami("https://api.skillhub.cn", "skh_key")
        check("SkillHub 无 cookie 时用 Bearer",
              seen["h"].get("authorization") == "Bearer skh_key", str(seen["h"]))
        check("SkillHub Bearer 模式不带 Cookie", "cookie" not in seen["h"], str(seen["h"]))
    finally:
        urllib.request.urlopen = real

    check("SkillHub 401 文案提到 Cookie 补救",
          "Cookie" in sh.format_error(401, {}))


def test_cookie_sniff() -> None:
    """Cookie 自检 —— 只保留「绝对可靠」的那条检查。

    这个函数的历史就是一部「结构判据被逐个推翻」的记录，用例负责防止复活：

    | 曾经的判据 | 结局 |
    |---|---|
    | 带 `qcloud_*` / `session_2` = 拿错站 | ❌ 本站自己的已登录请求就带着它们 |
    | `session_2` 被清 = 不认会话 | ❌ 对照实验推翻了 |
    | `session` 必须 4 段、超 4000 字符 = 被截断 | ❌ **HAR 里 45 条已登录请求全是 3 段 4001 字符，全 200** |

    最后一条错得最贵：它把一份**完全正确**的 Cookie 判成「坏的」，
    真正的病因是会话绑定 UA（见 test_workbuddy_user_agent）。

    所以现在只剩「有没有 `session`」这一条 —— 其余交给响应的状态码。
    """
    print("[workbuddy Cookie 自检]")
    import pub_workbuddy as wb

    # 登录态的真实形状：3 段、4001 字符 —— 这是**正常**的，不该报警。
    logged_in = ("_gcl_au=1.1.1425429930; qcloud_visitId=4fca73ac; tgw_l7_route=74f95491; "
                 "session=oiPcKB8F6mabw2ZupcfGUw|1792228947|" + "z" * 3967 +
                 "; session_2=MarQw8q_" + "y" * 1346 + "|XV5c9nuP_aH4h57dF4_wZDUgJ04")
    check("登录态真形状（3 段 / 4001 字符）零警告 —— 不再误判为截断",
          wb.sniff_cookie(logged_in) == [], str(wb.sniff_cookie(logged_in)))

    # 匿名形状：4 段 276 字符，同样正常。
    anon = ("tgw_l7_route=003a2ba1374d6fa2d5a87be6692a9f5f; "
            "session=AFeHdlhnWAP-jTYR3QPoFw|1792228670|" + "x" * 214
            + "|1pDemZ0vTJVmU0L0R3TeJzwOkjE")
    check("匿名态形状（4 段）也零警告", wb.sniff_cookie(anon) == [],
          str(wb.sniff_cookie(anon)))

    no_session = "qcloud_visitId=abc; tgw_l7_route=xyz"
    w2 = wb.sniff_cookie(no_session)
    check("缺 session 时提示 HttpOnly 取法",
          any("HttpOnly" in x and "document.cookie" in x for x in w2), str(w2))
    check("提示走 HAR 这条路", any("creds --from-har" in x for x in w2), str(w2))

    check("空串不崩", isinstance(wb.sniff_cookie(""), list))
    check("只有 session 就放行", wb.sniff_cookie("session=a|b") == [])


def test_workbuddy_user_agent() -> None:
    """会话绑定 UA —— 2026-10-10 卡住的真正原因。

    同一份 HAR 里的 `session`，只改 `User-Agent` 重放 `/parties`：
    真值 200；`Pixel 10→Pixel 9` / `Android 16→15` / `Chrome/154→155` /
    删 `Mobile` / 换机型 / 我们的 Windows 兜底 UA —— **全部 302**。
    所以 UA 是凭据的一部分，必须与取 Cookie 的那条请求一起存。

    这里不联网，只钉住「UA 能被正确注入到请求头」这一环。
    """
    print("[workbuddy UA 绑定]")
    import pub_workbuddy as wb

    true_ua = ("Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36")

    default_ua = wb.user_agent()
    check("默认有兜底 UA", bool(default_ua), default_ua)

    wb.set_user_agent(true_ua)
    check("set_user_agent 生效", wb.user_agent() == true_ua, wb.user_agent())
    check("_headers 用注入后的 UA",
          wb._headers("session=x")["User-Agent"] == true_ua,
          wb._headers("session=x")["User-Agent"])

    wb.set_user_agent("  " + true_ua + "  ")
    check("前后空格被清掉", wb.user_agent() == true_ua, wb.user_agent())

    wb.set_user_agent("")
    check("空值退回兜底 UA", wb.user_agent() == default_ua, wb.user_agent())
    wb.set_user_agent(None)  # type: ignore[arg-type]
    check("None 不崩且退回兜底", wb.user_agent() == default_ua, wb.user_agent())


def test_har_extraction(tmp_root: Path) -> None:
    """HAR 抽 Cookie + UA —— 一次把「一对凭据」都带走。"""
    print("[HAR 抽 open.workbuddy Cookie + UA]")
    import json as _json

    import pub_workbuddy as wb

    sig = "1pDemZ0vTJVmU0L0R3TeJzwOkjE"
    old = "OLD00000000000000000000|1792228000|" + "a" * 200 + "|" + sig
    new = "NEW00000000000000000000|1792228947|" + "b" * 300 + "|" + sig
    ua = ("Mozilla/5.0 (Linux; Android 16; Pixel 10) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36")
    har = {
        "log": {"entries": [
            # 腾讯云的请求 —— 必须被忽略
            {"request": {"url": "https://cloud.tencent.com/api/x",
                         "headers": [{"name": "Cookie", "value": "session=should_not_win"}]}},
            # 本站旧的一条（headers 形式）
            {"request": {"url": "https://open.workbuddy.cn/api/v2/a",
                         "headers": [{"name": "Cookie",
                                      "value": f"tgw_l7_route=r1; session={old}"}]}},
            # 本站新的一条（cookies[] 形式，应胜出；UA 在 headers 里）
            {"request": {"url": "https://open.workbuddy.cn/api/v1/b",
                         "headers": [{"name": "user-agent", "value": ua}],
                         "cookies": [{"name": "tgw_l7_route", "value": "r2"},
                                     {"name": "session", "value": new},
                                     {"name": "session_2", "value": "s2|" + sig}]}},
        ]}}
    p = tmp_root / "wb.har"
    p.write_text(_json.dumps(har), encoding="utf-8")

    cookie, info = wb.cookie_from_har(p)
    check("忽略了非本站域名", "should_not_win" not in cookie, cookie[:60])
    check("取最后一条带 session 的请求", f"session={new}" in cookie, cookie[:60])
    check("保留了同一条请求里的 session_2", "session_2=s2|" in cookie, cookie[:80])
    check("元信息带字节数与 session 长度",
          info["bytes"] == len(cookie) and info["session_len"] == len(new), str(info))
    check("★ 把同一条请求的 User-Agent 一起带出来", info["user_agent"] == ua,
          info.get("user_agent", "<缺>"))
    check("抽出的 Cookie 自检零警告", wb.sniff_cookie(cookie) == [],
          str(wb.sniff_cookie(cookie)))

    only_headers = {"log": {"entries": [
        {"request": {"url": "https://open.workbuddy.cn/api/v2/a",
                     "headers": [{"name": "Cookie", "value": f"session={old}"}]}}]}}
    p2 = tmp_root / "wb2.har"
    p2.write_text(_json.dumps(only_headers), encoding="utf-8")
    c2, _ = wb.cookie_from_har(p2)
    check("headers 形式也能抽", c2 == f"session={old}", c2[:50])

    p3 = tmp_root / "empty.har"
    p3.write_text(_json.dumps({"log": {"entries": []}}), encoding="utf-8")
    check("没有本站请求时报错并提示刷新",
          _raises(lambda: wb.cookie_from_har(p3), wb.WorkBuddyError))
    check("文件不存在时报错",
          _raises(lambda: wb.cookie_from_har(tmp_root / "nope.har"), wb.WorkBuddyError))

    p4 = tmp_root / "bad.har"
    p4.write_text("not json", encoding="utf-8")
    check("非 JSON 报错", _raises(lambda: wb.cookie_from_har(p4), wb.WorkBuddyError))

    # 「已清理」HAR：请求都在，但一个 Cookie 都没有（Chrome 130+ 的默认导出）。
    # 报错必须点名是这个原因，不能让人去查「为什么我没登录」。
    sanitized = {"log": {"entries": [
        {"request": {"url": "https://open.workbuddy.cn/api/v2/open-platform/parties",
                     "cookies": [],
                     "headers": [{"name": "accept", "value": "*/*"}]}},
        {"request": {"url": "https://open.workbuddy.cn/console/accounts",
                     "cookies": [], "headers": []}}]}}
    p5 = tmp_root / "sanitized.har"
    p5.write_text(_json.dumps(sanitized), encoding="utf-8")
    msg = _message(lambda: wb.cookie_from_har(p5))
    check("识别出「已清理」HAR（请求在、Cookie 全无）",
          "全部没有 Cookie" in msg, msg[:120])
    check("点名「允许生成包含敏感数据的 HAR」这个开关",
          "敏感数据" in msg and "偏好设置" in msg, msg[:200])


def test_workbuddy_upload_contract() -> None:
    """能力包上传的真实契约 —— 回归 2026-10-10 那次「字段名靠猜」的事故。

    事故：`upload_and_submit()` 走的是 `/common/uploads/presign`（那是**头像/封面**
    的通用上传，要 `scene` 字段），字段名也是按命名惯例猜的。真机一跑：
        ✗ open.workbuddy error  预签名返回里没有 uploadUrl：invalid request parameters
    从前端 bundle 反解 + 实测，真实契约是：

        POST /capabilities/init-upload  {capabilityType,fileName,fileSize,mimeType}
          → data.{uploadId,objectKey,uploadUrl}
        PUT  <uploadUrl>                Content-Type 必带
        POST /capabilities/complete-upload  {uploadId,assetId?} → data.uploadToken
        POST /capabilities/submit       {capabilityType,action,assetId,uploadToken,...}

    这里不联网，钉住三件不该再退化的事：响应剥 `data`、已存在资产不重复提交、
    枚举值不被改坏。
    """
    print("[workbuddy 上传契约]")
    import pub_workbuddy as wb

    env = {"code": 0, "data": {"uploadId": "su_x", "uploadUrl": "https://cos/x",
                               "objectKey": "k"}, "msg": "OK"}
    check("unwrap 剥掉 {code,data,msg} 信封",
          wb.unwrap(env) == {"uploadId": "su_x", "uploadUrl": "https://cos/x",
                             "objectKey": "k"}, str(wb.unwrap(env)))
    check("没有信封时原样返回", wb.unwrap({"uploadUrl": "u"}) == {"uploadUrl": "u"})
    check("非 dict 不崩", wb.unwrap(None) == {} and wb.unwrap("x") == {})
    check("code_of 认得出 0", wb.code_of(env) == 0)
    check("code_of 认得出业务错误码", wb.code_of({"code": 100001, "msg": "已存在"}) == 100001)
    check("code_of 对无 code 返回 None", wb.code_of({"raw": "html"}) is None)

    check("action 枚举值 = 前端那三个",
          (wb.ACTION_CREATE, wb.ACTION_REPUBLISH, wb.ACTION_RESUBMIT)
          == ("create", "republish", "resubmit"))
    check("under_review 常量与前端一致", wb.ASSET_UNDER_REVIEW == "under_review")

    # 服务类目 —— 回归 2026-10-14 那次「空 qualifications」驳回。
    # 控制台源码是 `a.length > 0 && (t.qualifications = a)`：空资质时**整键不发**。
    # 发成 `qualifications: []` 会被判成「选了类目却没交资质」，
    # 打回话术与「压根没选类目」一字不差，非常难查。
    check("类目：纯字符串自动包装，且不带空 qualifications",
          wb.normalize_categories(["tools:office"]) == [{"categoryId": "tools:office"}])
    check("类目：显式传空数组一样要被丢掉",
          wb.normalize_categories([{"categoryId": "tools:office",
                                    "qualifications": []}])
          == [{"categoryId": "tools:office"}])
    check("类目：真有资质文件时才带 qualifications",
          wb.normalize_categories([{"categoryId": "tools:office",
                                    "qualifications": [{"objectKey": "k"}]}])
          == [{"categoryId": "tools:office",
               "qualifications": [{"objectKey": "k"}]}])
    check("类目：空输入 / 空 id → 空列表（由调用方抛错拦住）",
          wb.normalize_categories(None) == []
          and wb.normalize_categories([""]) == []
          and wb.normalize_categories([{"categoryId": "  "}]) == [])

    # 「审核中」在列表接口里是 version.status=pending、asset.status=under_review；
    # 详情接口里两个都写成 under_review。只判一个会漏（实测漏过一次）。
    check("认列表形状（version=pending）",
          wb.in_review({"status": "under_review",
                        "currentVersion": {"status": "pending", "version": "1.1.0"}}))
    check("认详情形状（都叫 under_review）",
          wb.in_review({"status": "under_review",
                        "currentVersion": {"status": "under_review"}}))
    check("已发布的资产不算审核中",
          not wb.in_review({"status": "published",
                            "currentVersion": {"status": "approved"}}))
    check("空资产不算审核中", not wb.in_review({}) and not wb.in_review(None))

    # 已在审核 → 既不重复上传也不重复提交（拿真事件回归：douyin-downloader 当时
    # 就是 under_review，重跑 publish 不该再往审核队列塞一份）
    calls: list[str] = []
    real_find, real_call = wb.find_asset, wb._call

    def fake_find(*_a, **_k):
        # 真实形状：列表接口给的是 version.status=pending + asset.status=under_review
        return {"assetId": "os_test", "name": "demo", "status": "under_review",
                "currentVersion": {"version": "1.1.0", "status": "pending"}}

    def fake_call(*a, **k):
        calls.append(str(a[1]))
        return 200, {"code": 0, "data": {}}

    zipf = Path(wb.__file__).parent / "_fake.zip"
    zipf.write_bytes(b"PK\x03\x04fake")
    try:
        wb.find_asset, wb._call = fake_find, fake_call
        res = wb.upload_and_submit("https://x", "ck", zipf, name="demo")
        check("已审核中 → status=skipped", res["status"] == "skipped", str(res))
        check("已审核中 → 一次网络调用都不发", calls == [], str(calls))
        check("已审核中 → 消息里点明原因", "审核中" in res["message"], res["message"])
    finally:
        wb.find_asset, wb._call = real_find, real_call
        zipf.unlink(missing_ok=True)

    # 撤回：这是 `upload_and_submit` 那句「审核中直接跳过」的**唯一出路**。
    # 2026-10-14 枚举出 `POST /capabilities/{assetId}/withdraw`（控制台 bundle
    # 里查不到，是在 /capabilities 下扫出来的）。钉住路径形状与返回值解析，
    # 免得以后有人把路径写错成 /versions/{id}/withdraw（那是 404）。
    seen: list[tuple[str, str]] = []

    def fake_call2(base, path, cookie, *, method="GET", **k):  # noqa: ANN001
        seen.append((method, path))
        return 200, {"code": 0, "data": {
            "assetId": "os_w", "status": "draft",
            "currentVersion": {"versionId": 47736, "status": "draft"}}}

    real_call2 = wb._call
    try:
        wb._call = fake_call2
        res = wb.withdraw("https://x", "ck", "os_w", party_id="psn_x")
    finally:
        wb._call = real_call2
    check("withdraw 打到 /capabilities/{assetId}/withdraw",
          seen == [("POST", wb.CAPABILITIES + "/os_w/withdraw")], str(seen))
    check("withdraw 解析出 draft 与 versionId",
          res["status"] == "draft" and res["version_id"] == 47736, str(res))
    check("withdraw 空 assetId 直接抛错",
          _raises(lambda: wb.withdraw("https://x", "ck", ""), wb.WorkBuddyError))
    check("withdraw 在 __all__ 里（CLI 要 import 它）", "withdraw" in wb.__all__)


def test_github_identity(tmp_root: Path) -> None:
    """提交身份防线 —— 这是**真事故**的回归用例，不是假想。

    事故链条：`prepare_repo` 原本只在「身份缺失」时才补 noreply，而
    `git config user.email` 不带 `--local` 会回退读全局；开发机全局正好是
    真名 + 公司邮箱，于是 commit 直接带着它进了**公开**仓库。事后改代码
    也删不掉 —— commit 元数据已经写出去了。

    这里用 `GIT_CONFIG_GLOBAL` 指向临时配置复现「全局是真名」，
    不碰用户真实的全局 git 配置。三道防线各钉一遍。
    """
    print("[提交身份]")
    import subprocess
    import pub_github as gh

    # ---- 纯函数，不需要 git ----
    check("noreply 身份带数字 id 形态",
          gh.noreply_identity("Yemo3s", "103362262")[1]
          == "103362262+Yemo3s@users.noreply.github.com",
          gh.noreply_identity("Yemo3s", "103362262")[1])
    check("noreply 缺 id 时退化仍安全",
          gh.noreply_identity("Yemo3s")[1] == "Yemo3s@users.noreply.github.com")
    check("noreply 空 login 有兜底",
          gh.noreply_identity("")[1] == f"github-user@{gh.NOREPLY_DOMAIN}")
    check("默认身份本身就不泄漏",
          gh.DEFAULT_IDENTITY[1].endswith("@" + gh.NOREPLY_DOMAIN),
          gh.DEFAULT_IDENTITY[1])

    if not gh._git_available():
        check("git 可用", False, "本机应有 git，跳过身份链路用例")
        return

    fake_global = tmp_root / "fake-gitconfig"
    fake_global.write_text(
        "[user]\n\tname = zhangsan\n\temail = zhangsan@corp.example.com\n",
        encoding="utf-8")
    keep = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(fake_global)
    try:
        # 先确认这个「假全局」真的生效 —— 否则下面测的是空气
        gn, gm = gh.global_identity()
        check("假全局身份可读到（真名）",
              gm == "zhangsan@corp.example.com", f"{gn} / {gm}")

        good = gh.noreply_identity("Yemo3s", "103362262")

        # 防线 1：全局是真名，提交仍必须是 noreply
        w1 = tmp_root / "ghident-1"
        w1.mkdir(parents=True, exist_ok=True)
        (w1 / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        gh.prepare_repo(w1, "t", good)
        info = gh.identity_of(w1)
        check("全局是真名时提交仍用 noreply",
              info.get("author_email") == good[1], str(info))
        check("提交者身份也是 noreply",
              info.get("committer_email") == good[1], str(info))
        check("干净提交不报不安全",
              gh.unsafe_identity(w1) == [], str(gh.unsafe_identity(w1)))

        # 防御线的关键：即使调用方不传 identity，也不能回退到全局
        w2 = tmp_root / "ghident-2"
        w2.mkdir(parents=True, exist_ok=True)
        (w2 / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        gh.prepare_repo(w2, "t")
        check("不传 identity 也不回退全局",
              gh.identity_of(w2).get("author_email")
              == gh.DEFAULT_IDENTITY[1],
              str(gh.identity_of(w2)))

        # 防线 2：GIT_AUTHOR_* 环境变量优先级高于 config，必须被清掉
        w3 = tmp_root / "ghident-3"
        w3.mkdir(parents=True, exist_ok=True)
        (w3 / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        os.environ["GIT_AUTHOR_NAME"] = "injected"
        os.environ["GIT_AUTHOR_EMAIL"] = "injected@corp.example.com"
        os.environ["GIT_COMMITTER_EMAIL"] = "injected@corp.example.com"
        try:
            gh.prepare_repo(w3, "t", good)
            check("GIT_AUTHOR_* 注入被隔离",
                  gh.identity_of(w3).get("author_email") == good[1],
                  str(gh.identity_of(w3)))
        finally:
            for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
                os.environ.pop(k, None)

        # 防线 3：闸门本身 —— 手工造一个泄漏提交，必须被拦下
        # 注意用**新建提交**而不是 --amend：amend 会保留原 author，
        # 拿它测等于测了个寂寞（第一版就这么写的，被自检自己抓出来了）。
        env = dict(os.environ,
                   GIT_AUTHOR_NAME="zhangsan",
                   GIT_AUTHOR_EMAIL="zhangsan@corp.example.com",
                   GIT_COMMITTER_NAME="zhangsan",
                   GIT_COMMITTER_EMAIL="zhangsan@corp.example.com")
        (w1 / "leak.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(w1), "add", "-A"],
                       capture_output=True, text=True, env=env, timeout=60)
        subprocess.run(["git", "-C", str(w1), "commit", "-q", "-m", "leak"],
                       capture_output=True, text=True, env=env, timeout=60)
        bad = gh.unsafe_identity(w1)
        check("能认出泄漏的真实邮箱", len(bad) == 2, str(bad))
        check("闸门拦下泄漏提交",
              _raises(lambda: gh.assert_safe_identity(w1), gh.GitHubError))
    finally:
        if keep is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = keep


def test_audit() -> None:
    """身份审计的判据 —— 纯函数，不打网络。

    盯防名单为什么是「你的身份片段」而不是「公司域名黑名单」：
    上游 fork 的原作者邮箱五花八门，用黑名单必然漏，用你的身份片段才精准。
    """
    print("[身份审计]")
    import audit_identity as au

    noreply = {"name": "Yemo3s", "email": "103362262+Yemo3s@users.noreply.github.com"}
    real = {"name": "corpuser", "email": "corpuser@example.com"}
    upstream = {"name": "someone", "email": "someone@example.org"}
    watch = ["corpuser", "corpuser@example.com"]

    check("noreply 邮箱视为干净", au.match_identity(noreply, noreply, watch) == [])
    check("命中的真实身份被查出", len(au.match_identity(real, noreply, watch)) == 1)
    check("作者干净、提交者脏也能查出",
          len(au.match_identity(noreply, real, watch)) == 1)
    check("两者都脏时报两条", len(au.match_identity(real, real, watch)) == 2)
    check("上游作者的邮箱不属于你，不误报",
          au.match_identity(upstream, upstream, watch) == [])
    check("只按姓名也能命中",
          len(au.match_identity({"name": "corpuser", "email": "other@example.com"},
                                noreply, watch)) == 1)
    check("邮箱缺失不参与判定",
          au.match_identity({"name": "corpuser", "email": ""}, {}, watch) == [])
    check("大小写不敏感",
          len(au.match_identity({"name": "CorpUser", "email": "x@example.com"},
                                {}, watch)) == 1)

    # 降级：盯防名单为空时，任何非 noreply 身份都算可疑（宁可多报）
    check("名单为空时非 noreply 一律可疑",
          len(au.match_identity(upstream, upstream, [])) == 2)
    check("名单为空时 noreply 仍干净",
          au.match_identity(noreply, noreply, []) == [])

    # 用替身顶掉 subprocess 模块，避免动到真实环境、也避免读本机真实的全局身份
    class _FakeSub:
        @staticmethod
        def run(args, **kw):  # noqa: ANN001
            vals = {"user.name": "corpuser\n",
                    "user.email": "corpuser@example.com\n"}
            return type("R", (), {"stdout": vals.get(args[-1], "")})()

    real_sub = au.subprocess
    au.subprocess = _FakeSub
    try:
        ident = au.local_global_identity()
        w, degraded = au.build_watch(["Extra@example.com"])
    finally:
        au.subprocess = real_sub

    check("能读到本机全局身份（姓名 + 邮箱两条）",
          ident == ["corpuser", "corpuser@example.com"], str(ident))
    check("追加项并入名单并统一小写",
          "extra@example.com" in w, str(w))
    check("姓名与邮箱是两条独立片段（不是拼成整串）",
          w.count("corpuser") == 1 and "corpuser@example.com" in w, str(w))
    check("有名单时不降级", degraded is False)

    # 本机没有全局身份、也没追加时，才进入降级模式
    class _EmptySub:
        @staticmethod
        def run(args, **kw):  # noqa: ANN001
            return type("R", (), {"stdout": ""})()

    au.subprocess = _EmptySub
    try:
        empty_watch, degraded2 = au.build_watch([])
    finally:
        au.subprocess = real_sub
    check("名单为空时名单确实为空", empty_watch == [])
    check("名单为空时标记降级", degraded2 is True)

    check("ghx 多行错误被压成一行",
          au._first_line("[x] HTTP 409 Git Repository is empty.\n  提示：x\n  文档：y")
          == "[x] HTTP 409 Git Repository is empty.")

    f = au.Finding("Yemo3s/x", False, False, "abc1234567", "main", True,
                   "author: corpuser <e@example.com>", "2026-01-01T00:00:00Z", "msg",
                   "https://github.com/Yemo3s/x/commit/abc")
    clean = au.render("Yemo3s", [], [], 5, watch, False)
    dirty = au.render("Yemo3s", [f], [], 5, watch, False)
    check("无命中时结论写明干净", "干净" in clean)
    check("有命中时区分公开与私有", "公开" in dirty and "私有" in dirty and "Yemo3s/x" in dirty)
    check("有命中时给出处理顺序", "delete_repo" in dirty)
    check("降级状态会写进报告", "所有非 noreply" in au.render("Yemo3s", [], [], 1, [], True))
    check("Finding 可序列化", f.as_dict()["sha"] == "abc1234567")

    # 可见性清单：公开仓库 = 暴露面。fork 要单独标注（转私有会被平台挡下）
    inv = [
        {"full_name": "Yemo3s/a", "private": False, "fork": False, "size": 1, "pushed_at": ""},
        {"full_name": "Yemo3s/b", "private": False, "fork": True, "size": 1, "pushed_at": ""},
        {"full_name": "Yemo3s/c", "private": True, "fork": False, "size": 1, "pushed_at": ""},
    ]
    ov = au.public_overview(inv)
    check("公开仓库清单只列公开的", "Yemo3s/c" not in "\n".join(ov))
    check("公开仓库清单列出两个", "Yemo3s/a" in "\n".join(ov) and "Yemo3s/b" in "\n".join(ov))
    check("公开 fork 标注 Leave fork network", "Leave fork network" in "\n".join(ov))
    check("全私有时期确说明", "无" in au.public_overview([inv[2]])[0])

    with_ov = au.render("Yemo3s", [f], [], 3, watch, False, inv)
    check("报告会带出公开仓库清单", "公开仓库：2 个" in with_ov)
    check("报告里公开 fork 有提示", "Leave fork network" in with_ov)
    check("不传清单时向后兼容", "公开仓库" not in dirty)


def main() -> int:
    ap = argparse.ArgumentParser(description="skill-release 自检")
    ap.parse_args()

    tmp_root = Path(tempfile.mkdtemp(prefix="skillrelease-selftest-"))
    try:
        test_frontmatter()
        test_projection()
        fx = test_rules(tmp_root)
        test_pack(fx, tmp_root)
        test_rejected_ext(tmp_root)
        test_network_fallback()
        test_skillhub_local(fx)
        test_creds(tmp_root)
        test_auth_detection()
        test_cookie_sniff()
        test_workbuddy_user_agent()
        test_har_extraction(tmp_root)
        test_workbuddy_upload_contract()
        test_github_identity(tmp_root)
        test_audit()
        test_version()
        test_cli()
        test_bump(tmp_root)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print()
    print(f"通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
