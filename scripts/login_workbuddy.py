#!/usr/bin/env python3
"""扫码登录 open.workbuddy.cn，自动把会话写进发布凭据。

用法（必须用装了 playwright 的那个 Python）:
    python login_workbuddy.py                # 打开浏览器，扫码，自动收尾
    python login_workbuddy.py --timeout 600  # 自定义等待秒数
    python login_workbuddy.py --check-only   # 只验活，不打开浏览器

设计要点（都是踩过的坑）:
1. **复用系统 Chrome**（channel="chrome"），不下载 playwright 自带 chromium
2. **持久化 profile**：登录一次，下次 cookie 过期时重新扫码即可，不用重填资料
3. **同时抓 navigator.userAgent 写进配置** —— 平台会话与 UA 绑定，
   cookie 对了 UA 不对照样 302。手抄 HAR 最容易漏的就是这一条。
4. **验活用真实 API**，不看状态码（未登录会被 302 到 Keycloak，跟随重定向
   会拿到「200 + 登录页 HTML」，光看 200 会误判成功）

产出:
    ~/.workbuddy/.secrets/skill-release.json 的 workbuddy.cookie / user_agent
    ~/.workbuddy/.secrets/wb-browser-profile/ 浏览器 profile（复用登录态）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE = "https://open.workbuddy.cn"
# 直接进登录页。落到首页时若已有半失效会话，页面不会渲染二维码，用户会以为「没反应」。
LOGIN_URL = BASE + "/login/?platform=open&state=0"
SECRETS = Path.home() / ".workbuddy" / ".secrets"
CFG = SECRETS / "skill-release.json"
PROFILE = SECRETS / "wb-browser-profile"

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def pick_browser() -> tuple[str, str]:
    for p in CHROME_CANDIDATES:
        if Path(p).is_file():
            return "chrome", p
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return "msedge", p
    return "", ""


# ------------------------------------------------------------------ 凭据读写

def load_cfg() -> dict:
    if CFG.is_file():
        try:
            return json.loads(CFG.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_cfg(cfg: dict) -> None:
    SECRETS.mkdir(parents=True, exist_ok=True)
    tmp = CFG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(CFG)


def mask(v: str, keep: int = 4) -> str:
    if not v:
        return "(空)"
    return v[:keep] + "…" + v[-keep:] + f"（{len(v)} 字符）"


# ------------------------------------------------------------------ 验活

def verify(cookie: str, ua: str) -> tuple[bool, str]:
    """用真实 API 验活。返回 (是否有效, 说明)。不跟随重定向。"""
    import ssl
    import urllib.error
    import urllib.request

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx), NoRedirect())

    headers = {
        "Cookie": cookie,
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Referer": BASE + "/",
        "Origin": BASE,
    }
    req = urllib.request.Request(BASE + "/api/v2/open-platform/capabilities/list",
                                data=b"{}", headers=headers, method="POST")
    try:
        with opener.open(req, timeout=25) as r:
            status = r.status
            raw = r.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read() if hasattr(e, "read") else b""
    except Exception as exc:                      # 网络层
        return False, f"网络失败：{exc}"

    if status in (301, 302, 303, 307, 308):
        return False, f"被重定向（HTTP {status}）→ 未登录或会话无效"
    if status != 200:
        return False, f"HTTP {status}"
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        head = raw[:120].decode("utf-8", "replace").replace("\n", " ")
        return False, f"返回非 JSON（疑似登录页）：{head}"

    code = body.get("code")
    if code not in (0, 200, None):
        return False, f"业务错误 code={code} msg={body.get('message') or body.get('msg')}"
    return True, "会话有效"


# ------------------------------------------------------------------ 主流程

def cookie_header(cookies: list[dict]) -> str:
    """只保留 open.workbuddy.cn 的 cookie，按 name=value; 拼接。"""
    parts: list[tuple[str, str]] = []
    for c in cookies:
        dom = str(c.get("domain", ""))
        if "workbuddy" not in dom:
            continue
        name = str(c.get("name", ""))
        if not name:
            continue
        parts.append((name, str(c.get("value", ""))))
    # 同名去重，保留最后一个
    seen: dict[str, str] = {}
    for k, v in parts:
        seen[k] = v
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def has_session(cookies: list[dict]) -> bool:
    for c in cookies:
        if "workbuddy" in str(c.get("domain", "")) and str(c.get("name", "")) in (
                "session", "SESSION", "skh_token"):
            if str(c.get("value", "")):
                return True
    return False


def probe_in_page(page) -> tuple[bool, str]:
    """在浏览器里自己发一次接口请求，判断是否真的登录了。

    为什么不看 cookie 名字：`session` 这类名字在过期 profile 里也照样存在，
    靠名字判断会「一进去就以为登了」，然后一直验活失败到超时。
    在页面里 fetch 会自带真实 cookie / UA / TLS 指纹，结果才是可信的。
    """
    js = """async () => {
      try {
        const r = await fetch('/api/v2/open-platform/parties', {
          method: 'GET', credentials: 'include',
          headers: { 'Accept': 'application/json' }
        });
        const t = await r.text();
        return { status: r.status, url: r.url, body: t.slice(0, 300) };
      } catch (e) { return { status: 0, url: '', body: String(e) }; }
    }"""
    try:
        res = page.evaluate(js)
    except Exception as exc:
        return False, f"页面已关闭或不可用：{exc}"
    st = res.get("status") or 0
    body = res.get("body") or ""
    if st != 200:
        return False, f"HTTP {st}"
    if "keycloak" in (res.get("url") or "") or "<html" in body[:200].lower():
        return False, "仍被送到登录页"
    try:
        data = json.loads(body if body.strip().endswith("}") or body.strip().endswith("]") else "{}")
    except json.JSONDecodeError:
        return False, "返回非 JSON"
    code = data.get("code")
    if code not in (0, 200, None):
        return False, f"code={code} {data.get('message') or data.get('msg') or ''}"
    return True, "已登录"


def pick_profile(fresh: bool) -> Path:
    """选一个能用的 profile 目录。

    Chrome 会锁住 `SingletonLock`，同一 profile 被另一个窗口占着时再启动，
    新进程只会把 URL 转给已有窗口然后自己退出（Playwright 报 TargetClosedError），
    连 cookie 都拿不到。所以：被占用就换个独立目录，别硬抢。
    """
    if not fresh and not (PROFILE / "SingletonLock").exists():
        return PROFILE
    stamp = time.strftime("%Y%m%d-%H%M%S")
    alt = PROFILE.with_name(f"{PROFILE.name}-{stamp}")
    log(f"主 profile 被占用或要求全新登录，改用独立目录：{alt}")
    return alt


def run_login(timeout: int, fresh: bool = False) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("缺少 playwright。请用装了它的 Python 运行：")
        log(r'  C:\Users\User\.workbuddy\binaries\python\envs\default\Scripts\python.exe '
            + Path(__file__).name)
        return 2

    kind, exe = pick_browser()
    if not exe:
        log("未找到 Chrome / Edge，无法自动登录。")
        return 2
    log(f"浏览器：{kind}  ({exe})")
    profile = pick_profile(fresh)
    log(f"profile：{profile}")
    log("")

    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                executable_path=exe,
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                viewport={"width": 1280, "height": 860},
            )
        except Exception as exc:
            msg = str(exc)
            log("启动浏览器失败：" + msg.splitlines()[0][:200])
            if "TargetClosed" in msg or "closed" in msg:
                log("通常是这个 profile 已被另一个 Chrome 窗口占用。加 --fresh 换个目录重试。")
            return 2
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

        log("=" * 62)
        log(f"登录页：{LOGIN_URL}")
        log("请在刚弹出的窗口里用微信扫码，扫完不用关窗口，脚本会自动收尾。")
        log(f"最多等待 {timeout} 秒。")
        log("=" * 62)

        ua = ""
        try:
            ua = page.evaluate("() => navigator.userAgent")
        except Exception:
            pass

        deadline = time.time() + timeout
        cookie_str = ""
        last_note = ""
        ok_now, last_note = probe_in_page(page)

        while time.time() < deadline:
            if ok_now:
                try:
                    cookies = ctx.cookies()
                except Exception:
                    cookies = []
                cookie_str = cookie_header(cookies)
                if cookie_str:
                    break
            if page.is_closed():
                log("")
                log("窗口被关闭，已中止。")
                return 1
            time.sleep(2)
            ok_now, last_note = probe_in_page(page)
        else:
            log("")
            log(f"未能在超时前确认登录。最后一次探测：{last_note}")
            log("若窗口里已显示登录成功，跑 release.py login 重试一次（profile 会复用，通常直接过）。")
            ctx.close()
            return 1

        if ua:
            try:
                ua = page.evaluate("() => navigator.userAgent") or ua
            except Exception:
                pass

        if not cookie_str:
            log("")
            log("未能在超时前拿到有效会话。" + (f"最后一次验活：{last_note}" if last_note else ""))
            log("排查顺序：① 确认窗口里已登录成功 ② 确认登录域是 open.workbuddy.cn")
            ctx.close()
            return 1

        # 抓一下 party_id（有就写进配置，上传时要用）
        party_id = ""
        try:
            r = ctx.request.get(BASE + "/api/v2/open-platform/parties",
                                headers={"Cookie": cookie_str, "Accept": "application/json"})
            if r.ok:
                body = r.json()
                for p in (body.get("data") or body.get("parties") or body.get("items") or []):
                    pid = p.get("partyId") or p.get("id") or p.get("party_id")
                    if pid:
                        party_id = str(pid)
                        break
        except Exception:
            pass

        # 顺手截一张已登录的首页，便于确认到底进了哪个账号
        try:
            page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30000)
            shot = SECRETS / "wb-logged-in.png"
            page.screenshot(path=str(shot), full_page=False)
            log(f"已保存登录后截图：{shot}")
        except Exception:
            pass

        try:
            ctx.storage_state(path=str(SECRETS / "wb-storage-state.json"))
        except Exception:
            pass

        ctx.close()

    cfg = load_cfg()
    wb = cfg.setdefault("workbuddy", {})
    wb["cookie"] = cookie_str
    if ua:
        wb["user_agent"] = ua
    if party_id:
        wb["party_id"] = party_id
    save_cfg(cfg)

    log("")
    log("✅ 已写入凭据：" + str(CFG))
    log(f"   workbuddy.cookie     ：{mask(cookie_str)}")
    log(f"   workbuddy.user_agent ：{ua[:70]}{'…' if len(ua) > 70 else ''}")
    if party_id:
        log(f"   workbuddy.party_id   ：{party_id}")
    log("")
    log("下一步：release.py probe  →  release.py publish <技能目录> --targets workbuddy --yes")
    return 0


def run_refresh() -> int:
    """从持久化 profile 里把**当前有效**的会话倒回配置，不开窗口、不用扫码。

    为什么需要：Chrome 会在后台静默刷新 SSO 会话，profile 里的 cookie 一直是活的，
    但配置里那份字符串是某次扫码时抄下来的快照 —— 过几小时就 403，
    而 profile 本身还能正常访问控制台。此时不该逼用户重新扫码，
    只要把 profile 里的现成 cookie 重新导出即可。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("缺少 playwright。")
        return 2
    kind, exe = pick_browser()
    if not exe:
        log("未找到 Chrome / Edge。")
        return 2
    if not PROFILE.exists():
        log(f"没有可复用的 profile（{PROFILE}），请先跑一次完整登录（不带 --refresh）。")
        return 1

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE), executable_path=exe, headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
                viewport={"width": 1280, "height": 800})
        except Exception as exc:
            log("启动 profile 失败：" + str(exc).splitlines()[0][:200])
            return 2
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/skill/all", wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        ok, note = probe_in_page(page)
        if not ok:
            log(f"profile 里的会话也失效了（{note}），需要重新扫码：release.py login")
            ctx.close()
            return 1
        ua = ""
        try:
            ua = page.evaluate("() => navigator.userAgent") or ""
        except Exception:
            pass
        cookies = ctx.cookies()
        cookie_str = cookie_header(cookies)
        party_id = ""
        try:
            r = page.evaluate("""async () => {
              const r = await fetch('/api/v2/open-platform/parties', {credentials:'include'});
              return await r.text();
            }""")
            body = json.loads(r)
            for p in ((body.get("data") or {}).get("parties") or []):
                pid = p.get("partyId") or p.get("id")
                if pid:
                    party_id = str(pid)
                    break
        except Exception:
            pass
        ctx.close()

    if not cookie_str:
        log("profile 有效但没抓到 cookie，异常状态。")
        return 1
    cfg = load_cfg()
    wb = cfg.setdefault("workbuddy", {})
    wb["cookie"] = cookie_str
    if ua:
        wb["user_agent"] = ua
    if party_id:
        wb["party_id"] = party_id
    save_cfg(cfg)
    log("✅ 已从浏览器 profile 刷新凭据（无需扫码）")
    log(f"   workbuddy.cookie     ：{mask(cookie_str)}")
    if party_id:
        log(f"   workbuddy.party_id   ：{party_id}")
    return 0


def run_check() -> int:
    cfg = load_cfg()
    wb = cfg.get("workbuddy", {})
    cookie = str(wb.get("cookie") or "")
    ua = str(wb.get("user_agent") or "")
    if not cookie:
        log("配置里没有 workbuddy.cookie，先跑一次不带 --check-only 的登录。")
        return 1
    ok, note = verify(cookie, ua)
    log(("✅ " if ok else "❌ ") + "会话" + ("有效" if ok else "无效") + f"：{note}")
    log(f"   cookie：{mask(cookie)}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扫码登录 open.workbuddy.cn 并写入发布凭据")
    ap.add_argument("--timeout", type=int, default=300, help="等待登录的秒数（默认 300）")
    ap.add_argument("--check-only", action="store_true", help="只验活，不开浏览器")
    ap.add_argument("--fresh", action="store_true", help="清空旧 profile 再登录（旧会话残留时用）")
    ap.add_argument("--refresh", action="store_true",
                    help="不开窗口：从浏览器 profile 里把当前有效会话重新导出到配置")
    args = ap.parse_args(argv)
    if args.check_only:
        return run_check()
    if args.refresh:
        return run_refresh()
    return run_login(args.timeout, fresh=args.fresh)


if __name__ == "__main__":
    raise SystemExit(main())
