# -*- coding: utf-8 -*-
"""洛雪音源工具箱：音源检索 + 图形界面应用层。

本模块是 PyInstaller 打包入口，核心校验逻辑复用同目录 lx_source_updater 模块。
新增能力：
  * 音源检索：扫描候选 GitHub 仓库 -> 解析脚本头部 @name/@version/@author -> 勾选批量下载
  * 音源去重（v2.1 增强）：
      1) 名称键先剥离版本号（v1.2.3 / 1.2.3 / 93特供版 …）与 emoji / 标点噪声，再忽略大小写归一；
      2) 跨仓库内容 SHA-256 完全相同 -> 直接合并为一条；
      3) 主体指纹（剔头部注释与版本串后的正文哈希）近似去重 -> 同指纹只保留版本最高 / 体积最大的一条；
      4) 结果行显示保留版本、收录仓库数与其他版本号，可切换「去重显示 / 显示全部」；
         下载按内容 SHA-256 去重，同名同内容跳过、同名不同内容另存并标注冲突；
  * 扫描提速（v2.1 增强）：仓库级并发 + 仓库内文件级并发、磁盘扫描缓存（仓库+分支+tree sha 未变即复用）、
             单文件超时默认 8 秒快速切换镜像、可选 GitHub Token、逐仓库与总耗时日志
  * tkinter 图形界面：音源校验升级 / 音源检索 两个标签页
  * 命令行模式：无参数启动 GUI；带参数进入 CLI（--check / --force / --dir / --json / --discover）
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import lx_source_updater as core

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
except Exception:  # 无图形环境时降级为纯 CLI
    tk = None

APP_NAME = "洛雪音源工具箱"
APP_VERSION = core.TOOL_VERSION
APP_DIR = core.TOOL_DIR
# 洛雪（LX Music）官方图标文件名（打包时通过 --add-data 一并打入 exe）
ICON_NAME = "app_icon.ico"

GITHUB_API = "https://api.github.com"
RAW_HOST = "https://raw.githubusercontent.com"

# 打包为 exe 时，把备份/报告/状态等运行期数据收纳进 exe 同目录的数据文件夹，避免污染桌面
if getattr(sys, "frozen", False):
    DATA_DIR = APP_DIR / "洛雪音源工具箱数据"
    core.DEFAULT_REPORT_DIR = DATA_DIR / "reports"
    core.DEFAULT_STATE_FILE = DATA_DIR / "state.json"
    core.DEFAULT_BACKUP_ROOT = DATA_DIR / "backup"
    core.DEFAULT_LOCAL_DIR = DATA_DIR / "sources"
else:
    DATA_DIR = APP_DIR

DISCOVER_REPOS_FILE = DATA_DIR / "discover_repos.json"

# 磁盘扫描缓存：放在 exe 同目录（仓库 + 分支 + tree sha 未变则直接复用上次结果）
DISCOVER_CACHE_FILE = APP_DIR / "discover_cache.json"
CACHE_VERSION = 4  # v3 缓存含旧连通性存活判定，升版强制重扫重测（播放级）

# 扫描并发 / 超时默认值（界面与命令行均可调）
DEFAULT_REPO_WORKERS = 6      # 仓库级并发
DEFAULT_FILE_WORKERS = 8      # 仓库内文件级并发
DEFAULT_FIND_TIMEOUT = 8.0    # 单文件抓取超时（秒），8s 后快速切换镜像
MAX_MIRROR_TRY = 4            # 单文件最多尝试的镜像数（超过则判为失败，避免长时间卡住）

# 界面填写的 GitHub Token（仅内存，不落盘；优先于环境变量）
_TOKEN_OVERRIDE = ""

# 候选仓库清单（可在界面中增删，保存到 exe 同目录 discover_repos.json）
BUILTIN_DISCOVER_REPOS = [
    {"full_name": "pdone/lx-music-source", "branch": "main", "note": "洛雪音乐源集合（推荐）"},
    {"full_name": "Macrohard0001/lx-ikun-music-sources", "branch": "main", "note": "ikun 音源合集"},
    {"full_name": "ZxwyWebSite/lx-source", "branch": "main", "note": "Zxwy 音源"},
    {"full_name": "lyswhut/lx-music-source", "branch": "master", "note": "官方示例音源"},
    {"full_name": "cdyUuu/lx-music-xinghai-source", "branch": "main", "note": "星海音源"},
    {"full_name": "hwxlikemi/lxs", "branch": "main", "note": "lxs 音源合集"},
    {"full_name": "cc2415/lx-custom-music-source", "branch": "main", "note": "六音音源"},
    {"full_name": "Qian-Ning/LX-Music-Source", "branch": "main", "note": "LX 音源合集"},
    # 以下为实测可用仓库（第二批补充，共 9 个）
    {"full_name": "guoyue2010/lxmusic-", "branch": "main", "note": "全网最新最全音源"},
    {"full_name": "wzh15802/lxmusic", "branch": "main", "note": "含星海/独家v5/聚合"},
    {"full_name": "LuoXiaohei-2025/LX-music-collection", "branch": "main", "note": "音源合辑"},
    {"full_name": "kayee0212/lx-music-yinyuan", "branch": "main", "note": "含独家v4/聚合"},
    {"full_name": "LXJ-George666/LXMusic-Yinyuan", "branch": "main", "note": "含至尊源/monster"},
    {"full_name": "a97083435/lxmusic-source", "branch": "main", "note": "含废材公社内部源"},
    {"full_name": "wangxanshen/lx-music-source", "branch": "main", "note": "gdstudio"},
    {"full_name": "ycquah00/lx-music-source-v5", "branch": "main", "note": "独家音源v5镜像"},
    {"full_name": "ryan159115/lxmusic-source", "branch": "main", "note": "含墨澜聚合/HYW/K×H"},
]

DISCOVER_EXCLUDE_DIRS = {
    ".github", ".git", ".vscode", ".idea", "node_modules", "docs", "doc",
    "test", "tests", "assets", "images", "img", "screenshots", "website",
}
DISCOVER_EXCLUDE_NAMES = {
    "webpack.config.js", "gulpfile.js", "rollup.config.js", "vite.config.js",
    "package.json", "tsconfig.json", "eslint.config.js", ".eslintrc.js",
    "postcss.config.js", "tailwind.config.js", "service-worker.js", "sw.js",
    "commitlint.config.js", "babel.config.js", "jest.config.js",
}

STATUS_COLORS = {
    core.STATUS_AVAILABLE: "#1a7f37",
    core.STATUS_DEAD: "#c22b2b",
    core.STATUS_UNKNOWN: "#6b6b6b",
    core.STATUS_FAILED: "#c22b2b",
}
# 存活列配色：可用（绿）/ 失效（红）/ 无法判定（灰）
ALIVE_COLORS = {
    core.STATUS_AVAILABLE: "#1a7f37",
    core.STATUS_DEAD: "#c22b2b",
    core.STATUS_UNKNOWN: "#6b6b6b",
}

# 检索结果列表配色：重复收录（蓝）
FIND_TAG_COLORS = {"dup": "#0b5cad"}


def _user_agent() -> str:
    return "%s/%s (lx-music source toolkit)" % (core.TOOL_NAME, APP_VERSION)


def set_github_token(token: str) -> None:
    """设置界面填写的 GitHub Token（仅内存，不落盘）。"""
    global _TOKEN_OVERRIDE
    _TOKEN_OVERRIDE = (token or "").strip()


def github_token() -> str:
    return _TOKEN_OVERRIDE or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def github_api_get(url, timeout=20.0, retries=2):
    """调用 GitHub API，返回 (json_obj, error)。"""
    headers = {"User-Agent": _user_agent(), "Accept": "application/vnd.github+json"}
    token = github_token()
    if token:
        headers["Authorization"] = "Bearer " + token
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8", "replace")
            return json.loads(payload), None
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if exc.code in (403, 429) and attempt < retries:
                time.sleep(1.2 * (attempt + 1))
                last_err = "HTTP %s %s %s" % (exc.code, exc.reason, detail)
                continue
            return None, "HTTP %s %s %s" % (exc.code, exc.reason, detail)
        except Exception as exc:
            last_err = "%s: %s" % (type(exc).__name__, exc)
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    return None, str(last_err)


def _clean_repos(repos) -> list:
    cleaned = []
    if not isinstance(repos, list):
        return cleaned
    for item in repos:
        if isinstance(item, str):
            full_name, branch, note = item, "main", ""
        elif isinstance(item, dict):
            full_name = str(item.get("full_name") or item.get("repo") or "")
            branch = str(item.get("branch") or "main")
            note = str(item.get("note") or "")
        else:
            continue
        full_name = full_name.strip().strip("/")
        if "/" not in full_name:
            continue
        cleaned.append({"full_name": full_name, "branch": branch.strip() or "main", "note": note})
    return cleaned


def load_discover_repos() -> list:
    path = Path(DISCOVER_REPOS_FILE)
    if path.is_file():
        try:
            # utf-8-sig 兼容记事本等编辑器写入的 BOM 头
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            repos = data.get("repos") if isinstance(data, dict) else data
            cleaned = _clean_repos(repos)
            if cleaned:
                return cleaned
        except Exception:
            pass
    return [dict(item) for item in BUILTIN_DISCOVER_REPOS]


def save_discover_repos(repos) -> str:
    path = Path(DISCOVER_REPOS_FILE)
    try:
        path.write_text(
            json.dumps({"repos": _clean_repos(repos)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)
    except Exception as exc:
        core.log("[警告] 保存候选仓库列表失败：%s" % exc)
        return ""


def sanitize_filename(name: str) -> str:
    illegal = '<>:"/|?*' + chr(92)
    out = []
    for ch in str(name):
        out.append("_" if (ch in illegal or ord(ch) < 32) else ch)
    text = "".join(out).strip().strip(".")
    return text or "source.js"


def human_size(num) -> str:
    try:
        value = float(num)
    except Exception:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%.0f %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "-"


def short_hash(text: str, length: int = 12) -> str:
    return (text or "")[:length]


def redirect_core_log(sink) -> None:
    """把核心模块的进度输出重定向到 GUI 日志区。"""
    core.log = sink



# --------------------------------------------------------------------------
# 音源检索：扫描仓库 -> 解析头部 -> 批量下载
# --------------------------------------------------------------------------


# ---- 音源去重：名称归一化 / 同名合并 ---------------------------

# 名称中的 emoji / 装饰符号（不含 CJK 汉字区间，避免误伤中文名）
_EMOJI_RE = re.compile(
    "["
    "\u2190-\u21ff"      # 箭头
    "\u2300-\u23ff"      # 技术符号（⏰ 等）
    "\u2460-\u24ff"      # 带圈字符
    "\u25a0-\u25ff"      # 几何图形
    "\u2600-\u27bf"      # 杂项符号 / 装饰（☁ ✨ ❄ 等）
    "\u2b00-\u2bff"
    "\u200d\u20e3\ufe0e\ufe0f"
    "\U0001f000-\U0001faff"
    "]+",
    flags=re.UNICODE,
)

# 名称中的版本号：v1.2.3 / 1.2.3 / v9.3 / 93（"93特供版" 这类后缀版本号）
_NAME_VERSION_RE = re.compile(r"(?i)(?<![a-z0-9])v?\d+(?:\.\d+)*(?![a-z0-9])")

# 名称中的营销 / 版本后缀噪声词（小写匹配）
_NAME_NOISE_TOKENS = (
    "特供版", "特供", "需自行配置", "自行配置", "无需配置", "自备配置", "需配置",
    "测试版", "测试", "试用版", "体验版", "预览版", "beta", "alpha", "rc版", "rc",
    "稳定版", "稳定", "正式版", "最新版", "最新", "修复版", "修复", "修正版", "修正",
    "内部版", "内部", "破解版", "绿色版", "纯净版", "无广告", "免广告", "去广告",
    "精简版", "完整版", "增强版", "加强版", "优化版", "修改版", "重制版", "重制",
    "自用版", "可用版", "免费版", "未加密", "解密版", "整合版", "聚合版", "纯净",
    "特供", "高音质", "高音质版",
)


def strip_name_noise(value) -> str:
    """剥离名称中的 emoji、版本号与常见营销后缀（返回小写文本）。"""
    text = _EMOJI_RE.sub("", str(value or ""))
    text = _NAME_VERSION_RE.sub("", text)
    low = text.lower()
    for token in _NAME_NOISE_TOKENS:
        if token and token in low:
            low = low.replace(token, "")
    return low


def normalize_source_name(value) -> str:
    """把音源名称归一化为比较键：先剥版本号 / emoji / 营销噪声，再忽略大小写与标点。"""
    text = strip_name_noise(value)
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[《》〈〉\[\]【】()（）{}<>\"'`·、,，。.:：;；!！?？\-_—–~+/\\|@#$%^&*=×✕✖☆★♪♫]+", "", text)
    return text


def _file_stem(path) -> str:
    return os.path.splitext(str(path or "").rsplit("/", 1)[-1])[0]


def record_name_key(record: dict) -> str:
    """记录的去重键：优先 @name（忽略大小写与版本号），缺失时退回文件名。"""
    key = normalize_source_name(record.get("name"))
    return key or normalize_source_name(_file_stem(record.get("path")))


def _other_versions(best, others) -> list:
    """收集同组其他记录的版本号（去重、按版本从高到低）。"""
    seen, out = set(), []
    best_version = str(best.get("version") or "").strip()
    for item in others:
        version = str(item.get("version") or "").strip()
        if not version or version == best_version or version in seen:
            continue
        seen.add(version)
        out.append(version)
    out.sort(key=core.normalize_version, reverse=True)
    return out


def record_note(record: dict) -> str:
    """生成结果列表“备注”列文本：收录仓库数 / 保留版本 / 其他版本号 / 重复收录。"""
    parts = []
    repos = record.get("merged_repos") or []
    if len(repos) > 1:
        parts.append("收录 %d 仓" % len(repos))
    version = str(record.get("version") or "").strip()
    if version:
        parts.append("保留版本 %s" % version)
    versions = record.get("other_versions") or []
    if versions:
        shown = "、".join(versions[:4])
        more = " 等 %d 个" % len(versions) if len(versions) > 4 else ""
        parts.append("其他版本：%s%s" % (shown, more))
    others = record.get("also_in") or []
    if others:
        detail = []
        for item in others[:3]:
            repo = item.get("repo", "")
            other_version = item.get("version") or ""
            same = "内容相同" if item.get("same_content") else "内容不同"
            detail.append("%s%s（%s）" % (repo, (" " + other_version) if other_version else "", same))
        more = "，等 %d 处" % len(others) if len(others) > 3 else ""
        parts.append("重复收录：%s%s" % ("、".join(detail), more))
    return "；".join(parts)


def annotate_records(records, tokens=None) -> list:
    """就地补充 duplicate_count / also_in / note 标注，返回原列表。"""
    groups = {}
    for record in records:
        groups.setdefault(record_name_key(record), []).append(record)
    for items in groups.values():
        for record in items:
            record["duplicate_count"] = max(0, len(items) - 1)
            others = [item for item in items if item is not record]
            record["duplicate_repos"] = sorted({item.get("repo", "") for item in others})
            record["merged_repos"] = sorted({item.get("repo", "") for item in items})
            record["repo_count"] = len(record["merged_repos"])
            record["other_versions"] = _other_versions(record, others)
            record["also_in"] = [
                {
                    "repo": item.get("repo", ""),
                    "version": item.get("version") or "",
                    "path": item.get("path", ""),
                    "sha256": item.get("sha256", ""),
                    "same_content": bool(item.get("sha256")) and item.get("sha256") == record.get("sha256"),
                }
                for item in others
            ]
            record["note"] = record_note(record)
    return records


def _record_merge_keys(record):
    """返回记录的合并线索：(名称键, 内容 SHA-256, 主体指纹)。"""
    return (record_name_key(record),
            str(record.get("sha256") or ""),
            str(record.get("body_fp") or ""))


def _pick_best(items):
    """同组内挑选保留项：版本号最高，版本相同则体积最大。"""
    best = items[0]
    for item in items[1:]:
        cmp = core.compare_version(item.get("version"), best.get("version"))
        if cmp > 0:
            best = item
        elif cmp == 0 and int(item.get("size") or 0) > int(best.get("size") or 0):
            best = item
    return best


def _merge_entry(items):
    """把同一条音源的多份记录合并成一条：保留最优版本，聚合收录仓库与其他版本号。"""
    best = _pick_best(items)
    entry = dict(best)
    others = [item for item in items if item is not best]
    entry["duplicate_count"] = len(others)
    entry["duplicate_repos"] = sorted({item.get("repo", "") for item in others})
    entry["merged_repos"] = sorted({item.get("repo", "") for item in items})
    entry["repo_count"] = len(entry["merged_repos"])
    entry["other_versions"] = _other_versions(entry, others)
    entry["merged_items"] = len(items)
    entry["also_in"] = [
        {
            "repo": item.get("repo", ""),
            "version": item.get("version") or "",
            "path": item.get("path", ""),
            "sha256": item.get("sha256", ""),
            "body_fp": item.get("body_fp", ""),
            "size": item.get("size") or 0,
            "same_content": bool(item.get("sha256")) and item.get("sha256") == best.get("sha256"),
        }
        for item in others
    ]
    entry["note"] = record_note(entry)
    return entry


def dedupe_records(records, tokens=None, stats=None) -> list:
    """三级去重：名称键（已剥版本号 / emoji / 营销噪声）→ 内容 SHA-256 → 主体指纹。

    任一线索相同即视为同一音源，合并为一条，只保留版本最高（版本相同则体积最大）
    的一份，并在备注中标注收录仓库数与其他版本号。stats 非空时回填合并统计。
    """
    annotated = annotate_records(records, tokens)
    if not annotated:
        return []

    parent = list(range(len(annotated)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        root_left, root_right = find(left), find(right)
        if root_left == root_right:
            return False
        parent[root_right] = root_left
        return True

    reasons = {"name": 0, "sha": 0, "fingerprint": 0}
    buckets = {"name": {}, "sha": {}, "fingerprint": {}}
    for index, record in enumerate(annotated):
        name_key, sha, fingerprint = _record_merge_keys(record)
        for kind, key in (("name", name_key), ("sha", sha), ("fingerprint", fingerprint)):
            if not key:
                continue
            bucket = buckets[kind]
            if key in bucket:
                if union(bucket[key], index):
                    reasons[kind] += 1
            else:
                bucket[key] = index

    groups, order = {}, []
    for index in range(len(annotated)):
        root = find(index)
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append(annotated[index])

    merged = [_merge_entry(groups[root]) for root in order]
    if stats is not None:
        stats.update({
            "raw": len(annotated),
            "merged_rows": len(merged),
            "name_merges": reasons["name"],
            "sha_merges": reasons["sha"],
            "fingerprint_merges": reasons["fingerprint"],
        })
    return merged


def dedupe_summary(records, merged, stats=None) -> str:
    """生成去重统计说明文本。"""
    duplicate_rows = sum(1 for record in merged if record.get("duplicate_count"))
    duplicate_items = sum(int(record.get("duplicate_count") or 0) for record in merged)
    text = ("去重后 %d 条（原始 %d 条）：合并重复音源 %d 项、涉及 %d 条结果。"
            % (len(merged), len(records), duplicate_items, duplicate_rows))
    if stats:
        text += ("；合并依据：同名 %d / 同内容 SHA-256 %d / 同主体指纹 %d"
                 % (stats.get("name_merges", 0), stats.get("sha_merges", 0),
                    stats.get("fingerprint_merges", 0)))
    return text


def _is_candidate_js(path: str) -> bool:
    low = path.lower()
    parts = low.split("/")
    if len(parts) > 5:
        return False
    if not low.endswith(".js"):
        return False
    if any(p in DISCOVER_EXCLUDE_DIRS for p in parts[:-1]):
        return False
    base = parts[-1]
    if base in DISCOVER_EXCLUDE_NAMES or base.startswith("."):
        return False
    return True


def _candidate_rank(path: str) -> int:
    base = path.rsplit("/", 1)[-1].lower()
    if base == "latest.js":
        return 0
    if base.endswith("latest.js"):
        return 1
    if "source" in base or "音源" in base:
        return 2
    if "music" in base or base.startswith("lx"):
        return 3
    return 4


def _dedupe_key(path: str) -> str:
    base = path.rsplit("/", 1)[-1].lower()
    if base == "latest.js" and "/" in path:
        return path.rsplit("/", 1)[0].lower()
    return base


def pick_candidate_paths(paths, limit: int = 40) -> list:
    """从仓库文件树中挑选可能是音源脚本的 .js 文件。"""
    feasible = [p for p in paths if _is_candidate_js(p)]
    feasible.sort(key=lambda p: (_candidate_rank(p), p.count("/"), len(p), p.lower()))
    picked, seen = [], set()
    for path in feasible:
        key = _dedupe_key(path)
        if key in seen:
            continue
        seen.add(key)
        picked.append(path)
        if len(picked) >= max(1, int(limit)):
            break
    return picked


def repo_raw_url(full_name: str, branch: str, path: str) -> str:
    return "%s/%s/%s/%s" % (RAW_HOST, full_name, branch, path)


def is_lx_source_script(text: str) -> bool:
    if not text:
        return False
    if core.parse_header(text).get("name"):
        return True
    for marker in ("EVENT_NAMES.inited", "lx.send", "on(EVENT_NAMES.request", "globalThis.lx"):
        if marker in text:
            return True
    return False


# ---- 主体指纹：用于识别「同一音源被不同仓库改版本号后重新打包」 ------------
_BODY_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_BODY_LINE_COMMENT_RE = re.compile(r"//[^\r\n]*")
_BODY_VERSION_FIELD_RE = re.compile(r"""(?i)["']?version["']?\s*[:=]\s*["'][^"']*["']""")
_BODY_VERSION_LITERAL_RE = re.compile(r"(?i)(?<![a-z0-9_$])v?\d+(?:\.\d+){1,3}(?![a-z0-9_$])")


def body_fingerprint(text) -> str:
    """主体指纹：剔除注释与版本字符串后对本脚本正文取 SHA-256。

    头部注释块、行注释与版本号都会被抹掉，因此同一个音源被不同仓库改了 @version
    重新打包后，主体指纹仍然一致，可用于近似去重。
    """
    if not text:
        return ""
    body = _BODY_BLOCK_COMMENT_RE.sub("", text)
    body = _BODY_VERSION_FIELD_RE.sub("", body)
    body = _BODY_LINE_COMMENT_RE.sub("", body)
    body = _BODY_VERSION_LITERAL_RE.sub("", body)
    body = re.sub(r"\s+", "", body)
    if len(body) < 40:
        return ""
    return hashlib.sha256(body.encode("utf-8", "replace")).hexdigest()


def fetch_repo_tree(full_name, branch, timeout):
    """读取仓库文件树，返回 (tree_sha, paths, error, source)。

    GitHub API 有匿名限额（60 次/小时），限额耗尽时整仓会直接失败；因此按
    GitHub API -> jsDelivr 文件清单 -> codeload zipball 三级兜底，尽量保住结果。
    """
    tree_sha, paths, err = _fetch_tree_api(full_name, branch, timeout)
    if paths:
        return tree_sha, paths, None, "api"
    api_err = err

    paths, jd_err = fetch_repo_paths_jsdelivr(full_name, branch, timeout)
    if paths:
        return "", paths, None, "jsdelivr"

    paths, zip_err = fetch_repo_paths_zip(full_name, branch, timeout)
    if paths:
        return "", paths, None, "zipball"

    return "", [], ("读取仓库文件树失败：%s；兜底 jsDelivr：%s；兜底 zipball：%s"
                    % (api_err, jd_err, zip_err)), ""


def fetch_repo_commit_sha(full_name, branch, timeout):
    """通过 commits Atom 源取分支最新 commit sha（不占 GitHub API 限额）。

    用于磁盘缓存命中判断：commit 未变即可直接复用上次扫描结果，无需再调 API。
    失败返回空串。
    """
    atom_url = "https://github.com/%s/commits/%s.atom" % (full_name, branch)
    urls = [atom_url]
    for prefix in ("https://ghproxy.net/",):
        urls.append(prefix + atom_url)
    for url in urls:
        data, _err = core.http_get(url, timeout, 0)
        if not data:
            continue
        text = data.decode("utf-8", "replace")
        found = re.search(r"<id>tag:github\.com,2008:Grit::Commit/([0-9a-f]{7,40})</id>", text)
        if found:
            return found.group(1)
    return ""


_ATOM_LOCK = threading.Lock()
_ATOM_STATE = {"ok": None, "probing": False}


def fetch_repo_commit_sha_cached(full_name, branch, timeout):
    """带会话级探测的 commit 获取：Atom 源不可达时只探测一次，后续仓库不再等待。"""
    with _ATOM_LOCK:
        if _ATOM_STATE["ok"] is False:
            return ""
        if _ATOM_STATE["ok"] is None and _ATOM_STATE["probing"]:
            return ""
        probe = _ATOM_STATE["ok"] is None
        if probe:
            _ATOM_STATE["probing"] = True
    if not probe:
        return fetch_repo_commit_sha(full_name, branch, timeout)
    try:
        value = fetch_repo_commit_sha(full_name, branch, min(timeout, 4.0))
    finally:
        with _ATOM_LOCK:
            _ATOM_STATE["probing"] = False
            _ATOM_STATE["ok"] = bool(value)
    return value


def fetch_repo_paths_jsdelivr(full_name, branch, timeout):
    """jsDelivr 数据接口兜底：返回 (paths, error)，不占用 GitHub API 限额。"""
    url = "https://data.jsdelivr.com/v1/packages/gh/%s@%s?structure=flat" % (full_name, branch)
    data, err = core.http_get(url, timeout, 1)
    if data is None:
        return [], "%s" % err
    try:
        payload = json.loads(data.decode("utf-8", "replace"))
    except Exception as exc:
        return [], "返回内容解析失败（%s）" % exc
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        return [], "返回格式异常"
    paths = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lstrip("/")
        if not name:
            continue
        if str(item.get("type") or "").lower() == "directory":
            continue
        paths.append(name)
    return paths, ("" if paths else "未返回文件清单")


def fetch_repo_paths_zip(full_name, branch, timeout, max_bytes=80 * 1024 * 1024):
    """codeload zipball 兜底：下载仓库压缩包列出文件清单，返回 (paths, error)。

    仅在前两级都失败时使用；压缩包体积超过 max_bytes 时放弃，避免拖垮扫描。
    """
    import tempfile
    import zipfile

    target = "https://codeload.github.com/%s/zip/refs/heads/%s" % (full_name, branch)
    urls = [target]
    for prefix in ("https://ghproxy.net/", "https://gh.llkk.cc/"):
        urls.append(prefix + target)
    last_err = "未尝试"
    for url in urls:
        tmp_path = ""
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="lx-tree-")
                os.close(fd)
                total = 0
                with open(tmp_path, "wb") as handle:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError("压缩包超过 %.0fMB，已放弃" % (max_bytes / 1048576.0))
                        handle.write(chunk)
            with zipfile.ZipFile(tmp_path) as archive:
                names = archive.namelist()
        except Exception as exc:
            last_err = "%s: %s" % (type(exc).__name__, exc)
            names = None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        if not names:
            continue
        paths = []
        for name in names:
            if name.endswith("/"):
                continue
            parts = name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            paths.append(parts[1])
        if paths:
            return paths, ""
    return [], (last_err or "未取到文件清单")


def _fetch_tree_api(full_name, branch, timeout):
    """GitHub API 读取仓库文件树，返回 (tree_sha, paths, error)。"""
    api_url = "%s/repos/%s/git/trees/%s?recursive=1" % (GITHUB_API, full_name, branch)
    data, err = github_api_get(api_url, timeout=timeout, retries=1)
    if data is None:
        return "", [], "%s" % err
    tree = data.get("tree")
    if not isinstance(tree, list):
        return "", [], "仓库文件树格式异常"
    paths = [t.get("path") for t in tree if isinstance(t, dict) and t.get("path")]
    return str(data.get("sha") or ""), paths, None


def scan_repo(full_name, branch, limit, timeout, stop_event=None, progress=None,
              keep_data=True, file_workers=DEFAULT_FILE_WORKERS, paths=None, tree_sha="",
              liveness_cache=None, probe_timeout=5.0):
    """并发扫描单个仓库，返回 (records, error)。"""
    records = []
    if paths is None:
        tree_sha, paths, err, _source = fetch_repo_tree(full_name, branch, timeout)
        if err:
            return records, err
    candidates = pick_candidate_paths(paths or [], limit=limit)
    if progress:
        progress("[%s] 文件树 %d 项，筛出候选脚本 %d 个" % (full_name, len(paths or []), len(candidates)))
    if not candidates:
        return records, "未发现候选音源脚本"

    notes = []
    notes_lock = threading.Lock()

    def grab(path):
        """抓取并解析单个候选脚本（供文件级线程池调用）。"""
        if stop_event is not None and stop_event.is_set():
            return None
        raw_url = repo_raw_url(full_name, branch, path)
        probe = {"key": path.rsplit("/", 1)[-1], "url": raw_url}
        blob, text, used_url, errors = core.fetch_with_mirrors(
            probe, core.BUILTIN_MIRRORS, timeout, 0, max_mirror_try=MAX_MIRROR_TRY,
        )
        if blob is None or text is None:
            with notes_lock:
                notes.append("%s 抓取失败" % path)
            return None
        if not is_lx_source_script(text):
            with notes_lock:
                notes.append("%s 非音源脚本，已跳过" % path)
            return None
        meta = core.parse_header(text)
        record = {
            "repo": full_name,
            "branch": branch,
            "path": path,
            "raw_url": raw_url,
            "used_url": used_url or raw_url,
            "name": meta.get("name") or path.rsplit("/", 1)[-1],
            "version": meta.get("version") or "",
            "author": meta.get("author") or "",
            "homepage": meta.get("homepage") or "",
            "description": meta.get("description") or "",
            "size": len(blob),
            "sha256": core.sha256_bytes(blob),
            "body_fp": body_fingerprint(text),
            "data": blob if keep_data else None,
        }
        if liveness_cache is not None:
            alive_info = core.check_script_liveness(
                text, timeout=probe_timeout, mirrors=core.BUILTIN_MIRRORS,
                cache=liveness_cache, ttl=core.LIVE_CACHE_TTL)
            record["alive"] = alive_info.get("status") or ""
            record["alive_endpoint"] = alive_info.get("endpoint") or ""
            record["alive_note"] = alive_info.get("note") or ""
        return record

    workers = max(1, min(int(file_workers or 1), len(candidates)))
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(grab, path) for path in candidates]
        for future in concurrent.futures.as_completed(futures):
            record = future.result()
            done += 1
            if record is None:
                continue
            records.append(record)
            if progress:
                progress("  [%d/%d] %s  版本=%s 作者=%s"
                         % (done, len(candidates), record["name"],
                            record["version"] or "?", record["author"] or "?"))
    records.sort(key=lambda item: item["path"])
    return records, ("；".join(notes) if notes else None)


# ---- 磁盘扫描缓存：仓库 + 分支 + tree sha 未变即复用上次结果 ----------------
_CACHE_RECORD_FIELDS = (
    "repo", "branch", "path", "raw_url", "used_url", "name", "version", "author",
    "homepage", "description", "size", "sha256", "body_fp",
    "alive", "alive_endpoint", "alive_note",
)


def cache_key(full_name, branch) -> str:
    return "%s#%s" % (str(full_name or "").lower(), str(branch or "main").lower())


def cache_record(record) -> dict:
    """缓存前裁剪记录（不保存脚本内容，下载时按需重新抓取）。"""
    item = {field: record.get(field) for field in _CACHE_RECORD_FIELDS if field in record}
    item["data"] = None
    return item


def load_scan_cache(path=None) -> dict:
    """读取磁盘缓存，返回 {仓库#分支: {tree_sha, records: [...]}}（异常时返回空表）。"""
    target = Path(path) if path else DISCOVER_CACHE_FILE
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(data, dict) or int(data.get("version") or 0) != CACHE_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def save_scan_cache(cache, path=None) -> str:
    """写回磁盘缓存，返回缓存文件路径（失败返回空串）。"""
    target = Path(path) if path else DISCOVER_CACHE_FILE
    payload = {
        "version": CACHE_VERSION,
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entries": cache,
    }
    try:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(target)
    except Exception as exc:
        core.log("[警告] 写入扫描缓存失败：%s" % exc)
        return ""


def _dedupe_within_repo(records) -> list:
    """同一仓库内按内容 SHA-256（退化时按路径）去重。"""
    out, seen = [], set()
    for record in records:
        key = str(record.get("sha256") or "") or str(record.get("path") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


def _reuse_cached_records(entry):
    """从缓存条目还原记录（清空正文，只保留元数据），并做仓内去重。

    若缓存记录缺少存活探测字段（旧版本缓存），统一标注「无法判定」并提示，
    避免用户把缺失误认为未探测。
    """
    records = []
    for item in entry.get("records") or []:
        record = dict(item)
        record["data"] = None
        if not record.get("alive"):
            record["alive"] = core.STATUS_UNKNOWN
            record["alive_endpoint"] = ""
            record["alive_note"] = "缓存记录无存活信息，请「强制刷新」重扫后重新探测"
        records.append(record)
    return _dedupe_within_repo(records)


def scan_repos(repos, limit, timeout, stop_event=None, progress=None, keep_data=True,
               repo_workers=DEFAULT_REPO_WORKERS, file_workers=DEFAULT_FILE_WORKERS,
               cache=None, force_refresh=False, cache_path=None,
               liveness_cache=None, probe_timeout=5.0):
    """并发扫描多个仓库并按「仓库 + 内容」初步去重，返回 (records, messages)。

    提速策略：
      * 仓库级并发（repo_workers，默认 6）+ 仓库内文件级并发（file_workers，默认 8）；
      * 磁盘缓存：仓库 + 分支 + tree sha 未变则直接复用上次扫描结果（force_refresh=True 强制重扫）；
      * 跨仓库的同名音源仍会保留，以便后续去重时标注「重复收录」的来源仓库。
    """
    started = time.time()
    repo_list = [dict(item) for item in repos]
    if cache is None:
        cache = load_scan_cache(cache_path)
    outcomes = {}
    cache_changed = False

    def scan_one(repo):
        full_name = repo.get("full_name") or ""
        branch = repo.get("branch") or "main"
        began = time.time()
        outcome = {"full_name": full_name, "branch": branch, "records": [], "note": "",
                   "reused": False, "elapsed": 0.0, "stopped": False, "tree_sha": "",
                   "commit_sha": "", "cache_dirty": False}
        if stop_event is not None and stop_event.is_set():
            outcome["note"] = "已被用户停止"
            outcome["stopped"] = True
            return outcome
        if progress:
            progress(">>> 扫描仓库 %s (%s)" % (full_name, branch))
        entry = cache.get(cache_key(full_name, branch)) or {}
        # 仅当存在可复用缓存时才去比对分支 commit（避免冷启动白等网络超时）
        commit_sha = ""
        if entry and not force_refresh:
            commit_sha = fetch_repo_commit_sha_cached(full_name, branch, min(timeout, 5.0))
        outcome["commit_sha"] = commit_sha
        if (not force_refresh) and commit_sha and entry.get("commit_sha") == commit_sha:
            outcome["records"] = _reuse_cached_records(entry)
            outcome["reused"] = True
            outcome["note"] = "缓存命中：分支 commit 未变化，已跳过抓取（未调用 API）"
            outcome["elapsed"] = time.time() - began
            if progress:
                progress("[%s] 缓存命中，复用上次 %d 个音源" % (full_name, len(outcome["records"])))
            return outcome
        tree_sha, paths, err, tree_source = fetch_repo_tree(full_name, branch, timeout)
        if err:
            outcome["note"] = err
            outcome["elapsed"] = time.time() - began
            return outcome
        outcome["tree_sha"] = tree_sha
        if tree_source and tree_source != "api":
            outcome["note"] = "文件树来源：%s（GitHub API 不可用时的兜底）" % tree_source
            # 兜底清单可能滞后/不全，不写入可复用的 commit 指纹，下次仍走真实抓取
            outcome["commit_sha"] = ""
            commit_sha = ""
        if (not force_refresh) and tree_sha and entry.get("tree_sha") == tree_sha:
            outcome["records"] = _reuse_cached_records(entry)
            outcome["reused"] = True
            outcome["note"] = "缓存命中：文件树未变化，已跳过抓取"
            outcome["cache_dirty"] = bool(commit_sha) and entry.get("commit_sha") != commit_sha
            outcome["elapsed"] = time.time() - began
            if progress:
                progress("[%s] 缓存命中，复用上次 %d 个音源" % (full_name, len(outcome["records"])))
            return outcome
        records, note = scan_repo(
            full_name, branch, limit, timeout,
            stop_event=stop_event, progress=progress, keep_data=keep_data,
            file_workers=file_workers, paths=paths, tree_sha=tree_sha,
            liveness_cache=liveness_cache, probe_timeout=probe_timeout,
        )
        outcome["records"] = _dedupe_within_repo(records)
        if note:
            outcome["note"] = (outcome["note"] + "；" + note) if outcome["note"] else note
        outcome["elapsed"] = time.time() - began
        return outcome

    workers = max(1, int(repo_workers or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(scan_one, repo): index for index, repo in enumerate(repo_list)}
        for future in concurrent.futures.as_completed(futures):
            outcomes[futures[future]] = future.result()

    all_records, messages, seen = [], [], set()
    reused_count = 0
    for index, repo in enumerate(repo_list):
        outcome = outcomes.get(index)
        if outcome is None:
            continue
        if outcome.get("stopped"):
            messages.append("已被用户停止")
            break
        full_name = outcome["full_name"]
        records = outcome["records"]
        if outcome.get("reused"):
            reused_count += 1
            if outcome.get("cache_dirty"):
                # 文件树未变但分支 commit 已更新：仅刷新 commit 记录，避免下次重复调 API
                cache[cache_key(full_name, outcome["branch"])] = {
                    "tree_sha": outcome["tree_sha"],
                    "commit_sha": outcome["commit_sha"],
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(records),
                    "records": [cache_record(record) for record in records],
                }
                cache_changed = True
        elif not outcome["note"].startswith("读取仓库文件树失败") and not outcome["note"].startswith("仓库文件树格式异常"):
            cache[cache_key(full_name, outcome["branch"])] = {
                "tree_sha": outcome["tree_sha"],
                "commit_sha": outcome.get("commit_sha") or "",
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(records),
                "records": [cache_record(record) for record in records],
            }
            cache_changed = True
        for record in records:
            key = (str(record.get("repo") or "").lower(), record.get("sha256") or "")
            if key in seen:
                continue
            seen.add(key)
            all_records.append(record)
        messages.append("%s：发现 %d 个音源，耗时 %.1fs%s"
                        % (full_name, len(records), outcome["elapsed"],
                           ("（%s）" % outcome["note"]) if outcome["note"] else ""))

    if cache_changed:
        saved = save_scan_cache(cache, cache_path)
        if saved:
            messages.append("扫描缓存已更新：%s" % saved)
    elapsed = time.time() - started
    messages.append("扫描总耗时 %.1fs（%d 个仓库，仓库并发 %d / 文件并发 %d，缓存命中 %d 个，单文件超时 %.0fs）"
                    % (elapsed, len(repo_list), workers, max(1, int(file_workers or 1)),
                       reused_count, float(timeout)))
    return all_records, messages


def download_records(records, target_dir, timeout, stop_event=None, progress=None,
                     force_dead=False):
    """把检索到的音源写入目标目录，返回统计信息。

    去重与冲突规则：
      * 内容 SHA-256 去重：同一批次内内容相同的音源只落盘一次；
      * 目录中已存在同名文件且内容相同 -> 跳过，不重复落盘；
      * 目录中已存在同名文件但内容不同 -> 标注“冲突”，另存为 xxx_冲突N.js，不覆盖原文件。
    存活规则（下载前校验）：
      * 记录带 alive=失效 时默认自动跳过（不下载）；force_dead=True 时强制下载；
      * alive=可用/无法判定 正常下载。
    """
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    preexisting = {}
    try:
        for entry in target.iterdir():
            if entry.is_file():
                preexisting[entry.name.lower()] = entry
    except OSError:
        preexisting = {}

    written_names = set()
    seen_hashes = {}  # SHA-256 -> 已落盘文件名
    ok = failed = skipped = duplicate = conflict = dead_skipped = 0
    details = []

    def unique_name(stem, suffix, avoid):
        candidate = "%s%s" % (stem, suffix)
        counter = 1
        while candidate.lower() in avoid:
            counter += 1
            candidate = "%s_%d%s" % (stem, counter, suffix)
        return candidate

    def save(candidate, blob, record_label, status, note):
        nonlocal ok, failed, conflict
        try:
            (target / candidate).write_bytes(blob)
        except OSError as exc:
            failed += 1
            details.append({"name": record_label, "file": candidate, "status": "failed",
                            "note": "写入失败：%s" % exc})
            if progress:
                progress("  x 写入失败：%s（%s）" % (candidate, exc))
            return False
        written_names.add(candidate.lower())
        preexisting[candidate.lower()] = target / candidate
        details.append({"name": record_label, "file": candidate, "status": status, "note": note})
        return True

    for record in records:
        if stop_event is not None and stop_event.is_set():
            break
        label = (record.get("name") or "").strip() or record["path"].rsplit("/", 1)[-1]
        if not label.lower().endswith(".js"):
            label += ".js"
        record_label = record.get("name") or label
        filename = sanitize_filename(label[:80])
        stem, suffix = os.path.splitext(filename)

        # 0) 下载前存活校验：失效默认跳过，可强制下载
        if record.get("alive") == core.STATUS_DEAD and not force_dead:
            dead_skipped += 1
            note = "存活校验为「失效」，已自动跳过（%s）" % (record.get("alive_note") or "接口探测失败")
            details.append({"name": record_label, "file": filename, "status": "dead_skipped", "note": note})
            if progress:
                progress("  - 已跳过失效音源：%s（%s）"
                         % (record_label, record.get("alive_note") or "接口探测失败"))
            continue

        blob = record.get("data")
        if blob is None:
            probe = {"key": stem, "url": record.get("raw_url") or record.get("used_url")}
            blob, text, used_url, errors = core.fetch_with_mirrors(
                probe, core.BUILTIN_MIRRORS, timeout, 0, max_mirror_try=MAX_MIRROR_TRY
            )
            if blob is None:
                failed += 1
                details.append({"name": record_label, "file": filename, "status": "failed",
                                "note": "下载失败"})
                if progress:
                    progress("  x 下载失败：%s" % record_label)
                continue

        digest = core.sha256_bytes(blob)

        # 1) 本批次内已落盘同内容 -> 不重复落盘
        if digest in seen_hashes:
            duplicate += 1
            note = "与本次已下载的 %s 内容相同（SHA-256），未重复落盘" % seen_hashes[digest]
            details.append({"name": record_label, "file": filename, "status": "duplicate", "note": note})
            if progress:
                progress("  = 跳过同内容音源：%s（同 %s）" % (record_label, seen_hashes[digest]))
            continue

        # 2) 目录中已存在的同名文件
        if filename.lower() in preexisting and filename.lower() not in written_names:
            existing = preexisting[filename.lower()]
            existing_bytes = b""
            try:
                existing_bytes = existing.read_bytes()
            except OSError:
                existing_bytes = b""
            if existing_bytes and core.sha256_bytes(existing_bytes) == digest:
                skipped += 1
                seen_hashes[digest] = existing.name
                note = "下载目录中已存在同名同内容文件，已跳过"
                details.append({"name": record_label, "file": existing.name, "status": "skipped",
                                "note": note})
                if progress:
                    progress("  = 已存在同内容文件，跳过：%s" % existing.name)
                continue
            conflict += 1
            conflict_name = unique_name(stem + "_冲突", suffix,
                                       written_names | set(preexisting))
            note = "与目录中已有文件 %s 同名但内容不同，已另存为 %s" % (filename, conflict_name)
            if save(conflict_name, blob, record_label, "conflict", note):
                seen_hashes[digest] = conflict_name
                if progress:
                    progress("  ! 同名冲突：%s 与已有文件内容不同，另存为 %s" % (filename, conflict_name))
            continue

        # 3) 正常落盘（同名重复出现时自动加序号，绝不覆盖已有文件）
        candidate = unique_name(stem, suffix, written_names | set(preexisting))
        if save(candidate, blob, record_label, "ok", "已保存"):
            ok += 1
            seen_hashes[digest] = candidate
            if progress:
                progress("  + 已保存 %s  ->  %s" % (record_label, target / candidate))

    return {
        "dir": str(target),
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "duplicate": duplicate,
        "conflict": conflict,
        "dead_skipped": dead_skipped,
        "written": ok + conflict,
        "details": details,
    }


def format_discover_table(records) -> str:
    lines = []
    lines.append("%-26s %-22s %-9s %-12s %-7s %-20s %s"
                 % ("来源仓库", "文件名", "版本", "作者", "存活", "音源名称", "备注"))
    lines.append("-" * 138)
    for record in records:
        lines.append("%-26s %-22s %-9s %-12s %-7s %-20s %s" % (
            record["repo"][:26],
            record["path"].rsplit("/", 1)[-1][:22],
            (record["version"] or "-")[:9],
            (record["author"] or "-")[:12],
            (record.get("alive") or "-")[:7],
            str(record["name"])[:20],
            record.get("note") or "",
        ))
    return "\n".join(lines)


def json_record(record) -> dict:
    """把检索记录转换为可 JSON 序列化的字典（剔除脚本内容）。"""
    out = {k: v for k, v in record.items() if k != "data"}
    also_in = out.get("also_in")
    if isinstance(also_in, list):
        out["also_in"] = [{k: v for k, v in item.items() if k != "data"} for item in also_in]
    return out


def cli_discover(argv) -> int:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="音源检索：扫描候选 GitHub 仓库，解析 @name/@version/@author 并批量下载洛雪音乐音源脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  洛雪音源工具箱.exe --discover                      仅列出发现的音源（默认三级去重显示）\n"
            "  洛雪音源工具箱.exe --discover --no-dedupe          显示全部结果（含各仓库重复收录的音源）\n"
            "  洛雪音源工具箱.exe --discover --force-refresh      忽略磁盘缓存，强制重新扫描\n"
            "  洛雪音源工具箱.exe --discover --repo-workers 6 --file-workers 8 --timeout 8\n"
            "  洛雪音源工具箱.exe --discover --token <GitHubToken>   提升 API 限额\n"
            "  洛雪音源工具箱.exe --discover --download --dir \"D:\\lx-sources\"\n"
            "  洛雪音源工具箱.exe --discover --json\n"
        ),
    )
    parser.add_argument("--repos", default=str(DISCOVER_REPOS_FILE), help="候选仓库列表 JSON 路径")
    parser.add_argument("--limit", type=int, default=40, help="每个仓库最多解析的候选脚本数")
    parser.add_argument("--timeout", type=float, default=DEFAULT_FIND_TIMEOUT,
                        help="单文件抓取超时秒数（默认 %.0f 秒，超时快速切换镜像）" % DEFAULT_FIND_TIMEOUT)
    parser.add_argument("--repo-workers", type=int, default=DEFAULT_REPO_WORKERS,
                        help="仓库级并发线程数（默认 %d）" % DEFAULT_REPO_WORKERS)
    parser.add_argument("--file-workers", type=int, default=DEFAULT_FILE_WORKERS,
                        help="仓库内文件级并发线程数（默认 %d）" % DEFAULT_FILE_WORKERS)
    parser.add_argument("--force-refresh", action="store_true",
                        help="忽略磁盘扫描缓存，强制重新抓取全部仓库")
    parser.add_argument("--no-cache", action="store_true", help="本次不读写磁盘扫描缓存")
    parser.add_argument("--token", default="", help="可选 GitHub Token（提升 API 限额）")
    parser.add_argument("--dir", default=None, help="下载目标目录（配合 --download）")
    parser.add_argument("--download", action="store_true", help="下载全部发现的音源")
    parser.add_argument("--no-dedupe", action="store_true", help="关闭按 @name 去重（显示全部结果）")
    parser.add_argument("--download-dead", action="store_true",
                        help="强制下载存活校验为「失效」的音源（默认自动跳过）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    core.setup_console()
    core.set_log_to_stdout(not args.json)

    repos_path = Path(args.repos)
    if repos_path.is_file():
        try:
            # utf-8-sig 兼容记事本等编辑器写入的 BOM 头
            raw = json.loads(repos_path.read_text(encoding="utf-8-sig"))
            repos = _clean_repos(raw.get("repos") if isinstance(raw, dict) else raw)
        except Exception as exc:
            core.fatal("解析候选仓库列表失败（%s）：%s" % (repos_path, exc))
    else:
        repos = [dict(item) for item in BUILTIN_DISCOVER_REPOS]
    if not repos:
        core.fatal("候选仓库列表为空")

    if args.token:
        set_github_token(args.token)
    liveness_cache = core.load_liveness_cache()
    scan_started = time.time()
    records, messages = scan_repos(
        repos, args.limit, args.timeout, progress=core.log,
        repo_workers=args.repo_workers, file_workers=args.file_workers,
        force_refresh=args.force_refresh,
        cache_path=(None if args.no_cache else DISCOVER_CACHE_FILE),
        liveness_cache=liveness_cache, probe_timeout=5.0,
    )
    saved_live = core.save_liveness_cache(liveness_cache)
    if saved_live:
        messages.append("存活探测缓存已更新：%s" % saved_live)
    scan_seconds = time.time() - scan_started
    for message in messages:
        core.log("· " + message)

    if args.no_dedupe:
        shown = annotate_records(records)
        summary_text = "已关闭去重：共显示 %d 条结果" % len(shown)
    else:
        shown = dedupe_records(records)
        summary_text = dedupe_summary(records, shown)

    result = {
        "repos": len(repos),
        "found": len(records),
        "shown": len(shown),
        "dedupe": not args.no_dedupe,
        "scan_seconds": round(scan_seconds, 2),
        "messages": messages,
        "records": [json_record(record) for record in shown],
    }

    if args.download and shown:
        target_dir = Path(args.dir) if args.dir else (APP_DIR / "discovered")
        stats = download_records(shown, target_dir, args.timeout,
                                 progress=core.log, force_dead=args.download_dead)
        result["download"] = stats

    if args.json:
        core.emit(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        core.emit("")
        core.emit(format_discover_table(shown))
        core.emit("")
        core.emit(summary_text)
        core.emit("共发现 %d 个音源（扫描 %d 个候选仓库）" % (len(records), len(repos)))
        if result.get("download"):
            stats = result["download"]
            core.emit("下载完成：新增 %d / 已存在同内容跳过 %d / 同内容重复跳过 %d / 冲突另存 %d / 失效跳过 %d / 失败 %d -> %s"
                      % (stats["ok"], stats["skipped"], stats["duplicate"],
                         stats["conflict"], stats.get("dead_skipped", 0), stats["failed"],
                         stats["dir"]))
            for item in stats.get("details", []):
                if item.get("status") in ("conflict", "failed", "dead_skipped"):
                    core.emit("  - [%s] %s：%s" % (item["status"], item["file"], item["note"]))
    return 0



# --------------------------------------------------------------------------
# 图形界面
# --------------------------------------------------------------------------


def hide_console() -> None:
    """打包后双击启动 GUI 时隐藏控制台窗口（CLI 模式不受影响）。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetConsoleWindow()
        if handle:
            ctypes.windll.user32.ShowWindow(handle, 0)
    except Exception:
        pass


def show_console() -> None:
    """显式传入 --console 时重新显示控制台窗口（CLI 调试用）。"""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetConsoleWindow()
        if handle:
            ctypes.windll.user32.ShowWindow(handle, 5)  # SW_SHOW
    except Exception:
        pass


def bundled_icon_path():
    """返回洛雪官方图标（app_icon.ico）的实际路径，找不到时返回 None。"""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / ICON_NAME)
    candidates.append(Path(__file__).resolve().parent / ICON_NAME)
    candidates.append(APP_DIR / ICON_NAME)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _set_exact_sized_icons(root, path) -> None:
    """Windows 下用 WM_SETICON 把官方 ico 的原始尺寸图标（16x16 / 32x32）逐像素贴到窗口上。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        user32 = ctypes.windll.user32
        root.update_idletasks()
        hwnd = int(root.winfo_id())
        parent = user32.GetParent(hwnd)  # Tk 顶层窗口的父窗口才是带标题栏的窗口
        targets = [h for h in (parent, hwnd) if h]
        for size, which in ((16, ICON_SMALL), (32, ICON_BIG)):
            hicon = user32.LoadImageW(None, str(path), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if not hicon:
                continue
            for target in targets:
                user32.SendMessageW(target, WM_SETICON, which, hicon)
    except Exception:
        pass


def apply_window_icon(root) -> None:
    """把官方图标应用到 Tk 窗口，使标题栏与任务栏显示洛雪图标。"""
    path = bundled_icon_path()
    if path is None:
        return
    ok = False
    try:
        root.iconbitmap(default=str(path))
        ok = True
    except Exception:
        ok = False
    if not ok:
        try:
            root.iconbitmap(str(path))
            ok = True
        except Exception:
            ok = False
    if ok:
        _set_exact_sized_icons(root, path)


class LxToolkitApp:
    def __init__(self, root):
        self.root = root
        self.ui_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.results = []
        self.discovered = []
        self.find_view = []
        self.checked = set()
        self.checked_hashes = set()
        self.repos = load_discover_repos()
        self._orig_core_log = core.log

        root.title("%s v%s" % (APP_NAME, APP_VERSION))
        root.geometry("1120x740")
        root.minsize(940, 640)

        try:
            style = ttk.Style()
            for theme in ("vista", "winnative", "clam"):
                if theme in style.theme_names():
                    style.theme_use(theme)
                    break
            style.configure("Treeview", rowheight=24)
        except Exception:
            pass

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_check = ttk.Frame(notebook)
        self.tab_find = ttk.Frame(notebook)
        notebook.add(self.tab_check, text="  音源校验升级  ")
        notebook.add(self.tab_find, text="  音源检索  ")

        self._build_check_tab(self.tab_check)
        self._build_find_tab(self.tab_find)

        root.after(80, self._drain_ui_queue)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log_check("就绪。校验将全量在线扫描全部候选仓库并逐条播放级探测；点击“开始校验”。")
        self._log_find("就绪。勾选候选仓库后点击“扫描仓库”，可检索并批量下载音源脚本。")

    # ------------------------------------------------------------------
    # 线程 / UI 队列
    # ------------------------------------------------------------------

    def _post(self, func):
        self.ui_queue.put(func)

    def _drain_ui_queue(self):
        while True:
            try:
                func = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func()
            except Exception as exc:
                try:
                    self._log_check("[界面异常] %s" % exc)
                except Exception:
                    pass
        self.root.after(80, self._drain_ui_queue)

    def _on_close(self):
        self.stop_event.set()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _log_check(self, message):
        self.txt_check.configure(state="normal")
        self.txt_check.insert("end", str(message) + "\n")
        self.txt_check.see("end")
        self.txt_check.configure(state="disabled")

    def _log_find(self, message):
        self.txt_find.configure(state="normal")
        self.txt_find.insert("end", str(message) + "\n")
        self.txt_find.see("end")
        self.txt_find.configure(state="disabled")

    # ------------------------------------------------------------------
    # 标签页一：音源校验升级
    # ------------------------------------------------------------------

    def _build_check_tab(self, parent):
        self.var_dir = tk.StringVar(value="")
        self.var_timeout = tk.StringVar(value="20")
        self.var_retries = tk.StringVar(value="1")

        cfg = ttk.LabelFrame(parent, text="配置")
        cfg.pack(fill="x", padx=6, pady=(6, 4))

        row0 = ttk.Frame(cfg)
        row0.pack(fill="x", padx=6, pady=3)
        ttk.Label(row0, text="目标目录（已下载音源）：", width=18).pack(side="left")
        ttk.Entry(row0, textvariable=self.var_dir).pack(side="left", fill="x", expand=True)
        ttk.Button(row0, text="浏览…", width=8, command=self._pick_dir).pack(side="left", padx=4)
        ttk.Button(row0, text="自动探测", width=9, command=self._auto_detect_dir).pack(side="left")

        row2 = ttk.Frame(cfg)
        row2.pack(fill="x", padx=6, pady=3)
        ttk.Label(row2, text="超时(秒)：", width=18).pack(side="left")
        ttk.Spinbox(row2, from_=3, to=180, width=6, textvariable=self.var_timeout).pack(side="left")
        ttk.Label(row2, text="    重试次数：").pack(side="left")
        ttk.Spinbox(row2, from_=0, to=5, width=4, textvariable=self.var_retries).pack(side="left")

        row3 = ttk.Frame(cfg)
        row3.pack(fill="x", padx=6, pady=3)
        ttk.Label(row3, text="校验模式：", width=14).pack(side="left")
        ttk.Label(row3, text="纯在线播放级校验（默认）", foreground="#1a7f37").pack(side="left")
        ttk.Button(row3, text="清空存活缓存", width=12,
                   command=self._clear_live_cache_check).pack(side="left", padx=8)

        row4 = ttk.Frame(cfg)
        row4.pack(fill="x", padx=6, pady=(3, 6))
        self.btn_check = ttk.Button(row4, text="开始校验", width=12, command=self._on_check)
        self.btn_check.pack(side="left")
        self.btn_clean_dead = ttk.Button(row4, text="一键清除失效音源", width=18, command=self._on_clean_dead)
        self.btn_clean_dead.pack(side="left", padx=6)
        self.btn_upgrade_sel = ttk.Button(row4, text="一键升级", width=12, command=self._on_upgrade_selected)
        self.btn_upgrade_sel.pack(side="left")
        self.btn_stop = ttk.Button(row4, text="停止", width=8, command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(row4, text="清空日志", width=10, command=self._clear_check_log).pack(side="right")

        table = ttk.Frame(parent)
        table.pack(fill="both", expand=True, padx=6, pady=4)
        columns = ("name", "version", "repos", "alive", "basis", "endpoint", "sha256")
        headings = ("名称", "版本", "收录仓库数", "存活状态", "依据", "API端点", "远端SHA256")
        widths = (170, 90, 85, 95, 240, 270, 280)
        self.tree_check = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended")
        for col, head, width in zip(columns, headings, widths):
            self.tree_check.heading(col, text=head)
            self.tree_check.column(col, width=width, anchor="w", stretch=(col in ("basis", "endpoint", "sha256")))
        bar = ttk.Scrollbar(table, orient="vertical", command=self.tree_check.yview)
        self.tree_check.configure(yscrollcommand=bar.set)
        self.tree_check.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for status, color in STATUS_COLORS.items():
            self.tree_check.tag_configure(status, foreground=color)

        self.lbl_summary = ttk.Label(parent, text="尚未执行校验。", anchor="w")
        self.lbl_summary.pack(fill="x", padx=8, pady=(0, 2))

        log_frame = ttk.LabelFrame(parent, text="运行日志")
        log_frame.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.txt_check = ScrolledText(log_frame, height=9, wrap="word", state="disabled")
        self.txt_check.pack(fill="both", expand=True, padx=4, pady=4)

    def _clear_check_log(self):
        self.txt_check.configure(state="normal")
        self.txt_check.delete("1.0", "end")
        self.txt_check.configure(state="disabled")

    def _clear_live_cache_check(self):
        path = core.clear_liveness_cache()
        self._log_check("已清空存活探测缓存：%s" % path)

    def _clear_live_cache_find(self):
        path = core.clear_liveness_cache()
        self._log_find("已清空存活探测缓存：%s" % path)

    def _pick_dir(self):
        initial = self.var_dir.get().strip() or str(core.DEFAULT_LOCAL_DIR)
        chosen = filedialog.askdirectory(title="选择本地音源目录", initialdir=initial)
        if chosen:
            self.var_dir.set(chosen)

    def _auto_detect_dir(self):
        detected = core.detect_lx_music_dir()
        if detected:
            self.var_dir.set(str(detected))
            self._log_check("已自动探测到洛雪音乐数据目录：%s" % detected)
        else:
            target = self.var_dir.get().strip() or str(core.DEFAULT_LOCAL_DIR)
            self.var_dir.set(target)
            self._log_check("未检测到本机洛雪音乐数据目录，将使用：%s" % target)

    # ------------------------------------------------------------------
    # 校验 / 升级流程
    # ------------------------------------------------------------------

    def _on_check(self):
        self._start_check()

    def _on_clean_dead(self):
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "当前已有任务在执行，请稍候或点击“停止”。")
            return
        target = self._resolve_target_dir()
        if target is None:
            return
        self.stop_event.clear()
        self._set_check_busy(True)
        self._log_check("-" * 60)
        self._log_check("开始执行：一键清除失效音源（目标目录：%s）" % target)
        self.worker = threading.Thread(
            target=self._clean_dead_worker, args=(target,), daemon=True
        )
        self.worker.start()

    def _on_upgrade_selected(self):
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "当前已有任务在执行，请稍候或点击“停止”。")
            return
        indices = set()
        for iid in self.tree_check.selection():
            try:
                indices.add(int(iid))
            except (ValueError, TypeError):
                continue
        if not indices:
            messagebox.showinfo(APP_NAME, "请先在列表中选中要升级的音源（可按住 Ctrl / Shift 多选）。")
            return
        results = getattr(self, "results", None)
        if not results:
            messagebox.showinfo(APP_NAME, "请先执行「开始校验」获取在线结果列表。")
            return
        selected = [r for r in results if r.get("_index") in indices]
        if not selected:
            messagebox.showinfo(APP_NAME, "选中的条目没有有效记录，请重新选择。")
            return
        target = self._resolve_target_dir()
        if target is None:
            return
        self.stop_event.clear()
        self._set_check_busy(True)
        self._log_check("-" * 60)
        self._log_check("开始执行：一键升级（选中 %d 条，目标目录：%s）" % (len(selected), target))
        self.worker = threading.Thread(
            target=self._upgrade_worker, args=(selected, target), daemon=True
        )
        self.worker.start()

    def _resolve_target_dir(self):
        raw = self.var_dir.get().strip()
        local_dir, local_desc = core.resolve_local_dir(raw or None)
        target = Path(local_dir)
        if not target.is_dir():
            if messagebox.askyesno(APP_NAME, "目标目录不存在：\n%s\n\n是否自动创建？" % target):
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    messagebox.showerror(APP_NAME, "创建目录失败：%s" % exc)
                    return None
            else:
                return None
        return target

    def _on_stop(self):
        self.stop_event.set()
        self._log_check("已请求停止，正在结束当前任务…")

    def _start_check(self):
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "当前已有任务在执行，请稍候或点击“停止”。")
            return
        self.stop_event.clear()
        self._set_check_busy(True)
        self._log_check("-" * 60)
        self._log_check("开始执行：全量在线播放级校验（仅检查，不写文件）")
        self.worker = threading.Thread(target=self._check_worker, daemon=True)
        self.worker.start()

    def _set_check_busy(self, busy):
        state = "disabled" if busy else "normal"
        for btn in (self.btn_check, self.btn_clean_dead, self.btn_upgrade_sel):
            btn.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _finish_check(self):
        self._set_check_busy(False)

    def _check_worker(self):
        core.set_log_to_stdout(False)
        redirect_core_log(lambda message: self._post(lambda m=message: self._log_check(m)))
        try:
            raw_dir = self.var_dir.get().strip()
            local_dir, local_desc = core.resolve_local_dir(raw_dir or None)
            local_dir = Path(local_dir)
            try:
                timeout = float(self.var_timeout.get())
            except Exception:
                timeout = 20.0
            try:
                retries = int(float(self.var_retries.get()))
            except Exception:
                retries = 1

            liveness_cache = core.load_liveness_cache()
            self._post(lambda: self._log_check("校验模式：全量在线播放级校验（扫描全部候选仓库）"))
            self._post(lambda: self._log_check("本地音源目录：%s（%s）" % (local_dir, local_desc)))
            if not local_dir.is_dir():
                self._post(lambda: self._log_check("[提示] 本地音源目录当前不存在，执行下载时会自动创建。"))

            ctx = {
                "check_only": True,
                "force": False,
                "sources_file": "全量在线扫描（全部候选仓库）",
                "local_dir": local_dir,
                "local_dir_source": local_desc,
                "local_dir_exists": local_dir.is_dir(),
                "mirrors": list(core.BUILTIN_MIRRORS),
                "timeout": timeout,
                "retries": retries,
                "probe_timeout": 5.0,
                "live_cache_ttl": core.LIVE_CACHE_TTL,
                "liveness_cache": liveness_cache,
                "report_dir": core.DEFAULT_REPORT_DIR,
                "state_file": core.DEFAULT_STATE_FILE,
                "backup_root": core.DEFAULT_BACKUP_ROOT,
            }

            repos = load_discover_repos()
            self._post(lambda n=len(repos): self._log_check("候选仓库：%d 个（全量在线扫描 + 去重 + 逐条播放级存活探测）" % n))
            records, messages = scan_repos(
                repos, 60, timeout, stop_event=self.stop_event,
                progress=lambda m: self._post(lambda mm=m: self._log_check(mm)),
                keep_data=True, liveness_cache=liveness_cache, probe_timeout=5.0,
            )
            for message in messages:
                self._post(lambda m=message: self._log_check("· " + m))
            merged = dedupe_records(records)
            self._post(lambda: self._log_check("去重完成：原始 %d 条 -> 去重后 %d 条音源" % (len(records), len(merged))))

            results = []
            for index, record in enumerate(merged):
                if self.stop_event.is_set():
                    break
                result = core.record_to_result(record)
                result["target_file"] = str(local_dir / record.get("path", "").rsplit("/", 1)[-1])
                result["_index"] = index
                results.append(result)
            self.results = results
            self._post(lambda: self._refresh_check_tree(results))

            saved_cache = core.save_liveness_cache(ctx.get("liveness_cache") or {})
            if saved_cache:
                self._post(lambda p=saved_cache: self._log_check("存活探测缓存已更新：%s" % p))

            summary = core.summarize(results)
            action = "纯在线播放级校验"
            outputs = core.write_outputs(ctx, results, summary, action)
            self._post(lambda: self._refresh_check_tree(results))
            self._post(lambda: self._report_summary(summary, outputs))
        except SystemExit as exc:
            self._post(lambda e=exc: self._log_check("[已终止] %s" % e))
        except Exception as exc:
            self._post(lambda e=exc: self._log_check("[异常] %s: %s" % (type(e).__name__, e)))
        finally:
            core.log = self._orig_core_log
            self._post(self._finish_check)

    def _clean_dead_worker(self, target):
        core.set_log_to_stdout(False)
        redirect_core_log(lambda message: self._post(lambda m=message: self._log_check(m)))
        try:
            try:
                timeout = float(self.var_timeout.get())
            except Exception:
                timeout = 20.0
            try:
                retries = int(float(self.var_retries.get()))
            except Exception:
                retries = 1
            liveness_cache = core.load_liveness_cache()
            stats = core.clean_dead(
                target, timeout=timeout, retries=retries,
                mirrors=list(core.BUILTIN_MIRRORS),
                stop_event=self.stop_event,
                progress=lambda m: self._post(lambda mm=m: self._log_check(mm)),
                probe_timeout=5.0, liveness_cache=liveness_cache,
            )
            core.save_liveness_cache(liveness_cache)
            self._post(lambda: self._log_check("=" * 60))
            self._post(lambda: self._log_check(
                "清除失效音源完成：共 %d 个，删除 %d，保留 %d，跳过 %d，失败 %d"
                % (stats["total"], stats["deleted"], stats["kept"],
                   stats["skipped"], stats["failed"])))
            self._post(lambda: self.lbl_summary.configure(text=
                "清除失效音源：共 %d 个，删除 %d，保留 %d，跳过 %d，失败 %d"
                % (stats["total"], stats["deleted"], stats["kept"],
                   stats["skipped"], stats["failed"])))
            if stats["deleted_files"]:
                self._post(lambda: self._log_check("已删除（移入回收站）："))
                for item in stats["deleted_files"]:
                    self._post(lambda it=item: self._log_check(
                        "  - %s：%s" % (it["name"], it["file"])))
            if stats["errors"]:
                self._post(lambda: self._log_check("失败："))
                for err in stats["errors"]:
                    self._post(lambda e=err: self._log_check("  x %s" % e))
        except SystemExit as exc:
            self._post(lambda e=exc: self._log_check("[已终止] %s" % e))
        except Exception as exc:
            self._post(lambda e=exc: self._log_check("[异常] %s: %s" % (type(e).__name__, e)))
        finally:
            core.log = self._orig_core_log
            self._post(self._finish_check)

    def _upgrade_worker(self, selected, target):
        core.set_log_to_stdout(False)
        redirect_core_log(lambda message: self._post(lambda m=message: self._log_check(m)))
        try:
            try:
                timeout = float(self.var_timeout.get())
            except Exception:
                timeout = 20.0
            try:
                retries = int(float(self.var_retries.get()))
            except Exception:
                retries = 1
            import datetime as _dt
            backup_dir = Path(core.DEFAULT_BACKUP_ROOT) / _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            liveness_cache = core.load_liveness_cache()
            stats = core.upgrade_selected(
                selected, target, timeout=timeout, retries=retries,
                mirrors=list(core.BUILTIN_MIRRORS),
                stop_event=self.stop_event,
                progress=lambda m: self._post(lambda mm=m: self._log_check(mm)),
                backup_dir=backup_dir, probe_timeout=5.0, liveness_cache=liveness_cache,
            )
            core.save_liveness_cache(liveness_cache)
            self._post(lambda: self._log_check("=" * 60))
            self._post(lambda: self._log_check(
                "一键升级完成：共 %d 个，升级 %d，跳过 %d，失败 %d"
                % (stats["total"], stats["upgraded"], stats["skipped"], stats["errors"])))
            self._post(lambda: self.lbl_summary.configure(text=
                "一键升级：成功 %d / 跳过 %d / 失败 %d（共 %d）"
                % (stats["upgraded"], stats["skipped"], stats["errors"], stats["total"])))
            for item in stats["upgraded_files"]:
                self._post(lambda it=item: self._log_check(
                    "  + 已升级：%s  %s -> %s  ->  %s%s"
                    % (it["name"], it.get("from_version") or "?",
                       it.get("to_version") or "?",
                       it["file"],
                       ("（备份 %s）" % Path(it["backup"]).name) if it.get("backup") else "")))
            for item in stats["skipped_details"]:
                self._post(lambda it=item: self._log_check(
                    "  - 跳过：%s（%s）" % (it["name"], it["reason"])))
            if stats["errors"]:
                self._post(lambda: self._log_check("失败："))
                for err in stats["errors"]:
                    self._post(lambda e=err: self._log_check("  x %s" % e))
        except SystemExit as exc:
            self._post(lambda e=exc: self._log_check("[已终止] %s" % e))
        except Exception as exc:
            self._post(lambda e=exc: self._log_check("[异常] %s: %s" % (type(e).__name__, e)))
        finally:
            core.log = self._orig_core_log
            self._post(self._finish_check)

    def _refresh_check_tree(self, results):
        self.tree_check.delete(*self.tree_check.get_children())
        for index, item in enumerate(results):
            self.tree_check.insert(
                "", "end", iid=str(index),
                values=(
                    item.get("name", ""),
                    item.get("remote_version") or "-",
                    item.get("repo_count", 1),
                    item.get("alive") or "-",
                    (item.get("alive_note") or item.get("note") or "-")[:220],
                    item.get("alive_endpoint") or "-",
                    item.get("remote_sha256") or "-",
                ),
                tags=(item.get("status", ""),),
            )

    def _report_summary(self, summary, outputs):
        parts = "  ".join("%s %s" % (status, summary.get(status, 0)) for status in core.ALL_STATUSES)
        alive_ok = sum(1 for r in getattr(self, "results", []) or [] if r.get("alive") == core.STATUS_AVAILABLE)
        alive_dead = sum(1 for r in getattr(self, "results", []) or [] if r.get("alive") == core.STATUS_DEAD)
        alive_unknown = sum(1 for r in getattr(self, "results", []) or [] if r.get("alive") == core.STATUS_UNKNOWN)
        text = "汇总：%s  |  音源总数 %s  |  存活: 可用 %s / 失效 %s / 无法判定 %s  |  本次升级/安装 %s" % (
            parts, summary.get("total", 0), alive_ok, alive_dead, alive_unknown,
            summary.get("upgraded", 0))
        self.lbl_summary.configure(text=text)
        self._log_check(text)
        self._log_check("状态文件：%s" % outputs.get("state_file", ""))
        self._log_check("校验报告：%s" % outputs.get("report_file", ""))
        self._log_check("任务结束。")



    # ------------------------------------------------------------------
    # 标签页二：音源检索
    # ------------------------------------------------------------------

    def _build_find_tab(self, parent):
        self.var_new_repo = tk.StringVar(value="")
        self.var_limit = tk.StringVar(value="40")
        self.var_find_timeout = tk.StringVar(value="%.0f" % DEFAULT_FIND_TIMEOUT)
        self.var_repo_workers = tk.StringVar(value=str(DEFAULT_REPO_WORKERS))
        self.var_file_workers = tk.StringVar(value=str(DEFAULT_FILE_WORKERS))
        self.var_force_refresh = tk.BooleanVar(value=False)
        self.var_github_token = tk.StringVar(value="")
        self.var_download_dir = tk.StringVar(value=str(DATA_DIR / "discovered"))
        self.var_download_force = tk.BooleanVar(value=False)

        top = ttk.Frame(parent)
        top.pack(fill="both", expand=False, padx=6, pady=(6, 4))

        left = ttk.LabelFrame(top, text="候选仓库（保存于 exe 同目录 discover_repos.json）")
        left.pack(side="left", fill="both", expand=True)
        repo_wrap = ttk.Frame(left)
        repo_wrap.pack(fill="both", expand=True, padx=4, pady=4)
        self.tree_repos = ttk.Treeview(repo_wrap, columns=("repo", "branch", "note"),
                                       show="headings", height=6, selectmode="extended")
        for col, head, width in (("repo", "仓库", 240), ("branch", "分支", 80), ("note", "备注", 200)):
            self.tree_repos.heading(col, text=head)
            self.tree_repos.column(col, width=width, anchor="w")
        repo_bar = ttk.Scrollbar(repo_wrap, orient="vertical", command=self.tree_repos.yview)
        self.tree_repos.configure(yscrollcommand=repo_bar.set)
        self.tree_repos.pack(side="left", fill="both", expand=True)
        repo_bar.pack(side="right", fill="y")

        repo_edit = ttk.Frame(left)
        repo_edit.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Entry(repo_edit, textvariable=self.var_new_repo).pack(side="left", fill="x", expand=True)
        ttk.Button(repo_edit, text="添加", width=6, command=self._add_repo).pack(side="left", padx=3)
        ttk.Button(repo_edit, text="删除选中", width=9, command=self._del_repo).pack(side="left")
        ttk.Button(repo_edit, text="恢复默认", width=9, command=self._reset_repos).pack(side="left", padx=3)

        right = ttk.LabelFrame(top, text="检索参数")
        right.pack(side="left", fill="y", padx=(6, 0))
        opt = ttk.Frame(right)
        opt.pack(fill="x", padx=6, pady=4)
        ttk.Label(opt, text="每仓库最多解析：").pack(side="left")
        ttk.Spinbox(opt, from_=1, to=200, width=5, textvariable=self.var_limit).pack(side="left")
        opt2 = ttk.Frame(right)
        opt2.pack(fill="x", padx=6, pady=4)
        ttk.Label(opt2, text="超时(秒)：").pack(side="left")
        ttk.Spinbox(opt2, from_=3, to=180, width=5, textvariable=self.var_find_timeout).pack(side="left")
        opt_conc = ttk.Frame(right)
        opt_conc.pack(fill="x", padx=6, pady=4)
        ttk.Label(opt_conc, text="仓库并发：").pack(side="left")
        ttk.Spinbox(opt_conc, from_=1, to=32, width=4,
                    textvariable=self.var_repo_workers).pack(side="left")
        ttk.Label(opt_conc, text="文件并发：").pack(side="left", padx=(8, 0))
        ttk.Spinbox(opt_conc, from_=1, to=32, width=4,
                    textvariable=self.var_file_workers).pack(side="left")
        opt_cache = ttk.Frame(right)
        opt_cache.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Checkbutton(opt_cache, text="强制刷新（忽略磁盘缓存重扫）",
                        variable=self.var_force_refresh).pack(side="left")
        opt_token = ttk.Frame(right)
        opt_token.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(opt_token, text="GitHub Token（可选）：").pack(side="left")
        ttk.Entry(opt_token, textvariable=self.var_github_token, width=26,
                  show="*").pack(side="left", fill="x", expand=True)
        opt3 = ttk.Frame(right)
        opt3.pack(fill="x", padx=6, pady=4)
        ttk.Label(opt3, text="下载目录：").pack(side="left")
        ttk.Entry(opt3, textvariable=self.var_download_dir, width=28).pack(side="left", fill="x", expand=True)
        ttk.Button(opt3, text="浏览…", width=8, command=self._pick_download_dir).pack(side="left", padx=3)
        opt4 = ttk.Frame(right)
        opt4.pack(fill="x", padx=6, pady=(4, 6))
        self.btn_scan = ttk.Button(opt4, text="扫描仓库", width=12, command=self._on_scan)
        self.btn_scan.pack(side="left")
        self.btn_scan_stop = ttk.Button(opt4, text="停止", width=8, command=self._on_stop, state="disabled")
        self.btn_scan_stop.pack(side="left", padx=4)

        dedupe_bar = ttk.Frame(parent)
        dedupe_bar.pack(fill="x", padx=8, pady=(2, 0))
        self.var_dedupe = tk.BooleanVar(value=True)
        ttk.Checkbutton(dedupe_bar, text="去重显示（名称归一 + 内容 SHA-256 + 主体指纹三级合并，仅保留版本最高/体积最大的一条）",
                        variable=self.var_dedupe,
                        command=self._on_toggle_dedupe).pack(side="left")
        ttk.Label(dedupe_bar, text="“备注”列显示收录仓库数、保留版本与其他版本号。",
                  foreground="#6b6b6b").pack(side="left", padx=10)

        action = ttk.Frame(parent)
        action.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(action, text="检索结果（点击首列方框勾选）：").pack(side="left")
        ttk.Button(action, text="全选", width=6, command=lambda: self._set_all_checked(True)).pack(side="left", padx=4)
        ttk.Button(action, text="全不选", width=8, command=lambda: self._set_all_checked(False)).pack(side="left")
        self.btn_download = ttk.Button(action, text="下载选中音源", width=16, command=self._on_download, state="disabled")
        self.btn_download.pack(side="left", padx=8)
        ttk.Checkbutton(action, text="强制下载失效项（默认只下存活）",
                        variable=self.var_download_force).pack(side="left")
        ttk.Button(action, text="清空存活缓存", width=12,
                   command=self._clear_live_cache_find).pack(side="left", padx=8)
        ttk.Button(action, text="清空日志", width=10, command=self._clear_find_log).pack(side="right")
        self.lbl_find = ttk.Label(action, text="尚未扫描。")
        self.lbl_find.pack(side="right", padx=10)

        table = ttk.Frame(parent)
        table.pack(fill="both", expand=True, padx=6, pady=4)
        columns = ("sel", "repo", "file", "name", "version", "alive", "repos", "author", "size", "note")
        headings = ("选中", "来源仓库", "文件名", "音源名称(@name)", "版本(@version)", "存活",
                    "收录仓数", "作者(@author)", "大小", "备注（收录仓库 / 保留版本 / 其他版本）")
        widths = (50, 170, 170, 170, 90, 80, 70, 90, 70, 300)
        self.tree_find = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended")
        for col, head, width in zip(columns, headings, widths):
            self.tree_find.heading(col, text=head)
            self.tree_find.column(col, width=width, anchor="w", stretch=(col in ("file", "name", "note")))
        for tag, color in FIND_TAG_COLORS.items():
            self.tree_find.tag_configure(tag, foreground=color)
        find_bar = ttk.Scrollbar(table, orient="vertical", command=self.tree_find.yview)
        self.tree_find.configure(yscrollcommand=find_bar.set)
        self.tree_find.pack(side="left", fill="both", expand=True)
        find_bar.pack(side="right", fill="y")
        self.tree_find.bind("<Button-1>", self._on_find_click)

        log_frame = ttk.LabelFrame(parent, text="检索日志")
        log_frame.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.txt_find = ScrolledText(log_frame, height=8, wrap="word", state="disabled")
        self.txt_find.pack(fill="both", expand=True, padx=4, pady=4)

        self._refresh_repo_tree()

    def _clear_find_log(self):
        self.txt_find.configure(state="normal")
        self.txt_find.delete("1.0", "end")
        self.txt_find.configure(state="disabled")

    def _refresh_repo_tree(self):
        self.tree_repos.delete(*self.tree_repos.get_children())
        for index, repo in enumerate(self.repos):
            self.tree_repos.insert("", "end", iid=str(index),
                                   values=(repo.get("full_name", ""), repo.get("branch", "main"),
                                           repo.get("note", "")))

    def _add_repo(self):
        text = self.var_new_repo.get().strip()
        if not text:
            return
        branch = "main"
        if "#" in text:
            text, branch = text.split("#", 1)
        elif "@" in text:
            text, branch = text.split("@", 1)
        full_name = text.strip().strip("/")
        if "/" not in full_name:
            messagebox.showwarning(APP_NAME, "请输入 owner/repo 形式，例如 pdone/lx-music-source")
            return
        if any(r["full_name"].lower() == full_name.lower() for r in self.repos):
            messagebox.showinfo(APP_NAME, "该仓库已在列表中。")
            return
        self.repos.append({"full_name": full_name, "branch": branch.strip() or "main", "note": "手动添加"})
        self.var_new_repo.set("")
        self._refresh_repo_tree()
        saved = save_discover_repos(self.repos)
        self._log_find("已添加仓库 %s（分支 %s）%s" % (full_name, branch, ("，已保存到 %s" % saved) if saved else ""))

    def _del_repo(self):
        indexes = sorted((int(iid) for iid in self.tree_repos.selection()), reverse=True)
        if not indexes:
            messagebox.showinfo(APP_NAME, "请先在候选仓库列表中选中要删除的仓库。")
            return
        for index in indexes:
            if 0 <= index < len(self.repos):
                self.repos.pop(index)
        self._refresh_repo_tree()
        saved = save_discover_repos(self.repos)
        self._log_find("已删除 %d 个候选仓库%s" % (len(indexes), ("，已保存到 %s" % saved) if saved else ""))

    def _reset_repos(self):
        self.repos = [dict(item) for item in BUILTIN_DISCOVER_REPOS]
        self._refresh_repo_tree()
        saved = save_discover_repos(self.repos)
        self._log_find("已恢复默认候选仓库列表（%d 个）%s" % (len(self.repos), ("，已保存到 %s" % saved) if saved else ""))

    def _pick_download_dir(self):
        initial = self.var_download_dir.get().strip() or str(DATA_DIR)
        chosen = filedialog.askdirectory(title="选择音源下载目录", initialdir=initial)
        if chosen:
            self.var_download_dir.set(chosen)

    # ------------------------------------------------------------------
    # 检索 / 下载流程
    # ------------------------------------------------------------------

    def _selected_repos(self):
        ids = self.tree_repos.selection()
        if not ids:
            return list(self.repos)
        picked = []
        for iid in ids:
            try:
                index = int(iid)
            except ValueError:
                continue
            if 0 <= index < len(self.repos):
                picked.append(self.repos[index])
        return picked or list(self.repos)

    def _on_scan(self):
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "当前已有任务在执行，请稍候或点击“停止”。")
            return
        repos = self._selected_repos()
        if not repos:
            messagebox.showinfo(APP_NAME, "候选仓库列表为空，请先添加仓库。")
            return
        try:
            limit = int(float(self.var_limit.get()))
        except Exception:
            limit = 40
        try:
            timeout = float(self.var_find_timeout.get())
        except Exception:
            timeout = DEFAULT_FIND_TIMEOUT
        try:
            repo_workers = max(1, int(float(self.var_repo_workers.get())))
        except Exception:
            repo_workers = DEFAULT_REPO_WORKERS
        try:
            file_workers = max(1, int(float(self.var_file_workers.get())))
        except Exception:
            file_workers = DEFAULT_FILE_WORKERS
        force_refresh = bool(self.var_force_refresh.get())
        token = self.var_github_token.get().strip()
        set_github_token(token)
        self.stop_event.clear()
        self._set_find_busy(True)
        self._log_find("-" * 60)
        self._log_find("开始扫描 %d 个候选仓库（每仓库最多 %d 个候选脚本，单文件超时 %.0fs，"
                       "仓库并发 %d / 文件并发 %d%s%s）…"
                       % (len(repos), limit, timeout, repo_workers, file_workers,
                          "，强制刷新" if force_refresh else "，启用磁盘缓存",
                          "，已启用 GitHub Token" if token else ""))
        self.worker = threading.Thread(
            target=self._scan_worker,
            args=(repos, limit, timeout, repo_workers, file_workers, force_refresh),
            daemon=True)
        self.worker.start()

    def _set_find_busy(self, busy):
        self.btn_scan.configure(state="disabled" if busy else "normal")
        self.btn_scan_stop.configure(state="normal" if busy else "disabled")
        self.btn_download.configure(
            state=("disabled" if busy else ("normal" if self.discovered else "disabled")))

    def _scan_worker(self, repos, limit, timeout, repo_workers=DEFAULT_REPO_WORKERS,
                     file_workers=DEFAULT_FILE_WORKERS, force_refresh=False):
        core.set_log_to_stdout(False)
        redirect_core_log(lambda message: self._post(lambda m=message: self._log_find(m)))
        try:
            liveness_cache = core.load_liveness_cache()
            records, messages = scan_repos(
                repos, limit, timeout, stop_event=self.stop_event,
                progress=lambda m: self._post(lambda mm=m: self._log_find(mm)),
                repo_workers=repo_workers, file_workers=file_workers,
                force_refresh=force_refresh,
                liveness_cache=liveness_cache, probe_timeout=5.0,
            )
            saved_live = core.save_liveness_cache(liveness_cache)
            if saved_live:
                messages.append("存活探测缓存已更新：%s" % saved_live)
            self.discovered = records
            self._post(lambda: self._refresh_find_tree(records))
            for message in messages:
                self._post(lambda m=message: self._log_find("· " + m))
            self._post(lambda: self._log_find("扫描结束：共发现 %d 个音源脚本。" % len(records)))
        except Exception as exc:
            self._post(lambda e=exc: self._log_find("[异常] %s: %s" % (type(e).__name__, e)))
        finally:
            core.log = self._orig_core_log
            self._post(lambda: self._set_find_busy(False))

    # ---- 结果树渲染与去重开关 ----

    def _build_find_view(self):
        """按当前开关生成结果视图：去重显示（默认）或显示全部。"""
        if self.var_dedupe.get():
            return dedupe_records(self.discovered)
        return annotate_records([dict(record) for record in self.discovered])

    def _sync_checked_hashes(self):
        hashes = set()
        for iid in self.checked:
            try:
                index = int(iid)
            except ValueError:
                continue
            if 0 <= index < len(self.find_view):
                digest = self.find_view[index].get("sha256")
                if digest:
                    hashes.add(digest)
        self.checked_hashes = hashes

    def _checked_records(self):
        picked = []
        for iid in sorted(self.checked, key=lambda value: int(value)):
            try:
                index = int(iid)
            except ValueError:
                continue
            if 0 <= index < len(self.find_view):
                picked.append(self.find_view[index])
        return picked

    def _refresh_find_tree(self, records=None):
        if records is not None:
            self.discovered = records
            self.checked_hashes = set()
        view = self._build_find_view()
        self.find_view = view
        self.tree_find.delete(*self.tree_find.get_children())
        self.checked = set()
        for index, record in enumerate(view):
            iid = str(index)
            digest = record.get("sha256")
            mark = "☑" if digest and digest in self.checked_hashes else "☐"
            if mark == "☑":
                self.checked.add(iid)
            tags = []
            if record.get("duplicate_count"):
                tags.append("dup")
            self.tree_find.insert(
                "", "end", iid=iid,
                values=(mark, record["repo"], record["path"].rsplit("/", 1)[-1],
                        record["name"], record.get("version") or "-",
                        record.get("alive") or "-",
                        str(record.get("repo_count") or 1),
                        record.get("author") or "-", human_size(record.get("size")),
                        record.get("note") or "-"),
                tags=tuple(tags),
            )
        if self.var_dedupe.get():
            self.lbl_find.configure(
                text="去重显示 %d / 共 %d 个音源" % (len(view), len(self.discovered)))
        else:
            self.lbl_find.configure(text="显示全部 %d 个音源" % len(view))
        return view

    def _on_toggle_dedupe(self):
        self._sync_checked_hashes()
        view = self._refresh_find_tree()
        if not self.discovered:
            return view
        if self.var_dedupe.get():
            self._log_find("已切换为「去重显示」：%s" % dedupe_summary(self.discovered, view))
        else:
            self._log_find("已切换为「显示全部」：共 %d 条结果，重复收录来源已标注在备注列。"
                           % len(view))
        return view

    def _on_find_click(self, event):
        if self.tree_find.identify_region(event.x, event.y) != "cell":
            return
        if self.tree_find.identify_column(event.x) != "#1":
            return
        iid = self.tree_find.identify_row(event.y)
        if not iid:
            return
        self._toggle_checked(iid)

    def _toggle_checked(self, iid):
        values = list(self.tree_find.item(iid, "values"))
        if not values:
            return
        if iid in self.checked:
            self.checked.discard(iid)
            values[0] = "☐"
        else:
            self.checked.add(iid)
            values[0] = "☑"
        self.tree_find.item(iid, values=values)
        self._sync_checked_hashes()

    def _set_all_checked(self, flag):
        for iid in self.tree_find.get_children():
            values = list(self.tree_find.item(iid, "values"))
            if not values:
                continue
            if flag:
                self.checked.add(iid)
                values[0] = "☑"
            else:
                self.checked.discard(iid)
                values[0] = "☐"
            self.tree_find.item(iid, values=values)
        self._sync_checked_hashes()

    def _on_download(self):
        if self.worker is not None and self.worker.is_alive():
            messagebox.showinfo(APP_NAME, "当前已有任务在执行，请稍候或点击“停止”。")
            return
        if not self.discovered:
            messagebox.showinfo(APP_NAME, "请先扫描仓库获取音源列表。")
            return
        picked = self._checked_records()
        if not picked:
            messagebox.showinfo(APP_NAME, "请先点击结果列表首列方框勾选要下载的音源。")
            return
        target = self.var_download_dir.get().strip() or str(DATA_DIR / "discovered")
        try:
            timeout = float(self.var_find_timeout.get())
        except Exception:
            timeout = 20.0
        self.stop_event.clear()
        self._set_find_busy(True)
        self._log_find("-" * 60)
        self._log_find("开始下载 %d 个音源到 %s" % (len(picked), target))
        self._log_find("下载规则：内容 SHA-256 相同不重复落盘；同名同内容跳过；同名不同内容标注冲突并另存。")
        if self.var_download_force.get():
            self._log_find("已勾选「强制下载失效项」：存活校验为失效的音源也会下载。")
        else:
            self._log_find("存活校验：失效音源将自动跳过，仅下载「存活 / 无法判定」项。")
        self.worker = threading.Thread(
            target=self._download_worker, args=(picked, target, timeout), daemon=True)
        self.worker.start()

    def _download_worker(self, records, target, timeout):
        core.set_log_to_stdout(False)
        redirect_core_log(lambda message: self._post(lambda m=message: self._log_find(m)))
        try:
            force_dead = self.var_download_force.get()
            stats = download_records(
                records, target, timeout, stop_event=self.stop_event,
                progress=lambda m: self._post(lambda mm=m: self._log_find(mm)),
                force_dead=force_dead,
            )
            summary = ("下载结束：新增 %d / 已存在同内容跳过 %d / 同内容重复跳过 %d / 冲突另存 %d / 失效跳过 %d / 失败 %d -> %s"
                       % (stats["ok"], stats["skipped"], stats["duplicate"],
                          stats["conflict"], stats.get("dead_skipped", 0),
                          stats["failed"], stats["dir"]))
            self._post(lambda s=summary: self._log_find(s))
            conflicts = [item for item in stats.get("details", []) if item.get("status") == "conflict"]
            for item in conflicts:
                self._post(lambda it=item: self._log_find("  ! 冲突：%s" % it.get("note", "")))
            message = ("下载完成。\n\n新增落盘：%d 个\n已存在同内容跳过：%d 个\n"
                       "同内容重复跳过：%d 个\n同名冲突另存：%d 个\n失效自动跳过：%d 个\n失败：%d 个\n\n目录：%s"
                       % (stats["ok"], stats["skipped"], stats["duplicate"],
                          stats["conflict"], stats.get("dead_skipped", 0),
                          stats["failed"], stats["dir"]))
            if conflicts:
                message += "\n\n冲突文件（同名不同内容，已另存，未覆盖原文件）：\n" + "\n".join(
                    "· %s：%s" % (item.get("file"), item.get("note")) for item in conflicts[:8])
            self._post(lambda m=message: messagebox.showinfo(APP_NAME, m))
        except Exception as exc:
            self._post(lambda e=exc: self._log_find("[异常] %s: %s" % (type(e).__name__, e)))
        finally:
            core.log = self._orig_core_log
            self._post(lambda: self._set_find_busy(False))


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def main() -> int:
    core.setup_console()
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    argv = sys.argv[1:]
    # 命令行显式传 --console 时展示控制台窗口
    if "--console" in argv:
        show_console()
        argv = [item for item in argv if item != "--console"]
    if argv:
        if "--discover" in argv:
            rest = [item for item in argv if item != "--discover"]
            return cli_discover(rest)
        return core.main(argv)
    if tk is None:
        sys.stderr.write("当前环境缺少 tkinter，无法启动图形界面，请改用命令行参数运行。\n")
        return 1
    hide_console()
    root = tk.Tk()
    apply_window_icon(root)
    LxToolkitApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
