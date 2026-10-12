"""凭据读写。

**为什么放在技能目录之外**
--------------------------
技能目录会被打包上传到三个平台。只要凭据文件在技能目录里，就有被连带
打包的风险（github-ops 的 doctor 专门检查这一点）。所以统一落在：

    ~/.workbuddy/.secrets/skill-release.json

并且：读进来之后只在内存里用，打印一律走 mask()，不落日志。

**为什么不用系统凭据管理器**
--------------------------
跨平台成本高（Windows DPAPI / macOS Keychain / Linux Secret Service）。
这里用文件 + 目录权限约束，和 github-ops 的取法保持一致，够用且可移植。

环境变量优先级高于文件，便于 CI 或一次性调用：
    SKILLHUB_KEY / SKILLHUB_COOKIE / WORKBUDDY_COOKIE / SKILL_RELEASE_AUTHOR
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["SECRETS_PATH", "load", "save", "mask", "section", "set_value",
           "ENV_MAP", "describe"]

SECRETS_PATH = Path.home() / ".workbuddy" / ".secrets" / "skill-release.json"

ENV_MAP = {
    "skillhub.key": "SKILLHUB_KEY",
    "skillhub.cookie": "SKILLHUB_COOKIE",
    "workbuddy.cookie": "WORKBUDDY_COOKIE",
    "author": "SKILL_RELEASE_AUTHOR",
}

_EMPTY: dict = {
    "skillhub": {"key": "", "cookie": "", "host": "https://api.skillhub.cn"},
    "workbuddy": {"cookie": "", "base": "https://open.workbuddy.cn",
                  "party_id": "", "enterprise_id": ""},
    "github": {"owner": "", "private": True},
    "author": "",
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None) -> dict:
    """读配置。文件不存在时返回带默认值的空配置，绝不抛异常。"""
    p = Path(path) if path else SECRETS_PATH
    data = _merge(_EMPTY, {})
    if p.is_file():
        try:
            data = _merge(data, json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass  # 坏了就当空配置，由 require() 报缺什么，不在这里炸
    for dotted, env in ENV_MAP.items():
        val = os.environ.get(env, "").strip()
        if not val:
            continue
        if "." in dotted:
            head, tail = dotted.split(".", 1)
            data.setdefault(head, {})[tail] = val
        else:
            data[dotted] = val
    return data


def save(data: dict, path: Path | None = None) -> Path:
    """写配置，目录权限收紧到 0700。"""
    p = Path(path) if path else SECRETS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass  # Windows 上基本无效果，静默即可
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def set_value(dotted: str, value: str, path: Path | None = None) -> Path:
    """按 `skillhub.key` 这样的点号路径写入单个值。"""
    data = load(path)
    if "." in dotted:
        head, tail = dotted.split(".", 1)
        data.setdefault(head, {})[tail] = value
    else:
        data[dotted] = value
    return save(data, path)


def section(name: str, path: Path | None = None) -> dict:
    return dict(load(path).get(name) or {})


def mask(value: str, keep: int = 4) -> str:
    """脱敏打印。短于 keep*2 的一律打成 ****。"""
    if not value:
        return "(空)"
    if len(value) <= keep * 2:
        return "****"
    return f"{value[:keep]}…{value[-keep:]}（共 {len(value)} 字符）"


def describe(path: Path | None = None) -> str:
    """体检输出：每个平台的凭据是否就位（只显示脱敏值）。"""
    data = load(path)
    lines = [f"配置文件：{Path(path) if path else SECRETS_PATH}"]
    lines.append(f"  存在：{'是' if (Path(path) if path else SECRETS_PATH).is_file() else '否'}")
    sh = data.get("skillhub", {})
    wb = data.get("workbuddy", {})
    gh = data.get("github", {})
    lines.append(f"  skillhub.key     ：{mask(sh.get('key', ''))}")
    lines.append(f"  skillhub.cookie  ：{mask(sh.get('cookie', ''))}")
    lines.append(f"  skillhub.host    ：{sh.get('host', '')}")
    lines.append(f"  workbuddy.cookie ：{mask(wb.get('cookie', ''))}")
    lines.append(f"  workbuddy.user_agent：{wb.get('user_agent') or '(未设，用兜底 UA)'}")
    lines.append(f"  workbuddy.party_id  ：{wb.get('party_id') or '(未设)'}")
    lines.append(f"  workbuddy.base   ：{wb.get('base', '')}")
    lines.append(f"  github.owner     ：{gh.get('owner') or '(未设)'}")
    lines.append(f"  author           ：{data.get('author') or '(未设)'}")
    return "\n".join(lines)
