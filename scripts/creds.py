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
                  "party_id": "", "enterprise_id": "",
                  # 付费发布：pay_mode 0=免费 1=买断 2=订阅；pay_amount 单位是**元**
                  # （内部提交时换算成分）；pay_days 仅订阅用。付费方式是版本级
                  # 属性、过审即锁死，所以这些值一旦定下就别随手改。
                  # audience = 可触达人群估算（付费前必填，见 rules.revenue_ceiling）
                  "pay_mode": 0, "pay_amount": 0, "pay_days": 0,
                  "audience": 0},
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


def _maybe_json(value):
    """把 JSON 数组/对象字面量解析成真结构，其余原样返回。

    为什么需要：`--set categories=["tools:office"]` 命令行拿到的永远只是字符串。
    若原样落盘，提交时平台会把它当**可迭代字符串**逐字符遍历 ——
    `['[', '"', 't', 'o', 'o', 'l', 's', ':', ...]` 变成 16 个单字符类目，
    而类目字段没有任何 update 端点，只能重新发一整版才能改回来。
    2026-10-26 就这么废掉过一版 1.5.3。所以入口处就解析成真数组。
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s or s[0] not in "[{":
        return value
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        return value
    return parsed if isinstance(parsed, (list, dict)) else value


def set_value(dotted: str, value: str, path: Path | None = None) -> Path:
    """按 `skillhub.key` 这样的点号路径写入单个值。

    值是 JSON 字面量时（`--set workbuddy.categories=["tools:office"]`）
    会解析成真数组再落盘，见 `_maybe_json()`。
    """
    value = _maybe_json(value)
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
    mode = int(wb.get("pay_mode") or 0)
    mode_txt = {0: "免费", 1: "一次性买断", 2: "月订阅"}.get(mode, f"未知({mode})")
    amount = wb.get("pay_amount") or 0
    lines.append(f"  workbuddy.pay    ：{mode_txt}"
                 + (f" ¥{amount}" if mode == 1 else "")
                 + (f" ¥{amount}/{wb.get('pay_days') or '?'} 天" if mode == 2 else ""))
    aud = int(wb.get("audience") or 0)
    if mode:
        try:
            import rules as _rules
            rc = _rules.revenue_ceiling(aud, amount)
            lines.append(f"  workbuddy.audience：{aud or '(未设)'}  →  {rc['verdict']}")
        except Exception:
            lines.append(f"  workbuddy.audience：{aud or '(未设)'}")
    else:
        lines.append(f"  workbuddy.audience：{aud or '(未设，发付费时会强制要求)'}")
    lines.append(f"  github.owner     ：{gh.get('owner') or '(未设)'}")
    lines.append(f"  author           ：{data.get('author') or '(未设)'}")
    return "\n".join(lines)
