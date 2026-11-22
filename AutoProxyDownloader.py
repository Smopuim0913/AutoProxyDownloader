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

DEFAULTS = {
    "aria2": "",                   # 留空则自动在脚本目录 / PATH 里找 aria2c
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

# ---------------------------------------------------------------- 日志小工具

class Log:
    def __init__(self, quiet=False, verbose=False):
        self.quiet = quiet
        self.verbose = verbose
        self._lock = threading.Lock()

    def __call__(self, msg, prefix=""):
        if self.quiet and not prefix.startswith("!"):
            return
        with self._lock:
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
        tag = "代理" if p else "直连"
        try:
            log("拉取 gfwlist（%s）：%s" % (tag, url))
            raw = http_get(url, p, timeout=40, user_agent=cfg["user_agent"])
            return raw
        except Exception as exc:
            last_err = exc
            log("  %s 抓取失败：%s" % (tag, exc))
    raise RuntimeError("gfwlist 抓取失败：%s" % last_err)


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
        log("已禁用 gfwlist，只按学习记录 / 配置名单判断出网方式")
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
            log("gfwlist 已更新：%s（%d 行）" % (path, len(text.splitlines())))
        except Exception as exc:
            if path.exists():
                log("! 更新失败，改用本地缓存：%s" % exc)
            else:
                log("! 无本地缓存且更新失败：%s" % exc)
                return None
    if not path.exists():
        return None
    text = decode_gfwlist(path.read_bytes())
    rules = parse_gfwlist(text)
    log("gfwlist 规则：%d 条（其中拦截 %d / 例外 %d），来源 %s"
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
        return Decision(False, "非 HTTP(S) 协议，不套代理", "config")
    host = host_of(url)
    if mode == "never":
        return Decision(False, "proxy_mode=never", "never")
    if _is_local_host(host):
        return Decision(False, "本地/内网地址", "local")
    if host and host in [h.lower() for h in cfg["always_direct"]]:
        return Decision(False, "always_direct 命中", "config")
    if mode == "always":
        return Decision(True, "proxy_mode=always", "config")
    if host and host in [h.lower() for h in cfg["always_proxy"]]:
        return Decision(True, "always_proxy 命中", "config")
    learned_mode = learned.get(host) if learned else None
    if gfw is not None:
        rule = gfw.first_match(url)
        if rule is not None:
            # gfwlist 意见优先：白名单直连、拦截规则走代理
            if rule.exception:
                return Decision(False, "gfwlist 白名单：%s" % rule.raw, "gfwlist")
            return Decision(True, "gfwlist 命中：%s" % rule.raw, "gfwlist")
    # gfwlist 没收录（或压根没加载）：看历史学习结论
    if learned_mode == "proxy":
        return Decision(True, "历史学习：该域名直连曾失败、代理可通", "learned")
    if learned_mode == "direct" and gfw is not None:
        return Decision(False, "历史学习：该域名走代理曾失败、直连可通", "learned")
    if gfw is None:
        return Decision(False, "gfwlist 不可用，按直连", "default")
    return Decision(False, "gfwlist 未命中", "default")


# ---------------------------------------------------------------- 输出文件名

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def guess_filename(url, cfg):
    name = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).strip()
    if not name:
        # 目录式 URL：用主机名当文件名，避免多个站点都落成 index.html
        name = (host_of(url) or "download") + ".html"
    name = _BAD_CHARS.sub("_", name).strip(" .")
    if not name:
        name = "download.bin"
    if len(name) > 180:
        stem, ext = os.path.splitext(name)
        name = stem[: 180 - len(ext)] + ext
    return name


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


PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?[KMG]?i?B)/(\d+(?:\.\d+)?[KMG]?i?B)\((\d+)%\)")


def _pick_progress(line):
    m = PROGRESS_RE.search(line)
    if not m:
        pct = re.search(r"\((\d+)%\)", line)
        if not pct:
            return None
        m = None
    if m:
        done, total, pct = m.group(1), m.group(2), m.group(3)
    else:
        done, total, pct = "?", "?", re.search(r"\((\d+)%\)", line).group(1)
    spd = re.search(r"DL:([^\s\]]+)", line)
    eta = re.search(r"ETA:([^\s\]]+)", line)
    return "  %s/%s %s%%%s%s" % (
        done, total, pct,
        "  " + spd.group(1) + "/s" if spd else "",
        "  ETA " + eta.group(1) if eta else "",
    )


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


def run_aria2(aria2, cfg, item, log, show_progress, use_proxy, max_tries=None):
    argv = build_aria2_argv(aria2, cfg, item.url, item.out, use_proxy, max_tries)
    t0 = time.time()
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        item.error = "找不到 aria2c：%s" % aria2
        return 127
    tail = []
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)
        p = _pick_progress(line)
        if p and show_progress:
            sys.stdout.write("\r[%d] %s%s\x1b[K" % (item.idx, item.out.name[:24], p))
            sys.stdout.flush()
        elif cfg.get("verbose") and not p:
            log("  aria2: " + line)
    code = proc.wait()
    item.elapsed = time.time() - t0
    item.log_tail = tail
    if show_progress and item.elapsed > 1:
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()
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
        head_err = "HEAD 无 Last-Modified"
    try:
        h = dict(headers)
        h["Range"] = "bytes=0-0"
        req = urllib.request.Request(url, headers=h)
        with _opener(proxy).open(req, timeout=timeout) as r:
            lm = r.headers.get("Last-Modified")
        if lm:
            return _lm_to_epoch(lm), lm, None
        return None, None, "服务器未返回 Last-Modified（只有 ETag 或动态内容）"
    except Exception as exc:
        return None, None, "取服务器时间失败：%s / %s" % (head_err, exc)


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
        item.mtime_note = "已禁用"
        return
    if not item.out.exists():
        item.mtime_note = "文件不存在"
        return
    proxy = cfg["proxy"] if item.used_proxy else ""
    epoch, raw, err = remote_last_modified(item.url, proxy, timeout=max(10, cfg["connect_timeout"]),
                                           user_agent=cfg["user_agent"])
    if epoch is None:
        item.mtime_note = err or "未知"
        return
    item.mtime_epoch = epoch
    cur = item.out.stat().st_mtime
    if abs(cur - epoch) < 2:
        item.mtime_note = "aria2 已保留服务器时间"
        return
    try:
        os.utime(str(item.out), (epoch, epoch))
        item.mtime_note = "已修正为服务器时间（原 %s）" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cur))
    except OSError as exc:
        item.mtime_note = "设置时间失败：%s" % exc


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


def download_one(aria2, cfg, item, log, show_progress, learned=None):
    # 同一批次里前面的同域名任务已经把结论学出来了，后面就别再白试一轮直连
    if learned is not None and item.decision.source == "default" and not item.decision.use_proxy:
        if learned.get(host_of(item.url)) == "proxy":
            item.decision = Decision(True, "同批次学习：该域名直连刚失败过，改走代理", "learned")
    if item.out.exists() and cfg["overwrite"] is False and should_skip(cfg, item.out):
        item.status = "SKIP"
        item.used_proxy = item.decision.use_proxy
        item.size = item.out.stat().st_size
        item.mtime_note = "本地已存在"
        return
    rounds = [item.decision.use_proxy]
    if cfg["fallback"]:
        rounds.append(not item.decision.use_proxy)
    for i, use_proxy in enumerate(rounds):
        tag = "代理" if use_proxy else "直连"
        if i > 0:
            log("[%d/%d] 首轮失败，改用%s重试" % (item.idx, cfg["_total"], tag), prefix="")
        # 首轮用 first_tries 次快速失败，避免在被墙的域名上死等 retry 那么久
        tries = cfg["first_tries"] if (len(rounds) > 1 and i == 0) else cfg["retry"]
        code = run_aria2(aria2, cfg, item, log, show_progress, use_proxy, tries)
        if code == 0:
            item.status = "OK"
            item.exit_code = 0
            item.used_proxy = use_proxy
            if learned is not None and i > 0:
                why = "直连失败→代理成功" if use_proxy else "代理失败→直连成功"
                if learned.note(host_of(item.url), "proxy" if use_proxy else "direct", why):
                    item.learned_note = "已记入学习记录（%s），下次直接用%s" % (
                        why, tag)
            break
        if code == 13:   # aria2: file already existed
            item.status = "SKIP"
            item.exit_code = 13
            item.used_proxy = use_proxy
            item.mtime_note = "aria2 报告文件已存在"
            return
        if i == len(rounds) - 1:
            item.status = "FAIL"
            item.exit_code = code
            item.used_proxy = rounds[0]
            item.error = "aria2 退出码 %s%s" % (
                code, "（直连/代理均已尝试）" if len(rounds) > 1 else "")
        elif code not in FALLBACK_CODES:
            item.status = "FAIL"
            item.exit_code = code
            item.used_proxy = use_proxy
            item.error = "aria2 退出码 %s（该错误换出网方式无用，不再重试）" % code
            break
    if item.status == "OK":
        if item.out.exists():
            item.size = item.out.stat().st_size
        apply_remote_mtime(cfg, item, log)
        log("[%d/%d] 完成 %s（%s，%s" % (item.idx, cfg["_total"], item.out.name,
                                       fmt_size(item.size) if item.size else "?",
                                       "代理" if item.used_proxy else "直连")
            + (", %.1fs)" % item.elapsed if item.elapsed else ")"))
        if item.learned_note:
            log("        " + item.learned_note)
    else:
        log("[%d/%d] 失败 %s：%s" % (item.idx, cfg["_total"], item.url, item.error))
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
            raise SystemExit("配置文件不存在：%s" % p)
        return json.loads(p.read_text(encoding="utf-8")), p
    for p in (SCRIPT_DIR / "AutoProxyDownloader.json", Path.cwd() / "AutoProxyDownloader.json"):
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8")), p
    return {}, None


def build_parser():
    p = argparse.ArgumentParser(
        prog="AutoProxyDownloader",
        description="基于 aria2 的下载器：按 gfwlist 逐条判断是否走 HTTP 代理，并保留服务器修改时间。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python AutoProxyDownloader.py https://example.com/a.zip -d D:\\dl\n"
               "  python AutoProxyDownloader.py --input urls.txt --jobs 4\n"
               "  python AutoProxyDownloader.py https://x.com/f --explain\n"
               "  python AutoProxyDownloader.py --selftest\n"
               "  python AutoProxyDownloader.py --refresh-gfwlist\n",
    )
    S = argparse.SUPPRESS
    p.add_argument("urls", nargs="*", help="要下载的 URL")
    p.add_argument("--input", metavar="FILE", help="从文本文件读取 URL（每行一个，# 为注释）")
    p.add_argument("-d", "--dir", default=S, help="保存目录（默认读取配置）")
    p.add_argument("-o", "--out", default=S, help="指定文件名（仅单 URL 时可用）")
    p.add_argument("--proxy", default=S, help="HTTP 代理，默认 http://127.0.0.1:951")
    p.add_argument("--proxy-mode", default=S, choices=["auto", "always", "never"],
                   help="auto=按 gfwlist 判断；always=全走代理；never=全直连")
    p.add_argument("--gfwlist", default=S, help="gfwlist.txt 路径")
    p.add_argument("--gfwlist-url", default=S, help="gfwlist 在线地址")
    p.add_argument("--gfwlist-max-age", type=float, default=S, help="缓存过期小时数")
    p.add_argument("--refresh-gfwlist", action="store_true", default=S, help="运行前强制刷新 gfwlist")
    p.add_argument("--no-gfwlist", dest="gfwlist_enabled", action="store_false", default=S,
                   help="完全不使用 gfwlist")
    p.add_argument("--jobs", type=int, default=S, help="并发下载数")
    p.add_argument("--split", type=int, default=S, help="aria2 单文件分片数")
    p.add_argument("--connections", type=int, default=S, help="单服务器最大连接数")
    p.add_argument("--min-split-size", default=S, help="分片最小尺寸")
    p.add_argument("--no-mtime", dest="mtime", action="store_false", default=S, help="不保留服务器修改时间")
    p.add_argument("--overwrite", action="store_true", default=S, help="覆盖已存在的文件")
    p.add_argument("--no-fallback", dest="fallback", action="store_false", default=S,
                   help="失败后不换一种出网方式重试")
    p.add_argument("--retry", type=int, default=S, help="aria2 单次重试次数")
    p.add_argument("--first-tries", type=int, default=S,
                   help="首轮出网方式的最大尝试次数，用完即换另一种方式（默认 2）")
    p.add_argument("--no-learn", dest="learn", action="store_false", default=S,
                   help="不启用域名学习记录")
    p.add_argument("--learn-path", default=S, help="学习记录文件（默认 hosts-learned.json）")
    p.add_argument("--forget-learned", action="store_true", help="清空学习记录后退出")
    p.add_argument("--timeout", type=int, default=S, help="读写超时（秒）")
    p.add_argument("--connect-timeout", type=int, default=S, help="连接超时（秒）")
    p.add_argument("--file-allocation", default=S, choices=["none", "prealloc", "trunc", "falloc"])
    p.add_argument("--referer", default=S, help="Referer")
    p.add_argument("--header", action="append", default=S, help="附加 HTTP 头，可重复")
    p.add_argument("--extra-arg", action="append", default=S, help="透传给 aria2c 的参数，可重复")
    p.add_argument("--aria2", default=S, help="aria2c 可执行文件路径")
    p.add_argument("--config", default=S, help="配置文件路径（默认 AutoProxyDownloader.json）")
    p.add_argument("--dry-run", action="store_true", help="只打印决策和命令，不下载")
    p.add_argument("--explain", action="store_true", help="只解释每条 URL 走不走代理及命中的规则")
    p.add_argument("--selftest", action="store_true", help="跑内置自检")
    p.add_argument("-v", "--verbose", action="store_true", help="显示 aria2 原始输出")
    p.add_argument("-q", "--quiet", action="store_true", help="安静模式，只输出结果表")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果（便于脚本消费）")
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
    cfg["_config_path"] = str(file_path) if file_path else "(内置默认)"
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
        ("https://www.google.com/search?q=a", True, "||google.com 普通域名"),
        ("https://mail.google.com/", True, "域名锚点匹配子域"),
        ("https://www.google.cn/", False, "@@ 白名单优先命中"),
        ("https://raw.githubusercontent.com/a/b", True, "githubusercontent"),
        ("https://www.baidu.com/", False, "未命中"),
        ("http://prefix.test/x", True, "|前缀规则"),
        ("https://a.suffix.test/", True, "后缀规则"),
        ("https://host/path/to/regex.test/", True, "正则规则"),
        ("https://blogspot.abc.example.net/", True, "通配规则"),
        ("https://example.org.", True, "||example.org 允许尾部点"),
        ("http://127.0.0.1:8080/", False, "本地地址直连"),
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
                                    "代理" if d.use_proxy else "直连", url, why))
    # base64 解码与明文解析一致性
    same = [r.raw for r in rules] == [r.raw for r in parse_gfwlist(plain)]
    print("%s base64 解码结果与明文一致" % ("PASS" if same else "FAIL"))
    fails += 0 if same else 1
    # 通配 ^ 分隔符
    r = Rule("regex", _wildcard_to_regex("bad^"), False, "bad^")
    print("%s ^ 分隔符正则可用" % ("PASS" if r.matches("http://bad/x", "bad") else "FAIL"))
    # 学习记录：gfwlist 未收录、但曾直连失败 → 直接走代理
    learned = LearnedHosts(Path("selftest-learned.json"), enabled=True)
    learned.note("learned.test", "proxy", "selftest")
    d = decide("https://learned.test/a.zip", cfg, gfw, learned)
    print("%s 学习记录让未收录域名直接走代理（%s）" % ("PASS" if d.use_proxy else "FAIL", d.reason))
    fails += 0 if d.use_proxy else 1
    # gfwlist 命中优先于学习记录
    learned.note("google.com", "direct", "selftest")
    d = decide("https://www.google.com/x", cfg, gfw, learned)
    print("%s gfwlist 命中优先于学习记录（%s）" % ("PASS" if d.use_proxy else "FAIL", d.reason))
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
        print("%s 学习记录可落盘并读回" % ("PASS" if ok else "FAIL"))
        fails += 0 if ok else 1
    print("\n自检结果：%s（%d 项失败）" % ("全部通过" if fails == 0 else "有失败", fails))
    return 0 if fails == 0 else 1


# ---------------------------------------------------------------- 输出

def print_explain(items, cfg):
    print("代理：%s   模式=%s   gfwlist=%s" % (
        cfg["proxy"], cfg["proxy_mode"],
        Path(cfg["gfwlist"]).name if cfg["gfwlist_enabled"] else "(已禁用)"))
    for it in items:
        d = it.decision
        print("\n%s" % it.url)
        print("  出网：%s" % ("代理 " + cfg["proxy"] if d.use_proxy else "直连"))
        print("  理由：%s" % d.reason)
        print("  保存：%s" % it.out)


def print_table(items, cfg):
    print("")
    print(" #  STATUS  PROXY   SIZE          SERVER-MTIME        FILE")
    for it in items:
        print("%2d  %-6s  %-5s   %-12s  %-19s  %s" % (
            it.idx, it.status, "PROXY" if it.used_proxy else "DIRECT",
            fmt_size(it.size) if it.size else "-",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(it.mtime_epoch)) if it.mtime_epoch else "-",
            it.out.name,
        ))
        if it.mtime_note and it.status in ("OK", "SKIP"):
            print("    时间戳：%s" % it.mtime_note)
        if it.learned_note:
            print("    学习：%s" % it.learned_note)
        if it.status == "FAIL" and it.error:
            print("    错误：%s" % it.error)


def emit_json(items):
    data = []
    for it in items:
        data.append({
            "url": it.url,
            "status": it.status,
            "used_proxy": it.used_proxy,
            "reason": it.decision.reason,
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
        log("学习记录已清空：%s" % (learn_path or "(未配置路径)"))
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
        parser.error("没有给出 URL（可加 --input 文件，或 --selftest / --refresh-gfwlist）")

    out_dir = Path(expand_path(cfg["dir"]))
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if cfg["_config_path"] == "(内置默认)":
        log("! 未找到 AutoProxyDownloader.json，按内置默认运行（内置默认与仓库里的配置一致，可自行新建一份）")

    aria2 = find_aria2(cfg)
    gfw = load_gfwlist(cfg, log)

    if cfg["proxy_mode"] != "never" and not proxy_reachable(cfg["proxy"]):
        log("! 提示：代理 %s 当前连不上，命中 gfwlist 的 URL 会失败（可用 --no-fallback 关闭回落）" % cfg["proxy"])

    items = []
    for i, url in enumerate(urls, 1):
        d = decide(url, cfg, gfw, learned)
        name = args.out if (getattr(args, "out", None) and len(urls) == 1) else guess_filename(url, cfg)
        items.append(Item(i, url, out_dir / name, d))
    cfg["_total"] = len(items)

    if args.explain:
        print_explain(items, cfg)
        return 0

    if not aria2:
        log("! 找不到 aria2c：把它放到 %s 或设置配置项 aria2 / --aria2" % SCRIPT_DIR)
        return 2
    log("aria2c：%s" % aria2)

    if args.dry_run:
        show_progress = False
        for it in items:
            print("\n%s\n  出网：%s（%s）\n  保存：%s" % (
                it.url,
                "代理 " + cfg["proxy"] if it.decision.use_proxy else "直连",
                it.decision.reason, it.out))
            print("  argv：%s" % subprocess.list2cmdline(
                build_aria2_argv(aria2, cfg, it.url, it.out, it.decision.use_proxy)))
        return 0

    show_progress = cfg["jobs"] == 1 and not args.quiet and sys.stdout.isatty()
    t0 = time.time()
    if cfg["jobs"] <= 1:
        for it in items:
            download_one(aria2, cfg, it, log, show_progress, learned)
    else:
        with cfut.ThreadPoolExecutor(max_workers=cfg["jobs"]) as ex:
            list(ex.map(lambda it: download_one(aria2, cfg, it, log, False, learned), items))
    log("总耗时 %.1fs" % (time.time() - t0))
    if learned.save():
        log("学习记录已更新：%s（共 %d 个域名）" % (learned.path.name, len(learned.hosts)))

    if args.json:
        emit_json(items)
    else:
        print_table(items, cfg)
        ok = sum(1 for i in items if i.status == "OK")
        skip = sum(1 for i in items if i.status == "SKIP")
        bad = sum(1 for i in items if i.status == "FAIL")
        log("完成 %d，跳过 %d，失败 %d" % (ok, skip, bad))
        kept = sum(1 for i in items if i.mtime_epoch)
        if cfg["mtime"]:
            log("其中 %d 个文件带服务器时间戳" % kept)
    return 0 if all(i.status in ("OK", "SKIP") for i in items) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
