#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoProxyDownloader 的图形启动器。

双击 .lnk 后的流程：
  1. 弹出窗口，输入下载地址（每行一个，自动带出剪贴板里的链接）；
  2. 点“确定”或按 Ctrl+Enter 后，在当前控制台里跑 AutoProxyDownloader，进度和结果照旧打在控制台；
  3. 结束后暂停，“按任意键”才关窗口，方便看最后的汇总表。

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


def load_default_dir():
    try:
        cfg = json.loads((SCRIPT_DIR / "AutoProxyDownloader.json").read_text(encoding="utf-8"))
        d = cfg.get("dir") or "%USERPROFILE%\\Downloads"
    except Exception:
        d = "%USERPROFILE%\\Downloads"
    return os.path.expandvars(os.path.expanduser(str(d)))


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


class UrlDialog:
    def __init__(self, default_dir, prefill=""):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.result = None
        self.root = tk.Tk()
        self.root.title("AutoProxyDownloader 下载")
        self.root.resizable(True, False)
        self.root.attributes("-topmost", True)

        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="下载地址（每行一个，可直接 Ctrl+V 粘贴）").pack(anchor="w")
        self.text = tk.Text(frm, width=62, height=4, wrap="none", undo=True)
        self.text.pack(fill="both", expand=True, pady=(4, 8))
        if prefill:
            self.text.insert("1.0", prefill)
            self.text.tag_add("sel", "1.0", "end-1c")

        ttk.Label(frm, text="保存到：" + str(default_dir), foreground="#666666").pack(anchor="w")
        ttk.Label(frm, text="回车换行，Ctrl+Enter 开始下载，Esc 取消",
                  foreground="#888888").pack(anchor="w", pady=(2, 8))

        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        ttk.Button(btns, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="确定", command=self._ok).pack(side="right", padx=(0, 8))

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

    def _ok(self):
        lines = []
        for raw in self.text.get("1.0", "end").splitlines():
            s = raw.strip().strip('"').strip("'")
            if s and not s.startswith("#"):
                lines.append(s)
        self.result = lines or None
        self.root.destroy()

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
        ctypes.windll.kernel32.SetConsoleTitleW("AutoProxyDownloader 下载")
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
    print("\n按任意键关闭窗口 ...", end="", flush=True)
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

    if not urls:
        chosen = UrlDialog(load_default_dir(), clipboard_url()).run()
        if not chosen:
            return 2
        urls = chosen
        focus_console()
    else:
        print("接收到 %d 个地址，跳过输入框直接下载。" % len(urls))

    import AutoProxyDownloader

    print("")
    rc = 0
    try:
        rc = AutoProxyDownloader.main(forward + urls)
    except SystemExit as exc:
        rc = int(exc.code or 0)
    except KeyboardInterrupt:
        print("\n已中断")
        rc = 130
    except Exception as exc:
        print("! 运行出错：%s: %s" % (type(exc).__name__, exc))
        rc = 1
    return rc


def selftest_gui():
    """不开窗口、不等人，只验证弹框能构造出来并正确取到地址。"""
    dlg = UrlDialog(str(SCRIPT_DIR))
    dlg.root.withdraw()
    dlg.root.update_idletasks()
    dlg.text.delete("1.0", "end")
    dlg.text.insert("1.0", "https://example.com/a.zip\n\n  https://example.com/b.zip  \n#注释\n")
    dlg._ok()
    ok = dlg.result == ["https://example.com/a.zip", "https://example.com/b.zip"]
    print("%s 弹框构造与地址解析（%s）" % ("PASS" if ok else "FAIL", dlg.result))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest-gui" in sys.argv:
        sys.exit(selftest_gui())
    setup_console()
    rc = main()
    pause()
    sys.exit(rc)
