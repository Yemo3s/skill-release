"""SkillHub 适配器 —— 纯标准库实现官方 publish 协议。

两种凭据都支持（实测确认，不是推断）
------------------------------------
  a) API Key：`Authorization: Bearer skh_…`
     官方 CLI 走的路，需在 skillhub.cn 开发者中心签发。
  b) 会话 Cookie：`Cookie: language=zh; skh_token=<JWT>; sid=…`
     浏览器登录态直接可用。实测 `GET api.skillhub.cn/api/v1/auth/me` 带 Cookie
     返回 200 用户资料；同一个 JWT 作 Bearer 却返回 401 —— 说明服务端按两条
     独立通道解析，**二者不能同时带**（cookie + Bearer 一起发会被判 token 无效）。
     因此本模块的规则是：配了 cookie 就只发 Cookie，否则才发 Bearer。

协议来源
--------
逐字对照官方 CLI（`~/.local/bin/skillhub`，实为 `skills_store_cli.py`）的实现：
  - 校验身份：GET  {host}/api/v1/auth/me      头 Authorization: Bearer <key>
  - 发布：    POST {host}/api/v1/community/skills/publish   multipart/form-data
  - payload：JSON {slug, version, displayName, summary, description,
                   tags, license, homepage, changelog}
  - files：  重复字段名 `files`，**每个 part 一个原始文件**，
             filename 用相对路径（不是 zip！服务端不解压）
  - 成功：   HTTP 201，进入审核（status=pending_review）

**不用 zip** 是关键：官方 CLI 虽然接受 zip，但它会先在本地解开再逐个上传。
直接传 zip 会被服务端判为「缺少 frontmatter」。

为什么自己实现而不调官方 CLI：
  官方 CLI 装不上时（沙箱临时目录不可写，本机已踩过）整条链路就断了。
  这里只用标准库，不依赖任何外部可执行文件，装不装 CLI 都能发。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import rules

__all__ = ["SkillHubError", "DEFAULT_HOST", "whoami", "publish", "upload_icon",
           "collect_files", "validate"]

DEFAULT_HOST = "https://api.skillhub.cn"
_TIMEOUT = 120

# 服务端对单个 zip 解压后有大小限制；发布接口本身限制 10MB
_HARD_LIMIT = rules.SIZE_LIMITS["skillhub"][0]


class SkillHubError(RuntimeError):
    pass


# ---------------------------------------------------------------- 身份


def _request(url: str, *, token: str = "", cookie: str = "",
             data: bytes | None = None, headers: dict | None = None,
             method: str = "GET",
             timeout: int = _TIMEOUT) -> tuple[int, dict, bytes]:
    hdrs = {"Accept": "application/json",
            "User-Agent": "skill-release/1.0 (+https://skillhub.cn)"}
    if cookie.strip():
        # Cookie 通道优先，且**绝不能**同时带 Authorization ——
        # 两个一起发会被服务端判为 invalid token（实测）。
        hdrs["Cookie"] = cookie.strip()
        hdrs["Origin"] = "https://www.skillhub.cn"
        hdrs["Referer"] = "https://www.skillhub.cn/"
    elif token:
        hdrs["Authorization"] = f"Bearer {token}"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                body = {"raw": raw.decode("utf-8", errors="replace")[:1024]}
            return resp.getcode(), body, raw
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:  # noqa: BLE001
            pass
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"raw": raw.decode("utf-8", errors="replace")[:1024]}
        return exc.code, body, raw
    except urllib.error.URLError as exc:
        raise SkillHubError(f"网络错误：{exc.reason}") from exc


def whoami(host: str, token: str, cookie: str = "") -> dict:
    """校验凭据是否有效。失败抛 SkillHubError。

    优先用 Cookie（会话），没有才退回 API Key（Bearer）。
    """
    status, body, _ = _request(f"{host.rstrip('/')}/api/v1/auth/me",
                               token=token, cookie=cookie)
    if status == 200:
        return body.get("user") or body
    if status == 401:
        which = "会话 Cookie" if cookie.strip() else "API Key"
        raise SkillHubError(f"{which} 无效或已失效（401）。"
                            f"请重新登录 skillhub.cn 复制 Cookie，或到开发者中心签发 skh_ 开头的 key")
    raise SkillHubError(f"校验身份失败（{status}）：{_brief(body)}")


def _brief(body: dict) -> str:
    return str(body.get("error") or body.get("message") or body.get("raw") or "")[:300]


# ---------------------------------------------------------------- 收集与校验


def collect_files(skill_dir: Path) -> list[tuple[str, bytes]]:
    """收集待上传文件，返回 [(相对路径, 内容)]。

    走 rules.collect_files(for_platform="skillhub")，与打包口径完全一致 ——
    包括剔除 .sh。口径不一致会导致「检查通过但上传被拒」。
    """
    inv = rules.collect_files(skill_dir, for_platform="skillhub")
    if not any(rel.lower() == "skill.md" for rel, _ in inv.files):
        raise SkillHubError("技能根目录缺少 SKILL.md")
    root = Path(skill_dir).resolve()
    out: list[tuple[str, bytes]] = []
    for rel, _ in inv.files:
        out.append((rel, (root / rel).read_bytes()))
    return out


def validate(meta: dict) -> list[str]:
    """本地校验，返回问题列表。与官方 CLI 的 _validate_metadata 对齐。"""
    from frontmatter import SLUG_RE, SEMVER_RE
    problems: list[str] = []
    slug = str(meta.get("slug") or "").strip()
    version = str(meta.get("version") or "").strip()
    display = str(meta.get("displayName") or "").strip()
    if not slug:
        problems.append("缺少 slug（kebab-case，全网唯一）")
    elif not SLUG_RE.match(slug):
        problems.append(f"slug 不合法（只能小写字母/数字/连字符）：{slug}")
    elif not (2 <= len(slug) <= 128):
        problems.append(f"slug 长度需在 2-128：{slug}")
    if not version:
        problems.append("缺少 version")
    elif not SEMVER_RE.match(version):
        problems.append(f"version 不是合法 SemVer（如 1.0.0）：{version}")
    if not display:
        problems.append("缺少 displayName（对外展示名）")
    return problems


# ---------------------------------------------------------------- 上传


def _multipart(meta: dict, files: list[tuple[str, bytes]],
               changelog: str) -> tuple[bytes, str]:
    boundary = "----skillrelease" + str(int(time.time() * 1000))
    payload = {
        "slug": meta.get("slug", ""),
        "version": meta.get("version", ""),
        "displayName": meta.get("displayName", ""),
        "summary": meta.get("summary", ""),
        "description": meta.get("description", ""),
        "tags": meta.get("tags") if isinstance(meta.get("tags"), list) else [],
        "license": meta.get("license", ""),
        "homepage": meta.get("homepage", ""),
        "changelog": changelog,
        # 官网表单直传 iconUrl（2026-10-12 从 skillhub 前端 bundle 反解确认：
        # publish payload 支持 iconUrl 字段，值为 skill-icons/upload 返回的 COS URL）
        "iconUrl": meta.get("iconUrl") or None,
    }
    # iconUrl 为空时从 payload 里去掉（官网发的是 undefined），其余字段保持原样
    if payload["iconUrl"] is None:
        payload.pop("iconUrl")
    buf = bytearray()

    def part(name: str, data: bytes, filename: str | None = None,
             ctype: str = "application/octet-stream") -> None:
        buf.extend(f"--{boundary}\r\n".encode())
        disp = f'form-data; name="{name}"'
        if filename:
            disp += f'; filename="{filename}"'
        buf.extend(f"Content-Disposition: {disp}\r\n".encode())
        buf.extend(f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.extend(data)
        buf.extend(b"\r\n")

    part("payload", json.dumps(payload, ensure_ascii=False).encode("utf-8"),
         ctype="application/json")
    for rel, data in files:
        ctype = "text/markdown" if rel.lower().endswith(".md") else "application/octet-stream"
        part("files", data, filename=rel, ctype=ctype)
    buf.extend(f"--{boundary}--\r\n".encode())
    return bytes(buf), boundary


def upload_icon(host: str, token: str, icon_path: Path, cookie: str = "") -> str:
    """上传技能图标 → 返回 iconUrl（COS 地址）。

    契约（2026-10-12 从 skillhub.cn 前端 bundle 反解，函数名原样保留）：
      `uploadSkillIcon(file)` = `new FormData` + `append("file", file, name)`
      → `POST /api/v1/community/skill-icons/upload` → `{iconUrl}`。
    平台要求 512×512 PNG（前端 validateAvatarFile/crop 到方形）；官方 CLI
    不传图标 —— 图标只能走这条通道，或在 publish payload 里带 iconUrl。
    """
    icon_path = Path(icon_path)
    data = icon_path.read_bytes()
    if len(data) > 500_000:
        raise SkillHubError(f"图标超过 500 KB（当前 {len(data) / 1024:.0f} KB）：{icon_path}")
    boundary = "----skillrelease-icon" + str(int(time.time() * 1000))
    buf = bytearray()
    buf.extend(f"--{boundary}\r\n".encode())
    buf.extend(b'Content-Disposition: form-data; name="file"; filename="'
               + icon_path.name.encode("utf-8") + b'"\r\n')
    buf.extend(b"Content-Type: image/png\r\n\r\n")
    buf.extend(data)
    buf.extend(f"\r\n--{boundary}--\r\n".encode())
    status, body, _ = _request(
        f"{host.rstrip('/')}/api/v1/community/skill-icons/upload",
        token=token, cookie=cookie, data=bytes(buf), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if status not in (200, 201):
        if status == 401:
            raise SkillHubError("图标上传失败（401）：凭据失效，重新配 skillhub.cookie/key")
        raise SkillHubError(f"图标上传失败（{status}）：{_brief(body)}")
    url = str(body.get("iconUrl") or (body.get("data") or {}).get("iconUrl") or "")
    if not url:
        raise SkillHubError(f"图标上传未返回 iconUrl：{_brief(body)}")
    return url


def format_error(status: int, body: dict) -> str:
    """把服务端错误翻译成可执行的下一步。官方 CLI 的措辞，保持一致。"""
    msg = _brief(body)
    if status == 401:
        return ("凭据失效（401）：重新执行 "
                "skill-release creds --set skillhub.cookie='<浏览器 Cookie>'，"
                "或到开发者中心签发 skh_ 开头的 key")
    if status == 403:
        return f"权限不足（403）：{msg}（确认该 key 有 publish 权限、账号已实名）"
    if status == 409:
        return f"slug 冲突（409）：{msg}（换成别的 slug，或去后台确认归属）"
    if status == 429:
        rule = body.get("rule") or ""
        retry = body.get("retryAfter")
        hint = f"，请 {retry} 秒后重试" if retry else "，请稍后重试"
        return f"发布过于频繁（429{hint}）{('规则 ' + str(rule)) if rule else ''}：{msg}"
    if 400 <= status < 500:
        return f"请求被拒（{status}）：{msg}"
    return f"服务端错误（{status}）：{msg}"


def publish(host: str, token: str, skill_dir: Path, meta: dict,
            changelog: str, dry_run: bool = False, cookie: str = "") -> dict:
    """执行发布。dry_run=True 时只做本地校验，不发网络请求。

    返回 {'ok', 'status', 'message', 'files', 'bytes', 'response'}
    """
    files = collect_files(skill_dir)
    total = sum(len(d) for _, d in files)

    problems = validate(meta)
    no_ext = [r for r, _ in files if not Path(r).suffix]
    if no_ext:
        problems.append("存在无扩展名文件，SkillHub 会整包拒绝：" + "、".join(no_ext))
    if total > _HARD_LIMIT:
        problems.append(f"包体积 {total / 1048576:.2f} MB 超过 10 MB 上限")

    if problems:
        return {"ok": False, "status": "invalid", "files": len(files), "bytes": total,
                "message": "；".join(problems), "response": None}

    if dry_run:
        return {"ok": True, "status": "dry-run", "files": len(files), "bytes": total,
                "message": f"本地校验通过：{meta.get('slug')}@{meta.get('version')}",
                "response": None}

    body, boundary = _multipart(meta, files, changelog)
    status, resp, _ = _request(
        f"{host.rstrip('/')}/api/v1/community/skills/publish",
        token=token, cookie=cookie, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if status in (200, 201):
        return {"ok": True, "status": "pending_review", "files": len(files),
                "bytes": total,
                "message": f"已提交审核：{resp.get('skillId') or resp.get('slug') or meta.get('slug')}",
                "response": resp}
    return {"ok": False, "status": "error", "files": len(files), "bytes": total,
            "message": format_error(status, resp), "response": resp}


def skill_url(host: str, slug: str) -> str:
    return f"https://www.skillhub.cn/skills/{urllib.parse.quote(slug, safe='')}"
