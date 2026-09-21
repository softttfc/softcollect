#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""洛雪音乐音源自动校验升级工具 (lx-source-updater)

功能概述
--------
1. 音源清单外置在 sources.json（内置 8 个默认源），支持原始 GitHub 直连与多种
   ghproxy / 加速镜像自动回退。
2. 抓取远端 latest.js，解析文件头部 /*! ... */ 注释里的
   @name / @version / @author / @homepage 元信息。
3. 纯在线播放级校验：拉取远端 latest.js 解析元信息，并构造搜索请求验证歌曲
   可播放性（响应含 songname/name/title/singer/artist/url 等歌曲特征字段或合法
   JSON 歌曲结构才判「存活(可播放)」）。状态划分为：可用(可播放) / 失效 /
   无法判定 / 抓取失败。
4. 多镜像自动回退，超时与重试次数均可配置。
5. 升级前自动备份到 backup/<时间戳>/，并写出状态文件与 Markdown 报告。
6. 安装目标目录运行时动态探测（%APPDATA%\lx-music-desktop 等），
   支持 --dir 手动指定；探测不到时回退到工具内置 sources 目录并给出明确提示。
7. Windows GBK 控制台环境下中文输出不乱码。

仅依赖 Python 3 标准库，无需安装任何第三方包。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

# --------------------------------------------------------------------------
# 常量与内置默认配置
# --------------------------------------------------------------------------

TOOL_NAME = "lx-source-updater"
TOOL_VERSION = "2.3.0"

# 打包为 PyInstaller 单文件 exe 后，__file__ 指向临时解包目录，
# 这里改为以 exe 所在目录作为工具目录，保证 backup/reports/state 落在用户可见位置。
if getattr(sys, "frozen", False):
    TOOL_DIR = Path(sys.executable).resolve().parent
else:
    TOOL_DIR = Path(__file__).resolve().parent

DEFAULT_BACKUP_ROOT = TOOL_DIR / "backup"
DEFAULT_REPORT_DIR = TOOL_DIR / "reports"
DEFAULT_STATE_FILE = TOOL_DIR / "state.json"
DEFAULT_LOCAL_DIR = TOOL_DIR / "sources"

# 播放级存活探测结果磁盘缓存（key=候选搜索 URL，TTL 默认 24 小时）
DEFAULT_LIVE_CACHE_FILE = TOOL_DIR / "liveness_cache.json"
LIVE_CACHE_TTL = 24 * 3600
LIVE_CACHE_VERSION = 2  # v2：播放级存活（候选搜索 URL 为缓存键）

USER_AGENT = "%s/%s (lx-music custom source updater)" % (TOOL_NAME, TOOL_VERSION)

# 状态标签（v2.2.0 起固定纯在线播放级校验）
# 存活判定标准：必须构造搜索请求并确认响应含歌曲特征字段（songname/name/title/
# singer/artist/url 等）或合法 JSON 含歌曲数据结构，才标记「存活(可播放)」。
STATUS_AVAILABLE = "可用(可播放)"
STATUS_DEAD = "失效"
STATUS_UNKNOWN = "无法判定"
STATUS_FAILED = "抓取失败"

ALL_STATUSES = [STATUS_AVAILABLE, STATUS_DEAD, STATUS_UNKNOWN, STATUS_FAILED]
ONLINE_STATUSES = list(ALL_STATUSES)

# 加速镜像前缀（留空表示原始直连地址）
BUILTIN_MIRRORS = [
    "",
    "https://ghproxy.net/",
    "https://gh.llkk.cc/",
    "https://github.moeyy.xyz/",
    "https://ghproxy.cn/",
    "https://gh.api.99988866.xyz/",
    "https://ghp.ci/",
    "https://gh-proxy.org/",
]


HEADER_BLOCK_RE = re.compile(r"/\*!(.*?)\*/", re.S)
META_KEYS = ("name", "version", "author", "homepage", "description")

# --------------------------------------------------------------------------
# 控制台 / 输出
# --------------------------------------------------------------------------


def setup_console() -> None:
    """在 Windows GBK 控制台环境下强制使用 UTF-8，避免中文乱码 / 编码异常。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


_LOG_TO_STDOUT = False


def set_log_to_stdout(enabled: bool) -> None:
    """非 --json 模式下进度信息走 stdout（保证先后顺序自然）；
    --json 模式下走 stderr，保证 stdout 只承载 JSON。"""
    global _LOG_TO_STDOUT
    _LOG_TO_STDOUT = bool(enabled)


def log(message: str) -> None:
    """输出进度信息（目标流由 set_log_to_stdout 决定）。"""
    stream = sys.stdout if _LOG_TO_STDOUT else sys.stderr
    try:
        stream.write(message + "\n")
        stream.flush()
    except Exception:
        pass


def emit(message: str = "") -> None:
    try:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        pass


def fatal(message: str, code: int = 2) -> "None":
    log("[错误] " + message)
    raise SystemExit(code)


# --------------------------------------------------------------------------
# 版本号处理
# --------------------------------------------------------------------------


def normalize_version(version) -> tuple:
    """把版本字符串归一化为数字元组，兼容 'v1.2.1' 与 '6' 等写法。"""
    if version is None:
        return ()
    numbers = re.findall(r"\d+", str(version))
    return tuple(int(n) for n in numbers)


def compare_version(a, b) -> int:
    """比较两个版本号：a > b 返回 1，a < b 返回 -1，相等返回 0。"""
    ta = normalize_version(a)
    tb = normalize_version(b)
    if not ta and not tb:
        return 0
    if not ta:
        return -1
    if not tb:
        return 1
    width = max(len(ta), len(tb))
    ta = ta + (0,) * (width - len(ta))
    tb = tb + (0,) * (width - len(tb))
    if ta > tb:
        return 1
    if ta < tb:
        return -1
    return 0


# --------------------------------------------------------------------------
# 脚本头部解析
# --------------------------------------------------------------------------


def parse_header(text: str) -> dict:
    """解析音源脚本头部 /*! */ 注释中的 @name/@version/@author/@homepage 等字段。"""
    meta = {key: "" for key in META_KEYS}
    if not text:
        return meta
    head = text[:16384]
    match = HEADER_BLOCK_RE.search(head)
    block = match.group(1) if match else head
    for key in META_KEYS:
        field_re = re.compile(r"@%s[ \t]*[:：]?[ \t]*([^\r\n*]+)" % key, re.I)
        field = field_re.search(block)
        if field:
            meta[key] = field.group(1).strip()
    # @version 行可能附带说明文字（如 "9.3 93特供版 ..."），只取开头的版本号部分
    if meta.get("version"):
        version_match = re.match(r"\s*([vV]?\d+(?:\.\d+)*)\s*$", meta["version"])
        if not version_match:
            version_match = re.match(r"\s*([vV]?\d+(?:\.\d+)*)", meta["version"])
        if version_match:
            meta["version"] = version_match.group(1)
    return meta


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def looks_like_script(text: str) -> bool:
    """判定抓取内容是否为音源脚本，过滤镜像返回的 HTML 错误页。"""
    if not text:
        return False
    sample = text[:2048]
    if "@name" in sample or "/*!" in sample:
        return True
    low = sample.lower().lstrip()
    if low.startswith("<!doctype") or low.startswith("<html"):
        return False
    if "<html" in text[:512].lower():
        return False
    return len(text) > 200


# --------------------------------------------------------------------------
# 网络抓取（多镜像回退 + 超时 + 重试）
# --------------------------------------------------------------------------


def encode_url(url: str) -> str:
    """把 URL 中的空格 / 中文 / 特殊字符做百分号编码，避免 urllib 直接报错。

    仅处理 path 与 query，保留 scheme / netloc / 已有的 %XX 转义，因此对
    「镜像前缀 + 原始地址」这类拼接 URL（如 https://ghproxy.net/https://...）
    同样安全。
    """
    try:
        parts = urlsplit(url)
    except Exception:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    safe = "/%:@&=+$,;~()!*'"
    path = quote(parts.path, safe=safe)
    query = quote(parts.query, safe=safe + "?")
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def http_get(url: str, timeout: float, retries: int):
    """单 URL 抓取，带重试。返回 (data, error)。"""
    last_err = None
    url = encode_url(url)
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Cache-Control": "no-cache"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(), None
        except Exception as exc:  # urllib.error.URLError / HTTPError / socket.timeout ...
            last_err = exc
            if attempt < retries:
                time.sleep(min(0.6 * (attempt + 1), 3.0))
    return None, last_err


def build_candidate_urls(source: dict, mirrors) -> list:
    """根据音源定义与镜像列表生成候选 URL（保持顺序，去重）。"""
    if source.get("urls"):
        return [u for u in source["urls"] if u]
    raw_url = source.get("url") or ""
    if not raw_url:
        return []
    urls = []
    for prefix in mirrors:
        candidate = (prefix + raw_url) if prefix else raw_url
        if candidate not in urls:
            urls.append(candidate)
    if raw_url not in urls:
        urls.append(raw_url)
    return urls


def fetch_with_mirrors(source: dict, mirrors, timeout: float, retries: int, max_mirror_try=None):
    """依次尝试各镜像，返回 (data, text, used_url, errors)。

    max_mirror_try 不为空时，最多尝试该数量的候选 URL（超时后快速失败，避免长时间卡住）。
    """
    errors = []
    urls = build_candidate_urls(source, mirrors)
    if max_mirror_try:
        try:
            limit = max(1, int(max_mirror_try))
        except Exception:
            limit = len(urls)
        urls = urls[:limit]
    for url in urls:
        data, err = http_get(url, timeout, retries)
        if data is None:
            errors.append("%s -> %s" % (url, err))
            log("    - 失败: %s (%s)" % (url, err))
            continue
        text = data.decode("utf-8", errors="replace")
        if not looks_like_script(text):
            errors.append("%s -> 返回内容疑似非音源脚本，已忽略" % url)
            log("    - 跳过: %s (返回内容非音源脚本)" % url)
            continue
        return data, text, url, errors
    return None, None, None, errors


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 播放级存活探测（歌曲可播放判定）
#   判定标准：仅 HTTP 可达不算活，必须验证音源能真实解析出歌曲。
#   流程：提取 API base 与搜索/点歌路径模板 -> 构造 2~4 个候选搜索请求
#   （带浏览器 UA，keyword=test&page=1）-> 直连+镜像短超时(5-8s) GET ->
#   2xx/3xx 且响应体含歌曲特征字段（songname/name/title/singer/artist/url
#   等）或为合法 JSON 且含歌曲数据结构 -> 标记「存活(可播放)」；
#   请求失败/超时/响应体无歌曲特征（验证码页、错误页、空数组等）-> 「失效」；
#   脚本内无任何可构造请求的端点 -> 「无法判定」。
#   统一用于校验页（内置 8 源）与检索页（候选音源），结果走 24h 磁盘缓存。
# --------------------------------------------------------------------------

_GITHUB_HOSTS = {
    "github.com", "raw.githubusercontent.com", "gist.github.com", "api.github.com",
    "objects.githubusercontent.com", "codeload.github.com", "avatars.githubusercontent.com",
}
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]{}，。；、（）【】《》]+")
_ENDPOINT_FIELD_RE = re.compile(
    r"""(?i)\b(?:server|host|api|apiUrl|api_url|baseUrl|base_url|baseURL|endpoint|hostname)\b\s*[:=]\s*["']([^"']+)["']"""
)

# 播放级探测使用真实浏览器 UA（部分音源接口对非浏览器 UA 返回错误页/验证码）
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 歌曲特征字段：响应体命中任意一项即认为“可解析出歌曲”
_SONG_FEATURE_RE = re.compile(
    r"""(?i)(?:songname|songName|musicName|music_name|song_name|music_url|song_url|"title"\s*:|"singer"|"artist"|"url"\s*:|"name"\s*:|歌名|歌手|歌曲|专辑|音乐名|曲名)"""
)
# 搜索/点歌路径模板：脚本内出现的相对路径片段（/search、/api/search、?keyword 等）
_SEARCH_PATH_RE = re.compile(r"""(?i)["'](/[^"']*(?:search|find|query|suggest|song|music|api|hot|top)[^"']*)["']""")
# 请求参数名特征：?keyword / ?input / ?name / ?wd / ?songname 等
_QUERY_PARAM_RE = re.compile(r"""(?i)(?:[?&])(keyword|input|name|wd|songname|song|key|text|query|word|search|music|value)=""")
# 验证码/拦截/错误页特征
_BLOCK_PAGE_RE = re.compile(r"(?i)(验证码|captcha|滑块|geetest|安全验证|forbidden|access denied|waf|error page|登录|login)")


def _clean_url(raw: str) -> str:
    return raw.strip().strip("\"'.,;:!?)]}>）】》")


def _is_github_host(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return host in _GITHUB_HOSTS or host.endswith(".github.io")


def _looks_search_path(path: str) -> bool:
    low = (path or "").lower()
    return any(k in low for k in ("search", "find", "query", "suggest", "song", "music", "api", "hot", "top"))


def _has_query_param(url: str) -> bool:
    return "?" in (url or "") and bool(_QUERY_PARAM_RE.search(url))


def _ensure_test_query(url: str) -> str:
    """保证候选 URL 携带 keyword=test&page=1 测试参数。"""
    if not url:
        return url
    if re.search(r"(?i)[?&]keyword=", url):
        url = re.sub(r"(?i)([?&]keyword=)[^&]*", r"\1test", url)
        if "page=" not in url:
            url += ("&" if "?" in url else "?") + "page=1"
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "keyword=test&page=1"


def _decode_js_literals(text: str) -> list:
    """提取 JS 字符串字面量并解码 \\xNN/\\uNNNN 转义，拼接相邻字面量成串。

    仅拼接间距 <= 40 字符且中间无分号的相邻字面量（模拟 JS 相邻字符串
    拼接语义），避免把混淆脚本中互不相干的碎片串成垃圾 URL。
    """
    literal_re = re.compile(r"""(["'])((?:[^"'\\]|\\.)*)\1""")
    runs = []
    cur = []
    prev_end = None
    for match in literal_re.finditer(text or ""):
        raw = match.group(2)
        if prev_end is not None and (match.start() - prev_end > 40
                                     or ";" in text[prev_end:match.start()]):
            if cur:
                runs.append("".join(cur))
            cur = []
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                decoded = raw.encode("utf-8").decode("unicode_escape", errors="replace")
        except Exception:
            decoded = raw
        cur.append(decoded)
        prev_end = match.end()
    if cur:
        runs.append("".join(cur))
    return runs


def _extract_backtick_templates(text: str) -> list:
    """提取包含 ${...} 占位符的反引号模板字面量（点歌/搜索 URL 模板）。"""
    return re.findall(r"`([^`]*\$\{[^`]*\})[^`]*`", text or "")


_TEMPLATE_DEFAULTS = {
    "source": "wy", "src": "wy", "type": "128k", "quality": "128k",
    "keyword": "test", "key": "test", "name": "test", "wd": "test",
    "input": "test", "text": "test", "songname": "test", "song": "test",
    "page": "1", "limit": "10", "size": "10", "id": "186016",
    "songid": "186016", "songId": "186016", "musicid": "186016",
    "hash": "186016", "br": "128", "t": "1",
}
_BASE_VAR_NAMES = {"api_url", "api", "baseurl", "base", "host", "server", "url", "hosturl", "apiurl"}


def _fill_template(template: str, base: str) -> str:
    """将反引号模板中的 ${API_URL} 等替换为 base，其余占位符填测试值。"""
    def repl(m):
        name = (m.group(1) or "").strip()
        if name.lower() in _BASE_VAR_NAMES:
            return base or ""
        return _TEMPLATE_DEFAULTS.get(name, "test")
    return re.sub(r"\$\{([^}]+)\}", repl, template)


def extract_search_candidates(text: str, max_candidates: int = 4) -> list:
    """从音源脚本中提取可探测的候选请求 URL，返回 [{"url","kind","label"}]。

    kind="search"（关键词搜索）/ "musicurl"（点歌取链）。策略：
    ① 收集 server/host/api/baseUrl 等字段引号内的 API base（过滤 GitHub 分发域名）；
    ② 解码 \\xNN/\\uNNNN 转义字符串字面量并拼接相邻字面量，再扫 URL/路径；
    ③ 收集相对搜索路径模板（/search、/api/search、?keyword、?input、?wd 等）；
    ④ 提取反引号点歌模板（${API_URL}/url?...）并填充占位符；
    ⑤ base 与路径拼接，搜索类统一补 keyword=test&page=1，去重截断。
    """
    if not text:
        return []
    head = text[:131072]
    decoded_runs = _decode_js_literals(head)
    scan = [head]
    for run in decoded_runs:
        if run and run not in scan:
            scan.append(run)
    scan_text = "\n".join(scan)

    bases, seen_base = [], set()
    for match in _ENDPOINT_FIELD_RE.finditer(scan_text):
        url = _clean_url(match.group(1))
        low = url.lower()
        if low.startswith(("http://", "https://")) and url not in seen_base:
            seen_base.add(url)
            if not _is_github_host(url):
                bases.append(url)
    abs_urls = []
    for match in _HTTP_URL_RE.finditer(scan_text):
        url = _clean_url(match.group(0))
        if url not in seen_base and url not in abs_urls and not _is_github_host(url):
            abs_urls.append(url)
    search_paths = []
    for match in _SEARCH_PATH_RE.finditer(scan_text):
        path = match.group(1)
        if path not in search_paths and _looks_search_path(path):
            search_paths.append(path)

    candidates = []

    def push(url, kind, label):
        if kind == "search":
            url = _ensure_test_query(url)
        if url and url not in [c["url"] for c in candidates]:
            candidates.append({"url": url, "kind": kind, "label": label})

    def host_ok(url):
        try:
            host = urlsplit(url).hostname or ""
        except Exception:
            return False
        if not host or host != host.lower() or host.count(".") < 1 or len(host) > 253:
            return False
        for seg in host.split("."):
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", seg) or len(seg) > 63:
                return False
            if re.search(r"\d+[a-z]{2,}", seg):  # 混淆垃圾（如 latest84244quuopwjs）
                return False
        return True

    for url in abs_urls:
        if not host_ok(url):
            continue
        try:
            path_part = urlsplit(url).path or ""
        except Exception:
            path_part = ""
        if _looks_search_path(path_part) or _has_query_param(url):
            push(url, "search", "绝对URL")

    for base in bases:
        if not host_ok(base):
            continue
        for path in search_paths[:8]:
            push(base.rstrip("/") + path, "search", "base+路径")

    # 反引号点歌/搜索模板
    for template in _extract_backtick_templates(head):
        tlow = template.lower()
        if not any(k in tlow for k in ("url", "search", "song", "music", "?kw", "keyword")):
            continue
        filled = _fill_template(template, bases[0] if bases else "")
        if not filled.startswith(("http://", "https://", "/", "?")):
            continue
        if filled.startswith("/"):
            if not bases:
                continue
            filled = bases[0].rstrip("/") + filled
        if filled.startswith("?"):
            if not bases:
                continue
            filled = bases[0].rstrip("/") + filled
        kind = "musicurl" if re.search(r"(?i)(/url|url\?|/play|/songurl)", filled) else "search"
        if kind == "search":
            filled = _ensure_test_query(filled)
        if filled not in [c["url"] for c in candidates]:
            candidates.append({"url": filled, "kind": kind,
                               "label": "点歌模板" if kind == "musicurl" else "模板"})

    return candidates[:max_candidates]


def _json_has_song(data) -> bool:
    """递归判断 JSON 结构是否含歌曲数据（空数组视为无歌曲）。"""
    if isinstance(data, dict):
        keys = {str(k).lower() for k in data}
        if keys & {
            "songname", "songname2", "musicname", "song_name", "music_name",
            "songurl", "music_url", "song_url", "title", "singer", "artist",
            "url", "name", "歌名", "歌手", "歌曲", "专辑", "音乐名",
        }:
            return True
        return any(_json_has_song(v) for v in data.values())
    if isinstance(data, list):
        if not data:
            return False
        return any(_json_has_song(v) for v in data)
    return False


def _has_song_features(text: str) -> bool:
    """判定响应体是否含歌曲特征字段或合法 JSON 歌曲结构。"""
    if not text:
        return False
    if _SONG_FEATURE_RE.search(text):
        return True
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            data = json.loads(text[:262144])
        except Exception:
            return False
        return _json_has_song(data)
    return False


def _dead_note(body: str) -> str:
    """为失效判定补充原因（验证码/拦截页/HTML/空响应体等）。"""
    if not body:
        return "（空响应体）"
    if _BLOCK_PAGE_RE.search(body[:8192]):
        return "（疑似验证码/拦截页）"
    low = body[:4096].lstrip().lower()
    if low.startswith(("<html", "<!doctype")):
        return "（返回 HTML 页面）"
    return ""


def probe_playable(url: str, timeout: float = 6.0, mirrors=None) -> dict:
    """对单个搜索候选 URL 做播放级探测，返回 {"alive", "note", "url"}。

    带浏览器 UA 短超时(5-8s) GET，最多读取 256KB 响应体：
    2xx/3xx 且含歌曲特征 -> 存活(可播放)；其余（验证码页/错误页/空数组/
    超时/连接失败）-> 失效；直连失败时尝试镜像前缀。
    """
    urls = [url]
    low = url.lower()
    if mirrors and ("raw.githubusercontent.com" in low or "github.com" in low):
        for prefix in mirrors:
            if prefix and (prefix + url) not in urls:
                urls.append(prefix + url)
    last_note = "无可用探测地址"
    for candidate in urls:
        try:
            request = urllib.request.Request(
                candidate, method="GET",
                headers={"User-Agent": BROWSER_UA,
                         "Accept": "application/json,text/plain,*/*",
                         "Referer": "https://music.example.com/",
                         "Cache-Control": "no-cache"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 0) or 0
                body = response.read(262144).decode("utf-8", errors="replace")
            if _has_song_features(body):
                return {"alive": True, "url": candidate,
                        "note": "HTTP %s 响应含歌曲特征字段（可播放）" % status}
            return {"alive": False, "url": candidate,
                    "note": "HTTP %s 响应体无歌曲特征%s" % (status, _dead_note(body))}
        except urllib.error.HTTPError as exc:
            last_note = "%s HTTP %s 失败" % (candidate, exc.code)
        except Exception as exc:
            last_note = "%s GET 失败: %s" % (candidate, exc)
    return {"alive": False, "url": url, "note": last_note}

def load_liveness_cache(path=None) -> dict:
    """读取播放级存活探测磁盘缓存，返回 {endpoint: {alive, note, at}}；
    异常/版本不符返回空表（旧版端点连通性缓存自动作废）。"""
    target = Path(path) if path else DEFAULT_LIVE_CACHE_FILE
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if int(data.get("version") or 0) != LIVE_CACHE_VERSION:
        return {}
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def save_liveness_cache(cache: dict, path=None) -> str:
    """写回存活探测缓存，返回缓存文件路径（失败返回空串）。"""
    target = Path(path) if path else DEFAULT_LIVE_CACHE_FILE
    payload = {"version": LIVE_CACHE_VERSION, "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "entries": cache}
    try:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(target)
    except Exception as exc:
        log("[警告] 写入存活缓存失败：%s" % exc)
        return ""


def clear_liveness_cache(path=None) -> str:
    """清空存活探测缓存，返回缓存文件路径（文件不存在也返回路径）。"""
    target = Path(path) if path else DEFAULT_LIVE_CACHE_FILE
    try:
        if target.is_file():
            target.unlink()
    except OSError as exc:
        log("[警告] 清空存活缓存失败：%s" % exc)
    return str(target)


def cached_liveness(endpoint: str, cache: dict, ttl: int = LIVE_CACHE_TTL, now: float = None) -> dict:
    """读取有效期内（默认 24h）的端点存活缓存；无/过期返回 None。"""
    if not endpoint:
        return None
    entry = (cache or {}).get(endpoint)
    if not entry:
        return None
    at = entry.get("at") or 0
    if now is None:
        now = time.time()
    if now - at > ttl:
        return None
    return {"status": entry.get("alive"), "note": entry.get("note", ""),
            "endpoint": endpoint, "at": at, "cached": True}


def check_script_liveness(text: str, timeout: float = 6.0, mirrors=None,
                          cache: dict = None, ttl: int = LIVE_CACHE_TTL,
                          now: float = None, max_probe: int = 3) -> dict:
    """对音源脚本做播放级存活校验：提取搜索候选 -> 缓存/短超时探测 -> 写缓存。

    返回 {"status": "可用(可播放)"/"失效"/"无法判定", "endpoint", "note",
          "cached", "results"}。缓存键为候选搜索 URL（同一接口跨脚本复用，
          TTL 默认 24h）。校验页与检索页统一走此函数。
    """
    cache = cache if cache is not None else {}
    candidates = extract_search_candidates(text)
    if not candidates:
        return {"status": STATUS_UNKNOWN, "endpoint": "", "note": "脚本内无可用搜索/点歌端点，无法判定",
                "cached": False, "results": []}
    if now is None:
        now = time.time()
    results = []
    to_probe = []
    for cand in candidates:
        endpoint = cand["url"]
        hit = cached_liveness(endpoint, cache, ttl=ttl, now=now)
        if hit is not None:
            results.append(hit)
        else:
            to_probe.append((endpoint, cand["kind"], cand["label"]))
    for endpoint, kind, label in to_probe[:max_probe]:
        probe = probe_playable(endpoint, timeout=timeout, mirrors=mirrors)
        status = STATUS_AVAILABLE if probe["alive"] else STATUS_DEAD
        note = "[%s] %s" % (label, probe["note"])
        cache[endpoint] = {"alive": status, "note": note, "at": now}
        results.append({"status": status, "note": note,
                        "endpoint": endpoint, "at": now, "cached": False})
    if not results:
        return {"status": STATUS_UNKNOWN, "endpoint": candidates[0]["url"],
                "note": "搜索探测被中断，无法判定", "cached": False, "results": []}
    alive = [r for r in results if r["status"] == STATUS_AVAILABLE]
    dead = [r for r in results if r["status"] == STATUS_DEAD]
    unknown = [r for r in results if r["status"] not in (STATUS_AVAILABLE, STATUS_DEAD)]
    all_cached = all(r.get("cached") for r in results)
    if alive:
        best = alive[0]
        return {"status": STATUS_AVAILABLE, "endpoint": best["endpoint"],
                "note": "播放级存活(可播放): %s — %s" % (best["endpoint"], best["note"]),
                "cached": all_cached, "results": results}
    if dead:
        best = dead[0]
        return {"status": STATUS_DEAD, "endpoint": best["endpoint"],
                "note": "播放级失效: %s — %s" % (best["endpoint"], best["note"]),
                "cached": all_cached, "results": results}
    return {"status": STATUS_UNKNOWN, "endpoint": unknown[0]["endpoint"] if unknown else candidates[0]["url"],
            "note": "无法判定播放能力", "cached": all_cached, "results": results}


# --------------------------------------------------------------------------
# 本地音源目录探测
# --------------------------------------------------------------------------


def detect_lx_music_dir():
    """运行时探测本机洛雪音乐数据目录，找不到返回 None。"""
    candidates = []
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    userprofile = os.environ.get("USERPROFILE")

    if appdata:
        base = Path(appdata)
        candidates += [
            base / "lx-music-desktop",
            base / "lx-music-desktop" / "custom-source",
            base / "lx-music-desktop" / "customSource",
            base / "lx-music-desktop" / "sources",
            base / "lx-music-desktop" / "LxMusic" / "custom-source",
            base / "LX Music",
        ]
    if localappdata:
        base = Path(localappdata)
        candidates += [
            base / "lx-music-desktop",
            base / "Programs" / "lx-music-desktop",
        ]
    if userprofile:
        candidates.append(Path(userprofile) / "lx-music-desktop")
    candidates += [
        Path(r"C:\Program Files\lx-music-desktop"),
        Path(r"C:\Program Files (x86)\lx-music-desktop"),
    ]

    existing = [c for c in candidates if c.is_dir()]
    if not existing:
        return None
    # 优先选择内含 .js 音源脚本的目录
    for candidate in existing:
        try:
            if any(candidate.glob("*.js")):
                return candidate
        except OSError:
            continue
    return existing[0]


def resolve_local_dir(explicit):
    """解析安装目标目录，返回 (Path, 来源说明)。"""
    if explicit:
        return Path(explicit).expanduser(), "命令行 --dir 指定"
    env_dir = os.environ.get("LX_MUSIC_SOURCE_DIR")
    if env_dir:
        return Path(env_dir).expanduser(), "环境变量 LX_MUSIC_SOURCE_DIR"
    detected = detect_lx_music_dir()
    if detected:
        return detected, "自动探测到洛雪音乐数据目录"
    return DEFAULT_LOCAL_DIR, "未检测到洛雪音乐，回退到工具内置目录"


# --------------------------------------------------------------------------
# 配置加载
# --------------------------------------------------------------------------


def load_sources(sources_path):
    """读取外部音源清单 JSON；未指定或文件不存在时返回 None（调用方走全量在线扫描）。"""
    if not sources_path:
        return None
    path = Path(sources_path)
    if not path.is_file():
        log("[提示] 未找到音源清单 %s，将改为全量在线扫描全部候选仓库。" % path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fatal("解析音源清单失败：%s -> %s" % (path, exc))
    if not isinstance(data, dict):
        fatal("音源清单格式错误：根节点必须是 JSON 对象。")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        fatal("音源清单缺少有效的 sources 数组。")
    data["_path"] = str(path)
    return data


# --------------------------------------------------------------------------
# 单音源处理
# --------------------------------------------------------------------------


def record_to_result(record: dict) -> dict:
    """把在线扫描记录（含播放级存活结果与脚本文本）映射为统一的校验结果结构。"""
    alive = record.get("alive") or STATUS_UNKNOWN
    fname = str(record.get("path") or "").rsplit("/", 1)[-1]
    key = record.get("key") or (fname[:-3] if fname.lower().endswith(".js") else fname) or "source"
    result = {
        "key": key,
        "name": record.get("name") or fname or key,
        "remote_url": record.get("raw_url") or "",
        "used_url": record.get("used_url") or record.get("raw_url") or "",
        "status": alive,
        "remote_version": record.get("version") or "",
        "remote_author": record.get("author") or "",
        "remote_homepage": record.get("homepage") or "",
        "remote_sha256": record.get("sha256") or "",
        "remote_size": record.get("size") or 0,
        "note": record.get("alive_note") or record.get("note") or "",
        "errors": [],
        "alive": alive,
        "alive_endpoint": record.get("alive_endpoint") or "",
        "alive_note": record.get("alive_note") or "",
        "upgraded": False,
        "backup_file": "",
        "target_file": "",
        "repo": record.get("repo") or "",
        "repo_count": int(record.get("repo_count") or 1),
        "merged_repos": record.get("merged_repos") or [],
        "_data": record.get("data"),
    }
    return result


def process_source(source: dict, ctx: dict) -> dict:
    key = source.get("key") or "unknown"
    result = {
        "key": key,
        "name": source.get("name") or key,
        "remote_url": source.get("url") or "",
        "used_url": "",
        "status": STATUS_FAILED,
        "remote_version": "",
        "remote_author": "",
        "remote_homepage": "",
        "remote_sha256": "",
        "remote_size": 0,
        "note": "",
        "errors": [],
        "alive": "",
        "alive_endpoint": "",
        "alive_note": "",
        "upgraded": False,
        "backup_file": "",
        "target_file": "",
        "_data": None,
    }

    log("[%s] %s" % (key, result["name"]))
    data, text, used_url, errors = fetch_with_mirrors(
        source, ctx["mirrors"], ctx["timeout"], ctx["retries"]
    )
    result["errors"] = list(errors)

    if data is None:
        result["status"] = STATUS_FAILED
        result["note"] = "所有镜像均抓取失败"
        log("    = 状态: %s" % STATUS_FAILED)
        return result

    remote_meta = parse_header(text)
    result["remote_version"] = remote_meta.get("version", "")
    result["remote_author"] = remote_meta.get("author", "")
    result["remote_homepage"] = remote_meta.get("homepage", "")
    result["remote_sha256"] = sha256_bytes(data)
    result["remote_size"] = len(data)
    result["used_url"] = used_url
    result["_data"] = data
    log("    + 远端: 版本=%s sha256=%s" % (result["remote_version"] or "?", result["remote_sha256"][:12]))

    if not remote_meta.get("name"):
        result["status"] = STATUS_DEAD
        result["note"] = "头部缺少 @name，非标准音源脚本"
        log("    = 状态: %s" % STATUS_DEAD)
        return result

    # 播放级存活校验：构造搜索请求验证歌曲可播放性，结果走 24h 磁盘缓存
    liveness = check_script_liveness(
        text,
        timeout=float(ctx.get("probe_timeout", 5) or 5),
        mirrors=ctx.get("mirrors"),
        cache=ctx.get("liveness_cache"),
        ttl=float(ctx.get("live_cache_ttl", LIVE_CACHE_TTL) or LIVE_CACHE_TTL),
    )
    result["alive"] = liveness["status"]
    result["alive_endpoint"] = liveness.get("endpoint", "")
    result["alive_note"] = liveness.get("note", "")
    log("    * 存活: %s%s" % (liveness["status"], "  [缓存]" if liveness.get("cached") else ""))

    # 纯在线校验：状态只取 可用(可播放) / 失效 / 无法判定 / 抓取失败
    result["status"] = liveness["status"]
    result["note"] = "远端版本 %s；接口 %s" % (
        result["remote_version"] or "?", liveness["note"],
    )

    log("    = 状态: %s" % result["status"])
    return result


# --------------------------------------------------------------------------
# 升级执行
# --------------------------------------------------------------------------


def upgrade_source(result: dict, ctx: dict, backup_dir: Path):
    """把远端内容写入本地文件，覆盖前先备份。返回 (target, backup)。"""
    source_file = result.get("target_file") or ""
    if source_file:
        target = Path(source_file)
    else:
        target = Path(ctx["local_dir"]) / ("%s.js" % result["key"])

    if ctx["local_dir"] is None:
        raise RuntimeError("本地音源目录不可用")

    target.parent.mkdir(parents=True, exist_ok=True)

    backup_path = None
    if target.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / ("%s__%s" % (result["key"], target.name))
        shutil.copy2(target, backup_path)

    target.write_bytes(result["_data"])
    result["target_file"] = str(target)
    result["backup_file"] = str(backup_path) if backup_path else ""
    return target, backup_path


# --------------------------------------------------------------------------
# 清除失效 / 升级选中（v2.3.0）
# --------------------------------------------------------------------------


def trash_file(path) -> bool:
    """把文件移入 Windows 回收站（不弹确认框）。失败返回 False。"""
    path = Path(path)
    if not path.is_file():
        return False
    escaped = str(path).replace("'", "''")
    script = (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
        "'%s', 'OnlyErrorDialogs', 'SendToRecycleBin')" % escaped
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=60,
        )
        return proc.returncode == 0 and not path.exists()
    except Exception:
        return False


def _sanitize_filename(name: str) -> str:
    """与 GUI 落盘规则一致的本地文件名清洗（简易版）。"""
    illegal = '<>:"/|?*' + chr(92)
    out = []
    for ch in str(name):
        out.append("_" if (ch in illegal or ord(ch) < 32) else ch)
    text = "".join(out).strip().strip(".")
    return text or "source.js"


def _norm_name(text) -> str:
    """名称归一化（去版本号 / emoji / 标点 / 空白，小写），用于本地文件匹配。"""
    value = str(text or "")
    value = re.sub(r"(?i)(?<![a-z0-9])v?\d+(?:\.\d+)*(?![a-z0-9])", "", value)
    value = re.sub(r"[\U0001f000-\U0001faff\u2600-\u27bf\u2190-\u21ff\u2460-\u24ff"
                   r"\u25a0-\u25ff\u2b00-\u2bff\u200d\u20e3\ufe0e\ufe0f]+", "", value)
    value = re.sub(r"[\s\u3000]+", "", value)
    value = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", value)
    return value.lower()


def _local_script_name(record: dict) -> str:
    """按在线记录推导本地默认落盘文件名（与 GUI download_records 规则一致）。"""
    label = (record.get("name") or "").strip()
    if not label:
        label = record.get("path", "").rsplit("/", 1)[-1] or ("%s.js" % record.get("key", ""))
    if not label.lower().endswith(".js"):
        label += ".js"
    return _sanitize_filename(label[:80])


def _find_local_script(record: dict, target: Path):
    """在目标目录中定位与在线记录对应的本地音源脚本。

    优先使用记录自带 target_file；否则按默认文件名精确匹配，再退化为
    遍历目录比对头部 @name 归一化键。返回 Path 或 None。
    """
    explicit = record.get("target_file")
    if explicit:
        cand = Path(explicit)
        if cand.is_file() and cand.resolve().parent == target.resolve():
            return cand
    default_name = _local_script_name(record)
    cand = target / default_name
    if cand.is_file():
        return cand
    key = record.get("key") or ""
    if key:
        cand_key = target / ("%s.js" % key)
        if cand_key.is_file():
            return cand_key
    rec_norm = _norm_name(record.get("name")) or _norm_name(
        record.get("path", "").rsplit("/", 1)[-1])
    for entry in target.iterdir():
        if not entry.is_file() or entry.suffix.lower() != ".js":
            continue
        try:
            header = parse_header(entry.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if header.get("name") and _norm_name(header["name"]) == rec_norm:
            return entry
    return None


def clean_dead(target_dir, timeout=20.0, retries=1, mirrors=None, probe_timeout=5.0,
               stop_event=None, progress=None, liveness_cache=None) -> dict:
    """对目标目录中已下载的音源脚本逐个做播放级存活校验，失效的移入回收站。

    可用(可播放) 与 无法判定 的音源保留。返回统计信息：
    {"dir", "total", "deleted", "kept", "failed", "skipped",
     "deleted_files": [{name, file, note}], "kept_files": [...], "errors": [...]}
    """
    target = Path(target_dir)
    stats = {"dir": str(target), "total": 0, "deleted": 0, "kept": 0, "failed": 0,
             "skipped": 0, "deleted_files": [], "kept_files": [], "errors": []}
    if not target.is_dir():
        stats["errors"].append("目标目录不存在：%s" % target)
        return stats
    cache = liveness_cache if liveness_cache is not None else {}
    scripts = sorted([p for p in target.iterdir() if p.is_file() and p.suffix.lower() == ".js"],
                     key=lambda p: p.name.lower())
    stats["total"] = len(scripts)
    if progress:
        progress("目标目录：%s，发现 %d 个 .js 文件" % (target, len(scripts)))
    for path in scripts:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            stats["failed"] += 1
            stats["errors"].append("%s 读取失败：%s" % (path.name, exc))
            if progress:
                progress("  x 读取失败：%s（%s）" % (path.name, exc))
            continue
        if not looks_like_script(text):
            stats["skipped"] += 1
            if progress:
                progress("  - 跳过非音源脚本：%s" % path.name)
            continue
        header = parse_header(text)
        liveness = check_script_liveness(
            text, timeout=probe_timeout, mirrors=mirrors, cache=cache,
        )
        label = header.get("name") or path.stem
        if liveness["status"] == STATUS_DEAD:
            if trash_file(path):
                stats["deleted"] += 1
                stats["deleted_files"].append({
                    "name": label, "file": str(path),
                    "note": liveness.get("note") or "接口探测失败",
                })
                if progress:
                    progress("  x 已移入回收站（失效）：%s（%s）" % (path.name, label))
            else:
                stats["failed"] += 1
                stats["errors"].append("%s 移入回收站失败" % path.name)
                if progress:
                    progress("  ! 移入回收站失败：%s" % path.name)
        else:
            stats["kept"] += 1
            stats["kept_files"].append({
                "name": label, "file": str(path),
                "note": liveness.get("note") or "",
            })
            if progress:
                progress("  = 保留（%s）：%s（%s）" % (liveness["status"], path.name, label))
    if progress:
        progress("清除失效完成：共 %d 个，删除 %d，保留 %d，跳过 %d，失败 %d"
                 % (stats["total"], stats["deleted"], stats["kept"],
                    stats["skipped"], stats["failed"]))
    return stats


def upgrade_selected(records, target_dir, timeout=20.0, retries=1, mirrors=None,
                     stop_event=None, progress=None, backup_dir=None,
                     probe_timeout=5.0, liveness_cache=None) -> dict:
    """对选中的在线校验记录执行「一键升级」。

    升级条件 = 存活(可用可播放) && (远端版本更高 || 内容 SHA-256 不同)。
    不满足条件（失效 / 无法判定 / 本地未安装 / 本地已最新）一律跳过并说明原因。
    满足条件时在线拉取最新脚本，备份后覆盖本地文件。
    返回统计：{"dir", "total", "upgraded", "skipped", "errors",
               "upgraded_files": [{name, key, file, backup, from_version, to_version}],
               "skipped_details": [{name, key, reason}], "errors": [...]}
    """
    target = Path(target_dir)
    stats = {"dir": str(target), "total": len(records or []), "upgraded": 0,
             "skipped": 0, "errors": 0, "upgraded_files": [], "skipped_details": [],
             "errors": []}
    if not target.is_dir():
        stats["errors"].append("目标目录不存在：%s" % target)
        stats["skipped"] = stats["total"]
        for record in records or []:
            stats["skipped_details"].append({
                "name": record.get("name") or record.get("key", ""),
                "key": record.get("key", ""), "reason": "目标目录不存在",
            })
        return stats
    mirrors = mirrors if mirrors is not None else list(BUILTIN_MIRRORS)
    cache = liveness_cache if liveness_cache is not None else {}
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bk_root = Path(backup_dir) if backup_dir else (DEFAULT_BACKUP_ROOT / stamp)
    for record in records or []:
        if stop_event is not None and stop_event.is_set():
            break
        key = record.get("key", "")
        label = record.get("name") or key or "-"
        alive = record.get("alive") or record.get("status") or ""
        if alive != STATUS_AVAILABLE:
            stats["skipped"] += 1
            reason = ("存活判定为「%s」" % alive) if alive else "缺少存活判定结果"
            stats["skipped_details"].append({"name": label, "key": key, "reason": reason})
            if progress:
                progress("  - 跳过：%s（%s）" % (label, reason))
            continue
        local_path = _find_local_script(record, target)
        if local_path is None:
            stats["skipped"] += 1
            reason = "目标目录未找到已下载脚本"
            stats["skipped_details"].append({"name": label, "key": key, "reason": reason})
            if progress:
                progress("  - 跳过：%s（%s）" % (label, reason))
            continue
        try:
            local_bytes = local_path.read_bytes()
        except OSError as exc:
            stats["errors"].append("%s 读取失败：%s" % (local_path.name, exc))
            if progress:
                progress("  x 读取本地失败：%s（%s）" % (local_path.name, exc))
            continue
        local_text = local_bytes.decode("utf-8", errors="replace")
        local_header = parse_header(local_text)
        local_version = local_header.get("version") or ""
        local_sha = sha256_bytes(local_bytes)
        remote_version = record.get("remote_version") or ""
        remote_sha = record.get("remote_sha256") or record.get("sha256") or ""
        version_higher = compare_version(remote_version, local_version) > 0
        sha_diff = bool(remote_sha) and remote_sha != local_sha
        upgradable = version_higher or sha_diff
        if not upgradable:
            stats["skipped"] += 1
            if remote_version and local_version and remote_version == local_version and not sha_diff:
                reason = "本地已是最新版本（%s，SHA-256 一致）" % local_version
            else:
                reason = "不满足升级条件（远端 %s vs 本地 %s，SHA-256 %s）" % (
                    remote_version or "?", local_version or "?",
                    "相同" if (remote_sha and remote_sha == local_sha) else "不同/未知",
                )
            stats["skipped_details"].append({"name": label, "key": key, "reason": reason})
            if progress:
                progress("  - 跳过：%s（%s）" % (label, reason))
            continue
        blob = record.get("_data") or record.get("data")
        if blob is None:
            probe = {"key": key or local_path.stem,
                     "url": record.get("used_url") or record.get("raw_url") or record.get("url")}
            blob, text, used_url, errors = fetch_with_mirrors(
                probe, mirrors, timeout, retries, max_mirror_try=len(mirrors) or 3,
            )
            if blob is None:
                stats["errors"].append("%s 在线拉取失败" % label)
                if progress:
                    progress("  x 在线拉取失败：%s" % label)
                continue
        if not looks_like_script(blob.decode("utf-8", errors="replace")):
            stats["errors"].append("%s 拉取内容不是有效音源脚本" % label)
            if progress:
                progress("  x 拉取内容无效：%s" % label)
            continue
        try:
            bk_root.mkdir(parents=True, exist_ok=True)
            backup_path = None
            if local_path.is_file():
                backup_path = bk_root / ("%s__%s" % (key or local_path.stem, local_path.name))
                shutil.copy2(local_path, backup_path)
            local_path.write_bytes(blob)
        except OSError as exc:
            stats["errors"].append("%s 写入失败：%s" % (local_path.name, exc))
            if progress:
                progress("  x 覆盖失败：%s（%s）" % (local_path.name, exc))
            continue
        stats["upgraded"] += 1
        stats["upgraded_files"].append({
            "name": label, "key": key, "file": str(local_path),
            "backup": str(backup_path) if backup_path else "",
            "from_version": local_version or "?",
            "to_version": remote_version or "?",
        })
        if progress:
            progress("  + 已升级：%s  %s -> %s  ->  %s%s"
                     % (label, local_version or "?", remote_version or "?", local_path,
                        ("（已备份 %s）" % backup_path.name) if backup_path else ""))
    if progress:
        progress("一键升级完成：共 %d 个，升级 %d，跳过 %d，失败 %d"
                 % (stats["total"], stats["upgraded"], stats["skipped"], len(stats["errors"])))
    return stats


# --------------------------------------------------------------------------
# 报告与状态文件
# --------------------------------------------------------------------------


def summarize(results) -> dict:
    summary = {status: 0 for status in ALL_STATUSES}
    for item in results:
        summary[item["status"]] = summary.get(item["status"], 0) + 1
    summary["total"] = len(results)
    summary["upgraded"] = sum(1 for item in results if item.get("upgraded"))
    return summary


def build_report_markdown(ctx: dict, results, summary: dict, timestamp: str, action: str) -> str:
    lines = []
    lines.append("# 洛雪音乐音源校验报告")
    lines.append("")
    lines.append("- 生成时间：%s" % timestamp)
    lines.append("- 工具版本：%s %s" % (TOOL_NAME, TOOL_VERSION))
    lines.append("- 运行模式：%s" % action)
    lines.append("- 安装目标目录：`%s`（%s）" % (ctx["local_dir"], ctx["local_dir_source"]))
    lines.append("- 校验模式：纯在线播放级校验（歌曲可播放判定）")
    lines.append("- 音源清单：`%s`" % ctx["sources_file"])
    lines.append("- 超时 / 重试：%ss / %s 次" % (ctx["timeout"], ctx["retries"]))
    lines.append("- 镜像数量：%s" % len(ctx["mirrors"]))
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append("| 指标 | 数量 |")
    lines.append("| --- | ---: |")
    lines.append("| 音源总数 | %s |" % summary.get("total", 0))
    for status in ALL_STATUSES:
        count = summary.get(status, 0)
        if count:
            lines.append("| %s | %s |" % (status, count))
    lines.append("| 本次升级 / 安装 | %s |" % summary.get("upgraded", 0))
    lines.append("")
    lines.append("## 明细")
    lines.append("")
    lines.append("| 音源 | 远端版本 | 存活 | 状态 | 命中地址 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in results:
        lines.append(
            "| %s (%s) | %s | %s | %s | %s | %s |"
            % (
                item["name"],
                item["key"],
                item["remote_version"] or "-",
                item.get("alive") or "-",
                item["status"],
                ("`%s`" % item["used_url"]) if item["used_url"] else "-",
                item["note"] or "-",
            )
        )
    lines.append("")

    upgraded = [item for item in results if item.get("upgraded")]
    if upgraded:
        lines.append("## 已执行升级 / 安装")
        lines.append("")
        for item in upgraded:
            lines.append("- %s (%s)：%s" % (item["name"], item["key"], item.get("note") or ""))
            if item.get("target_file"):
                lines.append("  - 写入：`%s`" % item["target_file"])
            if item.get("backup_file"):
                lines.append("  - 备份：`%s`" % item["backup_file"])
        lines.append("")

    failed = [item for item in results if item["status"] == STATUS_FAILED]
    if failed:
        lines.append("## 抓取失败详情")
        lines.append("")
        for item in failed:
            lines.append("- %s (%s)" % (item["name"], item["key"]))
            for err in item.get("errors", []):
                lines.append("  - %s" % err)
        lines.append("")

    if ctx.get("local_dir_source") == "未检测到洛雪音乐，回退到工具内置目录":
        lines.append("> 提示：本机未检测到洛雪音乐数据目录，已回退使用工具内置 sources 目录。")
        lines.append("> 如需写入实际音源目录，请用 `--dir \"<你的音源目录>\"` 手动指定。")
        lines.append("")

    return "\n".join(lines) + "\n"


def write_outputs(ctx: dict, results, summary: dict, action: str) -> dict:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    report_dir = Path(ctx["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / ("report_%s.md" % stamp)
    report_path.write_text(
        build_report_markdown(ctx, results, summary, timestamp, action), encoding="utf-8"
    )

    serializable = []
    for item in results:
        row = {k: v for k, v in item.items() if k != "_data"}
        serializable.append(row)

    state = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "time": timestamp,
        "mode": action,
        "check_only": ctx["check_only"],
        "force": ctx["force"],
        "sources_file": ctx["sources_file"],
        "local_dir": str(ctx["local_dir"]),
        "local_dir_source": ctx["local_dir_source"],
        "timeout": ctx["timeout"],
        "retries": ctx["retries"],
        "mirrors": list(ctx["mirrors"]),
        "summary": summary,
        "sources": serializable,
        "report_file": str(report_path),
    }
    state_path = Path(ctx["state_file"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return {"state_file": str(state_path), "report_file": str(report_path), "state": state}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="洛雪音乐音源自动校验升级工具（仅用 Python 标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python lx_source_updater.py --check                仅纯在线播放级校验，不写本地文件（默认）\n"
            "  python lx_source_updater.py                        纯在线播放级校验并安装/覆盖到目标目录\n"
            "  python lx_source_updater.py --force                强制下载并覆盖（含失效音源）\n"
            "  python lx_source_updater.py --dir \"D:\\lx-sources\"  指定安装目标目录\n"
            "  python lx_source_updater.py --check --json         以 JSON 输出结果\n"
            "  python lx_source_updater.py --clean-dead --dir \"D:\\lx-sources\"\n"
            "                            对目标目录已下载音源逐个播放级校验，失效的移入回收站\n"
            "  python lx_source_updater.py --upgrade-selected --dir \"D:\\lx-sources\"\n"
            "                            仅升级存活且(远端版本更高||SHA-256不同)的已下载音源\n"
            "  python lx_source_updater.py --upgrade-selected --keys \"key1,key2\"\n"
            "                            仅对指定 key 的音源执行一键升级\n"
        ),
    )
    parser.add_argument("--check", action="store_true", help="仅校验，不下载覆盖本地音源文件")
    parser.add_argument("--force", action="store_true", help="强制重新下载并覆盖本地文件（含失效音源）")
    parser.add_argument("--clean-dead", action="store_true",
                        help="清除失效音源：对目标目录已下载脚本逐个播放级校验，失效的移入回收站（不弹确认）")
    parser.add_argument("--upgrade-selected", action="store_true",
                        help="一键升级：仅存活(可用可播放)且(远端版本更高||SHA-256不同)的已下载音源才覆盖升级，其余跳过")
    parser.add_argument("--keys", default=None,
                        help="配合 --upgrade-selected 使用：逗号分隔的选中音源 key 列表；缺省为全部可用项")
    parser.add_argument("--sources", default=None, help="可选外部音源清单 JSON 路径；缺省时全量在线扫描全部候选仓库")
    parser.add_argument("--dir", default=None, help="安装目标目录（在线脚本下载位置，手动指定，覆盖自动探测）")
    parser.add_argument("--probe-timeout", type=float, default=5.0,
                        help="存活探测单次超时秒数（默认 5）")
    parser.add_argument("--live-cache-ttl", type=float, default=LIVE_CACHE_TTL,
                        help="存活探测结果缓存有效秒数（默认 86400）")
    parser.add_argument("--clear-live-cache", action="store_true",
                        help="清空存活探测缓存并退出")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出结果（便于脚本消费）")
    parser.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数（默认取清单配置）")
    parser.add_argument("--retries", type=int, default=None, help="每个 URL 的重试次数（默认取清单配置）")
    parser.add_argument("--version", action="version", version="%s %s" % (TOOL_NAME, TOOL_VERSION))
    return parser


def main(argv=None) -> int:
    setup_console()
    args = build_arg_parser().parse_args(argv)
    set_log_to_stdout(not args.json)

    if args.clear_live_cache:
        cleared = clear_liveness_cache()
        if args.json:
            emit(json.dumps({"cleared": cleared, "tool": TOOL_NAME, "tool_version": TOOL_VERSION},
                            ensure_ascii=False, indent=2))
        else:
            emit("已清空存活探测缓存：%s" % cleared)
        return 0

    if args.clean_dead:
        local_dir, local_desc = resolve_local_dir(args.dir)
        liveness_cache = load_liveness_cache()
        log("=" * 62)
        log("%s %s" % (TOOL_NAME, TOOL_VERSION))
        log("模式：清除失效音源（--clean-dead）")
        log("目标目录：%s（%s）" % (local_dir, local_desc))
        log("=" * 62)
        stats = clean_dead(
            local_dir, timeout=args.timeout if args.timeout is not None else 20.0,
            retries=args.retries if args.retries is not None else 1,
            mirrors=list(BUILTIN_MIRRORS), probe_timeout=args.probe_timeout,
            progress=log, liveness_cache=liveness_cache,
        )
        saved_cache = save_liveness_cache(liveness_cache)
        if args.json:
            emit(json.dumps({
                "tool": TOOL_NAME, "tool_version": TOOL_VERSION,
                "mode": "clean-dead", "local_dir": str(local_dir),
                "local_dir_source": local_desc, "stats": stats,
                "live_cache_file": saved_cache,
            }, ensure_ascii=False, indent=2))
        else:
            emit("清除失效音源完成：共 %d 个，删除 %d，保留 %d，跳过 %d，失败 %d"
                 % (stats["total"], stats["deleted"], stats["kept"],
                    stats["skipped"], stats["failed"]))
            if stats["deleted_files"]:
                emit("已删除（移入回收站）：")
                for item in stats["deleted_files"]:
                    emit("  - %s：%s" % (item["name"], item["file"]))
            if stats["kept_files"]:
                emit("已保留：%d 个" % len(stats["kept_files"]))
            if stats["errors"]:
                emit("失败：")
                for err in stats["errors"]:
                    emit("  x %s" % err)
            if saved_cache and not args.json:
                emit("存活探测缓存已更新：%s" % saved_cache)
        return 0

    config = load_sources(args.sources)
    liveness_cache = load_liveness_cache()

    if config is not None:
        timeout = args.timeout if args.timeout is not None else float(config.get("timeout", 20) or 20)
        retries = args.retries if args.retries is not None else int(config.get("retries", 1) or 0)
        mirrors = config.get("mirrors") or list(BUILTIN_MIRRORS)
        sources = config.get("sources") or []
        sources_desc = str(config.get("_path", args.sources))
    else:
        # 全量在线校验：扫描全部候选仓库（复用 discover 的并发扫描/去重/磁盘缓存），
        # 扫描时逐条做播放级存活探测，去重后直接作为校验对象。
        from lx_toolkit_app import dedupe_records, load_discover_repos, scan_repos
        repos = load_discover_repos()
        mirrors = list(BUILTIN_MIRRORS)
        timeout = args.timeout if args.timeout is not None else 20.0
        retries = args.retries if args.retries is not None else 1
        log("全量在线校验：扫描 %d 个候选仓库（并发抓取 + 逐条播放级存活探测 + 去重）…" % len(repos))
        records, messages = scan_repos(
            repos, 40, timeout, stop_event=None, progress=log, keep_data=True,
            liveness_cache=liveness_cache, probe_timeout=args.probe_timeout,
        )
        for message in messages:
            log("· " + message)
        sources = dedupe_records(records)
        log("全量在线校验：去重后共 %d 条音源（原始 %d 条）" % (len(sources), len(records)))
        sources_desc = "全量在线扫描（%d 个候选仓库）" % len(repos)

    local_dir, local_desc = resolve_local_dir(args.dir)
    local_dir = Path(local_dir)
    local_dir_exists = local_dir.is_dir()

    action = "纯在线播放级校验 + 安装" if not args.check else "纯在线播放级校验 (--check)"
    if args.upgrade_selected:
        action = "纯在线播放级校验 + 一键升级（仅存活且远端版本更高/SHA-256不同）"
    if args.force:
        action += " [--force]"

    ctx = {
        "check_only": bool(args.check),
        "force": bool(args.force),
        "sources_file": sources_desc,
        "local_dir": local_dir,
        "local_dir_source": local_desc,
        "local_dir_exists": local_dir_exists,
        "mirrors": list(mirrors),
        "timeout": timeout,
        "retries": retries,
        "probe_timeout": args.probe_timeout,
        "live_cache_ttl": args.live_cache_ttl,
        "liveness_cache": liveness_cache,
        "report_dir": DEFAULT_REPORT_DIR,
        "state_file": DEFAULT_STATE_FILE,
        "backup_root": DEFAULT_BACKUP_ROOT,
    }

    if not args.json:
        log("=" * 62)
        log("%s %s" % (TOOL_NAME, TOOL_VERSION))
        log("模式：%s" % action)
        log("校验模式：纯在线播放级校验（歌曲可播放判定）")
        log("安装目标目录：%s（%s）" % (local_dir, local_desc))
        log("音源清单：%s（%s 个音源，%s 个镜像，超时 %ss，重试 %s 次）"
            % (ctx["sources_file"], len(sources), len(mirrors), timeout, retries))
        if not local_dir_exists:
            log("[提示] 安装目标目录当前不存在：%s" % local_dir)
            log("       纯在线校验模式无需本地目录；安装时将自动创建该目录。")
        if local_dir_desc_startswith_fallback(local_desc):
            log("[提示] 未检测到本机洛雪音乐数据目录，已回退使用工具内置目录。")
            log("       可执行时用 --dir \"<你的音源目录>\" 手动指定。")
        log("=" * 62)

    results = []
    for source in sources:
        if not isinstance(source, dict) or not (
            source.get("key") or source.get("url") or source.get("raw_url") or source.get("used_url")
        ):
            log("[警告] 跳过无效音源条目：%r" % (source,))
            continue
        if source.get("repo"):
            # 在线扫描记录：已含播放级存活探测结果与脚本文本，直接映射为校验结果
            result = record_to_result(source)
        else:
            result = process_source(source, ctx)
        fname = source.get("file") or (source.get("path", "").rsplit("/", 1)[-1]
                                       if source.get("path") else "%s.js" % source.get("key", ""))
        result["target_file"] = str(local_dir / fname)
        results.append(result)

    # 执行升级 / 安装（在线模式：把远端最新脚本下载/覆盖到目标目录，无本地文件时直接安装）
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(ctx["backup_root"]) / stamp
    upgrade_stats = None
    if not args.check:
        if not local_dir_exists:
            local_dir.mkdir(parents=True, exist_ok=True)
            ctx["local_dir_exists"] = True
        if args.upgrade_selected:
            keys = {k.strip() for k in args.keys.split(",") if k.strip()} if args.keys else None
            selected = [r for r in results if keys is None or r["key"] in keys]
            log("一键升级：选中 %d 条音源（keys=%s）" % (
                len(selected), ",".join(sorted(keys)) if keys else "全部"))
            upgrade_stats = upgrade_selected(
                selected, local_dir, timeout=timeout, retries=retries, mirrors=list(mirrors),
                stop_event=None, progress=log, backup_dir=backup_dir,
                probe_timeout=args.probe_timeout, liveness_cache=liveness_cache,
            )
        else:
            for result in results:
                if result["_data"] is None:
                    continue
                status = result["status"]
                # 在线模式：默认只安装「可用(可播放)/无法判定」，自动跳过「失效」；--force 强制安装失效项
                need = status in (STATUS_AVAILABLE, STATUS_UNKNOWN) or args.force
                tid = "安装"
                if args.force and status == STATUS_DEAD:
                    tid = "强制安装(失效)"
                if not need:
                    if status == STATUS_DEAD:
                        log("[%s] 已跳过失效音源（%s）" % (result["key"], result.get("alive_note") or "接口探测失败"))
                    continue
                try:
                    target, backup_path = upgrade_source(result, ctx, backup_dir)
                    result["upgraded"] = True
                    log("[%s] %s -> %s%s" % (
                        result["key"], tid, target,
                        ("  （已备份至 %s）" % backup_path) if backup_path else "",
                    ))
                except Exception as exc:
                    result["upgraded"] = False
                    result["note"] = (result["note"] + "；" if result["note"] else "") + "写入失败: %s" % exc
                    log("[%s] 写入失败: %s" % (result["key"], exc))

    saved_cache = save_liveness_cache(liveness_cache)
    if saved_cache and not args.json:
        log("存活探测缓存已更新：%s" % saved_cache)

    summary = summarize(results)
    outputs = write_outputs(ctx, results, summary, action)

    if args.json:
        payload = {
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "time": outputs["state"]["time"],
            "mode": action,
            "local_dir": str(local_dir),
            "local_dir_source": local_desc,
            "summary": summary,
            "upgrade_stats": upgrade_stats,
            "sources": [{k: v for k, v in r.items() if k != "_data"} for r in results],
            "report_file": outputs["report_file"],
            "state_file": outputs["state_file"],
            "live_cache_file": saved_cache,
        }
        emit(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    log("")
    log("-" * 62)
    for item in results:
        emit("[%-7s] %-10s 存活: %-5s 远端: %-10s 状态: %s%s"
             % (
                 item["key"],
                 item["name"],
                 item.get("alive") or "-",
                 item["remote_version"] or "-",
                 item["status"],
                 ("  -> " + item["target_file"]) if item.get("upgraded") else "",
             ))
    log("-" * 62)
    summary_line = " | ".join("%s %s" % (status, summary.get(status, 0)) for status in ALL_STATUSES)
    emit("")
    emit("汇总: %s | 音源总数 %s | 本次升级/安装 %s"
         % (summary_line, summary.get("total", 0), summary.get("upgraded", 0)))
    if upgrade_stats:
        emit("一键升级统计：共 %d 个，升级 %d，跳过 %d，失败 %d"
             % (upgrade_stats["total"], upgrade_stats["upgraded"],
                upgrade_stats["skipped"], len(upgrade_stats["errors"])))
        for item in upgrade_stats["skipped_details"]:
            emit("  跳过：%s（%s）" % (item["name"], item["reason"]))
    emit("状态文件: %s" % outputs["state_file"])
    emit("Markdown 报告: %s" % outputs["report_file"])
    return 0


def local_dir_desc_startswith_fallback(desc: str) -> bool:
    return "回退" in (desc or "")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n[已中断] 用户取消操作。")
        sys.exit(130)
