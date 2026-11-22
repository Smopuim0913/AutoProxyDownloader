#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoProxyDownloader 的图形启动器。

双击 .lnk 后的流程：
  1. 弹出窗口，输入下载地址（每行一个，自动带出剪贴板里的链接），
     顺手可以改保存目录、并发数、重试次数、代理地址与代理模式；
  2. 点“确定”或按 Ctrl+Enter 后，在当前控制台里跑 AutoProxyDownloader，进度和结果照旧打在控制台；
  3. 结束后暂停，“按任意键”才关窗口，方便看最后的汇总表。

勾上“保存为默认配置”再点确定，界面上的选项会写回 AutoProxyDownloader.json，下次弹框就是新值；
不勾则只在本次下载生效（通过命令行参数传给主程序）。

三种用法都能进：
  - 双击快捷方式 / 直接运行本文件：弹框输入；
  - 把地址或含地址的 txt 拖到快捷方式上：跳过弹框，直接下载；
  - 在命令行里带参数运行：参数会透传给 AutoProxyDownloader（例如 --jobs 2 -d D:\\dl）。
"""

import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import AutoProxyDownloader as apd      # 复用主程序的语言判断与 T() 文案

T = apd.T

CONFIG_NAME = "AutoProxyDownloader.json"
# 地址只对本次生效、从不入库；保存目录和其它界面选项可以持久化
SAVED_KEYS = ("dir", "jobs", "retry", "proxy", "proxy_mode", "mtime", "fallback")


def load_config():
    """读出配置：文件里的值优先，缺项用主程序的内置默认补上。"""
    cfg = dict(apd.DEFAULTS)
    path = SCRIPT_DIR / CONFIG_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                cfg[k] = v
    except Exception:
        pass
    return cfg, path


def expand_dir(value):
    return os.path.expandvars(os.path.expanduser(str(value or "")))


def clipboard_url():
    """剪贴板里是链接就带出来，省一次粘贴。"""
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        text = r.clipboard_get()
        r.destroy()
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(("http://", "https://")):
                return s
    except Exception:
        return ""
    return ""


def build_extra_args(opts, base):
    """只把用户真正改过的选项变成命令行参数；没动的交给配置文件与已有命令行。"""
    out = []
    base = base or {}
    if not opts:
        return out
    if str(opts.get("dir", "")) != str(base.get("dir", "")):
        out += ["-d", str(opts["dir"])]
    for key, flag in (("jobs", "--jobs"), ("retry", "--retry")):
        try:
            if int(opts.get(key, 0)) != int(base.get(key, 0)):
                out += [flag, str(opts[key])]
        except (TypeError, ValueError):
            pass
    if str(opts.get("proxy", "")) != str(base.get("proxy", "")):
        out += ["--proxy", str(opts["proxy"])]
    if str(opts.get("proxy_mode", "")) != str(base.get("proxy_mode", "")):
        out += ["--proxy-mode", str(opts["proxy_mode"])]
    if bool(opts.get("mtime", True)) != bool(base.get("mtime", True)):
        out.append("--mtime" if opts["mtime"] else "--no-mtime")
    if bool(opts.get("fallback", True)) != bool(base.get("fallback", True)):
        out.append("--fallback" if opts["fallback"] else "--no-fallback")
    return out


class UrlDialog:
    def __init__(self, cfg=None, prefill="", cfg_path=None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.cfg = dict(cfg or apd.DEFAULTS)
        self.cfg_path = Path(cfg_path) if cfg_path else (SCRIPT_DIR / CONFIG_NAME)
        self.result = None        # 地址列表
        self.options = None       # 界面上的选项
        self.saved = False        # 是否写回了默认配置

        self.root = tk.Tk()
        self.root.title(T("AutoProxyDownloader 下载", "AutoProxyDownloader download"))
        self.root.resizable(True, False)
        self.root.attributes("-topmost", True)

        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text=T("下载地址（每行一个，可直接 Ctrl+V 粘贴）",
                             "URLs to download (one per line, Ctrl+V pastes directly)")).pack(anchor="w")
        self.text = tk.Text(frm, width=64, height=4, wrap="none", undo=True)
        self.text.pack(fill="both", expand=True, pady=(4, 8))
        if prefill:
            self.text.insert("1.0", prefill)
            self.text.tag_add("sel", "1.0", "end-1c")

        box = ttk.LabelFrame(frm, text=T("下载选项", "Download options"), padding=10)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text=T("保存目录", "Directory")).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.var_dir = tk.StringVar(value=str(self.cfg.get("dir") or ""))
        self.ent_dir = ttk.Entry(box, textvariable=self.var_dir)
        self.ent_dir.grid(row=0, column=1, sticky="we")
        self.ent_dir.bind("<FocusOut>", lambda e: self._refresh_dir_hint())
        self.ent_dir.bind("<Return>", lambda e: self._refresh_dir_hint())
        ttk.Button(box, text=T("浏览…", "Browse…"), width=9,
                   command=self._browse).grid(row=0, column=2, padx=(6, 0))
        self.lbl_dir = ttk.Label(box, text=self._dir_hint(), foreground="#888888")
        self.lbl_dir.grid(row=1, column=1, columnspan=2, sticky="w", pady=(0, 6))

        nums = ttk.Frame(box)
        nums.grid(row=2, column=0, columnspan=3, sticky="w")
        ttk.Label(nums, text=T("并发下载数", "Parallel downloads")).pack(side="left")
        self.var_jobs = tk.StringVar(value=str(self.cfg.get("jobs", 3)))
        ttk.Spinbox(nums, from_=1, to=8, width=4,
                    textvariable=self.var_jobs).pack(side="left", padx=(6, 20))
        ttk.Label(nums, text=T("重试次数", "Retries")).pack(side="left")
        self.var_retry = tk.StringVar(value=str(self.cfg.get("retry", 5)))
        ttk.Spinbox(nums, from_=0, to=20, width=4,
                    textvariable=self.var_retry).pack(side="left", padx=(6, 20))

        ttk.Label(box, text=T("代理地址", "Proxy")).grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(4, 0))
        self.var_proxy = tk.StringVar(value=str(self.cfg.get("proxy") or ""))
        ttk.Entry(box, textvariable=self.var_proxy).grid(row=3, column=1, sticky="we", pady=(4, 0))
        self.var_mode = tk.StringVar(value=str(self.cfg.get("proxy_mode") or "auto"))
        ttk.Combobox(box, textvariable=self.var_mode, values=["auto", "always", "never"],
                     state="readonly", width=8).grid(row=3, column=2, padx=(6, 0), pady=(4, 0))

        checks = ttk.Frame(box)
        checks.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.var_mtime = tk.BooleanVar(value=bool(self.cfg.get("mtime", True)))
        ttk.Checkbutton(checks, text=T("保留服务器修改时间", "Preserve the server timestamp"),
                        variable=self.var_mtime).pack(side="left", padx=(0, 18))
        self.var_fallback = tk.BooleanVar(value=bool(self.cfg.get("fallback", True)))
        ttk.Checkbutton(checks, text=T("直连失败时自动换代理重试",
                                      "Retry via the proxy when direct fails"),
                        variable=self.var_fallback).pack(side="left")

        self.var_save = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text=T("保存为默认配置（写入 %s）" % CONFIG_NAME,
                                    "Save as default config (writes %s)" % CONFIG_NAME),
                        variable=self.var_save).pack(anchor="w", pady=(10, 0))
        ttk.Label(frm, text=T("地址不会写进配置；回车换行，Ctrl+Enter 开始下载，Esc 取消",
                              "URLs are never stored in the config; Enter adds a line, Ctrl+Enter starts, Esc cancels"),
                  foreground="#888888").pack(anchor="w", pady=(4, 8))

        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        ttk.Button(btns, text=T("取消", "Cancel"), command=self._cancel).pack(side="right")
        ttk.Button(btns, text=T("确定", "OK"), command=self._ok).pack(side="right", padx=(0, 8))

        self.root.bind("<Escape>", lambda e: self._cancel())
        self.root.bind("<Control-Return>", lambda e: self._ok())
        self.root.protocol("WM_DELETE_WINDOW", self._cancel)

        self.text.focus_set()
        self._center()

    def _center(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry("+%d+%d" % (max(0, (sw - w) // 2), max(0, (sh - h) // 3)))

    def _dir_hint(self):
        raw = (self.var_dir.get() or "").strip()
        if not raw:
            return ""
        expanded = expand_dir(raw)
        if expanded == raw:
            return ""
        return T("实际路径：%s", "Resolved: %s") % os.path.normpath(expanded)

    def _refresh_dir_hint(self):
        self.lbl_dir.configure(text=self._dir_hint())

    def _browse(self):
        from tkinter import filedialog
        cur = expand_dir(self.var_dir.get()) or str(Path.home() / "Downloads")
        if not os.path.isdir(cur):
            cur = str(Path.home())
        path = filedialog.askdirectory(initialdir=cur,
                                      title=T("选择保存目录", "Choose a download directory"))
        if path:
            self.var_dir.set(os.path.normpath(path))
            self._refresh_dir_hint()

    def _number(self, var, low, high):
        try:
            v = int(str(var.get()).strip())
        except (TypeError, ValueError):
            return None
        return v if low <= v <= high else None

    def _ok(self):
        from tkinter import messagebox
        lines = []
        for raw in self.text.get("1.0", "end").splitlines():
            s = raw.strip().strip('"').strip("'")
            if s and not s.startswith("#"):
                lines.append(s)
        dir_raw = (self.var_dir.get() or "").strip()
        if not dir_raw:
            messagebox.showwarning(T("地址不完整", "Missing value"),
                                   T("保存目录不能为空。", "The download directory cannot be empty."))
            return
        jobs = self._number(self.var_jobs, 1, 8)
        retry = self._number(self.var_retry, 0, 20)
        if jobs is None or retry is None:
            messagebox.showwarning(T("数值不对", "Invalid number"),
                                   T("并发下载数需在 1–8 之间，重试次数需在 0–20 之间。",
                                     "Parallel downloads must be 1-8 and retries 0-20."))
            return
        self.options = {
            "dir": dir_raw,
            "jobs": jobs,
            "retry": retry,
            "proxy": (self.var_proxy.get() or "").strip(),
            "proxy_mode": str(self.var_mode.get() or "auto"),
            "mtime": bool(self.var_mtime.get()),
            "fallback": bool(self.var_fallback.get()),
        }
        if self.var_save.get():
            self.saved = self._save_defaults()
        self.result = lines or None
        self.root.destroy()

    def _save_defaults(self):
        from tkinter import messagebox
        cfg = dict(self.cfg)
        for k in SAVED_KEYS:
            cfg[k] = self.options[k]
        try:
            self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cfg_path.with_name(self.cfg_path.name + ".tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(str(tmp), str(self.cfg_path))
            self.cfg = cfg
            return True
        except OSError as exc:
            messagebox.showwarning(
                T("写入失败", "Could not save"),
                T("无法写入 %s：%s", "cannot write %s: %s") % (self.cfg_path, exc))
            return False

    def _cancel(self):
        self.result = None
        self.root.destroy()

    def run(self):
        self.root.mainloop()
        return self.result


def looks_like_url(s):
    return s.lower().startswith(("http://", "https://"))


def setup_console():
    """给控制台窗口一个能看懂的名字。"""
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(T("AutoProxyDownloader 下载", "AutoProxyDownloader download"))
    except Exception:
        pass


def focus_console():
    """弹框关掉后把控制台拉到前台，否则进度可能被别的窗口盖住。"""
    try:
        import ctypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def pause():
    # APD_NO_PAUSE 为当前名字；ARIA2DL_NO_PAUSE 是改名前的旧名，保留兼容
    if os.environ.get("APD_NO_PAUSE") or os.environ.get("ARIA2DL_NO_PAUSE"):
        return
    print("\n" + T("按任意键关闭窗口 ...", "Press any key to close this window ..."), end="", flush=True)
    try:
        import msvcrt
        msvcrt.getch()
    except ImportError:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    extra = [a for a in sys.argv[1:]]
    urls = [a for a in extra if looks_like_url(a)]
    forward = [a for a in extra if not looks_like_url(a)]
    apd.relang(extra)          # 让弹框文案也跟 --lang / APD_LANG 一致

    if not urls:
        cfg, cfg_path = load_config()
        dlg = UrlDialog(cfg=cfg, prefill=clipboard_url(), cfg_path=cfg_path)
        chosen = dlg.run()
        if dlg.saved:
            print(T("已把当前选项写入默认配置：%s", "wrote the current options to the default config: %s")
                  % cfg_path)
        if not chosen:
            return 2
        urls = chosen
        forward += build_extra_args(dlg.options, cfg)
        focus_console()
    else:
        print(T("接收到 %d 个地址，跳过输入框直接下载。",
                "got %d URL(s), skipping the dialog and downloading right away.") % len(urls))

    print("")
    rc = 0
    try:
        rc = apd.main(forward + urls)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    except KeyboardInterrupt:
        print("\n" + T("已中断", "interrupted"))
        rc = 130
    except Exception as exc:
        print("! " + T("运行出错：%s: %s", "failed to run: %s: %s") % (type(exc).__name__, exc))
        rc = 1
    return rc


def selftest_gui():
    """不开窗口、不等人：验证弹框构造、地址解析、选项转参数、写回默认配置。"""
    import tempfile
    cfg, _ = load_config()
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / CONFIG_NAME
        dlg = UrlDialog(cfg=cfg, cfg_path=target)
        dlg.root.withdraw()
        dlg.root.update_idletasks()
        dlg.text.delete("1.0", "end")
        dlg.text.insert("1.0", "https://example.com/a.zip\n\n  https://example.com/b.zip  \n#注释\n")
        dlg.var_dir.set("%USERPROFILE%\\Downloads\\apd-test")
        dlg.var_jobs.set("5")
        dlg.var_retry.set("3")
        dlg.var_mtime.set(False)
        dlg.var_save.set(True)
        dlg._ok()

        ok_urls = dlg.result == ["https://example.com/a.zip", "https://example.com/b.zip"]
        args = build_extra_args(dlg.options, cfg)
        ok_args = all(x in args for x in ("-d", "--jobs", "5", "--retry", "3", "--no-mtime"))
        saved = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        # 保存目录跟着“保存为默认配置”一起入库；地址永远不写进配置
        ok_save = (saved.get("jobs") == 5 and saved.get("retry") == 3
                   and saved.get("mtime") is False
                   and saved.get("dir") == "%USERPROFILE%\\Downloads\\apd-test"
                   and not any(isinstance(v, list) and any("http" in str(x) for x in v)
                               for v in saved.values()))
        ok = ok_urls and ok_args and ok_save

        print("%s %s" % ("PASS" if ok else "FAIL",
                         T("弹框 / 选项转参数 / 写回默认配置", "dialog, option-to-args and save-as-default")))
        print("    urls :", dlg.result)
        print("    args :", args)
        print("    saved:", {k: saved.get(k) for k in SAVED_KEYS})
        return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest-gui" in sys.argv:
        sys.exit(selftest_gui())
    setup_console()
    rc = main()
    pause()
    sys.exit(rc)
