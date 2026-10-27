"""三平台发布规则引擎。

**为什么要有 verified / assumed 之分**
------------------------------------
发布失败的代价不对称：被拒一次要人工重来，所以宁可多报。但把猜测当成
「规矩」写进代码同样危险 —— 会逼着你去改本来没问题的东西。因此每条规则
都带来源等级：

  verified  平台文档原文，或一次真实的拒绝报错。命中即 **block**。
  assumed   文档的间接描述 / 社区经验。命中只报 **warn**，不阻断。

规则来源见 references/platforms.md，每条都标了出处。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Finding", "Inventory", "analyze", "render_report",
    "PLATFORMS", "PLATFORM_LABEL",
    "EXCLUDE_DIRS", "EXCLUDE_SUFFIXES", "EXCLUDE_NAMES",
    "MAX_DEPTH", "collect_files", "cross_reference_dangles",
]

# 包内 md 里写的包相对路径引用，如 references/xxx.md、scripts/yyy.py。
# 只认官方文档列出的三个二级目录前缀，避免把正文里的普通词组当路径。
_CROSS_REF_RE = re.compile(
    r"(?<![\w/])((?:references|scripts|templates)/[A-Za-z0-9._\-]+\.[A-Za-z0-9]{1,5})")

PLATFORMS = ("github", "skillhub", "workbuddy")
PLATFORM_LABEL = {
    "github": "GitHub",
    "skillhub": "SkillHub",
    "workbuddy": "open.workbuddy",
}

# 打包时一律排除 —— 这些是本地产物，不是技能内容
EXCLUDE_DIRS = {".git", ".idea", ".vscode", "node_modules", "__pycache__",
                ".pytest_cache", ".mypy_cache", ".venv", "venv"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")
EXCLUDE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

# 发布到 skillhub / workbuddy 时额外剔除的扩展名（github 保留，那里是技能源）
# 理由两条，都在 douyin-downloader 上验证过：
#   1. SkillHub 的扩展名白名单里没有 .sh，属未验证项，不想再赌一次上传；
#   2. uninstall.sh 是自删除脚本（shutil.rmtree 自身目录），过 AI 安全审核风险高。
PUBLISH_EXCLUDE_SUFFIXES = {".sh"}
PUBLISH_EXCLUDE_PLATFORMS = {"skillhub", "workbuddy"}

# SkillHub 的图标不是「内容文件」，而是平台元数据 —— 由平台侧单独设置。
# 实测：把 icon.png 当普通文件上传，会被整包拒绝：
#     400 {"error":"不允许的文件类型: icon.png"}
# 官方 CLI 同样不传头像，发布后需在详情页手动设 iconUrl。
# 注意：这一条与 .sh 不同 —— 只有**根目录**下的 icon.* 排除；
# 子目录里若真有 png 素材，那属于未验证类型，交给 warn 提示而不是静默丢掉。
SKILLHUB_EXCLUDE_ROOT_ICONS = ("icon.png", "icon.jpg", "icon.jpeg",
                               "icon.webp", "icon.svg", "icon.gif")

# 目录层级：根目录 / 二级目录 / 文件 —— 相对路径最多 2 段
MAX_DEPTH = 2

# 已被真实上传验证通过的扩展名
# 注意 .png 已被**证伪**：icon.png 实测被拒（见上），故不在此列。
VERIFIED_EXT = {".md", ".py", ".txt", ".json"}

# 已被平台**实测拒绝**的扩展名 —— 比「未验证」更强的一档：不是没试过，是试过被拒。
# 命中即 block（本地拦下，别浪费一次真实上传）。
#   .tsv：2026-10-13 probably-stolen 发布时实测
#        400 {"error":"不允许的文件类型: reference/icon-map.tsv"}
#        官方 CLI 的扩展名白名单里没有 .tsv；同内容的制表符文本改名 .txt 即可通过。
REJECTED_EXT = {".tsv"}

# 单平台体积上限 (字节, 来源等级, 出处)
SIZE_LIMITS = {
    "skillhub": (10 * 1024 * 1024, "verified",
                 "官方 CLI 源码注释：publish 接口 10MB 限制"),
    "workbuddy": (3 * 1024 * 1024, "assumed",
                  "官方文档未给数值；社区实录称 skill 包 ≤ 3MB"),
    "github": (100 * 1024 * 1024, "verified",
               "GitHub 单文件硬上限 100MB"),
}

ICON_MAX_BYTES = 500 * 1024
ICON_SIZE = (512, 512)

# 各平台的 frontmatter 硬性要求
FM_REQUIRED = {
    "skillhub": (["slug", "version", "displayName"], "verified",
                 "官方 release.md：slug / version / displayName 为必填"),
    "workbuddy": (["description", "description_zh", "description_en",
                   "version", "author"], "verified",
                  "open.workbuddy/docs/skill：以上五项标为必填"),
}

# 平台元数据字段：本体 SKILL.md 里通常没有，由本技能投影到临时副本
PLATFORM_META_KEYS = {"slug", "displayName", "author"}  # ghscan:allow 常量名含 KEY，值不是凭据


@dataclass
class Finding:
    level: str          # block | warn | info
    platform: str       # github | skillhub | workbuddy | common
    code: str
    message: str
    source: str = ""    # verified | assumed
    fix: str = ""

    @property
    def blocks(self) -> bool:
        return self.level == "block"


@dataclass
class Inventory:
    root: Path
    files: list[tuple[str, int]] = field(default_factory=list)  # (rel_posix, size)
    empty_dirs: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(s for _, s in self.files)

    def depth_violations(self) -> list[str]:
        """返回超过 MAX_DEPTH 层的路径。"""
        bad = []
        for rel, _ in self.files:
            if len(rel.split("/")) > MAX_DEPTH:
                bad.append(rel)
        return bad

    def extensionless(self) -> list[str]:
        return [rel for rel, _ in self.files if not Path(rel).suffix]

    def extensions(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rel, _ in self.files:
            ext = Path(rel).suffix.lower() or "(无扩展名)"
            out[ext] = out.get(ext, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def find(self, name: str) -> str | None:
        for rel, _ in self.files:
            if rel.lower() == name.lower():
                return rel
        return None


def collect_files(skill_dir: Path, for_platform: str | None = None) -> Inventory:
    """遍历技能目录，套用统一排除规则。软链接一律跳过（防打包出目录外内容）。

    for_platform 给定时套用平台级差异：
      - skillhub / workbuddy 剔除 .sh；
      - skillhub 另外剔除**根目录**下的 icon.*（图标属平台元数据，不是内容，
        实测上传会被判「不允许的文件类型: icon.png」）。
    让「检查」和「实际上传」看到同一份清单 —— 否则检查过了、上传被拒。
    """
    extra: set[str] = set()
    root_icons: tuple[str, ...] = ()
    if for_platform in PUBLISH_EXCLUDE_PLATFORMS:
        extra = PUBLISH_EXCLUDE_SUFFIXES
    if for_platform == "skillhub":
        root_icons = SKILLHUB_EXCLUDE_ROOT_ICONS
    root = Path(skill_dir).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"技能目录不存在: {root}")
    inv = Inventory(root=root)
    for cur, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        rel_dir = Path(cur).relative_to(root).as_posix()
        if rel_dir != "." and not dirs and not names:
            inv.empty_dirs.append(rel_dir)
        for n in sorted(names):
            if n in EXCLUDE_NAMES or n.endswith(EXCLUDE_SUFFIXES):
                continue
            if extra and Path(n).suffix.lower() in extra:
                continue
            p = Path(cur) / n
            if p.is_symlink():
                continue
            rel = p.relative_to(root).as_posix()
            if root_icons and "/" not in rel and n.lower() in root_icons:
                continue
            inv.files.append((rel, p.stat().st_size))
    return inv


def _icon_status(skill_dir: Path) -> tuple[str, str]:
    """返回 (状态, 说明)。状态：ok / missing / too_small / too_large / unreadable"""
    p = Path(skill_dir) / "icon.png"
    if not p.is_file():
        return "missing", "未找到 icon.png"
    size = p.stat().st_size
    if size > ICON_MAX_BYTES:
        return "too_large", f"icon.png {size / 1024:.0f} KB 超过 500 KB"
    try:
        import struct
        with p.open("rb") as fh:
            head = fh.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return "unreadable", "icon.png 不是合法 PNG"
        w, h = struct.unpack(">II", head[16:24])
    except Exception as exc:  # noqa: BLE001
        return "unreadable", f"读取 icon.png 失败: {exc}"
    if (w, h) != ICON_SIZE:
        return "wrong_size", f"icon.png 是 {w}×{h}，要求 {ICON_SIZE[0]}×{ICON_SIZE[1]}"
    return "ok", f"icon.png {w}×{h} / {size / 1024:.0f} KB"


def analyze(skill_dir: Path, frontmatter_data: dict[str, object] | None = None,
            targets: tuple[str, ...] = PLATFORMS,
            author: str = "", slug_override: str = "") -> tuple[Inventory, list[Finding]]:
    """跑全部规则，返回 (清单, 发现列表)。

    author 用于 workbuddy 的字段投影；slug_override 用于 skillhub ——
    slug 是全网唯一且可能被封禁的，允许从配置覆盖而不改源文件。
    """
    root = Path(skill_dir).resolve()
    inv = collect_files(root)
    fm = frontmatter_data or {}
    findings: list[Finding] = []

    def add(level, platform, code, message, source="", fix=""):
        findings.append(Finding(level, platform, code, message, source, fix))

    # ---------------- 三平台共同 ----------------
    if not inv.files:
        add("block", "common", "empty", "技能目录为空")
    if not inv.find("SKILL.md"):
        add("block", "common", "no-skill-md", "根目录缺少 SKILL.md")
    if not str(fm.get("version") or "").strip():
        add("block", "common", "no-version", "frontmatter 缺少 version",
            "verified", "三平台都要求版本号")

    # ---------------- GitHub ----------------
    if "github" in targets:
        if not inv.find("README.md"):
            add("warn", "github", "no-readme",
                "仓库没有 README.md，GitHub 首页会空着",
                fix="可在 staging 时自动由 SKILL.md 的 description 生成")
        if not inv.find(".gitignore"):
            add("info", "github", "no-gitignore",
                "没有 .gitignore，建议忽略 __pycache__ / .venv 等本地产物")
        big = [rel for rel, s in inv.files if s > 50 * 1024 * 1024]
        for rel in big:
            add("block", "github", "file-too-big",
                f"{rel} 超过 50MB，GitHub 推送会失败", "verified")
        add("info", "github", "secret-scan",
            "推送前会由 github-ops 做密钥扫描（ghscan）",
            "verified", "扫描命中会中止推送，需人工确认")

    # ---------------- SkillHub ----------------
    if "skillhub" in targets:
        # 用**平台过滤后**的清单做扩展名与体积判定 —— 上传的是这一份。
        # 若拿未过滤的清单判，icon.png / *.sh 这两个「本来就不会上传」的类型
        # 会天天报未验证扩展名，把真信号淹掉。
        inv_sh = collect_files(root, for_platform="skillhub")
        no_ext = inv_sh.extensionless()
        for rel in no_ext:
            add("block", "skillhub", "extensionless",
                f"{rel} 没有扩展名，SkillHub 按扩展名白名单过滤会整包拒绝",
                "verified", "改名为 <原名>.txt，并同步代码里的引用")
        rejected = sorted({Path(r).suffix.lower() for r, _ in inv_sh.files
                           if Path(r).suffix.lower() in REJECTED_EXT})
        for ext in rejected:
            hits = [r for r, _ in inv_sh.files if Path(r).suffix.lower() == ext]
            add("block", "skillhub", "rejected-ext",
                f"扩展名 {ext} 已被 SkillHub 实测拒绝（400 不允许的文件类型）："
                f"{'、'.join(hits[:3])}{'…' if len(hits) > 3 else ''}",
                "verified", "同内容改名 .txt 即可通过，并同步代码/文档里的引用")
        unknown = sorted({Path(r).suffix.lower() for r, _ in inv_sh.files
                          if Path(r).suffix and Path(r).suffix.lower() not in VERIFIED_EXT
                          and Path(r).suffix.lower() not in REJECTED_EXT})
        for ext in unknown:
            add("warn", "skillhub", "unverified-ext",
                f"扩展名 {ext} 未经真实上传验证（已知通过："
                f"{', '.join(sorted(VERIFIED_EXT))}）",
                "assumed", "首次发布时留意是否被判『不允许的文件类型』")

        keys, src, why = FM_REQUIRED["skillhub"]
        eff, auto = effective_frontmatter(fm, "skillhub", slug_override=slug_override)
        for k in keys:
            if not str(eff.get(k) or "").strip():
                add("block", "skillhub", f"fm-{k}",
                    f"frontmatter 缺少 {k}，且无法自动推导（{why}）", src,
                    "在 SKILL.md 补上，或用 --set 配置：本技能会投影到临时副本")
        for k, how in auto.items():
            add("info", "skillhub", f"auto-{k}", f"{k} 将自动补全：{how}", src)
        slug = str(eff.get("slug") or "")
        if slug and not (2 <= len(slug) <= 128):
            add("block", "skillhub", "slug-len", f"slug 长度需在 2-128: {slug}", src)
        if slug and not re.match(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", slug):
            add("block", "skillhub", "slug-charset",
                f"slug 只能小写字母/数字/连字符: {slug}", src)
        ver = str(eff.get("version") or "")
        if ver and not _semver_ok(ver):
            add("block", "skillhub", "semver", f"version 不是合法 SemVer: {ver}", src)

        limit, lsrc, why = SIZE_LIMITS["skillhub"]
        if inv_sh.total_bytes > limit:
            add("block", "skillhub", "size",
                f"实际上传体积 {inv_sh.total_bytes / 1048576:.2f} MB "
                f"超过 {limit // 1048576} MB", lsrc, why)

        status, note = _icon_status(root)
        if status != "ok":
            add("warn", "skillhub", "icon",
                f"图标不合规：{note}", "verified",
                "CLI 不传头像，发布后需在详情页手动设 iconUrl；先用 icon.png 备好")

    # ---------------- open.workbuddy ----------------
    if "workbuddy" in targets:
        bad = inv.depth_violations()
        for rel in bad:
            add("block", "workbuddy", "depth",
                f"{rel} 是第 {len(rel.split('/'))} 层，超过『根目录/二级目录/文件』限制",
                "verified", "把子目录里的文件上提到上一级，或给文件名加前缀")
        keys, src, why = FM_REQUIRED["workbuddy"]
        eff, auto = effective_frontmatter(fm, "workbuddy", author)
        for k in keys:
            if not str(eff.get(k) or "").strip():
                fix = ("执行 skill-release creds --set author=<你的展示名>"
                       if k == "author" else "在 SKILL.md 补上该字段")
                add("block", "workbuddy", f"fm-{k}",
                    f"frontmatter 缺少 {k}，且无法自动推导（{why}）", src, fix)
        for k, how in auto.items():
            add("info", "workbuddy", f"auto-{k}", f"{k} 将自动补全：{how}", src)
        limit, lsrc, why = SIZE_LIMITS["workbuddy"]
        if inv.total_bytes > limit:
            add("warn", "workbuddy", "size",
                f"包体积 {inv.total_bytes / 1048576:.2f} MB 可能超过 {limit // 1048576} MB",
                lsrc, why)
        add("info", "workbuddy", "zip-only",
            "平台只收 .zip，且会自动开包解析", "verified",
            "本技能负责打包；解析失败看 references/platforms.md")

        # 官方文档列出的二级目录只有 references / scripts / templates
        doc_dirs = {"references", "scripts", "templates"}
        subdirs = {rel.split("/")[0] for rel, _ in inv.files if "/" in rel}
        for d in sorted(subdirs - doc_dirs):
            add("warn", "workbuddy", "dir-name",
                f"二级目录 `{d}/` 不在官方文档列出的名字里"
                f"（{'、'.join(sorted(doc_dirs))}）", "assumed",
                "文档没说必须是这三个，且实测未报错；对齐命名可减少解析告警")

        # 交叉引用完整性：平台质检会扫包内 md 的相对路径引用并逐个核对
        # 文件存在（2026-10-27 实测驳回：「内置文件缺失：
        # references/rejection-codes.md（references/cruise.md 引用）」）。
        for src_rel, target in cross_reference_dangles(skill_dir):
            add("block", "workbuddy", "dangling-ref",
                f"`{src_rel}` 引用了包里不存在的 `{target}`", "verified",
                "补上被引用的文件，或删掉/改写这条引用"
                "（对照 references/rejection-codes.md 第 2 条）")

    # ---------------- 目录卫生 ----------------
    if inv.empty_dirs:
        add("info", "common", "empty-dirs",
            f"{len(inv.empty_dirs)} 个空目录不会被打包: {', '.join(inv.empty_dirs[:3])}")

    # 被剔出发布包的文件要显式告知，不能悄悄少东西
    if set(targets) & PUBLISH_EXCLUDE_PLATFORMS:
        dropped = [rel for rel, _ in inv.files
                   if Path(rel).suffix.lower() in PUBLISH_EXCLUDE_SUFFIXES]
        if dropped:
            add("info", "common", "publish-excluded",
                f"{len(dropped)} 个文件不进发布包（仅留在 GitHub 仓库）: "
                f"{', '.join(dropped[:4])}",
                "verified",
                "理由：SkillHub 扩展名白名单未验证 .sh；自删除脚本过审风险高")
        if "skillhub" in targets:
            icons = [rel for rel, _ in inv.files
                     if "/" not in rel and Path(rel).name.lower()
                     in SKILLHUB_EXCLUDE_ROOT_ICONS]
            if icons:
                add("info", "skillhub", "icon-not-uploaded",
                    f"图标不随文件清单上传: {', '.join(icons)}",
                    "verified",
                    "图标属平台元数据；实测当普通文件上传会被拒"
                    "（不允许的文件类型: icon.png），发布后在详情页设 iconUrl")

    return inv, findings


def cross_reference_dangles(skill_dir: Path) -> list[tuple[str, str]]:
    """扫描包内所有 .md 的包相对路径引用，返回悬空引用 [(来源文件, 目标)]。

    平台质检（2026-10-27 实测）会核对「references/xxx.md（yyy.md 引用）」
    这类交叉引用的目标文件是否存在，缺一个就整包驳回（rejectStage=quality）。
    所以提交前本地先查一遍。URL 里的路径（https://…/references/…）不算引用。
    """
    dangles: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for name in files:
            p = Path(root) / name
            if p.suffix.lower() not in (".md", ".txt"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            src_rel = p.relative_to(skill_dir).as_posix()
            for m in _CROSS_REF_RE.finditer(text):
                target = m.group(1)
                # 命中位置往前看：紧跟在 "://" 后面的是 URL 的一部分，跳过
                before = text[max(0, m.start() - 8):m.start()]
                if "://" in before:
                    continue
                if (skill_dir / target).is_file():
                    continue
                key = (src_rel, target)
                if key not in seen:
                    seen.add(key)
                    dangles.append(key)
    return dangles


def _semver_ok(ver: str) -> bool:
    from frontmatter import SEMVER_RE
    return bool(SEMVER_RE.match(ver))


# 付费发布前的收入天花板门槛。2026-10-27 事故沉淀：
# launch-guard（¥29，教人上架技能）在「可触达人群 9 人」的前提下就被做完了整套，
# 真实天花板 9 × 10% × 29 ≈ ¥26 —— 一天工钱都不到。问题不是定价错了，是**根本没算**。
# 所以把算式变成阻塞项：算得出且过线才允许发付费。
CEILING_FLOOR = 300.0      # 元。低于它就是「不值得做」，不是「先试试」。
CEILING_CONVERSION = 0.10  # 保守转化率：付费工具在内测期按 10% 估已经算乐观


def revenue_ceiling(audience: int, price: float,
                    rate: float = CEILING_CONVERSION) -> dict:
    """付费资产的天花板估算。

    `audience` 是**已经证明会装这类东西的人**（不是全平台用户）——
    取值必须来自真实装机数据，例如同类免费资产的安装量。
    `price` 单位是元（买断价，或订阅的月价）。

    返回 dict：
      audience / price / rate / ceiling（元）/ floor / pass（bool）/ verdict
    这不是预测收入，是**排除明显不该做的东西**。算出来过线也不代表能卖出去。
    """
    try:
        aud = max(0, int(audience))
    except (TypeError, ValueError):
        aud = 0
    try:
        pr = max(0.0, float(price))
    except (TypeError, ValueError):
        pr = 0.0
    ceiling = round(aud * float(rate) * pr, 2)
    ok = ceiling >= CEILING_FLOOR and aud > 0 and pr > 0
    if aud <= 0:
        verdict = "没有可触达人群 → 不该做"
    elif pr <= 0:
        verdict = "没定价 → 不算付费资产"
    elif ok:
        verdict = f"天花板 ¥{ceiling:.0f}，过线（≥ ¥{CEILING_FLOOR:.0f}）"
    else:
        verdict = (f"天花板 {aud} × {rate:.0%} × ¥{pr:g} = ¥{ceiling:.0f}，"
                   f"低于 ¥{CEILING_FLOOR:.0f} → 不该做")
    return {"audience": aud, "price": pr, "rate": float(rate),
            "ceiling": ceiling, "floor": CEILING_FLOOR,
            "pass": ok, "verdict": verdict}


def effective_frontmatter(fm: dict, platform: str,
                          author: str = "", slug_override: str = "") -> tuple[dict, dict]:
    """算出「投影到临时副本之后」该平台实际看到的 frontmatter。

    返回 (字段字典, {被自动补全的字段: 来源说明})。

    **为什么必须这样检查**：slug / displayName / author 属于平台元数据，
    技能本体里通常不写。如果规则直接对着源文件判缺失，每次发布都会被自己的
    规则挡住 —— 那是把「本技能要解决的问题」错当成了「技能的毛病」。
    判定依据应当是投影后的结果。
    """
    out: dict[str, object] = dict(fm)
    auto: dict[str, str] = {}

    if platform == "skillhub":
        if str(slug_override or "").strip():
            out["slug"] = slug_override
            auto["slug"] = f"由配置覆盖（{slug_override}）"
        elif not str(out.get("slug") or "").strip():
            src = str(fm.get("name") or "").strip()
            if src:
                out["slug"] = src
                auto["slug"] = f"由 name 推导（{src}）"
        if not str(out.get("displayName") or "").strip():
            for cand, label in ((fm.get("display_name"), "display_name"),
                                (fm.get("display_name_en"), "display_name_en"),
                                (out.get("slug"), "slug")):
                if str(cand or "").strip():
                    out["displayName"] = cand
                    auto["displayName"] = f"由 {label} 推导（{cand}）"
                    break
        if not str(out.get("summary") or "").strip():
            desc = str(fm.get("description") or "")
            if desc.strip():
                out["summary"] = " ".join(desc.split())[:150]
                auto["summary"] = "由 description 首句截取"

    elif platform == "workbuddy":
        chosen = str(author or fm.get("author") or "").strip()
        if chosen:
            out["author"] = chosen
            auto["author"] = ("由配置 author 注入" if not fm.get("author")
                              else "技能自带")

    return out, auto


def summarize(findings: list[Finding], targets: tuple[str, ...]) -> dict[str, bool]:
    """每个目标是否可发布（该目标下没有 block 级发现）。"""
    out = {}
    for t in targets:
        out[t] = not any(f.blocks and f.platform in (t, "common") for f in findings)
    return out


def render_report(inv: Inventory, findings: list[Finding],
                  targets: tuple[str, ...], slugs: dict[str, str] | None = None) -> str:
    """渲染人可读报告。结论先行，再上细节。"""
    status = summarize(findings, targets)
    lines: list[str] = []

    verdict = "、".join(
        f"{PLATFORM_LABEL[t]} {'可发' if ok else '不可发'}" for t, ok in status.items()
    )
    lines.append(f"结论：{verdict}")
    lines.append("")

    blocks = [f for f in findings if f.blocks]
    warns = [f for f in findings if f.level == "warn"]
    lines.append(f"阻断 {len(blocks)} 项 / 警告 {len(warns)} 项")
    lines.append("")

    if slugs:
        uniq = sorted(set(slugs.values()))
        detail = "，".join(f"{PLATFORM_LABEL[t]}={s}" for t, s in slugs.items()
                           if t in targets)
        lines.append(f"slug：{uniq[0]}" if len(uniq) == 1 else f"slug 不一致：{detail}")
        lines.append("")

    lines.append("包清单")
    lines.append(f"  文件 {len(inv.files)} 个，共 {inv.total_bytes / 1024:.1f} KB")
    lines.append(f"  最大层级 {max((len(r.split('/')) for r, _ in inv.files), default=0)}"
                 f"（上限 {MAX_DEPTH}）")
    ext = inv.extensions()
    lines.append("  扩展名 " + "，".join(f"{k}×{v}" for k, v in ext.items()))
    lines.append("")

    if blocks:
        lines.append("必须处理")
        for f in blocks:
            tag = PLATFORM_LABEL.get(f.platform, "通用")
            lines.append(f"  ✗ [{tag}] {f.message}")
            if f.fix:
                lines.append(f"      改法：{f.fix}")
        lines.append("")

    if warns:
        lines.append("建议处理（不阻断）")
        for f in warns:
            tag = PLATFORM_LABEL.get(f.platform, "通用")
            lines.append(f"  ! [{tag}] {f.message}")
            if f.fix:
                lines.append(f"      {f.fix}")
        lines.append("")

    infos = [f for f in findings if f.level == "info"]
    if infos:
        lines.append("提示")
        for f in infos:
            tag = PLATFORM_LABEL.get(f.platform, "通用")
            lines.append(f"  · [{tag}] {f.message}")
        lines.append("")

    return "\n".join(lines)
