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
    # 付费计费（2026-10-27 从 skillhub 前端 bundle 反解，EnterprisePaidSkillForm
    # 与个人发布向导同构）：billingMode=per_call 时 payload 带 pricing 对象，
    # 金额字符串两位小数、币种固定 CNY，展示区间 0.01–100 元/次。
    # 注意 SkillHub 只有按次计费一种付费形态（x402：调用→402 账单→付款→验付），
    # 没有买断/订阅档 —— 买断要在收费后端里自己实现（首单发 license key）。
    pricing = meta.get("pricing")
    if isinstance(pricing, dict) and pricing:
        payload["pricing"] = {
            "billingType": "per_call",
            "pricingMode": "unified",
            "pricePerCall": f"{float(pricing['pricePerCall']):.2f}",
            "currency": "CNY",
        }
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
            changelog: str, dry_run: bool = False, cookie: str = "",
            price_per_call: float | None = None) -> dict:
    """执行发布。dry_run=True 时只做本地校验，不发网络请求。

    price_per_call：按次调用单价（元，0.01–100，两位小数）。给定即走 SkillHub
    付费发布（payload.pricing，per_call）。前置条件（官方教程 2026-10-27 反解）：
      1. 账号完成「SkillPay 个人入驻」（GET /api/v1/aipay/status → onboarded:true），
         用 aipay_status() 查；
      2. 技能必须调用真实收费后端（402 → 付款 → Payment-Proof 验付 → 交付），
         纯知识包会被预检盯住、审核驳回 —— 不能仅凭文案声明替代真实服务；
      3. 建议提交前先跑 precheck_paid_skill() 拿官方预检结果。

    返回 {'ok', 'status', 'message', 'files', 'bytes', 'response'}
    """
    files = collect_files(skill_dir)
    total = sum(len(d) for _, d in files)

    if price_per_call is not None:
        if not 0.01 <= price_per_call <= 100:
            return {"ok": False, "status": "invalid", "files": len(files),
                    "bytes": total,
                    "message": f"per_call 单价 {price_per_call} 越界：SkillHub 展示区间 0.01–100 元/次",
                    "response": None}
        meta = dict(meta)
        meta["pricing"] = {"pricePerCall": price_per_call}

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


# ---------------------------------------------------------------- 只读查询
# 「图标传上去了吗、审核到哪一步」以前只能靠肉眼看页面。
# 其实两个接口就能问清楚（2026-10-13 从前端 bundle 反解 + 实测）：
#   GET /api/v1/dashboard/skills           → 技能级 iconUrl / reviewStatus
#   GET /api/v1/dashboard/skills/{slug}/versions
#                                          → 版本级 iconAuditStatus / reviewStatus
# 判据（实测对比 approved 与 pending 两种技能得出）：
#   技能级 iconUrl 非空          ⟺ 图标已生效
#   版本级 iconAuditStatus=passed ⟺ 图标审核通过（approved 的技能都是 passed 且有
#                                   iconAuditSubmittedAt 时间戳）
#   reviewStatus=pending 时技能级 iconUrl 一直是 null —— 图标随版本一起过审，
#   不是「没传上去」。看到 null 先别急着重传。

__all__ += ["list_my_skills", "list_versions", "icon_report",
            "aipay_status", "precheck_paid_skill"]


def _read(host: str, path: str, token: str = "", cookie: str = "") -> dict:
    status, body, _ = _request(f"{host.rstrip('/')}{path}", token=token,
                              cookie=cookie, method="GET")
    if status != 200:
        raise SkillHubError(format_error(status, body if isinstance(body, dict) else {}))
    return body


def list_my_skills(host: str, token: str = "", cookie: str = "") -> list[dict]:
    """我发布的技能列表（含技能级 iconUrl / reviewStatus）。"""
    body = _read(host, "/api/v1/dashboard/skills", token=token, cookie=cookie)
    if isinstance(body, list):
        return body
    return list(body.get("skills") or body.get("items") or [])


def list_versions(host: str, slug: str, token: str = "",
                  cookie: str = "") -> list[dict]:
    """某个技能的版本列表（含版本级 iconAudit* / reviewStatus）。"""
    body = _read(host, f"/api/v1/dashboard/skills/{urllib.parse.quote(slug, safe='')}"
                       "/versions", token=token, cookie=cookie)
    if isinstance(body, list):
        return body
    return list(body.get("versions") or [])


def icon_report(host: str, slug: str, token: str = "",
                cookie: str = "") -> dict:
    """图标现状一句话结论 + 原始字段，供 `release.py status` 渲染。"""
    skills = list_my_skills(host, token=token, cookie=cookie)
    mine = next((s for s in skills if str(s.get("slug")) == slug), None)
    if mine is None:
        return {"found": False, "slug": slug}
    # 版本接口对个别 slug 会回 403（you are not the owner of this skill，
    # 2026-10-13 在 skill-release 上复现）——只读探测不能因为一处失败就整体抛错，
    # 降级为「只有技能级字段」继续给结论。
    try:
        versions = list_versions(host, slug, token=token, cookie=cookie)
    except SkillHubError:
        versions = []
    latest = versions[0] if versions else {}
    icon_url = str(mine.get("iconUrl") or "")
    audit = str(latest.get("iconAuditStatus") or "")
    review = str(latest.get("reviewStatus") or mine.get("reviewStatus") or "")
    submitted = latest.get("iconAuditSubmittedAt")
    if icon_url:
        verdict = "图标已生效"
    elif audit == "passed":
        verdict = "图标审核已过，等版本过审后生效"
    elif audit in ("pending", "auditing") and submitted:
        verdict = "图标已随版本提交，审核中（版本过审后生效）"
    elif audit in ("pending", "auditing") and not submitted:
        # 实战判据（2026-10-13）：审核通过的技能 iconAuditSubmittedAt 都有时间戳，
        # 且下发是秒级的（douyin v1.0.0 提交后 8 秒即写入）。只 pending 而
        # submitted 一直是 null ⇒ 这一版根本没带 iconUrl，等多久也不会变。
        verdict = "publish 未携带 iconUrl（无提交时间戳），需带图标重发一版"
    elif audit == "rejected":
        verdict = "图标审核未通过，需换图重发一版"
    else:
        verdict = "未查到图标提交记录，需要带 iconUrl 重发一版"
    return {"found": True, "slug": slug, "name": mine.get("name"),
            "skill_icon_url": icon_url, "review_status": mine.get("reviewStatus"),
            "latest_version": latest.get("version"),
            "icon_audit_status": audit,
            "icon_audit_submitted_at": latest.get("iconAuditSubmittedAt"),
            "version_review_status": review, "verdict": verdict,
            "versions": versions}


def aipay_status(host: str, token: str = "", cookie: str = "") -> dict:
    """SkillPay 个人入驻状态（2026-10-27 从 bundle 反解 + 实测）。

    `GET /api/v1/aipay/status` → `{onboarded: bool, phone, onboardedAt, status}`。
    onboarded=true 是发布付费 Skill（per_call）的前置条件；没入驻会拿 401/403。
    注意：`/api/v1/merchant/info` 是**企业商户**通道，个人账号只会拿到
    403 ENTERPRISE_NOT_VERIFIED —— 别把它当「个人不能付费」的证据。
    """
    try:
        body = _read(host, "/api/v1/aipay/status", token=token, cookie=cookie)
    except SkillHubError as exc:
        return {"onboarded": False, "status": "error", "message": str(exc)}
    return {"onboarded": bool(body.get("onboarded")),
            "status": body.get("status") or "",
            "phone": body.get("phone") or "",
            "onboarded_at": body.get("onboardedAt")}


def precheck_paid_skill(host: str, skill_md_path: Path, token: str = "",
                        cookie: str = "") -> dict:
    """付费 Skill 官方预检（X402 合规）。发布向导在提交前对 SKILL.md 跑这条：

    `POST /paid-skills/precheck`（multipart，字段名 file = SKILL.md 文件）
    → `{compliance: "pass" | ...}`；不通过时向导允许「仍要提交」，但审核会盯。

    预检查的是 SKILL.md 里有没有描述真实 402 收费链路（Payment-Needed 账单、
    Payment-Proof 验付重试等）。纯知识包过不了这一关 —— 官方原话：
    「Skill 可以通过官方 CLI、脚本或依赖封装这些能力；不能仅凭文案声明替代
    真实可用的服务」。
    """
    skill_md_path = Path(skill_md_path)
    if not skill_md_path.exists():
        raise SkillHubError(f"SKILL.md 不存在：{skill_md_path}")
    data = skill_md_path.read_bytes()
    boundary = "----skillrelease-precheck" + str(int(time.time() * 1000))
    buf = bytearray()
    buf.extend(f"--{boundary}\r\n".encode())
    buf.extend(b'Content-Disposition: form-data; name="file"; filename="SKILL.md"\r\n')
    buf.extend(b"Content-Type: text/markdown\r\n\r\n")
    buf.extend(data)
    buf.extend(f"\r\n--{boundary}--\r\n".encode())
    status, body, _ = _request(
        f"{host.rstrip('/')}/api/v1/paid-skills/precheck",
        token=token, cookie=cookie, data=bytes(buf), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if status != 200:
        raise SkillHubError(format_error(status, body if isinstance(body, dict) else {}))
    return body
