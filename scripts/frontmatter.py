"""SKILL.md frontmatter 的解析、按平台投影、以及安全回写。

设计取舍
--------
**刻意不用 PyYAML**：本技能对外承诺零第三方依赖，装了 yaml 的技能在别人的
机器上会直接崩。只需要支持平台实际用到的子集：

    key: value                 裸标量
    key: "a: b"                双引号标量（支持 \\" 转义）
    key: 'a: b'                单引号标量
    key: [a, b]                行内列表
    key: |                     字面块标量（保留换行）
    key: >                     折叠块标量（换行折成空格）

**不做原地修改**：平台要求的字段各不相同（SkillHub 要 slug / displayName，
open.workbuddy 要 author），这些字段属于「平台元数据」而不是技能本体。
一律投影到临时副本，源 SKILL.md 保持干净 —— 这样同一份技能可以同时喂给
三个平台，不会互相污染。
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "FrontmatterError",
    "split_frontmatter",
    "parse_frontmatter",
    "render_frontmatter",
    "build_skill_md",
    "project_for_skillhub",
    "project_for_workbuddy",
    "SLUG_RE",
    "SEMVER_RE",
]

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

_BLOCK_RE = re.compile(r"^[|>][+-]?$")


class FrontmatterError(ValueError):
    """SKILL.md 的 frontmatter 不合法。"""


# ---------------------------------------------------------------- 解析


def split_frontmatter(text: str) -> tuple[str, str]:
    """切出 (frontmatter 原文, 正文)。首行必须是独立的 ---。"""
    if not text.startswith("---"):
        raise FrontmatterError("SKILL.md 必须以 `---` 开头（缺少 frontmatter）")
    lines = text.split("\n")
    if lines[0].strip() != "---":
        raise FrontmatterError("首行必须恰好是 `---`")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    raise FrontmatterError("frontmatter 缺少结束标记 `---`")


def _unquote(val: str) -> str:
    """去引号。双引号里处理反斜杠转义（\\" / \\\\ / \\n）。"""
    if len(val) >= 2 and val[0] == val[-1] == '"':
        body = val[1:-1]
        out: list[str] = []
        i = 0
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
                i += 2
                continue
            out.append(c)
            i += 1
        return "".join(out)
    if len(val) >= 2 and val[0] == val[-1] == "'":
        return val[1:-1].replace("''", "'")
    return val


def _parse_inline_list(val: str) -> list[str]:
    inner = val[1:-1].strip()
    if not inner:
        return []
    return [_unquote(s.strip()) for s in inner.split(",") if s.strip()]


def parse_frontmatter(text: str) -> dict[str, object]:
    """把 frontmatter 解析成 dict。值为 str 或 list[str]。"""
    raw, _ = split_frontmatter(text)
    lines = raw.split("\n")
    out: dict[str, object] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):  # 块标量的续行由下面消费，这里跳过
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if not key:
            continue

        if _BLOCK_RE.match(val):
            folded = val.startswith(">")
            block: list[str] = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1] in (" ", "\t")):
                block.append(lines[i])
                i += 1
            # 去掉公共缩进
            indents = [len(b) - len(b.lstrip()) for b in block if b.strip()]
            cut = min(indents) if indents else 0
            body = [b[cut:] if len(b) >= cut else "" for b in block]
            while body and not body[-1].strip():
                body.pop()
            out[key] = (" ".join(x.strip() for x in body if x.strip())
                        if folded else "\n".join(body))
            continue

        if val.startswith("[") and val.endswith("]"):
            out[key] = _parse_inline_list(val)
        else:
            out[key] = _unquote(val)
    return out


# ---------------------------------------------------------------- 渲染


def _needs_quotes(val: str) -> bool:
    if val == "" or val != val.strip():
        return True
    if val[:1] in ("-", "?", ":", "#", "&", "*", "!", "|", ">", "@", "`", "[", "{", "'", '"'):
        return True
    return any(c in val for c in (": ", " #", "\n", "\t"))


def _dump_scalar(val: str) -> str:
    if "\n" in val:
        # 多行走块标量，比转义成一长串 \n 可读得多
        body = "\n".join("  " + ln for ln in val.split("\n"))
        return "|\n" + body
    if _needs_quotes(val):
        esc = val.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{esc}"'
    return val


def render_frontmatter(data: dict[str, object]) -> str:
    """把 dict 渲染回 frontmatter 文本（含首尾 ---）。"""
    lines: list[str] = ["---"]
    for key, val in data.items():
        if isinstance(val, list):
            joined = ", ".join(str(x) for x in val)
            lines.append(f"{key}: [{joined}]")
        else:
            dumped = _dump_scalar(str(val))
            if dumped.startswith("|\n"):
                lines.append(f"{key}: |")
                lines.append(dumped[2:])
            else:
                lines.append(f"{key}: {dumped}")
    lines.append("---")
    return "\n".join(lines)


def build_skill_md(original_md: str, overrides: dict[str, object],
                   key_order: list[str] | None = None) -> str:
    """在原文基础上覆盖/新增 frontmatter 字段，正文原样保留。

    key_order 给定时，这些键排在最前（顺序固定，diff 才稳定）。
    """
    fm_text, body = split_frontmatter(original_md)
    data = parse_frontmatter(original_md)
    data.update({k: v for k, v in overrides.items() if v not in (None, "")})

    if key_order:
        ordered: dict[str, object] = {}
        for k in key_order:
            if k in data:
                ordered[k] = data.pop(k)
        ordered.update(data)
        data = ordered

    return render_frontmatter(data) + "\n" + body


# ---------------------------------------------------------------- 平台投影


def _first_line(text: str, limit: int = 150) -> str:
    """取描述的第一句/第一行做 summary —— 平台对 summary 的长度有限制。"""
    flat = " ".join(str(text or "").split())
    for stop in ("。", "；", ". ", "\n"):
        idx = flat.find(stop)
        if 0 < idx < limit:
            return flat[: idx + len(stop.strip())].strip()
    return flat[:limit].strip()


def project_for_skillhub(fm: dict[str, object],
                         slug_override: str = "") -> dict[str, object]:
    """投影出 SkillHub publish 需要的 payload。

    依据 skillhub 官方 CLI（skills_store_cli.py）的 _validate_metadata：
    硬性要求 slug / version / displayName 三项，其余为推荐字段。

    slug_override 存在的理由很实在：slug 是**全网唯一且可能被封禁**的，
    而技能名不该为了迁就平台域名而改。例如 `douyin-downloader` 这个 slug
    已被平台封禁，实际可用的历史 slug 是复数的 `douyin-downloaders` ——
    于是把映射放进配置（`skillhub.slug_map`），源 SKILL.md 保持不动。
    """
    slug = str(slug_override or fm.get("slug") or fm.get("name") or "").strip()
    display = str(fm.get("displayName") or fm.get("display_name") or slug).strip()
    description = str(fm.get("description") or "").strip()
    tags = fm.get("tags")
    return {
        "slug": slug,
        "version": str(fm.get("version") or "").strip(),
        "displayName": display,
        "summary": str(fm.get("summary") or _first_line(description)).strip(),
        "description": description,
        "tags": tags if isinstance(tags, list) else ([str(tags)] if tags else []),
        "license": str(fm.get("license") or "").strip(),
        "homepage": str(fm.get("homepage") or "").strip(),
    }


def project_for_workbuddy(fm: dict[str, object], author: str = "") -> dict[str, object]:
    """投影出 open.workbuddy 的 SKILL.md frontmatter 覆盖项。

    依据 open.workbuddy/docs/skill：必填 description / description_zh /
    description_en / version / author；其余可选。
    """
    overrides: dict[str, object] = {
        "description": str(fm.get("description") or "").strip(),
        "description_zh": str(fm.get("description_zh") or "").strip(),
        "description_en": str(fm.get("description_en") or "").strip(),
        "version": str(fm.get("version") or "").strip(),
    }
    if author:
        overrides["author"] = author
    elif fm.get("author"):
        overrides["author"] = str(fm["author"]).strip()
    if fm.get("category"):
        overrides["category"] = str(fm["category"]).strip()
    return overrides


def skill_md_path(skill_dir: Path) -> Path:
    p = Path(skill_dir) / "SKILL.md"
    if not p.is_file():
        raise FrontmatterError(f"未找到 SKILL.md: {p}")
    return p
