"""巡航列表 —— 记录「已提交、等审核」的资产，供 `release.py watch` 每日巡检。

## 为什么要有这个文件

发布技能的流程有一步是「等人」：提交之后要等 7 个工作日审核，过审之后
**还要再点一次发布**才真正上架。人不会天天登控制台，于是两种损失反复发生：

1. 过审了没人知道，资产停在 `approved`，市场里看不到，白等两周；
2. 等的时候反复登控制台刷新，纯浪费时间。

所以 `publish` 提交成功时把资产登记进来，`watch --check` 每天扫一遍，
发现 `approved` 就自动上线，全部落地后列表清空 —— **列表空了，巡航就该停**。

## 这里只放两件事

- **存取**：`load` / `save` / `add` / `remove` / `clear`（同目录 JSON，无外部依赖）
- **判定**：`classify()` 纯函数，把「服务端状态」翻译成「下一步动作」，
  不碰网络，可以在 selftest 里穷举所有状态组合

网络那一半在 `pub_workbuddy.review_state()` 和 `release.cmd_watch()` 里。

## 状态 → 动作

| 服务端 | 动作 | 说明 |
|---|---|---|
| under_review / reviewing / pending | `waiting` | 继续挂着，别动 |
| approved | `ship` | **过审待发布**，自动调 publish 上线 |
| published | `done` | 已在线 |
| rejected | `rejected` | 有驳回原文，需要人改，巡航到此为止 |
| draft | `withdrawn` | 被撤回（可能是自己撤的） |
| delisted / removed | `done` | 已下线/删除，没得等 |
| 未知 | `unknown` | 别自作主张，报出来让人看 |
"""

from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_WORKDIR = Path.home() / ".workbuddy" / ".release"
STORE_NAME = "watch.json"
SCHEMA = 1

# 状态 → 动作。键一律小写；服务端给的是什么大小写都有过，比对时统一 lowercase。
_ACTION = {
    "under_review": "waiting",
    "reviewing": "waiting",
    "pending": "waiting",
    "pending_review": "waiting",
    "submitted": "waiting",
    "approved": "ship",
    "published": "done",
    "online": "done",
    "delisted": "done",
    "removed": "done",
    "rejected": "rejected",
    "reject": "rejected",
    "draft": "withdrawn",
}

# 这些动作意味着「不用再等了」—— 巡航列表该把这条移走
RESOLVED = {"done", "rejected", "withdrawn"}


def store_path(workdir: Path | str | None = None) -> Path:
    base = Path(workdir).expanduser() if workdir else DEFAULT_WORKDIR
    return base / STORE_NAME


def classify(status: str, pending_status: str = "", current_status: str = "") -> str:
    """把服务端状态翻成下一步动作（纯函数）。

    多级回退的原因：`asset.status` 在「已上线 + 新版本在审」时不一定等于
    `under_review`，某些账号下它仍报 `published`；这时要优先看
    `pendingVersion.status`，否则会把一个正在审核的版本误判成已完成。
    顺序：pending 有活跃态就以 pending 为准 → 再看 asset.status。
    """
    for raw in (pending_status, status, current_status):
        s = str(raw or "").strip().lower()
        if s and s in _ACTION:
            return _ACTION[s]
    return "unknown"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load(workdir: Path | str | None = None) -> dict:
    p = store_path(workdir)
    if not p.is_file():
        return {"schema": SCHEMA, "updatedAt": "", "items": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 坏文件不该让整个发布流程挂掉：当成空列表，但保留现场供人排查
        try:
            p.rename(p.with_suffix(".broken.json"))
        except OSError:
            pass
        return {"schema": SCHEMA, "updatedAt": "", "items": []}
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return {"schema": SCHEMA, "updatedAt": "", "items": []}
    d.setdefault("schema", SCHEMA)
    d["items"] = [it for it in d["items"] if isinstance(it, dict) and it.get("assetId")]
    return d


def save(data: dict, workdir: Path | str | None = None) -> Path:
    p = store_path(workdir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["schema"] = SCHEMA
    data["updatedAt"] = _now()
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def add(entry: dict, workdir: Path | str | None = None) -> dict:
    """登记一个待审资产。同一个 assetId 重复发布时覆盖旧条目（版本会变）。"""
    aid = str(entry.get("assetId") or "").strip()
    if not aid:
        raise ValueError("watchlist.add 需要 assetId")
    data = load(workdir)
    item = dict(entry)
    item["assetId"] = aid
    item.setdefault("submittedAt", _now())
    item.setdefault("checks", 0)
    item.pop("addedAt", None)
    items = [it for it in data["items"] if it.get("assetId") != aid]
    items.insert(0, item)
    data["items"] = items
    save(data, workdir)
    return item


def remove(asset_id: str, workdir: Path | str | None = None) -> bool:
    data = load(workdir)
    before = len(data["items"])
    data["items"] = [it for it in data["items"]
                     if it.get("assetId") != str(asset_id).strip()]
    if len(data["items"]) != before:
        save(data, workdir)
        return True
    return False


def clear(workdir: Path | str | None = None) -> int:
    data = load(workdir)
    n = len(data["items"])
    data["items"] = []
    save(data, workdir)
    return n


def touch(asset_id: str, *, status: str = "", note: str = "",
          workdir: Path | str | None = None) -> None:
    """记录一次巡检结果（不改列表成员，只更新状态与计数）。"""
    data = load(workdir)
    for it in data["items"]:
        if it.get("assetId") == str(asset_id).strip():
            it["lastCheckedAt"] = _now()
            it["checks"] = int(it.get("checks") or 0) + 1
            if status:
                it["lastStatus"] = status
            if note:
                it["lastNote"] = note
            break
    save(data, workdir)


def summary(items: list[dict]) -> dict:
    """给巡航一个机器可读的结论 —— 自动化任务靠 `done` 决定要不要停自己。"""
    return {
        "total": len(items),
        "done": not items,          # 列表空 = 没有要等的了 = 巡航可以停
        "waiting": sum(1 for it in items if it.get("action") == "waiting"),
        "needs_human": sum(1 for it in items if it.get("action") == "rejected"),
        "ids": [it.get("assetId") for it in items],
    }
