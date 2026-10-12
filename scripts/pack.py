"""打包与暂存：把一份技能目录派生成三个平台各自要的形态。

三个形态的差别（这是踩出来的，不是猜的）
--------------------------------------
| 目标 | 交付物 | 是否要外层目录包裹 | 原因 |
|---|---|---|---|
| SkillHub   | 逐个文件上传（无 zip） | 否 | 官方 CLI 就是把每个文件作为一个 multipart part，filename 用相对路径 |
| open.workbuddy | .zip | **要**，外层是 `{slug}/` | 平台按『根目录/二级目录/文件』计数，根目录就是技能名 |
| GitHub     | git 仓库 | 否 | 仓库根 = 技能内容，额外补 README / .gitignore |

**SKILL.md 一律在临时副本上改**：源文件保留作者原样，平台字段只进副本。
好处是同一份技能能同时喂三个平台而互不污染，也不需要「发布完再改回来」。
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

import frontmatter as fm_mod
import rules

__all__ = ["stage", "make_zip", "assert_package", "PackageError",
           "README_TEMPLATE", "GITIGNORE_TEXT"]

GITIGNORE_TEXT = """__pycache__/
*.py[cod]
.venv/
venv/
.DS_Store
Thumbs.db
"""

README_TEMPLATE = """# {title}

{summary}

## 安装

把本仓库目录放进 `~/.workbuddy/skills/` 即可（目录名 = `{slug}`）。

## 用法

见 [SKILL.md](./SKILL.md)。

## 版本

{version}

## 许可

{license}
"""

# 各平台注入到 SKILL.md frontmatter 的键，以及排在最前的顺序
_STAGE_ORDER = {
    "skillhub": ["slug", "displayName", "name", "version", "summary",
                 "description", "tags", "license", "homepage"],
    "workbuddy": ["name", "display_name", "display_name_en", "description",
                  "description_zh", "description_en", "category", "version",
                  "author", "allowed-tools"],
    "github": [],
}


class PackageError(RuntimeError):
    """打包过程或产物校验失败。"""


def _copy_tree(src: Path, dst: Path, extra_suffixes: frozenset[str] = frozenset(),
               root_icon_names: tuple[str, ...] = ()) -> None:
    """按 rules 的排除规则复制目录树，软链接一律跳过。

    extra_suffixes 用于平台级差异（如 skillhub/workbuddy 剔除 .sh），
    root_icon_names 用于剔除根目录下的图标（skillhub 的图标是平台元数据），
    让暂存目录与实际上传内容严格一致 —— 否则「检查过的」和「发出去的」会是两份东西。
    """
    src = Path(src).resolve()
    for cur, dirs, names in os.walk(src, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in rules.EXCLUDE_DIRS)
        at_root = Path(cur) == src
        rel = Path(cur).relative_to(src)
        target_dir = Path(dst) / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for n in sorted(names):
            if n in rules.EXCLUDE_NAMES or n.endswith(rules.EXCLUDE_SUFFIXES):
                continue
            if extra_suffixes and Path(n).suffix.lower() in extra_suffixes:
                continue
            if at_root and root_icon_names and n.lower() in root_icon_names:
                continue
            sp = Path(cur) / n
            if sp.is_symlink():
                continue
            shutil.copy2(sp, target_dir / n)


def stage(skill_dir: Path, dest: Path, *, platform: str, slug: str,
          author: str = "") -> Path:
    """把技能目录复制到 dest，并按平台要求重写 SKILL.md 的 frontmatter。

    返回 dest。dest 会被清空重建，传进来以前请确认它是个可丢弃的目录。
    """
    src = Path(skill_dir).resolve()
    dst = Path(dest).resolve()
    if dst == src:
        raise PackageError("暂存目录不能就是技能源目录")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    extra = (frozenset(rules.PUBLISH_EXCLUDE_SUFFIXES)
             if platform in rules.PUBLISH_EXCLUDE_PLATFORMS else frozenset())
    root_icons = (rules.SKILLHUB_EXCLUDE_ROOT_ICONS
                  if platform == "skillhub" else ())
    _copy_tree(src, dst, extra, root_icons)

    md_path = dst / "SKILL.md"
    if not md_path.is_file():
        raise PackageError("技能根目录缺少 SKILL.md")
    text = md_path.read_text(encoding="utf-8")
    data = fm_mod.parse_frontmatter(text)

    if platform == "skillhub":
        # 这里传入的 slug 已经是「该平台实际要用的 slug」（可能被配置覆盖过），
        # 直接以它为准，避免源文件名与平台 slug 不一致时又退回 name 推导。
        overrides = fm_mod.project_for_skillhub(data, slug_override=slug)
        # slug / displayName 落到 frontmatter，平台解析时也能读到
        overrides = {k: v for k, v in overrides.items()
                     if k in ("slug", "displayName", "version", "summary")}
    elif platform == "workbuddy":
        # author 未配置时不要注入空值，留给规则引擎去报缺失
        overrides = fm_mod.project_for_workbuddy(data, author)
    else:
        overrides = {}

    if overrides:
        md_path.write_text(
            fm_mod.build_skill_md(text, overrides, _STAGE_ORDER.get(platform)),
            encoding="utf-8",
        )

    if platform == "github":
        if not (dst / "README.md").is_file():
            title = str(data.get("display_name") or data.get("name") or slug)
            (dst / "README.md").write_text(
                README_TEMPLATE.format(
                    title=title,
                    summary=str(data.get("description") or "").split("\n")[0][:400],
                    slug=slug,
                    version=str(data.get("version") or ""),
                    license=str(data.get("license") or "未声明"),
                ),
                encoding="utf-8",
            )
        if not (dst / ".gitignore").is_file():
            (dst / ".gitignore").write_text(GITIGNORE_TEXT, encoding="utf-8")

    return dst


def make_zip(src_dir: Path, out_zip: Path, prefix: str = "") -> Path:
    """打包 src_dir 为 zip。prefix 非空时给每个条目加一层外层目录。"""
    src = Path(src_dir).resolve()
    out = Path(out_zip).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for cur, dirs, names in os.walk(src, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in rules.EXCLUDE_DIRS)
            for n in sorted(names):
                if n in rules.EXCLUDE_NAMES or n.endswith(rules.EXCLUDE_SUFFIXES):
                    continue
                p = Path(cur) / n
                if p.is_symlink():
                    continue
                rel = p.relative_to(src).as_posix()
                arc = f"{prefix.strip('/')}/{rel}" if prefix else rel
                info = zipfile.ZipInfo(filename=arc, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3           # unix：解压不带 Windows ACL
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, p.read_bytes())
    return out


def assert_package(zip_path: Path, platform: str) -> list[str]:
    """对已生成的 zip 复跑平台断言，返回违规项（空列表 = 通过）。

    这一步的意义：把「平台的校验规则」变成本地可执行的断言，
    不必等上传被拒才知道。两个平台的规则不同，这里按 platform 分别断言。
    """
    problems: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        return ["zip 为空"]

    if platform == "workbuddy":
        # 根目录/二级目录/文件 —— 含外层目录最多 3 段
        for n in names:
            if len(n.split("/")) > 3:
                problems.append(f"层级超限（{len(n.split('/'))} 段）: {n}")
        if not any(n.endswith("/SKILL.md") for n in names):
            problems.append("zip 内未找到 SKILL.md（应位于外层目录下）")

    if platform in ("workbuddy", "skillhub"):
        for n in names:
            leaf = n.rsplit("/", 1)[-1]
            if "." not in leaf:
                problems.append(f"存在无扩展名文件: {n}")

    total = sum(zi.file_size for zi in zipfile.ZipFile(zip_path).infolist())
    limit, _, why = rules.SIZE_LIMITS[platform]
    if total > limit:
        problems.append(f"解压后 {total / 1048576:.2f} MB 超过 {limit // 1048576} MB（{why}）")

    return problems
