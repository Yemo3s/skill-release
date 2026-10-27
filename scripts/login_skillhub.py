#!/usr/bin/env python3
"""登录 skillhub.cn，把会话 Cookie 写进发布凭据。

与 login_workbuddy.py 同一套思路（复用那边的工具函数）：
复用系统 Chrome + 持久化 profile + 在页面里发真实 API 请求验活 ——
不看 cookie 名字（过期 profile 里 session 名照样存在）、不靠状态码猜。

区别于 open.workbuddy：SkillHub 登录方式不固定（账号密码 / 验证码 / 扫码都行），
所以打开站点首页让用户自己完成登录，脚本轮询探活，成功即自动收尾。
SkillHub 不绑 UA，无需抓 UA。

产出:
    ~/.workbuddy/.secrets/skill-release.json 的 skillhub.cookie
    ~/.workbuddy/.secrets/sh-browser-profile/  浏览器 profile（复用登录态）

用法（必须用装了 playwright 的那个 Python）:
    python login_skillhub.py                # 弹浏览器，登录后自动收尾
    python login_skillhub.py --timeout 600  # 自定义等待秒数
    python login_skillhub.py --check-only   # 只验活，不打开浏览器
    python login_skillhub.py --fresh        # 清空旧 profile 重新登
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

BASE = "https://www.skillhub.cn"
API = "https://api.skillhub.cn"
SECRETS = Path.home() / ".workbuddy" / ".secrets"
PROFILE = SECRETS / "sh-browser-profile"

sys_path = str(Path(__file__).resolve().parent)
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from login_workbuddy import (  # noqa: E402
    log, load_cfg, save_cfg, mask, pick_browser, pick_profile,
)

PROBE_JS = """async () => {
  try {
    const r = await fetch('%API%/api/v1/dashboard/skills?pageSize=1&pageNum=1', {
      method: 'GET', credentials: 'include',
      headers: { 'Accept': 'application/json' }
    });
    const t = await r.text();
    return { status: r.status, url: r.url, body: t.slice(0, 300) };
  } catch (e) { return { status: 0, url: '', body: String(e) }; }
}""".replace("%API%", API)


def cookie_header(cookies: list[dict]) -> str:
    """只保留 skillhub 域的 cookie，按 name=value; 拼接（同名保留最后一个）。"""
    seen: dict[str, str] = {}
    for c in cookies:
        if "skillhub" not in str(c.get("domain", "")):
            continue
        name = str(c.get("name", ""))
        if name:
            seen[name] = str(c.get("value", ""))
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def verify(cookie: str, ua: str = "") -> tuple[bool, str]:
    """用真实 API 验活（不跟随重定向）。返回 (是否有效, 说明)。"""
    import ssl
    import urllib.error
    import urllib.request

    if not cookie:
        return False, "凭据为空"
    ctx = ssl.create_default_context()
    headers = {
        "Cookie": cookie,
        "User-Agent": ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json, text/plain, */*",
        "Referer": BASE + "/",
    }
    req = urllib.request.Request(API + "/api/v1/dashboard/skills?pageSize=1&pageNum=1",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, (e.read() if hasattr(e, "read") else b"")
    except Exception as exc:
        return False, f"网络失败：{exc}"

    if status != 200:
        return False, f"HTTP {status}（凭据无效或过期）"
    try:
        json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return False, "返回非 JSON（可能被重定向到登录页）"
    return True, "有效"


def probe_in_page(page) -> tuple[bool, str]:
    """在页面里发真实请求判断登录态 —— fetch 自带真实 cookie / TLS 指纹。"""
    try:
        res = page.evaluate(PROBE_JS)
    except Exception as exc:
        return False, f"页面不可用：{exc}"
    st = res.get("status") or 0
    if st == 200:
        return True, "已登录"
    return False, f"HTTP {st}"


def run_login(timeout: int, fresh: bool = False) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("缺少 playwright。请用装了它的 Python 运行：")
        log(r"  C:\Users\User\.workbuddy\binaries\python\envs\default\Scripts\python.exe "
            + Path(__file__).name)
        return 2

    kind, exe = pick_browser()
    if not exe:
        log("未找到 Chrome / Edge，无法自动登录。")
        return 2
    log(f"浏览器：{kind}  ({exe})")
    profile = pick_profile(fresh)
    # pick_profile 返回的是 workbuddy 的目录；换成本技能自己的
    if fresh and profile.exists():
        import shutil
        moved = profile.with_name(profile.name + "-old")
        if moved.exists():
            shutil.rmtree(moved, ignore_errors=True)
        profile.rename(moved)
        log(f"旧 profile 已移走 -> {moved.name}")
    profile = PROFILE
    profile.mkdir(parents=True, exist_ok=True)
    log(f"profile：{profile}")
    log("")

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
            log("启动浏览器失败：" + str(exc).splitlines()[0][:200])
            return 2
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)

        log("=" * 62)
        log(f"登录入口：{BASE}")
        log("请在弹出的窗口里完成登录（账号密码 / 验证码 / 扫码均可）。")
        log("登录成功后不用关窗口，脚本会自动收尾。")
        log(f"最多等待 {timeout} 秒。")
        log("=" * 62)

        ua = ""
        try:
            ua = page.evaluate("() => navigator.userAgent")
        except Exception:
            pass

        deadline = time.time() + timeout
        last_note = ""
        while time.time() < deadline:
            ok, note = probe_in_page(page)
            if ok != (last_note == "已登录"):
                log(f"  [{time.strftime('%H:%M:%S')}] 探活：{'✓ 已登录' if ok else note}")
            last_note = "已登录" if ok else note
            if ok:
                cookies = ctx.cookies()
                cookie_str = cookie_header(cookies)
                if not cookie_str:
                    log("登录态探活通过但没抓到 skillhub cookie，继续等……")
                    time.sleep(4)
                    continue
                cfg = load_cfg()
                cfg.setdefault("skillhub", {})
                cfg["skillhub"]["cookie"] = cookie_str
                save_cfg(cfg)
                log("")
                log("✓ Cookie 已写入凭据文件：" + mask(cookie_str))
                ok2, why = verify(cookie_str, ua)
                log(f"  独立验活：{'通过' if ok2 else '失败 —— ' + why}")
                try:
                    ctx.close()
                except Exception:
                    pass
                return 0 if ok2 else 1
            time.sleep(4)

        log(f"超时（{timeout}s）仍未检测到登录态，退出。可加长 --timeout 重试。")
        try:
            ctx.close()
        except Exception:
            pass
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="登录 skillhub.cn 并保存会话 Cookie")
    ap.add_argument("--timeout", type=int, default=600, help="等待登录的秒数，默认 600")
    ap.add_argument("--check-only", action="store_true", help="只验活，不打开浏览器")
    ap.add_argument("--fresh", action="store_true", help="清空旧浏览器 profile 再登录")
    a = ap.parse_args()

    if a.check_only:
        cfg = load_cfg()
        cookie = str((cfg.get("skillhub") or {}).get("cookie") or "")
        ok, why = verify(cookie)
        log(("✓ " if ok else "✗ ") + mask(cookie) + " —— " + why)
        return 0 if ok else 1

    return run_login(a.timeout, a.fresh)


if __name__ == "__main__":
    raise SystemExit(main())
