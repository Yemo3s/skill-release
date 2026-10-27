# 驳回代码对照表（open.workbuddy 质检/审核驳回原文 → 改法）

驳回原因的权威来源：`GET /api/v2/open-platform/capabilities/{assetId}/reasons`
（结构：`rejectStage: auto|manual|quality` + `reviewDetail.reviewNote`）。
本表沉淀本技能历史上真实吃过的驳回，按原文关键词索引。**先贴原文，再对照改法。**

---

## 1. 服务类目：「无资质类目字段，需重新选择对应类目」

- **原文**：服务类目审核：无资质类目字段，需重新选择对应类目，如需资质请一并提交。
- **根因**：publish payload 里发了 `qualifications: []`（空数组）。控制台规则是
  「有资质文件才带 qualifications 键」——空数组被当成「选了类目但没交资质」。
- **改法**：没有资质文件时**整个 qualifications 键都不要发**（不是发 `[]`）。
  工具链已在 `pub_workbuddy.normalize_categories()` 里丢空键，并有两层本地断言
  （`release.py check` + `pub_workbuddy` 提交前）。
- **历史**：2026-10-13、2026-10-14 两次驳回换来的，见 troubleshooting.md。

## 2. 质检：「内置文件缺失：文件 references/<file>.md（<source>.md 引用）」

- **原文**：资产包缺少必需资源 N 项：文件「references/<file>.md」
  （references/<source>.md 引用）。
- **根因**：文档里写了指向包内文件的交叉引用，但目标文件不在包里。
  平台质检会**扫描包内所有 md 的交叉引用并逐个核对文件存在**。
- **改法**：二选一 —— 补上被引用的文件，或删掉/改写悬空引用。
  工具链已在提交前跑「交叉引用完整性」断言（`pub_workbuddy.check_cross_references()`），
  悬空引用会直接拦截。
- **历史**：2026-10-27 驳回（skill-release 1.5.7，cruise.md 悬空引用本文件）。

## 3. 通用排查路径

1. `release.py probe` / `watch` 看资产状态是否变 `rejected`；
2. `GET /api/v2/open-platform/capabilities/{assetId}/reasons` 拿驳回原文；
3. 对照本表；表里没有的 → 看 `GET /messages` 通知流水里的补充说明；
4. 修完走 `release.py publish . --yes`（rejected 资产允许直接重提，不必撤回）；
   也可先 `--dry-run` 让新加的断言兜一遍底。
