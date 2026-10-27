# 审核巡航 —— 发布之后那段没人盯的时间

发布技能最容易被忽略的不是「怎么发」，是**发完之后等的那 7 个工作日**。
这段时间里会发生三件事，每一件都需要有人看一眼：

1. 审核通过了 —— 但**不会自动上架**
2. 审核被驳回 —— 有一句具体的驳回原因，改完要重发
3. 什么也没发生 —— 继续等

人不会天天登控制台刷新，所以真正亏的往往不是被驳回，而是**过了审却一直没上线**。
巡航就是把这三种情况自动化盯住。

---

## 一、先理解状态机

```
draft ──submit──> under_review ──approve──> approved ──publish──> published
  ↑                    │                       │                     │
  └──── withdraw ──────┘                       │                     │
                       └── reject ──> rejected │                     └─ 市场可见
                                               └─ 过审但没人点发布 = 幽灵状态
```

**`approved` 是最贵的一个状态**：审核通过了、资质没问题、包也没问题，
但用户搜不到 —— 因为还差一次 `publish` 调用。

`review_state()` 读回来的字段里，要看两个地方：

| 字段 | 含义 | 为什么要看 |
|---|---|---|
| `status` | 资产整体状态 | 决定「现在该干什么」 |
| `pendingVersion.status` | 待审版本的状态 | **已上线资产再发新版时，整体 status 可能仍是 `published`** |
| `currentVersion.version` | 线上那一版是哪一版 | 判断「新版本到底上没上」 |

只看 `status` 会把「线上还是旧版、新版在审」误判成「已经完成」，
于是巡航提前收工 —— 新版本永远不上线。这就是 `classify()` 里
**pending 优先于 status** 的原因，测试里专门钉了这条。

---

## 二、巡航列表存在哪

`~/.workbuddy/.release/watch.json`（可用 `--workdir` 改）

```json
{
  "schema": 1,
  "updatedAt": "2026-10-27T13:44:43",
  "items": [
    {
      "assetId": "os_e1a727dfe880fe87",
      "name": "launch-guard",
      "version": "1.0.0",
      "versionId": 84690,
      "payMode": 1,
      "price": 29,
      "dir": "D:\\...\\skill-launch-guard",
      "submittedAt": "2026-10-27T13:44:35",
      "lastStatus": "under_review",
      "checks": 1,
      "lastCheckedAt": "2026-10-27T13:44:43"
    }
  ]
}
```

`publish --yes` 提交成功就自动写入（结果里的 `watched: true`）。
**不需要手工维护**；要手工加一条只在迁移历史资产时才用：

```python
import watchlist
watchlist.add({"assetId": "os_xxx", "name": "xxx", "version": "1.0.0",
               "payMode": 0, "price": 0})
```

存档坏掉（手工编辑出错、磁盘写一半）不会让发布流程挂掉：
`load()` 会把坏文件改名成 `watch.broken.json` 当成空列表继续跑。

---

## 三、命令

```bash
release.py watch                        # 列表：谁在等、等多久、查过几次
release.py watch --check                # 巡检一次（只读，不上线）
release.py watch --check --ship         # 巡检 + 过审的自动上线   ← 巡航用这个
release.py watch --check --json         # 结构化输出，含 done
release.py watch --asset os_xxx         # 只看/只查某一个
release.py watch --stop                 # 停止巡航（清空全部登记）
release.py watch --stop --asset os_xxx  # 只摘掉一个
```

退出码：`0` 正常（含「列表已空」）；`1` 有资产读不到或上线失败；`2` 没配 Cookie。

`--json` 的关键字段：

```json
{
  "results": [{"assetId": "...", "action": "shipped|waiting|rejected|done|...",
               "remote_status": "approved", "remote_version": "1.0.0", "note": "..."}],
  "remaining": [...],                // 还剩几个在等
  "summary": {"total": 0, "done": true, "waiting": 0, "needs_human": 0},
  "done": true                       // ← 自动化看这个字段决定停不停自己
}
```

---

## 四、挂成定时任务（这才是完整形态）

发布完之后让代理做两件事：**建（或唤醒）一个每日任务**，prompt 大致如下：

```
每天巡检 open.workbuddy 的待审资产：

C:/Users/User/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
  "<skill-release>/scripts/release.py" watch --check --ship --json

- done=true  → 报告「全部落地，巡航结束」，然后把本任务暂停（status=PAUSED）
- waiting    → 只回一行：仍在审核（名字/版本/查过几次）
- rejected   → 贴驳回原文，对照 references/rejection-codes.md 给改法，询问是否修
- 输出含 403/会话失效 → 先跑 release.py login --refresh 再重试一次

硬约束：不撤回、不改平台上任何字段、不发布清单之外的资产。
```

要点：

- **任务要能停自己**。`done: true` 时调 `automation_update` 把本任务置为
  `PAUSED`（不是删除 —— 下次发布还要唤醒它）。
- **下次发布时唤醒**。`publish` 结果有 `watched: true` 就把任务置回 `ACTIVE`。
  这种「按需启停」比常驻跑更干净：没在等审核的时候不该有定时任务在跑。
- 一个任务管**所有**资产，不要一个资产一个任务。列表天然支持多条目。

---

## 五、失败模式

| 现象 | 原因 | 处理 |
|---|---|---|
| 整批都报 403 / `会话失效` | 配置里的 cookie 是扫码时的快照，几小时就过期 | `release.py login --refresh`（不开窗口，从 profile 导回） |
| `--refresh` 也 403 | profile 里的会话也真过期了 | `release.py login` 让用户扫一次码 |
| 一直 `under_review` 超过 10 个工作日 | 审核侧卡住，或提交的字段有硬伤 | 去控制台看审核记录；必要时 `withdraw` 后重发 |
| `rejected` | 被驳回 | 读 `pendingVersion.rejectReason`，改完重新 `publish` |
| `unknown` 状态 | 平台加了新状态值 | **不要猜**。原样报出来，人工看一眼；同时把新状态补进 `watchlist._ACTION` |
| 上线调用返回 `update goods failed` 之类 | `publish` 接口契约变动 | 记下来，别重试到死；`ship` 手动补一次，然后更新 `publish_asset()` |

---

## 六、为什么不直接用 `ship`

`ship` 是更早的版本，做的是「扫全站所有 `approved` 资产，统统上线」。
它缺三样东西，正是巡航要补的：

1. **不知道在等谁** —— 会把历史遗留、别的项目、甚至不想发的资产一起发出去；
2. **不认结束** —— 每次都全站扫，没有「都办完了」这个概念，自动化只能常驻空跑；
3. **不报被驳回** —— 驳回的资产它看不见，于是永远是「没有待发布」，
   看起来一切正常，实际上那版早就废了。

新流程用 `watch`。`ship` 保留只为两件事：手工补救，以及 `approved`
但没进过列表的老资产。
