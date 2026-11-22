#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoProxyDownloader —— 基于 aria2 的智能代理下载器

1. 读取 gfwlist.txt（base64 或明文均可），逐条 URL 判断是否需要走 HTTP 代理；
2. 命中的 URL 才给 aria2 传 --all-proxy，其余显式直连；
3. 下载完成后把服务器 Last-Modified 应用到本地文件（aria2 -R 先做一遍，再用 HEAD 校验兜底）；
4. gfwlist 可从 GitHub 自动拉取并缓存，按过期策略刷新。

仅依赖 Python 3.9+ 标准库和 aria2c。
"""

import argparse
import base64
import concurrent.futures as cfut
import email.utils
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_GFWLIST_URL = "https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt"
NO_CONFIG = "__builtin_defaults__"      # 没找到配置文件时的哨兵值

DEFAULTS = {
    "aria2": "",                   # 留空则自动在脚本目录 / PATH 里找 aria2c
    "ffmpeg": "",                  # 留空则自动在脚本目录 / PATH 里找 ffmpeg（HLS 用）
    "dir": "%USERPROFILE%\\Downloads",
    "proxy": "http://127.0.0.1:951",
    "proxy_mode": "auto",          # auto | always | never
    "gfwlist_enabled": True,
    "gfwlist": "gfwlist.txt",
    "gfwlist_url": DEFAULT_GFWLIST_URL,
    "gfwlist_max_age": 24,         # 小时
    "refresh_gfwlist": False,      # 每次运行都强制刷新
    "jobs": 3,
    "split": 5,
    "connections": 5,
    "min_split_size": "1M",
    "mtime": True,
    "overwrite": False,
    "fallback": True,              # 首轮失败后换一种出网方式再试一次
    "retry": 5,
    "first_tries": 2,              # 首轮出网方式的最大尝试次数，用尽即换方式
    "learn": True,                 # 记住“gfwlist 未收录但直连不通”的域名
    "learn_path": "hosts-learned.json",
    "timeout": 60,
    "connect_timeout": 20,
    "file_allocation": "none",
    "user_agent": DEFAULT_USER_AGENT,
    "referer": "",
    "headers": [],
    "extra_aria2_args": [],
    "always_direct": [],
    "always_proxy": [],
}

# ---------------------------------------------------------------- 语言

def _system_language():
    """系统界面语言：Windows 看 UI 语言，其他平台退回 locale。"""
    if sys.platform == "win32":
        try:
            import ctypes
            # 主语言 ID（低 10 位）0x04 = 中文，其余一律按英文
            if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x04:
                return "zh"
            return "en"
        except Exception:
            pass
    try:
        import locale
        name = locale.getdefaultlocale()[0] or ""
    except Exception:
        name = ""
    return "zh" if name.lower().startswith("zh") else "en"


def resolve_language(argv=None):
    """优先级：APD_LANG 环境变量 > --lang 参数 > 系统语言。"""
    pref = (os.environ.get("APD_LANG") or "").strip().lower()
    if not pref:
        args = list(sys.argv[1:] if argv is None else argv)
        for i, a in enumerate(args):
            if a == "--lang" and i + 1 < len(args):
                pref = args[i + 1].strip().lower()
                break
            if a.startswith("--lang="):
                pref = a.split("=", 1)[1].strip().lower()
                break
    if pref.startswith("zh") or pref in ("cn", "chinese"):
        return "zh"
    if pref.startswith("en") or pref == "english":
        return "en"
    return _system_language()


LANG = resolve_language()


def T(zh, en):
    """按当前语言取文案：T("中文", "English")。"""
    return zh if LANG == "zh" else en


def relang(argv=None):
    """main() 拿到真实 argv 后再定一次语言（支持 --lang / --lang=en）。"""
    global LANG
    LANG = resolve_language(argv)
    return LANG


# ---------------------------------------------------------------- 日志 / 进度板

LIVE = None       # 由 main() 创建：一行原地刷新的进度板


class Log:
    def __init__(self, quiet=False, verbose=False):
        self.quiet = quiet
        self.verbose = verbose
        self._lock = threading.Lock()

    def __call__(self, msg, prefix=""):
        if self.quiet and not prefix.startswith("!"):
            return
        with self._lock:
            if LIVE is not None:
                LIVE.clear()      # 先擦掉进度行，否则日志会接在它后面
            print((prefix + " " + msg) if prefix else msg, flush=True)

    def verbose_log(self, msg):
        if self.verbose:
            self(msg)


def _safe_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class LiveProgress:
    """一行原地刷新的进度板：单任务显示字节/百分比/速度，多任务压成一条。

    只用 \\r 回退 + 右侧补空格覆盖旧内容，不依赖 ANSI，老控制台也不会打出乱码。
    """

    def __init__(self, enabled=True):
        self.enabled = bool(enabled) and sys.stdout.isatty()
        self._lock = threading.Lock()
        self._segs = {}
        self._drawn = False
        self._cols = 0

    def _erase(self):
        if self._drawn:
            sys.stdout.write("\r" + " " * self._cols + "\r")
            self._drawn = False

    def _draw(self):
        if not self._segs:
            self._erase()
            sys.stdout.flush()
            return
        self._cols = max(40, shutil.get_terminal_size((100, 24)).columns - 1)
        text = " | ".join(self._segs[k] for k in sorted(self._segs))
        if len(text) > self._cols:      # 太窄就只留 "[序号] 名字 百分比"
            text = " | ".join(s.split("  ")[0] for s in (self._segs[k] for k in sorted(self._segs)))
        sys.stdout.write("\r" + text[:self._cols].ljust(self._cols))
        sys.stdout.flush()
        self._drawn = True

    def update(self, idx, seg):
        if not self.enabled or not seg:
            return
        with self._lock:
            self._segs[idx] = seg
            self._draw()

    def drop(self, idx):
        if not self.enabled:
            return
        with self._lock:
            self._segs.pop(idx, None)
            self._draw()

    def clear(self):
        if not self.enabled:
            return
        with self._lock:
            self._erase()
            sys.stdout.flush()

    def close(self):
        if not self.enabled:
            return
        with self._lock:
            self._segs.clear()
            self._erase()
            sys.stdout.flush()


# ---------------------------------------------------------------- gfwlist 规则

@lru_cache(maxsize=None)
def _compile(pattern):
    return re.compile(pattern, re.I)


def _wildcard_to_regex(pattern):
    """AdBlock 风格通配：* 任意字符，^ 分隔符（除字母数字和 -_.% 之外的字符）。"""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append(".*")
        elif ch == "^":
            out.append(r"([^\w\-_.%]|$)")
        else:
            out.append(re.escape(ch))
    return "".join(out)


@dataclass(frozen=True)
class Rule:
    kind: str          # domain | prefix | suffix | contains | regex
    pattern: str       # domain 规则里是小写域名；regex 里是正则源串
    exception: bool
    raw: str

    def matches(self, url, host):
        k = self.kind
        if k == "domain":
            return host == self.pattern or host.endswith("." + self.pattern)
        if k == "prefix":
            return url.startswith(self.pattern)
        if k == "suffix":
            return url.endswith(self.pattern) or url.rstrip("/").endswith(self.pattern)
        if k == "contains":
            return self.pattern in url
        if k == "regex":
            return _compile(self.pattern).search(url) is not None
        return False


def decode_gfwlist(raw_bytes):
    """gfwlist.txt 通常是 base64 编码的 AdBlock 列表，也兼容明文。"""
    text = raw_bytes.decode("utf-8", "replace").strip()
    if "[AutoProxy" in text[:200] or text.startswith("!") or text.startswith("||"):
        return text
    b = "".join(text.split())
    try:
        b += "=" * ((-len(b)) % 4)
        return base64.b64decode(b).decode("utf-8", "replace")
    except Exception:
        return text


def parse_gfwlist(text):
    rules = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "![":
            continue
        exception = s.startswith("@@")
        body = s[2:].strip() if exception else s
        if not body:
            continue
        # /正则/
        if len(body) > 2 and body.startswith("/") and body.endswith("/"):
            try:
                _compile(body[1:-1])
            except re.error:
                continue
            rules.append(Rule("regex", body[1:-1], exception, s))
            continue
        # ||域名锚点
        if body.startswith("||"):
            dom = re.split(r"[/^]", body[2:], maxsplit=1)[0].strip().lower()
            if dom:
                rules.append(Rule("domain", dom, exception, s))
            continue
        # |前缀
        if body.startswith("|"):
            val = body[1:]
            if val.endswith("|"):
                val = val[:-1]
            if val.endswith("^"):
                val = val[:-1]
            if val:
                rules.append(Rule("prefix", val.lower(), exception, s))
            continue
        # 后缀|
        if body.endswith("|") and not body.endswith("\\|"):
            val = body[:-1]
            if val:
                rules.append(Rule("suffix", val.lower(), exception, s))
            continue
        # 其余按“包含”处理
        val = body.rstrip("^")
        if not val:
            continue
        if "*" in val or "^" in val:
            rules.append(Rule("regex", _wildcard_to_regex(val), exception, s))
        else:
            rules.append(Rule("contains", val.lower(), exception, s))
    return rules


def host_of(url):
    """主机名小写化并去掉 FQDN 尾部点，供域名规则比对。"""
    return (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")


class GFWList:
    def __init__(self, rules, source, updated_at=None):
        self.rules = rules
        self.source = source
        self.updated_at = updated_at

    def first_match(self, url):
        host = host_of(url)
        for r in self.rules:
            if r.matches(url.lower(), host):
                return r
        return None

    @property
    def block_count(self):
        return sum(1 for r in self.rules if not r.exception)


# ---------------------------------------------------------------- 网络工具

def _opener(proxy):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    handlers.append(urllib.request.HTTPRedirectHandler())
    return urllib.request.build_opener(*handlers)


def http_get(url, proxy, timeout, user_agent=DEFAULT_USER_AGENT):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})
    with _opener(proxy).open(req, timeout=timeout) as r:
        return r.read()


_PROXY_ALIVE_CACHE = {}


def proxy_reachable(proxy):
    """快速探测代理端口是否可连，用来决定 gfwlist 抓取顺序与启动告警。"""
    if proxy in _PROXY_ALIVE_CACHE:
        return _PROXY_ALIVE_CACHE[proxy]
    ok = False
    try:
        sp = urllib.parse.urlsplit(proxy if "//" in proxy else "http://" + proxy)
        host = sp.hostname or "127.0.0.1"
        port = sp.port or 80
        with socket.create_connection((host, port), timeout=1.0):
            ok = True
    except OSError:
        ok = False
    _PROXY_ALIVE_CACHE[proxy] = ok
    return ok


def fetch_gfwlist(cfg, log):
    """先走代理再直连（反过来的顺序在受限网络里要白等一次超时）。"""
    url = cfg["gfwlist_url"]
    proxy = cfg["proxy"] if cfg["proxy_mode"] != "never" else ""
    attempts = []
    if proxy and proxy_reachable(proxy):
        attempts.append(proxy)
        attempts.append("")
    else:
        attempts.append("")
        if proxy:
            attempts.append(proxy)
    last_err = None
    for p in attempts:
        tag = T("代理", "proxy") if p else T("直连", "direct")
        try:
            log(T("拉取 gfwlist（%s）：%s", "fetching gfwlist (%s): %s") % (tag, url))
            raw = http_get(url, p, timeout=40, user_agent=cfg["user_agent"])
            return raw
        except Exception as exc:
            last_err = exc
            log("  " + T("%s 抓取失败：%s", "%s fetch failed: %s") % (tag, exc))
    raise RuntimeError(T("gfwlist 抓取失败：%s", "gfwlist fetch failed: %s") % last_err)


def expand_path(value):
    """展开 %USERPROFILE% / ~ 这类写法，便于配置在不同机器上通用。"""
    if not value:
        return value
    return os.path.expandvars(os.path.expanduser(str(value)))


def resolve_local_path(name, cfg):
    """相对路径优先按程序所在文件夹解析，其次默认下载目录、当前目录。"""
    p = Path(expand_path(name))
    if p.is_absolute():
        return p
    bases = (SCRIPT_DIR, Path(expand_path(cfg["dir"])), Path.cwd())
    for base in bases:
        cand = base / p
        if cand.exists():
            return cand
    return SCRIPT_DIR / p


def resolve_gfwlist_path(cfg):
    return resolve_local_path(cfg["gfwlist"], cfg)


def load_gfwlist(cfg, log):
    if not cfg["gfwlist_enabled"]:
        log(T("已禁用 gfwlist，只按学习记录 / 配置名单判断出网方式",
              "gfwlist disabled; deciding from learned hosts and config lists only"))
        return None
    path = resolve_gfwlist_path(cfg)
    stale = True
    if path.exists():
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        stale = age_h > cfg["gfwlist_max_age"]
    if cfg["refresh_gfwlist"] or not path.exists() or stale:
        try:
            raw = fetch_gfwlist(cfg, log)
            text = decode_gfwlist(raw)
            if path.exists():
                try:
                    shutil.copy2(path, str(path) + ".bak")
                except OSError:
                    pass
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(path) + ".tmp")
            tmp.write_bytes(raw)       # 原样保存：gfwlist 上游是 base64，保持格式一致
            os.replace(str(tmp), str(path))
            log(T("gfwlist 已更新：%s（%d 行）", "gfwlist updated: %s (%d lines)")
                % (path, len(text.splitlines())))
        except Exception as exc:
            if path.exists():
                log("! " + T("更新失败，改用本地缓存：%s", "refresh failed, using the local cache: %s") % exc)
            else:
                log("! " + T("无本地缓存且更新失败：%s", "no local cache and refresh failed: %s") % exc)
                return None
    if not path.exists():
        return None
    text = decode_gfwlist(path.read_bytes())
    rules = parse_gfwlist(text)
    log(T("gfwlist 规则：%d 条（拦截 %d / 例外 %d），来源 %s",
          "gfwlist rules: %d (block %d / allow %d), from %s")
        % (len(rules), sum(1 for r in rules if not r.exception),
           sum(1 for r in rules if r.exception), path.name))
    return GFWList(rules, str(path), path.stat().st_mtime)


# ---------------------------------------------------------------- 代理决策

def _is_local_host(host):
    if not host:
        return True
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if re.match(r"^10\.", host) or re.match(r"^192\.168\.", host) or re.match(r"^169\.254\.", host):
        return True
    m = re.match(r"^172\.(\d+)\.", host)
    if m and 16 <= int(m.group(1)) <= 31:
        return True
    return False


@dataclass
class Decision:
    use_proxy: bool
    reason: str
    source: str = ""      # config | never | local | gfwlist | learned | default


class LearnedHosts:
    """记住“gfwlist 没收录、但直连不通”的域名，下次不再白试一轮直连。

    只记录“换了出网方式之后成功”的结论，失败不写入，所以不会因为一次抖动
    就把域名永久钉在某个方向上；用 --no-learn 或删掉 json 即可全部失效。
    """

    def __init__(self, path, enabled=True):
        self.path = Path(path) if path else None
        self.enabled = bool(enabled and path)
        self.hosts = {}
        self._dirty = False
        if self.enabled and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.hosts = data.get("hosts", {}) or {}
            except Exception:
                self.hosts = {}

    def get(self, host):
        if not self.enabled or not host:
            return None
        entry = self.hosts.get(host)
        return (entry or {}).get("mode")

    def note(self, host, mode, why):
        if not self.enabled or not host:
            return False
        if (self.hosts.get(host) or {}).get("mode") == mode:
            return False
        self.hosts[host] = {"mode": mode, "why": why,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        self._dirty = True
        return True

    def save(self):
        if not self.enabled or not self._dirty:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(str(self.path) + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "hosts": self.hosts},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(self.path))
            self._dirty = False
            return True
        except OSError:
            return False

    def clear(self):
        self.hosts = {}
        self._dirty = True


def decide(url, cfg, gfw, learned=None):
    mode = cfg["proxy_mode"]
    if not url.lower().startswith(("http://", "https://")):
        return Decision(False, T("非 HTTP(S) 协议，不套代理", "non-HTTP(S) scheme, no proxy"), "config")
    host = host_of(url)
    if mode == "never":
        return Decision(False, "proxy_mode=never", "never")
    if _is_local_host(host):
        return Decision(False, T("本地 / 内网地址", "local or private address"), "local")
    if host and host in [h.lower() for h in cfg["always_direct"]]:
        return Decision(False, T("always_direct 命中", "matched always_direct"), "config")
    if mode == "always":
        return Decision(True, "proxy_mode=always", "config")
    if host and host in [h.lower() for h in cfg["always_proxy"]]:
        return Decision(True, T("always_proxy 命中", "matched always_proxy"), "config")
    learned_mode = learned.get(host) if learned else None
    if gfw is not None:
        rule = gfw.first_match(url)
        if rule is not None:
            # gfwlist 意见优先：白名单直连、拦截规则走代理
            if rule.exception:
                return Decision(False, T("gfwlist 白名单：%s", "gfwlist allowlist: %s") % rule.raw, "gfwlist")
            return Decision(True, T("gfwlist 命中：%s", "gfwlist match: %s") % rule.raw, "gfwlist")
    # gfwlist 没收录（或压根没加载）：看历史学习结论
    if learned_mode == "proxy":
        return Decision(True, T("历史学习：该域名直连曾失败、代理可通",
                                "learned: direct failed before, proxy works"), "learned")
    if learned_mode == "direct" and gfw is not None:
        return Decision(False, T("历史学习：该域名走代理曾失败、直连可通",
                                 "learned: proxy failed before, direct works"), "learned")
    if gfw is None:
        return Decision(False, T("gfwlist 不可用，按直连", "gfwlist unavailable, going direct"), "default")
    return Decision(False, T("gfwlist 未命中", "no gfwlist match"), "default")


# ---------------------------------------------------------------- 输出文件名

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _clean_name(name, fallback):
    name = _BAD_CHARS.sub("_", str(name or "")).strip(" .")
    if not name:
        name = fallback
    if len(name) > 180:
        stem, ext = os.path.splitext(name)
        name = stem[: 180 - len(ext)] + ext
    return name


def guess_filename(url, cfg):
    name = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).strip()
    if not name:
        # 目录式 URL：用主机名当文件名，避免多个站点都落成 index.html
        name = (host_of(url) or "download") + ".html"
    return _clean_name(name, "download.bin")


HLS_SUFFIXES = (".m3u8", ".m3u")


def is_hls(url):
    """路径以 .m3u8 / .m3u 结尾就当 HLS 流处理（查询串里的 token 不影响判断）。"""
    path = urllib.parse.urlsplit(url).path.lower().rstrip("/")
    return path.endswith(HLS_SUFFIXES)


def hls_out_name(url, cfg):
    """HLS 输出名：把地址里的 index.m3u8 换成 index.mp4。"""
    base = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).strip()
    stem, _ext = os.path.splitext(base)
    if not stem:
        stem = host_of(url) or "stream"
    return _clean_name(stem + ".mp4", "stream.mp4")


# ---------------------------------------------------------------- aria2 调用

def find_aria2(cfg):
    cand = expand_path(cfg.get("aria2") or "").strip()
    if cand:
        p = Path(cand)
        if p.is_file():
            return str(p.resolve())
        alt = SCRIPT_DIR / p.name
        if alt.is_file():
            return str(alt)
        w = shutil.which(cand)
        if w:
            return w
    for name in ("aria2c.exe", "aria2c"):
        p = SCRIPT_DIR / name
        if p.is_file():
            return str(p)
        w = shutil.which(name)
        if w:
            return w
    return None


FFMPEG_HELP_URL = "https://www.gyan.dev/ffmpeg/builds/"


def find_ffmpeg(cfg):
    """HLS 引擎：先看配置，再看程序目录，最后看 PATH。"""
    cand = expand_path(cfg.get("ffmpeg") or "").strip()
    if cand:
        p = Path(cand)
        if p.is_file():
            return str(p.resolve())
        alt = SCRIPT_DIR / p.name
        if alt.is_file():
            return str(alt)
        w = shutil.which(cand)
        if w:
            return w
    for name in ("ffmpeg.exe", "ffmpeg"):
        p = SCRIPT_DIR / name
        if p.is_file():
            return str(p)
        w = shutil.which(name)
        if w:
            return w
    return None


def ffmpeg_hint():
    return T(
        "这条地址是 HLS 流（.m3u8），需要 ffmpeg 才能合并成 mp4："
        "请到 https://www.gyan.dev/ffmpeg/builds/ 下载 ffmpeg-release-essentials.7z，"
        "把里面的 ffmpeg.exe 放到 %s 或加进 PATH（也可用配置项 ffmpeg / --ffmpeg 指定路径）",
        "this URL is an HLS stream (.m3u8), which needs ffmpeg to be remuxed into mp4: "
        "grab ffmpeg-release-essentials.7z from https://www.gyan.dev/ffmpeg/builds/, "
        "put its ffmpeg.exe into %s or add it to PATH (or point at it with the ffmpeg config key / --ffmpeg)") \
        % SCRIPT_DIR


def ffmpeg_missing_short():
    return T("缺少 ffmpeg，无法把 HLS 合并成 mp4（见上方提示，或 https://www.gyan.dev/ffmpeg/builds/）",
             "ffmpeg is missing, so this HLS stream cannot be remuxed into mp4 "
             "(see the note above, or https://www.gyan.dev/ffmpeg/builds/)")


def build_ffmpeg_argv(ffmpeg, cfg, url, out_path, use_proxy):
    """ffmpeg 走 HLS：不解码、只换封装，所以 -c copy。"""
    argv = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y" if cfg["overwrite"] else "-n",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-user_agent", cfg["user_agent"],
    ]
    if cfg["referer"]:
        argv += ["-referer", cfg["referer"]]
    if cfg["headers"]:
        argv += ["-headers", "\r\n".join(cfg["headers"]) + "\r\n"]
    if use_proxy:
        # http / https 输入都认这一个 key（ffmpeg 没有单独的 https_proxy 参数）
        argv += ["-http_proxy", cfg["proxy"]]
    argv += ["-i", url, "-c", "copy", str(out_path)]
    return argv


def _ffmpeg_progress(line):
    """解析 `-progress pipe:1` 的 key=value 行：total_size / speed / progress。"""
    if "=" not in line:
        return None
    k, _, v = line.strip().partition("=")
    return k.strip(), v.strip()


def run_ffmpeg(ffmpeg, cfg, item, log, live, use_proxy):
    argv = build_ffmpeg_argv(ffmpeg, cfg, item.url, item.out, use_proxy)
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        item.error = T("找不到 ffmpeg：%s", "ffmpeg not found: %s") % ffmpeg
        return 127
    tail = []

    def drain_stderr():
        for line in proc.stderr:
            s = line.rstrip("\r\n")
            if s:
                tail.append(s)
                if len(tail) > 40:
                    tail.pop(0)

    th = threading.Thread(target=drain_stderr, daemon=True)
    th.start()
    total = 0
    speed = ""
    last_milestone = t0
    for line in proc.stdout:
        kv = _ffmpeg_progress(line)
        if not kv:
            continue
        k, v = kv
        if k == "total_size":
            try:
                total = int(v)
            except ValueError:
                pass
        elif k == "speed":
            speed = v
        elif k == "progress" and v != "end":
            seg = _ffmpeg_segment(item, total, speed, cfg)
            if seg:
                if live is not None:
                    live.update(item.idx, seg)
                elif cfg.get("_milestones") and (time.time() - last_milestone) >= 5:
                    last_milestone = time.time()
                    log("  " + T("进度 %s：%s%s", "progress %s: %s%s")
                        % (item.out.name, fmt_size(total) if total else "?",
                           (" " + speed) if speed else ""))
    code = proc.wait()
    th.join(timeout=2)
    item.elapsed = time.time() - t0
    item.log_tail = tail
    if live is not None:
        live.drop(item.idx)
    return code


def _ffmpeg_segment(item, total, speed, cfg):
    size = fmt_size(total) if total else "?"
    if cfg["_total"] > 1:
        seg = "[%d] %s %s" % (item.idx, item.out.name[:22], size)
    else:
        seg = "[%d] %s  %s" % (item.idx, item.out.name, size)
    if speed:
        seg += " " + speed
    return seg


PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?[KMG]?i?B)/(\d+(?:\.\d+)?[KMG]?i?B)\((\d+)%\)")


def _progress_fields(line):
    """从 aria2 的 `[#1 1.2MiB/2.6MiB(45%) CN:5 DL:1.2MiB ETA:2s]` 里取字段。"""
    m = PROGRESS_RE.search(line)
    pct = re.search(r"\((\d+)%\)", line)
    if not m and not pct:
        return None
    spd = re.search(r"DL:([^\s\]]+)", line)
    eta = re.search(r"ETA:([^\s\]]+)", line)
    return {
        "done": m.group(1) if m else "?",
        "total": m.group(2) if m else "?",
        "pct": int(pct.group(1)) if pct else 100,
        "speed": spd.group(1) if spd else "",
        "eta": eta.group(1) if eta else "",
    }


def _progress_segment(item, f, cfg):
    """进度板里的一条：多任务时压短，单任务时带字节数、速度和 ETA。"""
    if cfg["_total"] > 1:
        seg = "[%d] %s %d%%" % (item.idx, item.out.name[:22], f["pct"])
        return seg + (" " + f["speed"] + "/s" if f["speed"] else "")
    seg = "[%d] %s  %s/%s %d%%" % (item.idx, item.out.name, f["done"], f["total"], f["pct"])
    if f["speed"]:
        seg += " " + f["speed"] + "/s"
    if f["eta"]:
        seg += " ETA " + f["eta"]
    return seg


def build_aria2_argv(aria2, cfg, url, out_path, use_proxy, max_tries=None):
    argv = [
        aria2,
        "--no-conf",
        "--dir=" + str(out_path.parent),
        "--out=" + out_path.name,
        "--continue=true",
        "--auto-file-renaming=false",
        "--file-allocation=" + cfg["file_allocation"],
        "--remote-time=" + ("true" if cfg["mtime"] else "false"),
        "--split=" + str(cfg["split"]),
        "--max-connection-per-server=" + str(cfg["connections"]),
        "--min-split-size=" + str(cfg["min_split_size"]),
        "--max-tries=" + str(max_tries if max_tries else cfg["retry"]),
        "--retry-wait=5",
        "--timeout=" + str(cfg["timeout"]),
        "--connect-timeout=" + str(cfg["connect_timeout"]),
        "--user-agent=" + cfg["user_agent"],
        "--console-log-level=notice",
        "--summary-interval=0",
        "--quiet=false",
        "--allow-overwrite=" + ("true" if cfg["overwrite"] else "false"),
    ]
    if cfg["referer"]:
        argv.append("--referer=" + cfg["referer"])
    for h in cfg["headers"]:
        argv.append("--header=" + h)
    if use_proxy:
        argv.append("--all-proxy=" + cfg["proxy"])
    else:
        argv.append("--all-proxy=")   # 显式清空，确保直连
    argv.extend(str(a) for a in cfg["extra_aria2_args"])
    argv.append(url)
    return argv


@dataclass
class Item:
    idx: int
    url: str
    out: Path
    decision: Decision
    engine: str = "aria2"          # aria2 | ffmpeg（HLS）
    status: str = "pending"
    exit_code: int | None = None
    size: int = 0
    elapsed: float = 0.0
    mtime_epoch: float | None = None
    mtime_note: str = ""
    used_proxy: bool = False
    error: str = ""
    learned_note: str = ""
    log_tail: list = field(default_factory=list)


def run_aria2(aria2, cfg, item, log, live, use_proxy, max_tries=None):
    argv = build_aria2_argv(aria2, cfg, item.url, item.out, use_proxy, max_tries)
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        item.error = T("找不到 aria2c：%s", "aria2c not found: %s") % aria2
        return 127
    tail = []
    shown_pct = 0
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)
        f = _progress_fields(line)
        if f is None:
            if cfg.get("verbose"):
                log("  aria2: " + line)
            continue
        if live is not None:
            live.update(item.idx, _progress_segment(item, f, cfg))
        elif cfg.get("_milestones") and f["pct"] >= shown_pct + 10 and f["pct"] < 100:
            # 非交互输出（重定向到日志、多任务无 tty）时，按 10% 一档报进度
            shown_pct = f["pct"]
            extra = (" " + f["speed"] + "/s") if f["speed"] else ""
            log("  " + T("进度 %s：%d%%（%s/%s%s）", "progress %s: %d%% (%s/%s%s)") % (
                item.out.name, f["pct"], f["done"], f["total"], extra))
    code = proc.wait()
    item.elapsed = time.time() - t0
    item.log_tail = tail
    if live is not None:
        live.drop(item.idx)
    return code


# ---------------------------------------------------------------- 时间戳

def remote_last_modified(url, proxy, timeout=20, user_agent=DEFAULT_USER_AGENT):
    """返回 (epoch|None, 原始头|None, 错误说明)。HEAD 不行就退化成 Range GET。"""
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    try:
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with _opener(proxy).open(req, timeout=timeout) as r:
            lm = r.headers.get("Last-Modified")
        if lm:
            return _lm_to_epoch(lm), lm, None
    except Exception as exc:
        head_err = str(exc)
    else:
        head_err = T("HEAD 无 Last-Modified", "HEAD had no Last-Modified")
    try:
        h = dict(headers)
        h["Range"] = "bytes=0-0"
        req = urllib.request.Request(url, headers=h)
        with _opener(proxy).open(req, timeout=timeout) as r:
            lm = r.headers.get("Last-Modified")
        if lm:
            return _lm_to_epoch(lm), lm, None
        return None, None, T("服务器未返回 Last-Modified（只有 ETag 或动态内容）",
                             "server sent no Last-Modified (ETag only, or dynamic content)")
    except Exception as exc:
        return None, None, T("取服务器时间失败：%s / %s", "failed to read server time: %s / %s") % (
            head_err, exc)


def _lm_to_epoch(value):
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            return time.mktime(dt.timetuple())
        return dt.timestamp()
    except Exception:
        return None


def apply_remote_mtime(cfg, item, log):
    if not cfg["mtime"]:
        item.mtime_note = T("已禁用", "disabled")
        return
    if not item.out.exists():
        item.mtime_note = T("文件不存在", "file missing")
        return
    proxy = cfg["proxy"] if item.used_proxy else ""
    epoch, raw, err = remote_last_modified(item.url, proxy, timeout=max(10, cfg["connect_timeout"]),
                                           user_agent=cfg["user_agent"])
    if epoch is None:
        item.mtime_note = err or T("未知", "unknown")
        return
    item.mtime_epoch = epoch
    cur = item.out.stat().st_mtime
    if abs(cur - epoch) < 2:
        item.mtime_note = T("aria2 已保留服务器时间", "kept by aria2")
        return
    try:
        os.utime(str(item.out), (epoch, epoch))
        item.mtime_note = T("已修正为服务器时间（原 %s）", "corrected to the server time (was %s)") % time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(cur))
    except OSError as exc:
        item.mtime_note = T("设置时间失败：%s", "failed to set the timestamp: %s") % exc


# ---------------------------------------------------------------- 单文件流程

def should_skip(cfg, out_path):
    if cfg["overwrite"] or not out_path.exists():
        return False
    if out_path.with_name(out_path.name + ".aria2").exists():
        return False
    try:
        return out_path.stat().st_size > 0
    except OSError:
        return False


# aria2 里只有“换一种出网方式可能有用”的错误才值得再试一轮；
# 3=资源不存在 24=认证失败 9=磁盘不足 13=文件已存在 等都不重试。
FALLBACK_CODES = {1, 2, 4, 5, 6, 7, 19, 20, 22, 23}


def download_one(aria2, ffmpeg, cfg, item, log, live=None, learned=None):
    # 同一批次里前面的同域名任务已经把结论学出来了，后面就别再白试一轮直连
    if learned is not None and item.decision.source == "default" and not item.decision.use_proxy:
        if learned.get(host_of(item.url)) == "proxy":
            item.decision = Decision(
                True, T("同批次学习：该域名直连刚失败过，改走代理",
                        "learned in this batch: direct just failed, switching to proxy"),
                "learned")
    if item.out.exists() and cfg["overwrite"] is False and should_skip(cfg, item.out):
        item.status = "SKIP"
        item.used_proxy = item.decision.use_proxy
        item.size = item.out.stat().st_size
        item.mtime_note = T("本地已存在", "already present locally")
        return
    head = (T("代理下载（%s）", "Downloading with proxy (%s)") % cfg["proxy"]) if item.decision.use_proxy \
        else T("直连下载", "Direct Downloading")
    log("[%d/%d] %s%s%s" % (item.idx, cfg["_total"], head, T("：", ": "), item.url), prefix="")
    if item.engine == "ffmpeg" and not ffmpeg:
        item.status = "FAIL"
        item.error = ffmpeg_missing_short()
        log("[%d/%d] %s %s" % (item.idx, cfg["_total"], T("失败", "failed"), item.error))
        return
    rounds = [item.decision.use_proxy]
    if cfg["fallback"]:
        rounds.append(not item.decision.use_proxy)
    for i, use_proxy in enumerate(rounds):
        tag = T("代理", "proxy") if use_proxy else T("直连", "direct")
        if i > 0:
            log("[%d/%d] %s" % (item.idx, cfg["_total"],
                               T("首轮失败，改为%s重试", "first attempt failed, retrying via %s") % tag),
                prefix="")
        if item.engine == "ffmpeg":
            # ffmpeg 自己带 http 层重试，而且没有“退出码分类”，非 0 一律当成可换出网方式
            code = run_ffmpeg(ffmpeg, cfg, item, log, live, use_proxy)
            retryable = True
        else:
            # 首轮用 first_tries 次快速失败，避免在被墙的域名上死等 retry 那么久
            tries = cfg["first_tries"] if (len(rounds) > 1 and i == 0) else cfg["retry"]
            code = run_aria2(aria2, cfg, item, log, live, use_proxy, tries)
            retryable = code in FALLBACK_CODES
        if code == 0:
            item.status = "OK"
            item.exit_code = 0
            item.used_proxy = use_proxy
            if learned is not None and i > 0:
                why = (T("直连失败 → 代理成功", "direct failed -> proxy worked") if use_proxy
                       else T("代理失败 → 直连成功", "proxy failed -> direct worked"))
                if learned.note(host_of(item.url), "proxy" if use_proxy else "direct", why):
                    item.learned_note = T("已记入学习记录（%s），下次直接用%s",
                                          "saved to learned hosts (%s); next run uses %s directly") % (
                        why, tag)
            break
        if code == 13:   # aria2: file already existed
            item.status = "SKIP"
            item.exit_code = 13
            item.used_proxy = use_proxy
            item.mtime_note = T("aria2 报告文件已存在", "aria2 reported the file already exists")
            return
        engine_name = "ffmpeg" if item.engine == "ffmpeg" else "aria2"
        if i == len(rounds) - 1:
            item.status = "FAIL"
            item.exit_code = code
            item.used_proxy = rounds[0]
            suffix = T("（直连、代理均已尝试）", " (both direct and proxy tried)") if len(rounds) > 1 else ""
            if item.engine == "ffmpeg":
                # ffmpeg 的退出码在 Windows 上会变成一长串无意义数字，直接用它最后一行报错
                detail = item.log_tail[-1] if item.log_tail else T("退出码 %s", "exit code %s") % code
                item.error = T("ffmpeg 合并失败：%s%s", "ffmpeg failed: %s%s") % (detail, suffix)
            else:
                item.error = T("aria2 退出码 %s%s", "aria2 exit code %s%s") % (code, suffix)
        elif not retryable:
            item.status = "FAIL"
            item.exit_code = code
            item.used_proxy = use_proxy
            item.error = T("%s 退出码 %s（该错误换出网方式无用，不再重试）",
                           "%s exit code %s (switching egress would not help, not retried)") % (
                engine_name, code)
            break
    if item.status == "OK":
        if item.out.exists():
            item.size = item.out.stat().st_size
        if item.engine == "ffmpeg":
            # HLS 是边下边合并，源是一串分片，服务器时间戳对合并出来的 mp4 没有意义
            item.mtime_note = T("已跳过：ffmpeg 合并的 mp4 不保留服务器时间",
                                "skipped: an ffmpeg-remuxed mp4 has no single server timestamp")
        else:
            apply_remote_mtime(cfg, item, log)
        parts = ["PROXY" if item.used_proxy else "DIRECT"]
        if item.engine == "ffmpeg":
            parts.append("HLS")
        parts.append(fmt_size(item.size) if item.size else "?")
        if item.elapsed:
            parts.append("%.1fs" % item.elapsed)
        log("[%d/%d] %s %s%s" % (
            item.idx, cfg["_total"], T("完成", "done"), item.out.name,
            T("（%s）", " (%s)") % T("，", ", ").join(parts)))
        if item.learned_note:
            log("        " + item.learned_note)
    else:
        log("[%d/%d] %s %s%s" % (item.idx, cfg["_total"], T("失败", "failed"), item.url,
                                T("：%s", ": %s") % item.error))
        for line in item.log_tail[-8:]:
            log("    " + line)


def fmt_size(n):
    if not n:
        return "0"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return ("%d %s" % (f, u)) if u == "B" else ("%.1f %s" % (f, u))
        f /= 1024.0
    return str(n)


# ---------------------------------------------------------------- 配置

def load_config_file(explicit):
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit(T("配置文件不存在：%s", "config file not found: %s") % p)
        return json.loads(p.read_text(encoding="utf-8")), p
    for p in (SCRIPT_DIR / "AutoProxyDownloader.json", Path.cwd() / "AutoProxyDownloader.json"):
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8")), p
    return {}, None


def build_parser():
    p = argparse.ArgumentParser(
        prog="AutoProxyDownloader",
        description=T("基于 aria2 的下载器：按 gfwlist 逐条判断是否走 HTTP 代理，并保留服务器修改时间。",
                      "aria2 wrapper that decides per URL whether to use an HTTP proxy (via gfwlist) "
                      "and preserves the server's Last-Modified timestamp."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=T("示例：\n", "Examples:\n") +
               "  python AutoProxyDownloader.py https://example.com/a.zip -d D:\\dl\n"
               "  python AutoProxyDownloader.py --input urls.txt --jobs 4\n"
               "  python AutoProxyDownloader.py https://x.com/f --explain\n"
               "  python AutoProxyDownloader.py --selftest\n"
               "  python AutoProxyDownloader.py --refresh-gfwlist\n",
    )
    S = argparse.SUPPRESS
    p.add_argument("urls", nargs="*", help=T("要下载的 URL", "URLs to download"))
    p.add_argument("--input", metavar="FILE",
                   help=T("从文本文件读取 URL（每行一个，# 为注释）",
                          "read URLs from a text file (one per line, # for comments)"))
    p.add_argument("-d", "--dir", default=S,
                   help=T("保存目录（默认读取配置）", "output directory (default comes from the config file)"))
    p.add_argument("-o", "--out", default=S,
                   help=T("指定文件名（仅单 URL 时可用）", "explicit file name (single URL only)"))
    p.add_argument("--proxy", default=S,
                   help=T("HTTP 代理，默认 http://127.0.0.1:951", "HTTP proxy, default http://127.0.0.1:951"))
    p.add_argument("--proxy-mode", default=S, choices=["auto", "always", "never"],
                   help=T("auto=按 gfwlist 判断；always=全走代理；never=全直连",
                          "auto=decide via gfwlist; always=proxy everything; never=always direct"))
    p.add_argument("--gfwlist", default=S, help=T("gfwlist.txt 路径", "path to gfwlist.txt"))
    p.add_argument("--gfwlist-url", default=S, help=T("gfwlist 在线地址", "gfwlist upstream URL"))
    p.add_argument("--gfwlist-max-age", type=float, default=S,
                   help=T("缓存过期小时数", "cache age limit in hours"))
    p.add_argument("--refresh-gfwlist", action="store_true", default=S,
                   help=T("运行前强制刷新 gfwlist", "force a gfwlist refresh before running"))
    p.add_argument("--no-gfwlist", dest="gfwlist_enabled", action="store_false", default=S,
                   help=T("完全不使用 gfwlist", "ignore gfwlist entirely"))
    p.add_argument("--jobs", type=int, default=S, help=T("并发下载数", "concurrent downloads"))
    p.add_argument("--split", type=int, default=S, help=T("aria2 单文件分片数", "aria2 segments per file"))
    p.add_argument("--connections", type=int, default=S,
                   help=T("单服务器最大连接数", "max connections per server"))
    p.add_argument("--min-split-size", default=S, help=T("分片最小尺寸", "minimum split size"))
    p.add_argument("--no-mtime", dest="mtime", action="store_false", default=S,
                   help=T("不保留服务器修改时间", "do not preserve the server timestamp"))
    p.add_argument("--mtime", dest="mtime", action="store_true", default=S,
                   help=T("保留服务器修改时间（覆盖配置里的 false）",
                          "preserve the server timestamp (overrides a false in the config)"))
    p.add_argument("--overwrite", action="store_true", default=S,
                   help=T("覆盖已存在的文件", "overwrite existing files"))
    p.add_argument("--no-fallback", dest="fallback", action="store_false", default=S,
                   help=T("失败后不换一种出网方式重试", "do not retry through the other egress mode"))
    p.add_argument("--fallback", dest="fallback", action="store_true", default=S,
                   help=T("失败后换一种出网方式重试（覆盖配置里的 false）",
                          "retry through the other egress mode (overrides a false in the config)"))
    p.add_argument("--retry", type=int, default=S, help=T("aria2 单次重试次数", "aria2 retries per attempt"))
    p.add_argument("--first-tries", type=int, default=S,
                   help=T("首轮出网方式的最大尝试次数，用完即换另一种方式（默认 2）",
                          "attempts before switching egress mode for the first round (default 2)"))
    p.add_argument("--no-learn", dest="learn", action="store_false", default=S,
                   help=T("不启用域名学习记录", "disable learned host preferences"))
    p.add_argument("--learn-path", default=S,
                   help=T("学习记录文件（默认 hosts-learned.json）",
                          "learned-hosts file (default hosts-learned.json)"))
    p.add_argument("--forget-learned", action="store_true",
                   help=T("清空学习记录后退出", "clear learned hosts and exit"))
    p.add_argument("--timeout", type=int, default=S, help=T("读写超时（秒）", "read/write timeout (s)"))
    p.add_argument("--connect-timeout", type=int, default=S,
                   help=T("连接超时（秒）", "connect timeout (s)"))
    p.add_argument("--file-allocation", default=S, choices=["none", "prealloc", "trunc", "falloc"])
    p.add_argument("--referer", default=S, help="Referer")
    p.add_argument("--header", dest="headers", action="append", default=S,
                   help=T("附加 HTTP 头，可重复", "extra HTTP header, repeatable"))
    p.add_argument("--extra-arg", dest="extra_aria2_args", action="append", default=S,
                   help=T("透传给 aria2c 的参数，可重复", "raw aria2c flag, repeatable"))
    p.add_argument("--aria2", default=S,
                   help=T("aria2c 可执行文件路径", "path to the aria2c executable"))
    p.add_argument("--ffmpeg", default=S,
                   help=T("ffmpeg 可执行文件路径（仅 HLS 流用得到，默认在程序目录 / PATH 里找）",
                          "path to the ffmpeg executable (HLS only; defaults to the program folder / PATH)"))
    p.add_argument("--config", default=S,
                   help=T("配置文件路径（默认 AutoProxyDownloader.json）",
                          "config file (default AutoProxyDownloader.json)"))
    p.add_argument("--lang", default=S, choices=["auto", "zh", "en"],
                   help=T("界面语言：auto=跟随系统，zh=中文，en=英文",
                          "output language: auto=follow the system, zh=Chinese, en=English"))
    p.add_argument("--dry-run", action="store_true",
                   help=T("只打印决策和命令，不下载", "print decisions and commands without downloading"))
    p.add_argument("--explain", action="store_true",
                   help=T("只解释每条 URL 走不走代理及命中的规则",
                          "explain the per-URL proxy decision and the matching rule"))
    p.add_argument("--selftest", action="store_true", help=T("跑内置自检", "run the built-in self-test"))
    p.add_argument("-v", "--verbose", action="store_true",
                   help=T("显示 aria2 原始输出", "show raw aria2 output"))
    p.add_argument("-q", "--quiet", action="store_true",
                   help=T("安静模式，只输出结果表", "quiet mode: only print the result table"))
    p.add_argument("--json", action="store_true",
                   help=T("以 JSON 输出结果（便于脚本消费）", "emit the result as JSON"))
    return p


LIST_KEYS = {"headers", "extra_aria2_args", "always_direct", "always_proxy"}


def merge_config(args):
    cfg = dict(DEFAULTS)
    file_cfg, file_path = load_config_file(getattr(args, "config", ""))
    for k, v in (file_cfg or {}).items():
        if k in cfg:
            cfg[k] = v
    for k, v in vars(args).items():
        if k in cfg:
            cfg[k] = v
    for k in LIST_KEYS:
        if not isinstance(cfg[k], list):
            cfg[k] = [cfg[k]] if cfg[k] else []
    cfg["_config_path"] = str(file_path) if file_path else NO_CONFIG
    cfg["_total"] = 1
    return cfg


# ---------------------------------------------------------------- 自检

SELFTEST_LIST = """[AutoProxy 0.2.9]
! 自检用
! checksum placeholder
@@||google.cn^
||google.com^
||githubusercontent.com^
||example.org^
|http://prefix.test/
suffix.test|
/path/to/regex\\.test/
blogspot.*.example.net^
"""


def selftest():
    cases = [
        # (url, 期望走代理, 说明)
        ("https://www.google.com/search?q=a", True, T("||google.com 普通域名", "||google.com plain domain")),
        ("https://mail.google.com/", True, T("域名锚点匹配子域", "domain anchor matches a subdomain")),
        ("https://www.google.cn/", False, T("@@ 白名单优先命中", "@@ allowlist wins")),
        ("https://raw.githubusercontent.com/a/b", True, "githubusercontent"),
        ("https://www.baidu.com/", False, T("未命中", "no match")),
        ("http://prefix.test/x", True, T("|前缀规则", "| prefix rule")),
        ("https://a.suffix.test/", True, T("后缀规则", "suffix rule")),
        ("https://host/path/to/regex.test/", True, T("正则规则", "regex rule")),
        ("https://blogspot.abc.example.net/", True, T("通配规则", "wildcard rule")),
        ("https://example.org.", True, T("||example.org 允许尾部点", "||example.org allows a trailing dot")),
        ("http://127.0.0.1:8080/", False, T("本地地址直连", "local address stays direct")),
    ]
    plain = SELFTEST_LIST
    b64 = base64.b64encode(plain.encode("utf-8")).decode("ascii")
    rules = parse_gfwlist(decode_gfwlist(b64.encode("utf-8")))
    gfw = GFWList(rules, "selftest")
    cfg = dict(DEFAULTS)
    cfg["always_direct"] = []
    cfg["always_proxy"] = []
    fails = 0
    for url, want, why in cases:
        d = decide(url, cfg, gfw)
        ok = d.use_proxy == want
        if not ok:
            fails += 1
        print("%s %-6s %s  <- %s" % ("PASS" if ok else "FAIL",
                                    T("代理", "proxy") if d.use_proxy else T("直连", "direct"), url, why))
    # base64 解码与明文解析一致性
    same = [r.raw for r in rules] == [r.raw for r in parse_gfwlist(plain)]
    print("%s %s" % ("PASS" if same else "FAIL",
                    T("base64 解码结果与明文一致", "base64 output parses the same as plain text")))
    fails += 0 if same else 1
    # 通配 ^ 分隔符
    r = Rule("regex", _wildcard_to_regex("bad^"), False, "bad^")
    print("%s %s" % ("PASS" if r.matches("http://bad/x", "bad") else "FAIL",
                     T("^ 分隔符正则可用", "^ separator works in wildcards")))
    # 学习记录：gfwlist 未收录、但曾直连失败 → 直接走代理
    learned = LearnedHosts(Path("selftest-learned.json"), enabled=True)
    learned.note("learned.test", "proxy", "selftest")
    d = decide("https://learned.test/a.zip", cfg, gfw, learned)
    print("%s %s%s" % ("PASS" if d.use_proxy else "FAIL",
                       T("学习记录让未收录域名直接走代理", "learned hosts send an unlisted domain straight to the proxy"),
                       T("（%s）", " (%s)") % d.reason))
    fails += 0 if d.use_proxy else 1
    # gfwlist 命中优先于学习记录
    learned.note("google.com", "direct", "selftest")
    d = decide("https://www.google.com/x", cfg, gfw, learned)
    print("%s %s%s" % ("PASS" if d.use_proxy else "FAIL",
                       T("gfwlist 命中优先于学习记录", "gfwlist beats learned hosts"),
                       T("（%s）", " (%s)") % d.reason))
    fails += 0 if d.use_proxy else 1
    # 学习记录落盘再读回
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        lp = Path(td) / "learned.json"
        w = LearnedHosts(lp, enabled=True)
        w.note("round.trip.test", "proxy", "selftest")
        w.save()
        r2 = LearnedHosts(lp, enabled=True)
        ok = r2.get("round.trip.test") == "proxy"
        print("%s %s" % ("PASS" if ok else "FAIL",
                        T("学习记录可落盘并读回", "learned hosts survive a save/load round trip")))
        fails += 0 if ok else 1

    # 双引擎：HLS 判定 / 输出名 / ffmpeg 参数
    for url, want, why in [
        ("https://v.test/hls/index.m3u8", True, T(".m3u8 走 ffmpeg", ".m3u8 goes to ffmpeg")),
        ("https://v.test/hls/master.m3u8?token=x", True, T("查询串不影响判定",
                                                          "a query string does not change the decision")),
        ("https://v.test/hls/playlist.m3u", True, T(".m3u 同样按 HLS", ".m3u counts as HLS too")),
        ("https://v.test/a.zip", False, T(".zip 仍走 aria2", ".zip still goes to aria2")),
    ]:
        got = is_hls(url)
        ok = got == want
        fails += 0 if ok else 1
        print("%s %-6s %s  <- %s" % ("PASS" if ok else "FAIL",
                                     "ffmpeg" if got else "aria2", url, why))
    name = hls_out_name("https://v.test/hls/index.m3u8?token=x", cfg)
    ok = name == "index.mp4"
    fails += 0 if ok else 1
    print("%s %s%s" % ("PASS" if ok else "FAIL",
                       T("输出名把 .m3u8 换成 .mp4", "the .m3u8 name becomes .mp4"),
                       T("（%s）", " (%s)") % name))
    argv_p = build_ffmpeg_argv("ffmpeg", cfg, "https://v.test/i.m3u8", Path("out/i.mp4"), True)
    argv_d = build_ffmpeg_argv("ffmpeg", cfg, "https://v.test/i.m3u8", Path("out/i.mp4"), False)
    ok = ("-c" in argv_p and argv_p[argv_p.index("-c") + 1] == "copy"
          and argv_p[argv_p.index("-http_proxy") + 1] == cfg["proxy"]
          and "-http_proxy" not in argv_d
          and argv_p[argv_p.index("-i") + 1] == "https://v.test/i.m3u8")
    fails += 0 if ok else 1
    print("%s %s" % ("PASS" if ok else "FAIL",
                     T("ffmpeg 用 -c copy，代理只在需要时加 -http_proxy",
                       "ffmpeg remuxes with -c copy and only adds -http_proxy when proxying")))
    print("\n" + T("自检结果：%s（%d 项失败）", "selftest: %s (%d failed)")
          % (T("全部通过", "all passed") if fails == 0 else T("有失败", "failures"), fails))
    return 0 if fails == 0 else 1


# ---------------------------------------------------------------- 输出

def print_explain(items, cfg):
    print(T("代理：%s   模式=%s   gfwlist=%s", "proxy: %s   mode=%s   gfwlist=%s") % (
        cfg["proxy"], cfg["proxy_mode"],
        Path(cfg["gfwlist"]).name if cfg["gfwlist_enabled"] else T("(已禁用)", "(disabled)")))
    for it in items:
        d = it.decision
        print("\n%s" % it.url)
        engine = T("ffmpeg（HLS → mp4）", "ffmpeg (HLS -> mp4)") if it.engine == "ffmpeg" else "aria2c"
        print(T("  引擎：%s", "  engine: %s") % engine)
        egress = (T("代理 %s", "proxy %s") % cfg["proxy"]) if d.use_proxy else T("直连", "direct")
        print(T("  出网：%s", "  egress: %s") % egress)
        print(T("  理由：%s", "  reason: %s") % d.reason)
        print(T("  保存：%s", "  save to: %s") % it.out)


def print_table(items, cfg):
    print("")
    wide = any(it.engine == "ffmpeg" for it in items)
    if wide:
        print(" #  STATUS  ENGINE  PROXY   SIZE          SERVER-MTIME        FILE")
    else:
        print(" #  STATUS  PROXY   SIZE          SERVER-MTIME        FILE")
    for it in items:
        cols = [it.idx, it.status] + (["ffmpeg" if it.engine == "ffmpeg" else "aria2"] if wide else []) \
            + ["PROXY" if it.used_proxy else "DIRECT",
               fmt_size(it.size) if it.size else "-",
               time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(it.mtime_epoch)) if it.mtime_epoch else "-",
               it.out.name]
        if wide:
            print("%2d  %-6s  %-6s  %-5s   %-12s  %-19s  %s" % tuple(cols))
        else:
            print("%2d  %-6s  %-5s   %-12s  %-19s  %s" % tuple(cols))
        if it.mtime_note and it.status in ("OK", "SKIP"):
            print(T("    时间戳：%s", "    mtime: %s") % it.mtime_note)
        if it.learned_note:
            print(T("    学习：%s", "    learned: %s") % it.learned_note)
        if it.status == "FAIL" and it.error:
            print(T("    错误：%s", "    error: %s") % it.error)


def emit_json(items):
    data = []
    for it in items:
        data.append({
            "url": it.url,
            "status": it.status,
            "used_proxy": it.used_proxy,
            "reason": it.decision.reason,
            "engine": it.engine,
            "path": str(it.out),
            "size": it.size,
            "server_mtime": int(it.mtime_epoch) if it.mtime_epoch else None,
            "mtime_note": it.mtime_note,
            "learned": it.learned_note,
            "elapsed": round(it.elapsed, 2),
            "error": it.error,
        })
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- main

def main(argv=None):
    global LIVE
    relang(argv)
    _safe_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    cfg = merge_config(args)
    cfg["verbose"] = args.verbose
    log = Log(quiet=args.quiet, verbose=args.verbose)

    learn_path = resolve_local_path(cfg["learn_path"], cfg) if cfg.get("learn_path") else None
    learned = LearnedHosts(learn_path, enabled=cfg["learn"])
    if args.forget_learned:
        learned.enabled = True
        learned.clear()
        learned.save()
        log(T("学习记录已清空：%s", "learned hosts cleared: %s")
            % (learn_path or T("(未配置路径)", "(no path configured)")))
        return 0

    urls = list(args.urls)
    if getattr(args, "input", None):
        text = Path(args.input).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                urls.append(s)
    urls = [u.strip().strip('"').strip("'") for u in urls if u.strip()]
    if not urls:
        parser.error(T("没有给出 URL（可加 --input 文件，或 --selftest / --refresh-gfwlist）",
                       "no URL given (use an --input file, or --selftest / --refresh-gfwlist)"))

    out_dir = Path(expand_path(cfg["dir"]))
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if cfg["_config_path"] == NO_CONFIG:
        log("! " + T("未找到 AutoProxyDownloader.json，按内置默认运行（内置默认与仓库里的配置一致，可自行新建一份）",
                     "AutoProxyDownloader.json not found, running on built-in defaults "
                     "(same values as the shipped config; create one when you want to edit it)"))

    aria2 = find_aria2(cfg)
    ffmpeg = find_ffmpeg(cfg)
    gfw = load_gfwlist(cfg, log)

    if cfg["proxy_mode"] != "never" and not proxy_reachable(cfg["proxy"]):
        log("! " + T("提示：代理 %s 当前连不上，命中 gfwlist 的 URL 会失败（可用 --no-fallback 关闭回落）",
                     "note: proxy %s is unreachable right now; gfwlist-matched URLs will fail "
                     "(use --no-fallback to turn the retry off)") % cfg["proxy"])

    items = []
    for i, url in enumerate(urls, 1):
        d = decide(url, cfg, gfw, learned)
        hls = is_hls(url)
        if getattr(args, "out", None) and len(urls) == 1:
            name = args.out
        else:
            name = hls_out_name(url, cfg) if hls else guess_filename(url, cfg)
        items.append(Item(i, url, out_dir / name, d, engine="ffmpeg" if hls else "aria2"))
    cfg["_total"] = len(items)

    if args.explain:
        print_explain(items, cfg)
        return 0

    if any(it.engine == "ffmpeg" for it in items) and not ffmpeg:
        log("! " + ffmpeg_hint())

    if not any(it.engine == "aria2" for it in items):
        pass          # 全 HLS 的批次不要求 aria2
    elif not aria2:
        log("! " + T("找不到 aria2c：把它放到 %s 或设置配置项 aria2 / --aria2",
                     "aria2c not found: put it in %s or set the aria2 config key / --aria2") % SCRIPT_DIR)
        return 2
    if aria2 and any(it.engine == "aria2" for it in items):
        log(T("aria2c：%s", "aria2c: %s") % aria2)
    if ffmpeg:
        log(T("ffmpeg：%s", "ffmpeg: %s") % ffmpeg)

    if args.dry_run:
        for it in items:
            egress = (T("代理 %s", "proxy %s") % cfg["proxy"]) if it.decision.use_proxy else T("直连", "direct")
            engine = T("引擎：ffmpeg（HLS → mp4）", "engine: ffmpeg (HLS -> mp4)") \
                if it.engine == "ffmpeg" else T("引擎：aria2c", "engine: aria2c")
            print(T("\n%s\n  %s\n  出网：%s（%s）\n  保存：%s",
                    "\n%s\n  %s\n  egress: %s (%s)\n  save to: %s") % (
                it.url, engine, egress, it.decision.reason, it.out))
            if it.engine == "ffmpeg":
                argv = build_ffmpeg_argv(ffmpeg or "ffmpeg", cfg, it.url, it.out, it.decision.use_proxy)
            else:
                argv = build_aria2_argv(aria2 or "aria2c", cfg, it.url, it.out, it.decision.use_proxy)
            print(T("  argv：%s", "  argv: %s") % subprocess.list2cmdline(argv))
        return 0

    LIVE = LiveProgress(enabled=not args.quiet)
    cfg["_milestones"] = (not args.quiet) and not args.json and not LIVE.enabled
    board = LIVE if LIVE.enabled else None     # 没 tty 就不走进度板，改用按 10% 一档的日志
    t0 = time.time()
    if cfg["jobs"] <= 1 or len(items) == 1:
        for it in items:
            download_one(aria2, ffmpeg, cfg, it, log, board, learned)
    else:
        with cfut.ThreadPoolExecutor(max_workers=min(cfg["jobs"], len(items))) as ex:
            list(ex.map(lambda it: download_one(aria2, ffmpeg, cfg, it, log, board, learned), items))
    LIVE.close()
    log(T("总耗时 %.1fs", "total %.1fs") % (time.time() - t0))
    if learned.save():
        log(T("学习记录已更新：%s（共 %d 个域名）", "learned hosts updated: %s (%d hosts)")
            % (learned.path.name, len(learned.hosts)))

    if args.json:
        emit_json(items)
    else:
        print_table(items, cfg)
        ok = sum(1 for i in items if i.status == "OK")
        skip = sum(1 for i in items if i.status == "SKIP")
        bad = sum(1 for i in items if i.status == "FAIL")
        log(T("完成 %d，跳过 %d，失败 %d", "done %d, skipped %d, failed %d") % (ok, skip, bad))
        kept = sum(1 for i in items if i.mtime_epoch)
        if cfg["mtime"]:
            log(T("其中 %d 个文件带服务器时间戳", "%d file(s) carry the server timestamp") % kept)
    return 0 if all(i.status in ("OK", "SKIP") for i in items) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n" + T("已中断", "interrupted"))
        sys.exit(130)
