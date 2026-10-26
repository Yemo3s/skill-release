#!/usr/bin/env python3
"""open.workbuddy 资产运营指标：一句话看「我的东西到底有多少人在用」。

被 `release.py metrics` 调用，也可单独跑。

接口（只读，2026-10-26 实测）：
    POST /api/v2/open-platform/asset-metrics/daily
    {assetIds:[...], metricTypes:[...], startTime:"YYYY-MM-DD", endTime:"YYYY-MM-DD"}

三个靠报错才问出来的契约，别再猜：
1. 字段是 `startTime` / `endTime`，不是 startDate（写成 startDate 会得到
   「时间格式须为 YYYY-MM-DD，实际 startTime=**空**」—— 报错骂的是空值，
   但根因是键名不对，极易误判成日期格式写错）
2. 指标枚举：installs / calls / activeUsers / frequency / weekRetention /
   monthRetention（服务端会在 100001 里把可选值列出来）
3. 返回按 `assetData[].daily[] + overview` 组织，每个指标是
   `{value, status}`，`status=pending` 表示口径未就绪（不是 0，别当 0 显示）

注意口径陷阱：`installs` 在某天之前恒为 0、之后突然出现三位数，
通常不是「流量爆发」而是**埋点/口径上线或回填**。判断方法看同日 activeUsers：
真新增 200+ 装机，当日活跃不可能只有 50。脚本会标注这种断点。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import creds  # noqa: E402
import pub_workbuddy as wb  # noqa: E402

METRICS = ["installs", "calls", "activeUsers", "frequency",
           "weekRetention", "monthRetention"]
PAY_MODE = {0: "免费", None: "?", 1: "买断", 2: "订阅"}


def fetch_assets(base: str, cookie: str, party_id: str) -> list[dict]:
    st, body = wb._call(base, f"{wb.CAPABILITIES}/list", cookie, method="POST",
                        payload={}, party_id=party_id)
    if st != 200:
        raise wb.WorkBuddyError(f"资产列表获取失败（{st}）：{wb._brief(body)}")
    return wb.unwrap(body).get("items") or []


def fetch_metrics(base: str, cookie: str, party_id: str, ids: list[str],
                  days: int) -> dict:
    today = date.today()
    payload = {
        "assetIds": ids,
        "metricTypes": METRICS,
        "startTime": (today - timedelta(days=days)).isoformat(),
        "endTime": today.isoformat(),
    }
    st, body = wb._call(base, "/api/v2/open-platform/asset-metrics/daily", cookie,
                        method="POST", payload=payload, party_id=party_id)
    if not (isinstance(body, dict) and body.get("code") == 0):
        raise wb.WorkBuddyError("指标获取失败：" + json.dumps(body, ensure_ascii=False)[:200])
    return wb.unwrap(body)


def _val(metrics: dict, key: str):
    m = metrics.get(key) or {}
    return m.get("value")


def summarize(items: list[dict], data: dict) -> list[dict]:
    pay = {}
    for it in items:
        ver = it.get("currentVersion") or it.get("pendingVersion") or {}
        pay[it["assetId"]] = (ver.get("payMode"), it.get("status"))
    rows = []
    for blk in data.get("assetData") or []:
        aid = blk.get("assetId")
        ov = blk.get("overview") or {}
        rows.append({
            "assetId": aid,
            "name": blk.get("assetName") or "",
            "type": blk.get("assetType") or "",
            "payMode": pay.get(aid, (None, None))[0],
            "status": pay.get(aid, (None, None))[1],
            "installs": _val(ov, "installs"),
            "activeUsers": _val(ov, "activeUsers"),
            "calls": _val(ov, "calls"),
            "frequency": _val(ov, "frequency"),
            "weekRetention": _val(ov, "weekRetention"),
            "monthRetention": _val(ov, "monthRetention"),
            "daily": blk.get("daily") or [],
        })
    rows.sort(key=lambda r: (-(r["installs"] or 0), r["name"]))
    return rows


def detect_rebase(daily: list[dict]) -> str:
    """找出 installs 从恒 0 突然变三位数的断开点，返回提示文案（没有则空串）。"""
    prev_zero = True
    for d in daily:
        inst = (_val(d.get("metrics") or {}, "installs") or 0)
        if prev_zero and inst >= 50:
            act = _val(d.get("metrics") or {}, "activeUsers") or 0
            note = f"installs 在 {d['date']} 由 0 跳到 {inst}"
            if act and act < inst * 0.4:
                note += f"，同日活跃仅 {act} → 疑为口径上线/历史回填，不是新增爆发"
            return note
        if inst:
            prev_zero = False
    return ""


def _w(s: str) -> int:
    """按终端显示宽度算：CJK 全角字符占两格。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _w(s))
    if align == "right":
        return " " * gap + s
    return s + " " * gap


def _cut(s: str, width: int) -> str:
    """按显示宽度截断，超出加省略号。"""
    if _w(s) <= width:
        return s
    out, used = "", 0
    for ch in s:
        w = 2 if __import__("unicodedata").east_asian_width(ch) in "WF" else 1
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


def print_table(rows: list[dict], days: int) -> None:
    today = date.today()
    print(f"口径：最近 {days} 天（{(today - timedelta(days=days)).isoformat()} ~ {today.isoformat()}）")
    print()

    cols = [("资产", 24), ("类型", 7), ("付费", 6), ("安装", 6), ("活跃", 6),
            ("调用", 6), ("频次", 6), ("周留存", 8), ("月留存", 8)]
    print("".join(_pad(h, w) for h, w in cols))
    print("-" * sum(w for _, w in cols))

    def fmt(x, pct=False):
        if x is None:
            return "-"
        return f"{x * 100:.1f}%" if pct else f"{x:.0f}"

    for r in rows:
        freq = "-" if r["frequency"] is None else f"{r['frequency']:.2f}"
        print(_pad(_cut(r["name"] or "", 24), 24)
              + _pad(r["type"][:6], 7)
              + _pad(PAY_MODE.get(r["payMode"], "?"), 6)
              + _pad(fmt(r["installs"]), 6, "right")
              + _pad(fmt(r["activeUsers"]), 6, "right")
              + _pad(fmt(r["calls"]), 6, "right")
              + _pad(freq, 6, "right")
              + _pad(fmt(r["weekRetention"], True), 8, "right")
              + _pad(fmt(r["monthRetention"], True), 8, "right"))
    print("-" * sum(w for _, w in cols))
    print(_pad("合计", 24) + _pad("", 7) + _pad("", 6)
          + _pad(str(sum(r["installs"] or 0 for r in rows)), 6, "right")
          + _pad(str(sum(r["activeUsers"] or 0 for r in rows)), 6, "right")
          + _pad(str(sum(r["calls"] or 0 for r in rows)), 6, "right"))
    pays = {r["payMode"] for r in rows}
    if pays <= {0}:
        print("\n⚠️  全部资产 payMode=0（免费）。平台明确：已上架的免费资产不支持转付费，")
        print("    要收费只能新发一个付费资产，从 0 装机开始。")
    notes = [(r["name"], detect_rebase(r["daily"])) for r in rows]
    notes = [(n, t) for n, t in notes if t]
    if notes:
        print("\n口径提示：")
        for n, t in notes:
            print(f"  · {n}：{t}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="open.workbuddy 资产真实运营指标")
    ap.add_argument("--days", type=int, default=90, help="回溯天数，默认 90")
    ap.add_argument("--json", metavar="FILE", help="另存 JSON")
    ap.add_argument("--only", metavar="NAME", help="只看某个资产（按名称模糊匹配）")
    args = ap.parse_args(argv)

    cfg = creds.load().get("workbuddy", {})
    cookie = str(cfg.get("cookie") or "")
    if not cookie:
        print("没有 workbuddy.cookie，先跑：release.py login", file=sys.stderr)
        return 2
    if cfg.get("user_agent"):
        wb.set_user_agent(str(cfg["user_agent"]))
    base = str(cfg.get("base") or wb.DEFAULT_BASE)
    party_id = str(cfg.get("party_id") or "")

    try:
        items = fetch_assets(base, cookie, party_id)
    except Exception as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    if not items:
        print("账号下没有任何资产。")
        return 0
    if args.only:
        items = [it for it in items
                 if args.only in str(it.get("name", "")) or args.only in str(it.get("displayName", ""))]
        if not items:
            print(f"没有名称包含「{args.only}」的资产", file=sys.stderr)
            return 1

    data = fetch_metrics(base, cookie, party_id, [it["assetId"] for it in items], args.days)
    rows = summarize(items, data)
    print_table(rows, args.days)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        slim = [{k: v for k, v in r.items() if k != "daily"} for r in rows]
        out.write_text(json.dumps({"query": data.get("query"), "rows": slim},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已存：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
