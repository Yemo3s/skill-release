"""open.workbuddy 适配器 —— Cookie 会话 + 预签名直传。

**这个模块的确定性分两档，必须说清楚**
------------------------------------
已确认（从前端构建产物 `_next/static/chunks/007.js` 读出的真实调用）：

    base          NEXT_PUBLIC_API_BASE_URL，为空即同源 https://open.workbuddy.cn
    鉴权          credentials: "include" —— **Cookie 会话**，不是 token
    额外头        X-Party-Id（入驻主体）、X-Enterprise-Id（企业），没有可省
    401 语义      会话过期 → 前端跳登录

    上传链路（能力 = capabilities，技能是其中一类）—— 2026-10-10 真机实测：
      1. POST /api/v2/open-platform/capabilities/init-upload    → {uploadId, uploadUrl, objectKey}
      2. PUT  {uploadUrl}  body=zip  Content-Type=<mime>        → 直传 COS
      3. POST /api/v2/open-platform/capabilities/complete-upload → {uploadToken}
      4. POST /api/v2/open-platform/capabilities/submit         → 进审核队列

    ⚠️ 别拿 `/common/uploads/presign` 传能力包 —— 那是封面/头像的通道（要 scene），
    传包只会得到 `invalid request parameters`。反过来封面**只能**走它，见 upload_cover()。

    只读接口：
      GET  /api/v2/open-platform/parties
      GET  /api/v2/open-platform/capabilities/categories?capabilityType=skill  （市场类目）
      GET  /api/v2/open-platform/capabilities/{assetId}                        （详情，含 fields）
      POST /api/v2/open-platform/capabilities/summary
      POST /api/v2/open-platform/capabilities/list

    资质类目（`fields.common.categories`）的枚举在**文档站**，不在 API 里：
      https://open.workbuddy.cn/docs/service-categories-individual
    """

from __future__ import annotations

import json
import mimetypes
import ssl
import urllib.error
import urllib.request
from pathlib import Path

__all__ = ["WorkBuddyError", "DEFAULT_BASE", "whoami", "probe", "upload_and_submit",
           "upload_cover", "normalize_categories", "CAPABILITIES", "PRESIGN",
           "SCENE_COVER", "sniff_cookie", "cookie_from_har", "find_asset", "unwrap",
           "set_user_agent", "user_agent"]

DEFAULT_BASE = "https://open.workbuddy.cn"
CAPABILITIES = "/api/v2/open-platform/capabilities"
PRESIGN = "/api/v2/open-platform/common/uploads/presign"
# 前端 imageType 枚举（chunk 79908）：头像/封面/证件照共用 presign 通道，靠 scene 区分
SCENE_COVER = "capability_cover"

# 曾经这里有一份 DEFAULT_PAYLOADS（字段名靠命名惯例猜：fileName/fileSize/
# cosKey/assetId…），配一条 `/common/uploads/presign` 的「预签名」路径。
# **2026-10-10 全部推翻**：实测报 `invalid request parameters`，
# 从前端 bundle 反解出真实契约后才跑通。真实步骤见 `upload_and_submit()`
# 的 docstring，别再照猜的字段名发请求。

# 兜底 UA。**但真正能用的是「签发会话时那个浏览器的 UA」** —— 见下面的长注释。
_DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
_UA = _DEFAULT_UA


def user_agent() -> str:
    return _UA


def set_user_agent(ua: str) -> None:
    """用凭据里存的 UA 覆盖默认值。空值 = 退回兜底 UA。

    为什么需要这个 —— **本站会话绑定客户端 UA**（2026-10-10 实测）：

    拿**同一份** HAR 里的 `session`，只改 `User-Agent` 重放同一个 GET
    `/api/v2/open-platform/parties`：

    | 请求头里的 User-Agent | 结果 |
    |---|---|
    | `… (Linux; Android 16; Pixel 10) … Chrome/154.0.0.0 Mobile Safari/537.36`（真值） | **200** |
    | 真值 + 结尾一个空格 | 200（空格被忽略） |
    | `Pixel 10` → `Pixel 9` | 302 |
    | `Android 16` → `Android 15` | 302 |
    | `Chrome/154` → `Chrome/155` | 302 |
    | 删掉 `Mobile` | 302 |
    | 换个 Android 机型（SM-S928B） | 302 |
    | 我们兜底的 Windows Chrome UA | 302 |

    **差一个字符就 302**，命中就 200 —— 精确匹配，不是「像浏览器就行」。
    所以 UA 不是装饰，它是鉴权的组成部分，必须与浏览器发出会话时一致。
    这条也是本链路**最后一个**坑：在此之前，Cookie 明明是对的（HAR 原样读出、
    4001 字符、浏览器同款），却因为发了个自己的 UA，被 302 骗了整整一轮。
    """
    global _UA
    ua = (ua or "").strip()
    _UA = ua or _DEFAULT_UA

# 未登录时 open.workbuddy.cn 的行为（实测）：
#   APISIX 把接口 302 到 Keycloak 登录页
#   Location: /auth/realms/copilot/protocol/openid-connect/auth?client_id=console&…
#   若跟随重定向，最终拿到的是 HTTP 200 + 登录页 HTML。
# 这正是首版踩过的坑：只要状态码是 200 就当鉴权通过，于是一个**完全没登录**
# 的会话被判成「通过」。所以下面三条：不跟随重定向、非 JSON 一律判失败、
# 只有 200 + JSON 才算成功。
_LOGIN_MARKERS = ("/auth/realms/", "login-pf", "<!DOCTYPE html", "<html")


class WorkBuddyError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向 —— 要亲眼看到那个 302，而不是被喂一个 200 的登录页。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def _headers(cookie: str, party_id: str = "", enterprise_id: str = "",
             extra: dict | None = None) -> dict:
    h = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": _UA,
        "Cookie": cookie,
        "Origin": DEFAULT_BASE,
        "Referer": DEFAULT_BASE + "/",
    }
    if party_id:
        h["X-Party-Id"] = party_id
    if enterprise_id:
        h["X-Enterprise-Id"] = enterprise_id
    h.update(extra or {})
    return h


def _call(base: str, path: str, cookie: str, *, method: str = "GET",
          payload: dict | None = None, party_id: str = "",
          enterprise_id: str = "", timeout: int = 60) -> tuple[int, dict]:
    url = base.rstrip("/") + path
    data = None
    hdrs = _headers(cookie, party_id, enterprise_id)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=_ctx()))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.getcode(), json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return resp.getcode(), {"raw": raw.decode("utf-8", errors="replace")[:800]}
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:  # noqa: BLE001
            pass
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            body = {"raw": raw.decode("utf-8", errors="replace")[:800]}
        if not isinstance(body, dict):
            body = {"raw": str(body)[:800]}
        loc = exc.headers.get("Location", "") if exc.headers else ""
        if loc:
            body["_location"] = loc[:400]
        return exc.code, body
    except urllib.error.URLError as exc:
        raise WorkBuddyError(f"网络错误：{exc.reason}") from exc


def auth_problem(status: int, body: dict) -> str | None:
    """这次响应的实质是不是「未登录」？是则返回原因，否则 None。

    单独抽出来是因为 whoami / probe / 每一步上传都要用同一把尺子量 ——
    首版只在 whoami 里判 401，结果 302→登录页 这条最常见的情况全被漏掉。
    """
    inv = body.get("invalid")
    loc = str(body.get("_location") or "")
    raw = str(body.get("raw") or "")

    if status in (301, 302, 303, 307, 308):
        if "/auth/realms/" in loc or "/auth/" in loc:
            return ("会话已失效：接口被重定向到 Keycloak 登录页"
                    f"（{loc.split('?')[0] or '未知'}）")
        return f"接口返回重定向（{status} → {loc[:120]}）"
    if status == 401:
        return "会话已失效（401）"
    if status == 403:
        return f"权限不足（403）：{_brief(body)}"
    if status == 200 and not inv and ("raw" in body):
        if any(m in raw for m in _LOGIN_MARKERS):
            return "会话已失效：接口返回登录页 HTML 而非 JSON"
        return f"接口返回了非 JSON 内容：{' '.join(raw.split())[:160]}"
    return None


def _auth_or_raise(status: int, body: dict) -> None:
    problem = auth_problem(status, body)
    if not problem:
        return
    if status in (200, 401, 302):
        raise WorkBuddyError(
            f"{problem}\n"
            f"    优先怀疑两件事：\n"
            f"    1) Cookie 是坏的 —— 登录后的 session 约 4 KB，手工复制极易被截断，"
            f"末尾的 27 位 HMAC 一丢就必然 302。用 "
            f"`creds --from-har <浏览器导出的 .har>` 重新取，别走剪贴板。\n"
            f"    2) 请求所属域名不是 open.workbuddy.cn —— F12 → Network 筛选框里"
            f"输入这个域名，只认它自己的请求。"
        )
    raise WorkBuddyError(problem)


def _brief(body: dict) -> str:
    for k in ("message", "msg", "error", "errorMessage", "detail"):
        if body.get(k):
            return str(body[k])[:400]
    return str(body.get("raw") or body)[:400]


# ── Cookie 自检 ──────────────────────────────────────────────────
# 链路走到这里，**唯一被证明有效的判据是响应的状态码**（200 + JSON = 通过，
# 302 → Keycloak = 不通过）。下面只保留一条绝对可靠的检查：有没有 `session`。
#
# ⚠️ 历史上这里堆过三个「结构判据」，全部被后来的实测否掉，别再复活：
#
#   1. ❌「带 `qcloud_visitId` / `qcloud_from` / `trafficParams` /
#      `sensorsdata*` / `session_2` = 拿错站」
#      → 实测 open.workbuddy.cn **自己的已登录请求就带着这一整串**（同源埋点）。
#        2026-10-10 从 HAR 里逐条核对确认。
#   2. ❌「`Set-Cookie: session_2=; Max-Age=0` = 服务端不认这份会话」
#      → 对照实验：本站签发的匿名 session / 篡改末段 HMAC / 砍掉末段 /
#        只带 session_2，服务端都**不清**它。它只是服务端重置会话的副产物。
#   3. ❌「session 必须是 4 段 `<22位id>|<10位过期>|<payload>|<27位HMAC>`；
#      `4001 字符 / 3 段` = 复制被截断」
#      → **错得最离谱的一条。** HAR（落盘文件，无剪贴板参与）里 45 条已登录
#        请求**全部**是 `4001 字符 / 3 段 [22,10,3967]`，状态码全 200。
#        也就是说：匿名会话是 4 段 276 字符，**登录会话本来就是 3 段 4001 字符**。
#        长度和段数根本不是「坏值」的指纹。
#
#    这个错判让我把 mo 手抄的值和 HAR 里的值都当成了「被截断」，
#    白烧了一轮。**真正的差别在 User-Agent 上** —— 见 set_user_agent()。
#
# 教训：结构特征只是「当下采样长这样」，不是契约。要写进判据，先做反例。


def _cookie_map(cookie: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k:
            jar[k.strip()] = v.strip()
    return jar


def sniff_cookie(cookie: str) -> list[str]:
    """先看 Cookie 的身世，再去打网络请求。返回人话警告，空列表 = 看不出问题。

    这**不是判据** —— 判据永远是响应的状态码。它只拦一种「根本不可能成功」的
    情况（没有 `session`），省掉一次无意义的请求。
    """
    jar = _cookie_map(cookie)
    warns: list[str] = []

    if not jar.get("session"):
        warns.append(
            "没找到 session —— 本站鉴权全靠它。它是 HttpOnly 的，"
            "Console 里执行 document.cookie **看不到**；必须从 F12 → Network → "
            "Request Headers → Cookie，或 Application → Cookies 面板取。"
            "（更省事的做法：整份 HAR 交给 `creds --from-har`。）")
    return warns


def cookie_from_har(har_path: Path, host: str = "") -> tuple[str, dict]:
    """从浏览器导出的 HAR 里抠出本站 Cookie。

    **为什么需要这个**：登录后的 `session` 约 4 KB，手工「选中-复制」会被截断
    （实测两次都卡在 4001 字符，末尾 27 位 HMAC 整段丢失 → 302）。
    HAR 是落盘文件，读它零损耗 —— 绕开的正是那个最脆弱的一环。

    取法：F12 → Network → 刷新 → 右键 → `Save all as HAR`，然后把文件路径给我。

    ⚠️ Chrome 130 起，**HAR 默认导出的是「已清理」档**：Cookie / Set-Cookie /
    Authorization 全被剥掉（官方隐私变更）。整份 HAR 一个 Cookie 都没有，
    必须先在 DevTools 里打开 **设置 → 偏好设置 → 网络 →
    允许生成包含敏感数据的 HAR**，再从「导出」下拉里选
    **导出 HAR（包含敏感数据）**。下面对这种情况直接点名，别让人去查
    「为什么我没登录」—— 请求明明都在。

    只挑**最后一条带 `session` 的 `host` 请求**，不做跨条目合并 ——
    合并会把旧 `session` 和新 `session_2` 拼在一起，比不取更糟。

    **同时把那条请求的 `User-Agent` 一起带回来**，因为本站会话绑定 UA
    （差一个字符就 302，见 `set_user_agent()`）。只带 Cookie 不带 UA，
    在别的机器/别的 UA 上重放必然失败 —— 这正是 2026-10-10 卡住的那一轮。

    返回 (Cookie 串, 元信息)；元信息里的 `user_agent` 要一并落盘。
    """
    import json
    from urllib.parse import urlsplit

    if not host:
        host = urlsplit(DEFAULT_BASE).hostname or "open.workbuddy.cn"
    path = Path(har_path)
    if not path.is_file():
        raise WorkBuddyError(f"HAR 文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise WorkBuddyError(f"HAR 不是合法 JSON：{exc}") from exc

    entries = ((data.get("log") or {}).get("entries")) or []
    best: dict[str, str] = {}
    best_url = ""
    best_ua = ""
    scanned = 0
    with_cookie = 0      # 有多少条本站请求是「带着 Cookie」的
    for e in entries:
        req = (e or {}).get("request") or {}
        url = str(req.get("url") or "")
        try:
            if (urlsplit(url).hostname or "") != host:
                continue
        except ValueError:
            continue
        scanned += 1
        jar: dict[str, str] = {}
        for c in req.get("cookies") or []:
            name = str((c or {}).get("name") or "")
            if name:
                jar[name] = str(c.get("value") or "")
        ua = ""
        for hd in req.get("headers") or []:
            n = str((hd or {}).get("name") or "").lower()
            if n == "cookie" and not jar:
                # 有些工具把 Cookie 留在 headers 里
                for part in str(hd.get("value") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k:
                        jar[k] = v
            elif n == "user-agent":
                ua = str(hd.get("value") or "")
        if jar:
            with_cookie += 1
        if jar.get("session"):
            best, best_url, best_ua = jar, url, ua      # 越靠后越新

    if not best:
        if scanned and not with_cookie:
            raise WorkBuddyError(
                f"HAR 里 host={host} 的 {scanned} 条请求**全部没有 Cookie** —— "
                f"这不是没登录，是 Chrome 把敏感信息剥掉了。\n"
                f"    Chrome 130 起 HAR 默认导出「已清理」档（Cookie / Set-Cookie / "
                f"Authorization 全不含）。修法：\n"
                f"      DevTools → 设置(F1) → 偏好设置 → 网络 → "
                f"勾选「允许生成包含敏感数据的 HAR」\n"
                f"      再点 Network 面板上的「导出」→ 选「导出 HAR（包含敏感数据）」\n"
                f"    导出后把新文件路径给我即可。")
        raise WorkBuddyError(
            f"HAR 里没有 host={host} 且带 session 的请求（扫到 {scanned} 条该域名的条目）。\n"
            f"    导出前请在 open.workbuddy.cn 上刷新一次页面，让 /api/ 请求落进记录。")

    cookie = "; ".join(f"{k}={v}" for k, v in best.items())
    return cookie, {"host": host, "url": best_url, "names": list(best),
                    "bytes": len(cookie),
                    "session_len": len(best.get("session", "")),
                    "user_agent": best_ua}


def whoami(base: str, cookie: str, party_id: str = "", enterprise_id: str = "") -> dict:
    """只读鉴权检查：能列出入驻主体即说明 Cookie 有效。

    判据是 **200 + JSON**。200 + 登录页 HTML 一律算失败 ——
    首版只看状态码，把一个未登录的会话报成了「通过」。
    """
    if not cookie.strip():
        raise WorkBuddyError("未配置 open.workbuddy 的 Cookie")
    status, body = _call(base, "/api/v2/open-platform/parties", cookie,
                         party_id=party_id, enterprise_id=enterprise_id)
    _auth_or_raise(status, body)
    if status != 200:
        raise WorkBuddyError(f"鉴权检查失败（{status}）：{_brief(body)}")
    return body


def probe(base: str, cookie: str, party_id: str = "",
          enterprise_id: str = "") -> list[tuple[str, str, str]]:
    """只读探测，报告各端点可达性与会话状态。返回 [(端点, 结果, 说明)]。"""
    checks = [
        ("GET  /parties", "/api/v2/open-platform/parties", "GET", None),
        ("GET  /capabilities/categories", f"{CAPABILITIES}/categories?capabilityType=skill",
         "GET", None),
        ("POST /capabilities/summary", f"{CAPABILITIES}/summary", "POST", {}),
        ("POST /capabilities/list", f"{CAPABILITIES}/list", "POST", {}),
    ]
    out: list[tuple[str, str, str]] = []
    for label, path, method, payload in checks:
        try:
            status, body = _call(base, path, cookie, method=method, payload=payload,
                                 party_id=party_id, enterprise_id=enterprise_id)
        except WorkBuddyError as exc:
            out.append((label, "网络失败", str(exc)))
            continue
        problem = auth_problem(status, body)
        if problem:
            out.append((label, "会话失效", problem))
            continue
        n = len(body.get("items") or body.get("categories") or body.get("parties")
                or body.get("data") or [])
        out.append((label, "OK", f"200 JSON，返回 {n} 项"))
    return out


def unwrap(body: dict) -> dict:
    """把 `{"code":0,"data":{…},"msg":"OK"}` 拆成 `data`。

    本站所有 v2 接口都是这个信封。首版没拆，于是 `body["uploadUrl"]` 永远取不到，
    报「预签名返回里没有 uploadUrl」—— 看着像服务端没给，其实是自己少剥了一层。
    """
    if isinstance(body, dict) and "code" in body and isinstance(body.get("data"), dict):
        return body["data"]
    return body if isinstance(body, dict) else {}


def code_of(body: dict) -> int | None:
    if isinstance(body, dict) and isinstance(body.get("code"), int):
        return body["code"]
    return None


# 资产状态（前端枚举 capabilityAssetStatus）
ASSET_UNDER_REVIEW = "under_review"
ASSET_DRAFT = "draft"
ASSET_PUBLISHED = "published"
ASSET_REJECTED = "rejected"
# 版本状态：同一个「审核中」在列表接口里叫 `pending`，在详情接口里叫 `under_review`，
# 两个都得认 —— 只判一个的话「已在审核」会被漏掉，然后白传一遍包并撞上
# 「版本号必须大于当前最新版本」（实测踩过）。
VERSION_IN_REVIEW = ("pending", "under_review", "auditing")
# 提交动作（前端枚举 publicationAction）
ACTION_CREATE = "create"
ACTION_REPUBLISH = "republish"
ACTION_RESUBMIT = "resubmit"


def in_review(asset: dict) -> bool:
    """这份资产是不是「正在审核 / 已提交待审」？"""
    if not asset:
        return False
    if str(asset.get("status") or "") == ASSET_UNDER_REVIEW:
        return True
    ver = asset.get("currentVersion") or asset.get("pendingVersion") or {}
    return str(ver.get("status") or "") in VERSION_IN_REVIEW


def find_asset(base: str, cookie: str, name: str, party_id: str = "",
               enterprise_id: str = "") -> dict | None:
    """按 name 在「我的能力」里找资产；没找到返回 None。

    存在的意义：`complete-upload` 不带 assetId 时，服务端会回
    `100001 技能 "x" 已存在，请携带其 assetId 发布新版本` —— 得先查出来再带上。
    """
    status, body = _call(base, f"{CAPABILITIES}/list", cookie, method="POST",
                         payload={}, party_id=party_id, enterprise_id=enterprise_id)
    if status != 200:
        return None
    items = unwrap(body).get("items") or []
    for it in items:
        if str(it.get("name") or "") == name:
            return it
    return None


def _png_size(png_path: Path) -> tuple[int, int]:
    """读 PNG IHDR 拿宽高。坏文件/非 PNG 抛 WorkBuddyError。"""
    try:
        head = Path(png_path).read_bytes()[:33]
    except OSError as exc:
        raise WorkBuddyError(f"读不了封面文件：{exc}") from exc
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise WorkBuddyError(f"封面不是 PNG：{png_path}")
    import struct
    return struct.unpack(">II", head[16:24])


def upload_cover(base: str, cookie: str, png_path: Path, *,
                 party_id: str = "", enterprise_id: str = "",
                 scene: str = SCENE_COVER) -> dict:
    """上传封面/图标 → 返回 {'object_key','preview_url'}。

    **这条与能力包上传是两条通道**（upload_and_submit 的坑 1 说 presign
    不能传包 —— 反过来，封面恰好只能走 presign）。契约 2026-10-12 从前端
    bundle 反解（chunk 48663 / tf controller + skill-icons 场景枚举）：

      1. POST /api/v2/open-platform/common/uploads/presign
         {scene:"capability_cover", fileName, fileSize, mimeType}
         → data.{uploadUrl, objectKey, previewUrl}
      2. PUT <uploadUrl>  body=图片字节  Content-Type: image/png   → 204/200

    presign 通道**没有 complete 步骤**（前端 prepareUpload → uploadToCos
    直接返回 objectKey）。objectKey 填进 submit/update 的
    fields.common.coverObjectKey 即可。

    平台硬约束（前端 CapabilityCover 规则，违反会被前端拦，这里前置同款断言）：
    512×512 正方形 PNG、≤ 512000 字节。
    """
    png_path = Path(png_path)
    if not png_path.is_file():
        raise WorkBuddyError(f"封面文件不存在：{png_path}")
    size = png_path.stat().st_size
    w, h = _png_size(png_path)
    if (w, h) != (512, 512):
        raise WorkBuddyError(f"封面必须 512×512（当前 {w}×{h}）：{png_path}")
    if size > 512_000:
        raise WorkBuddyError(f"封面超过 512 KB（当前 {size / 1024:.0f} KB）：{png_path}")

    status, body = _call(base, PRESIGN, cookie, method="POST",
                         payload={"scene": scene, "fileName": png_path.name,
                                  "fileSize": size, "mimeType": "image/png"},
                         party_id=party_id, enterprise_id=enterprise_id)
    if status != 200:
        _auth_or_raise(status, body)
        raise WorkBuddyError(f"封面 presign 失败（{status}）：{_brief(body)}")
    data = unwrap(body)
    upload_url = str(data.get("uploadUrl") or "")
    object_key = str(data.get("objectKey") or "")
    if not upload_url or not object_key:
        raise WorkBuddyError(f"封面 presign 未返回 uploadUrl/objectKey：{_brief(body)}")

    req = urllib.request.Request(
        upload_url, data=png_path.read_bytes(), method="PUT",
        headers={"Content-Type": "image/png", "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=120, context=_ctx()) as resp:
            if resp.getcode() not in (200, 201, 204):
                raise WorkBuddyError(f"封面直传 COS 失败：HTTP {resp.getcode()}")
    except urllib.error.HTTPError as exc:
        raise WorkBuddyError(f"封面直传 COS 失败：HTTP {exc.code}") from exc

    return {"object_key": object_key,
            "preview_url": str(data.get("previewUrl") or data.get("url") or "")}


def normalize_categories(cats: list | None) -> list[dict]:
    """把类目配置规范成平台要的 `[{"categoryId":…,"qualifications":[…]}]`。

    2026-10-13 实测确认的契约（从 open.workbuddy 详情接口
    `GET /capabilities/{assetId}` 取已通过技能的 `fields.common` 反解）：

      "categories":       [{"categoryId": "tools:office", "qualifications": []}]
      "marketCategories": ["development-tools", "collaboration"]

    两个字段是**两套体系**，都要填：

    | 字段 | 体系 | 取值 |
    |---|---|---|
    | `categories` | 资质类目 | `<大类>:<三级类目>`，如 `tools:office`（工具-办公） |
    | `marketCategories` | 市场展示类目 | 连字符 id，如 `development-tools` |

    资质类目枚举见 `https://open.workbuddy.cn/docs/service-categories-individual`
    （个人主体）。**「工具」大类下有**：记账 / 日历 / 天气 / 办公 /
    图片处理 / 计算器 / 报价·比价 / 信息查询 / 预约·报名 / 健康管理 / 备忘录
    —— 注意**没有「开发工具」**，所以技术类技能一般落 `tools:office`。

    ⚠️ 只填 `marketCategories` 而 `categories` 为空会被审核驳回：
    「无资质类目字段，需重新选择对应类目，如需资质请一并提交」。
    别等平台驳回，本地就拦住 —— 见下面的前置断言。
    """
    out: list[dict] = []
    for item in cats or []:
        if isinstance(item, str):
            cid = item.strip()
            if cid:
                out.append({"categoryId": cid, "qualifications": []})
        elif isinstance(item, dict):
            cid = str(item.get("categoryId") or item.get("id") or "").strip()
            if cid:
                quals = [q for q in (item.get("qualifications") or []) if q]
                out.append({"categoryId": cid, "qualifications": quals})
    return out


def upload_and_submit(base: str, cookie: str, zip_path: Path, *,
                     capability_type: str = "skill",
                     party_id: str = "", enterprise_id: str = "",
                     changelog: str = "", submit: bool = True,
                     dry_run: bool = False, name: str = "",
                     cover_object_key: str = "",
                     categories: list | None = None,
                     market_categories: list | None = None) -> dict:
    """走完 init-upload → PUT COS → complete-upload →（可选）submit。

    **真实契约**（2026-10-10 从前端 bundle 反解 + 真机实测，替换掉此前靠猜的版本）：
    ｜步骤｜请求｜返回｜
    |---|---|---|
    | 1 | `POST /capabilities/init-upload` `{capabilityType,fileName,fileSize,mimeType}` | `data.{uploadId,objectKey,uploadUrl,expireAt}` |
    | 2 | `PUT <uploadUrl>` body=zip，`Content-Type` 必须带 | 204/200 |
    | 3 | `POST /capabilities/complete-upload` `{uploadId, assetId?}` | `data.{uploadToken,…}` |
    | 4 | `POST /capabilities/submit` `{capabilityType,action,assetId,uploadToken,clientToken,fields}` | `data.{assetId,versionId,status}` |

    三个曾经踩过的坑，别再犯：
    1. **别用 `/common/uploads/presign` 传能力包** —— 那是头像/封面的通道
       （要 `scene` 字段），拿它传包只会得到 `invalid request parameters`。
       反过来，封面/图标**只能**走它 —— 见 `upload_cover()`。
    2. **响应要剥 `data`**（见 `unwrap()`）。
    3. **`displayName` / `description` 不在 submit 里** —— 平台开包解析 SKILL.md
       的 frontmatter 自己取；`fields.common` 需要 categories（**资质类目，不可为空**）
       / marketCategories（市场类目）/ coverObjectKey / releaseNote，
       格式与取值见 `normalize_categories()`。

    `dry_run=True` 只校验本地文件与鉴权，不产生任何服务端副作用。
    """
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise WorkBuddyError(f"待上传的 zip 不存在：{zip_path}")
    size = zip_path.stat().st_size
    mime = mimetypes.guess_type(zip_path.name)[0] or "application/zip"
    name = name or zip_path.stem.rsplit("-", 1)[0]

    if dry_run:
        whoami(base, cookie, party_id, enterprise_id)
        return {"ok": True, "status": "dry-run", "bytes": size,
                "message": f"鉴权通过，待上传 {zip_path.name}（{size / 1024:.1f} KB）"}

    # 0) 先查资产 —— 已存在就带上 assetId；正在审核就别再提交一遍
    asset = find_asset(base, cookie, name, party_id, enterprise_id)
    asset_id = str((asset or {}).get("assetId") or "")
    if submit and in_review(asset):
        ver = (asset or {}).get("currentVersion") or (asset or {}).get("pendingVersion") or {}
        return {"ok": True, "asset_id": asset_id,
                "status": "skipped", "bytes": size, "name": name,
                "message": f"「{name}」已在 open.workbuddy 审核中"
                           f"（assetId={asset_id}，v{ver.get('version')}，"
                           f"状态 {ver.get('status') or (asset or {}).get('status')}），"
                           f"本次不重复提交，也不重复上传"}

    # 1) 初始化上传（这一步才返回 uploadUrl）
    status, body = _call(base, f"{CAPABILITIES}/init-upload", cookie, method="POST",
                         payload={"capabilityType": capability_type,
                                  "fileName": zip_path.name, "fileSize": size,
                                  "mimeType": mime},
                         party_id=party_id, enterprise_id=enterprise_id)
    if status != 200:
        _auth_or_raise(status, body)
        raise WorkBuddyError(f"init-upload 失败（{status}）：{_brief(body)}")
    data = unwrap(body)
    upload_url = str(data.get("uploadUrl") or "")
    upload_id = str(data.get("uploadId") or "")
    object_key = str(data.get("objectKey") or "")
    if not upload_url or not upload_id:
        raise WorkBuddyError(f"init-upload 未返回 uploadUrl/uploadId：{_brief(body)}")

    # 2) 直传 COS（Content-Type 必带，否则 COS 侧签名不匹配）
    req = urllib.request.Request(
        upload_url, data=zip_path.read_bytes(), method="PUT",
        headers={"Content-Type": mime, "User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=300, context=_ctx()) as resp:
            if resp.getcode() not in (200, 201, 204):
                raise WorkBuddyError(f"直传 COS 失败：HTTP {resp.getcode()}")
    except urllib.error.HTTPError as exc:
        raise WorkBuddyError(f"直传 COS 失败：HTTP {exc.code}") from exc

    # 3) 完成上传 —— 拿到 uploadToken（submit 要用它）
    payload: dict = {"uploadId": upload_id}
    if asset_id:
        payload["assetId"] = asset_id
    status, body = _call(base, f"{CAPABILITIES}/complete-upload", cookie, method="POST",
                         payload=payload, party_id=party_id,
                         enterprise_id=enterprise_id)
    if status != 200 or code_of(body) not in (0, None):
        _auth_or_raise(status, body)
        raise WorkBuddyError(f"complete-upload 失败（{status}）：{_brief(body)}")
    cdata = unwrap(body)
    upload_token = str(cdata.get("uploadToken") or cdata.get("upload_token") or "")
    asset_id = str(cdata.get("assetId") or asset_id)

    result = {"ok": True, "asset_id": asset_id, "object_key": object_key,
              "upload_id": upload_id, "status": "uploaded", "bytes": size,
              "message": f"已上传（assetId={asset_id or '新建'}）"}
    if not submit:
        result["status"] = "uploaded"
        result["message"] += "，未提交审核"
        return result

    # 4) 提交审核 —— 只带 fields.common，名称/简介由平台解包 SKILL.md 得到
    import uuid

    # 资质类目为空 = 必然被驳回（2026-10-13 实测踩过），本地先拦，别浪费一次提交
    cats = normalize_categories(categories)
    if not cats:
        raise WorkBuddyError(
            "open.workbuddy 要求至少一个**资质类目**（fields.common.categories），"
            "为空会被审核驳回：「无资质类目字段，需重新选择对应类目」。\n"
            "    请在凭据文件里配 workbuddy.categories，例如：\n"
            '      "categories": ["tools:office"]\n'
            "    个人主体可用枚举见 "
            "https://open.workbuddy.cn/docs/service-categories-individual"
        )
    fields = {"common": {
        "categories": cats,
        "marketCategories": [str(x) for x in (market_categories or [])],
        "coverObjectKey": cover_object_key,
        "releaseNote": changelog,
    }}
    # 驳回过的资产走 resubmit（「修改后重新提交」），其余更新走 republish。
    # 三者都带 uploadToken —— action 只是告诉平台这次提交的性质。
    if not asset_id:
        action = ACTION_CREATE
    elif str((asset or {}).get("status") or "") == ASSET_REJECTED:
        action = ACTION_RESUBMIT
    else:
        action = ACTION_REPUBLISH
    status, body = _call(base, f"{CAPABILITIES}/submit", cookie, method="POST",
                         payload={
                             "capabilityType": capability_type,
                             "action": action,
                             "assetId": asset_id,
                             "uploadToken": upload_token,
                             "clientToken": f"pub_{uuid.uuid4()}",
                             "fields": fields,
                         },
                         party_id=party_id, enterprise_id=enterprise_id)
    if status != 200 or code_of(body) not in (0, None):
        _auth_or_raise(status, body)
        raise WorkBuddyError(
            f"submit 失败（{status}）：{_brief(body)}\n"
            f"    包已上传成功（assetId={asset_id or '（未创建）'}），"
            f"可在控制台手工提交；或回填 workbuddy.payloads 后重试")
    sdata = unwrap(body)
    result["status"] = "submitted"
    result["asset_id"] = str(sdata.get("assetId") or asset_id)
    result["version_id"] = sdata.get("versionId")
    result["message"] = (f"已提交审核（assetId={result['asset_id']}，"
                         f"versionId={sdata.get('versionId')}），审核期约 7 个工作日")
    result["response"] = body
    return result

