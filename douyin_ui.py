#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抖音爬虫本地网页外壳

基于标准库 http.server 实现，无额外依赖。
功能：网页里填 目标(抖音号/主页链接/分享文案) + cookie(可选)，点开始后
     以无人值守模式后台运行 douyin_spider.py，日志实时回显到页面。

启动：python douyin_ui.py  （会自动打开浏览器）
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).parent
PORT = 8765

# ---------------------------------------------------------------- 运行状态

class Runner:
    """管理一次爬取子进程的生命周期与日志缓冲"""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.log_lines = []
        self.running = False
        self.exit_code = None
        self.stopped = False   # 是否被「停止」按钮终止（用于状态展示）
        self.cookie_tmp = None
        self._pending = []   # 批量模式下排队等待的命令列表
        self.mode = None     # 当前任务类型（live 任务点「停止」走优雅收尾）
        self._dropped = 0    # 日志缓冲因超上限丢弃的头部行数（逻辑序号补偿）

    MAX_LOG_LINES = 3000   # 日志缓冲上限，防止长时间任务内存无限增长

    def _push_line(self, text):
        self.log_lines.append(text)
        if len(self.log_lines) > self.MAX_LOG_LINES:
            drop = len(self.log_lines) - self.MAX_LOG_LINES
            del self.log_lines[:drop]
            self._dropped += drop

    def log(self, msg):
        with self.lock:
            self._push_line(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def state(self, after):
        with self.lock:
            # after 是客户端已消费的逻辑行数；头部被丢弃后要补偿
            start = max(after - self._dropped, 0)
            return {
                "lines": self.log_lines[start:],
                "total": self._dropped + len(self.log_lines),
                "running": self.running,
                "exit_code": self.exit_code,
                "stopped": self.stopped,
            }

    def start(self, mode, query, cookie_text, limit, since=None, until=None,
              start_at=None, batch_type=None, outdir=None, sort=None, danmode=None):
        with self.lock:
            if self.running:
                return "已有任务在运行，请先停止或等待完成"

        mode = mode or "user"

        # 一键登录：调用登录助手扫码，成功后自动写 cookies.txt，不需要其他参数
        if mode == "login":
            with self.lock:
                self.log_lines = []
                self._dropped = 0
                self.exit_code = None
                self.running = True
                self.stopped = False
                self._pending = []
            self.log("启动登录助手：请在弹出的浏览器窗口里扫码登录抖音…")
            self.log("登录成功后会自动保存 Cookie 并结束，期间不用管它。")
            self._launch([sys.executable, str(BASE_DIR / "douyin_login.py"),
                          "--out", str(BASE_DIR / "cookies.txt")])
            return None

        query = (query or "").strip()
        if mode != "batch" and not query:
            return "请填写目标（抖音号/主页链接/视频链接/搜索关键词）"

        def as_int(v, name):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        # 存储位置：留空用默认 downloads；相对路径拼到脚本目录下
        outdir_arg = []
        if (outdir or "").strip():
            od = Path(outdir.strip().strip('"'))
            if not od.is_absolute():
                od = BASE_DIR / od
            try:
                od.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                return f"存储位置无法创建：{e}"
            outdir_arg = ["--outdir", str(od)]

        # cookie：网页粘贴的优先，否则用同目录 cookies.txt
        # （直播录制未登录也能录大部分直播间，cookie 缺失只提示、不放行报错）
        cookie_path = None
        if (cookie_text or "").strip():
            self.cookie_tmp = Path(os.environ.get("TEMP", str(BASE_DIR))) / "douyin_ui_cookie.txt"
            self.cookie_tmp.write_text(cookie_text.strip(), encoding="utf-8")
            cookie_path = self.cookie_tmp
        else:
            default_cookie = BASE_DIR / "cookies.txt"
            if default_cookie.exists():
                cookie_path = default_cookie
            elif mode != "live":
                return "未提供 cookie，且同目录下没有 cookies.txt（直播录制可忽略此项直接开始）"

        limit_n = None
        if (limit or "").strip():
            limit_n = as_int(limit, "limit")
            if limit_n is None:
                return "数量/时长必须是数字"

        # 搜索排序：general=综合排序(0) / most_like=点赞最多(2)，已实测参数回显生效
        sort_arg = []
        if sort == "most_like":
            sort_arg = ["--sort", "most_like"]
        elif sort not in (None, "", "general"):
            return "排序参数不合法"

        spider = str(BASE_DIR / "douyin_spider.py")
        recorder = str(BASE_DIR / "douyin_live_recorder.py")

        def base_spider_cmd():
            return [sys.executable, spider, "--cookie-file", str(cookie_path)] + outdir_arg

        if mode == "live":
            cmd = [sys.executable, recorder, "--cookie-file", str(cookie_path)] + outdir_arg
            cmd += ["--room" if re.fullmatch(r"\d{5,}", query) else "--url", query]
            # 录制视角：clean=纯净原始流（默认） / view=观看视角（画面含弹幕）
            if danmode == "view":
                cmd += ["--danmode", "view"]
            elif danmode not in (None, "", "clean"):
                return "录制视角参数不合法"
            if (start_at or "").strip():
                sa = start_at.strip()
                if not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?|\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}(:\d{2})?", sa):
                    return "定时开始格式不对：请填 20:00 或 2026-10-11 20:00"
                cmd += ["--start", sa]
            if limit_n:
                cmd += ["--minutes", str(limit_n)]
            cmds, desc = [cmd], "直播录制"
        elif mode == "batch":
            lines = [ln.strip() for ln in query.splitlines() if ln.strip()]
            if not lines:
                return "批量模式：请每行填一个目标"
            if len(lines) > 20:
                return "一次最多 20 个任务"
            bt = batch_type or "user"
            cmds = []
            for ln in lines:
                c = base_spider_cmd()
                if bt == "single":
                    c += ["--video", ln]
                elif bt == "search":
                    c += ["--search", ln] + sort_arg
                elif bt == "comments":
                    c += ["--comments", ln]
                else:
                    flag = "--url" if ("douyin.com" in ln or "MS4wLjAB" in ln) else "--account"
                    c += [flag, ln]
                    if since:
                        c += ["--since", since]
                    if until:
                        c += ["--until", until]
                if limit_n:
                    c += ["--limit", str(limit_n)]
                cmds.append(c)
            desc = f"批量任务（共 {len(cmds)} 个，依次执行）"
        else:
            cmd = base_spider_cmd()
            if mode == "single":
                cmd += ["--video", query]
            elif mode == "comments":
                cmd += ["--comments", query]
            elif mode == "search":
                cmd += ["--search", query] + sort_arg
            else:
                flag = "--url" if ("douyin.com" in query or "MS4wLjAB" in query) else "--account"
                cmd += [flag, query]
                if since:
                    cmd += ["--since", since]
                if until:
                    cmd += ["--until", until]
            if limit_n:
                cmd += ["--limit", str(limit_n)]
            cmds, desc = [cmd], None

        with self.lock:
            self.log_lines = []
            self._dropped = 0
            self.exit_code = None
            self.running = True
            self.stopped = False
            self.mode = mode
            self._pending = cmds[1:]
        if desc:
            self.log(f"启动{desc}")
        self._launch(cmds[0])
        return None

    def _launch(self, cmd):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        # 标记无人值守：子进程里 stdin 的 isatty 不可靠（DEVNULL 下也可能为 True），
        # 用环境变量确定性地告诉脚本"当前由网页外壳启动"
        env["DY_UNATTENDED"] = "1"
        self.proc = subprocess.Popen(
            cmd, cwd=str(BASE_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", env=env,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            proc = self.proc
            for line in proc.stdout:
                with self.lock:
                    self._push_line(line.rstrip("\r\n"))
            code = proc.wait()
            with self.lock:
                nxt = self._pending.pop(0) if self._pending else None
                if nxt is None:
                    self.exit_code = code
                    self.running = False
            if nxt is None:
                self.log(f"任务结束（退出码 {code}）")
                if self.cookie_tmp and self.cookie_tmp.exists():
                    try:
                        self.cookie_tmp.unlink()
                    except OSError:
                        pass
                return
            self.log(f"一段结束（退出码 {code}），继续下一个（剩 {len(self._pending)} 个）...")
            self._launch(nxt)

    def stop(self):
        with self.lock:
            if not self.running or not self.proc:
                return "当前没有运行中的任务"
            pid = self.proc.pid
            self.stopped = True
            self._pending = []   # 批量队列一并取消
            is_live = (self.mode == "live")
        if is_live:
            # 直播录制走优雅停止：放标记文件，录制器轮询到后自己收尾
            # （转封装已录片段）再退出；卡死超时才强杀进程树
            try:
                (BASE_DIR / "stop.flag").write_text("stop", encoding="utf-8")
            except Exception:
                pass

            def _force_kill():
                try:
                    if self.proc and self.proc.poll() is None:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                       capture_output=True)
                except Exception:
                    pass

            threading.Timer(90, _force_kill).start()
        else:
            # 爬虫任务没有收尾需求，直接终止整棵进程树（含 chromium）
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True)
        return None


runner = Runner()

# 「浏览…」选择文件夹的互斥锁：同一时刻只弹一个系统窗口
_browse_lock = threading.Lock()


def _win_pick_folder(title):
    """Windows 原生「选择文件夹」对话框（IFileDialog，Vista+）。

    用 ctypes 直调 COM，不依赖 tkinter —— 部分精简版 Python 没带 tkinter，
    而这套对话框在任何 Windows 的 Python 上都可用。
    """
    import ctypes
    import uuid
    from ctypes import POINTER, Structure, byref, c_uint, c_ubyte, c_void_p, c_wchar_p

    class GUID(Structure):
        """Windows GUID：按 uuid.bytes_le 的小端内存布局构造"""
        _fields_ = [("raw", c_ubyte * 16)]

        def __init__(self, s):
            b = uuid.UUID(s).bytes_le
            super().__init__((c_ubyte * 16)(*b))

    ole32 = ctypes.OleDLL("ole32")
    CLSCTX_INPROC_SERVER = 1
    FOS_PICKFOLDERS = 0x20      # 只允许选文件夹
    FOS_FORCEFILESYSTEM = 0x40  # 只返回文件系统路径
    SIGDN_FILESYSPATH = 0x80058000

    CLSID_FileOpenDialog = GUID("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")
    IID_IFileOpenDialog = GUID("{D57C7288-D4AD-4768-BE02-9D969532D960}")

    # HTTP 处理线程可能还没初始化 COM，先初始化；模型冲突说明已初始化，忽略
    try:
        ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    except OSError:
        pass

    dlg = c_void_p()
    ole32.CoCreateInstance(byref(CLSID_FileOpenDialog), None, CLSCTX_INPROC_SERVER,
                           byref(IID_IFileOpenDialog), byref(dlg))
    if not dlg.value:
        return None

    def vmethod(obj, idx, *argtypes):
        """按虚表索引取 COM 方法（obj: 对象指针 c_void_p）"""
        vtbl = ctypes.cast(ctypes.cast(obj, POINTER(c_void_p)).contents,
                           POINTER(c_void_p))
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)
        return proto(vtbl[idx])

    try:
        # 叠加选项：保留系统默认行为，再加上「只选文件夹」
        cur = c_uint()
        vmethod(dlg, 10, POINTER(c_uint))(dlg, byref(cur))     # GetOptions（出参指针）
        opts = c_uint(cur.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM)
        vmethod(dlg, 9, c_uint)(dlg, opts)                     # SetOptions（按值传 DWORD）
        vmethod(dlg, 17, c_wchar_p)(dlg, title)                # SetTitle

        Show = vmethod(dlg, 3, c_void_p)                       # IModalWindow.Show
        if Show(dlg, None) != 0:                               # 用户取消 → 非 0
            return None

        item = c_void_p()
        vmethod(dlg, 20, POINTER(c_void_p))(dlg, byref(item))  # GetResult
        if not item.value:
            return None
        try:
            buf = c_wchar_p()
            vmethod(item, 5, c_uint, POINTER(c_wchar_p))(
                item, SIGDN_FILESYSPATH, byref(buf))           # GetDisplayName
            path = buf.value or ""
            ole32.CoTaskMemFree(buf)                           # 释放系统分配的字符串
            return path
        finally:
            vmethod(item, 2)(item)                             # IShellItem.Release
    finally:
        vmethod(dlg, 2)(dlg)                                   # IFileDialog.Release


def browse_folder():
    """弹出系统文件夹选择窗口（本地工具，服务器与用户在同一台机器）"""
    if not _browse_lock.acquire(blocking=False):
        return {"error": "已经有一个选择窗口打开着"}
    try:
        if sys.platform != "win32":
            return {"error": "文件夹选择窗口仅支持 Windows"}
        try:
            path = _win_pick_folder("选择保存位置")
        except Exception as e:
            return {"error": f"打开选择窗口失败：{e}"}
        return {"path": (path or "").replace("/", "\\")}
    finally:
        _browse_lock.release()

def cookie_status():
    """检查脚本目录 cookies.txt 的登录态，供页面显示状态徽标"""
    p = BASE_DIR / "cookies.txt"
    if not p.exists():
        return {"exists": False, "ready": False}
    lines = [ln.strip() for ln in
             p.read_text(encoding="utf-8", errors="replace").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        return {"exists": False, "ready": False}
    # 与爬虫的 load_cookies 一致：取最长一行作为 cookie 串
    names = set()
    for pair in max(lines, key=len).split(";"):
        k, _, _ = pair.strip().partition("=")
        if k.strip():
            names.add(k.strip())
    has_login = bool(names & {"sessionid", "sessionid_ss"})
    has_anti = bool(names & {"msToken", "ttwid"})
    return {"exists": True, "ready": has_login and has_anti,
            "has_login": has_login, "has_anti": has_anti}


# ---------------------------------------------------------------- 页面

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>抖音工具箱</title>
<link rel="preconnect" href="https://fonts.loli.net" crossorigin>
<link href="https://fonts.loli.net/css2?family=ZCOOL+KuaiLe&family=Noto+Sans+SC:wght@400;500;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #f5f6f8; --card: #ffffff; --line: #e4e8ef;
    --fg: #18191c; --dim: #6d757a;
    --pink: #fb7299; --blue: #00a1d6; --ok: #2ac864; --warn: #ff9f43;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; color: var(--fg);
    font: 14px/1.65 "Noto Sans SC", "Microsoft YaHei", sans-serif;
    background:
      radial-gradient(900px 420px at 85% -8%, rgba(0,161,214,.10), transparent 60%),
      radial-gradient(900px 480px at -10% 105%, rgba(251,114,153,.12), transparent 60%),
      var(--bg);
    min-height: 100vh;
  }
  ::selection { background: rgba(251,114,153,.28); }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: #c9d2e0; border-radius: 8px; }
  ::-webkit-scrollbar-thumb:hover { background: #aeb9cc; }
  ::-webkit-scrollbar-track { background: transparent; }

  .wrap { max-width: 1320px; margin: 0 auto; padding: 40px 22px 70px; position: relative; z-index: 1; }

  /* 左右分栏：左边表单，右边日志（随屏幕固定，不用往下翻） */
  .layout {
    display: grid; grid-template-columns: minmax(400px, 470px) 1fr;
    gap: 18px; align-items: start;
  }
  .layout .card { margin-bottom: 0; }
  .layout .logpanel {
    position: sticky; top: 18px;
    display: flex; flex-direction: column;
    height: calc(100vh - 58px); min-height: 460px;
  }
  @media (max-width: 1020px) {
    .layout { grid-template-columns: 1fr; }
    .layout .logpanel { position: static; height: 480px; }
  }

  @keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
  h1, .sub, .card, .logpanel { animation: rise .5s cubic-bezier(.2,.75,.25,1) backwards; }
  .sub { animation-delay: .06s; } .card { animation-delay: .12s; } .logpanel { animation-delay: .2s; }
  @media (prefers-reduced-motion: reduce) {
    h1, .sub, .card, .logpanel { animation: none; }
  }

  h1 {
    font-family: "ZCOOL KuaiLe", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    font-size: 40px; font-weight: 400; letter-spacing: 3px; margin: 0 0 6px;
    background: linear-gradient(120deg, var(--pink) 18%, var(--blue) 82%);
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .sub { color: var(--dim); font-size: 12.5px; letter-spacing: .5px; margin-bottom: 26px; }
  .sub b { color: var(--pink); font-weight: 500; }

  .card {
    position: relative; overflow: hidden;
    background: var(--card);
    border: 1px solid var(--line); border-radius: 16px;
    padding: 24px; margin-bottom: 18px;
    box-shadow: 0 6px 24px rgba(85,105,135,.10);
  }
  .card::before {
    content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px;
    background: linear-gradient(90deg, var(--pink), var(--blue));
  }
  .card, .logpanel { margin-bottom: 0; }

  label { display: block; font-size: 12px; color: var(--dim); margin: 14px 0 7px; letter-spacing: .5px; }
  label:first-child { margin-top: 0; }
  input[type=text], input[type=date], textarea, select {
    width: 100%; background: #fff; color: var(--fg);
    border: 1px solid #dfe3ea; border-radius: 10px;
    padding: 11px 13px; font: inherit; outline: none;
    transition: border-color .18s, box-shadow .18s;
  }
  input[type=text]:focus, input[type=date]:focus, textarea:focus, select:focus {
    border-color: rgba(251,114,153,.75);
    box-shadow: 0 0 0 3px rgba(251,114,153,.13);
  }
  input::placeholder, textarea::placeholder { color: #b0b9c5; }
  select {
    cursor: pointer; appearance: none; -webkit-appearance: none; padding-right: 38px;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='8'><path d='M1 1l5 5 5-5' fill='none' stroke='%236d757a' stroke-width='1.6' stroke-linecap='round'/></svg>");
    background-repeat: no-repeat; background-position: right 14px center;
  }
  input[type=date] { color-scheme: light; }
  textarea { height: 92px; resize: vertical; font-size: 12.5px; }

  .row { display: flex; gap: 12px; align-items: center; margin-top: 18px; flex-wrap: wrap; }
  button {
    border: 0; border-radius: 10px; padding: 11px 28px;
    font: inherit; font-weight: 700; letter-spacing: 1px; cursor: pointer; color: #fff;
    transition: transform .15s, box-shadow .15s, background .15s, color .15s, border-color .15s;
  }
  #btnStart {
    background: linear-gradient(135deg, #fb7299, #fc8bab);
    box-shadow: 0 6px 16px rgba(251,114,153,.35);
  }
  #btnStart:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 10px 22px rgba(251,114,153,.45); }
  #btnStart:active:not(:disabled) { transform: translateY(0); }
  #btnStart:disabled { background: #f3cede; color: #fff; cursor: not-allowed; box-shadow: none; }
  #btnStop { background: #fff; border: 1px solid var(--line); color: var(--dim); }
  #btnStop:hover:not(:disabled) { border-color: rgba(251,114,153,.8); color: var(--pink); }
  #btnStop:disabled { opacity: .35; cursor: not-allowed; }
  #btnBrowse { background: #fff; border: 1px solid var(--line); color: var(--dim); padding: 10px 18px; }
  #btnBrowse:hover:not(:disabled) { border-color: rgba(0,161,214,.7); color: var(--blue); }
  #btnBrowse:disabled { opacity: .5; cursor: not-allowed; }
  .outdirrow { display: flex; gap: 10px; }
  .outdirrow input { flex: 1; }
  #btnLogin { background: #fff; border: 1px solid rgba(0,161,214,.55); color: var(--blue); padding: 9px 20px; }
  #btnLogin:hover:not(:disabled) { background: rgba(0,161,214,.08); }
  #btnLogin:disabled { opacity: .5; cursor: not-allowed; }
  .sortbtn { background: #fff; border: 1px solid var(--line); color: var(--dim); padding: 8px 18px; }
  .sortbtn:hover { border-color: rgba(0,161,214,.7); color: var(--blue); }
  .sortbtn.active { background: var(--pink); border-color: var(--pink); color: #fff; }
  #limit { width: 120px; }
  .hint { color: var(--dim); font-size: 12px; }

  /* 一键登录区 */
  .loginrow {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    margin-top: 16px; padding: 12px 14px;
    background: rgba(0,161,214,.06); border: 1px dashed rgba(0,161,214,.35);
    border-radius: 12px;
  }
  .loginrow .why { margin-left: auto; font-size: 11.5px; color: #8b98a8; }
  .ck { font-size: 12px; padding: 4px 12px; border-radius: 999px; white-space: nowrap; }
  .ck::before {
    content: ""; display: inline-block; width: 7px; height: 7px;
    border-radius: 50%; background: var(--dim); margin-right: 8px; vertical-align: 1px;
  }
  .ck.ok { color: #1fa353; background: rgba(42,200,100,.12); }
  .ck.ok::before { background: var(--ok); }
  .ck.warn { color: #d97b0d; background: rgba(255,159,67,.16); }
  .ck.warn::before { background: var(--warn); }
  .ck.bad { color: #e05585; background: rgba(251,114,153,.13); }
  .ck.bad::before { background: var(--pink); }

  #status {
    margin-left: auto; font-size: 12px; padding: 5px 14px;
    border-radius: 999px; border: 1px solid var(--line); background: #f7f8fa;
  }
  #status::before {
    content: ""; display: inline-block; width: 7px; height: 7px;
    border-radius: 50%; background: var(--dim); margin-right: 8px; vertical-align: 1px;
  }
  #status.run { color: var(--ok); border-color: rgba(42,200,100,.35); }
  #status.run::before { background: var(--ok); animation: pulse 1.2s ease-in-out infinite; }
  #status.end { color: var(--dim); }
  #status.err { color: var(--pink); border-color: rgba(251,114,153,.45); }
  #status.err::before { background: var(--pink); }
  @keyframes pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(42,200,100,.5); } 55% { box-shadow: 0 0 0 5px rgba(42,200,100,0); } }

  details { border: 1px dashed #d8dee8; border-radius: 12px; padding: 10px 14px; margin-top: 16px; }
  details[open] { border-style: solid; border-color: #c9d3e2; }
  summary { color: var(--dim); font-size: 12px; cursor: pointer; user-select: none; letter-spacing: .5px; }
  summary:hover { color: var(--fg); }

  .logpanel {
    border: 1px solid #2a2f3a; border-radius: 16px; overflow: hidden;
    background: #23272f; box-shadow: 0 6px 24px rgba(85,105,135,.14);
    margin-bottom: 0;
  }
  .loghead {
    display: flex; align-items: center; gap: 12px; padding: 11px 16px;
    border-bottom: 1px solid #31363f; background: #2b303a;
  }
  .dots { width: 10px; height: 10px; border-radius: 50%; background: var(--pink);
          box-shadow: 17px 0 0 #ffd04d, 34px 0 0 var(--ok); margin-right: 34px; }
  .logtitle { font-size: 12px; letter-spacing: 3px; color: #aab3c0; }
  .loghint { margin-left: auto; font-size: 11px; color: #5d6673; letter-spacing: 1px; }
  #logbox {
    padding: 15px 16px; flex: 1; min-height: 0; overflow-y: auto;
    font: 12.5px/1.65 "JetBrains Mono", Consolas, "Courier New", monospace;
    white-space: pre-wrap; word-break: break-all; color: #c3cbd6;
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>抖音工具箱<span class="dot">.</span></h1>
  <div class="sub"><b>视频/图集</b> · <b>评论 CSV</b> · <b>批量队列</b> · <b>直播录制</b></div>

  <div class="layout">
  <div class="card">
    <label>功能类型</label>
    <select id="mode" onchange="onMode()">
      <option value="user">用户作品（可按时间段）</option>
      <option value="single">单个视频</option>
      <option value="search">关键词搜索</option>
      <option value="comments">视频评论（导出 CSV）</option>
      <option value="batch">批量任务（每行一个，依次执行）</option>
      <option value="live">直播录制</option>
    </select>

    <label id="queryLabel">目标（抖音号 / 主页链接 / App 分享文案）</label>
    <input type="text" id="query" placeholder="例如：27285332742 或 https://v.douyin.com/xxxx/">

    <div id="sortRow" style="display:none; align-items:center; gap:8px; margin-top:10px; flex-wrap:wrap">
      <label style="margin:0">搜索结果排序</label>
      <button type="button" id="sortGeneral" class="sortbtn" onclick="setSort('general')">综合排序</button>
      <button type="button" id="sortLike" class="sortbtn" onclick="setSort('most_like')">点赞最多</button>
    </div>

    <div id="searchTip" style="display:none; margin-top:8px; padding:10px 12px;
         border-radius:8px; background:#fff8e6; border:1px solid #ffd591;
         font-size:13px; line-height:1.7; color:#8c6d1f">
      <b>搜索模式工作方式（请先读一遍）：</b><br>
      ① 用上面的按钮选排序：综合排序（默认）/ 点赞最多；<br>
      ② 点开始后会弹出浏览器，自动打开搜索结果页并滚动收集视频；<br>
      ③ 搜索页<b>大概率触发平台安全验证</b>（如点选相同图案），脚本不会也无法替你绕过——
      它会自动暂停等待，<b>请你直接在那个弹出的浏览器窗口里手动完成验证</b>；<br>
      ④ 完成后无需任何操作，脚本检测到验证解除会自动继续收集并下载；<br>
      ⑤ 约一半概率需要手动过一次验证，属正常现象，多试几个词即可。
    </div>

    <div id="dateRow" style="display:flex; gap:12px; margin-top:2px; flex-wrap:wrap;">
      <div style="flex:1; min-width:150px">
        <label>起始日期（含，可留空）</label>
        <input type="date" id="since">
      </div>
      <div style="flex:1; min-width:150px">
        <label>结束日期（含，可留空）</label>
        <input type="date" id="until">
      </div>
    </div>

    <div id="liveRow" style="display:none">
      <label>录制视角</label>
      <select id="danmode">
        <option value="clean">纯净原始流（无弹幕，原画质）</option>
        <option value="view">观看视角（画面含弹幕，录屏+原声）</option>
      </select>
      <div class="hint" style="margin-top:4px">观看视角：录下浏览器窗口里看到的画面（含弹幕/礼物特效），声音取自原始流；录制期间请勿移动、最小化或遮挡弹出的浏览器窗口；画质为播放分辨率，需要已安装 ffmpeg。</div>
      <label style="margin-top:10px">定时开始（如 20:00 或 2026-10-11 20:00，留空 = 立即开始）</label>
      <input type="text" id="startat" placeholder="主播没开播会自动等待，开播即录">
    </div>

    <div id="batchRow" style="display:none">
      <label>批量类型</label>
      <select id="batchType" onchange="onMode()">
        <option value="user">用户作品（每行一个抖音号/主页链接）</option>
        <option value="single">单个视频（每行一个视频链接）</option>
        <option value="search">关键词搜索（每行一个关键词）</option>
        <option value="comments">视频评论（每行一个视频链接）</option>
      </select>
      <label>每个任务爬取数量（留空 = 不限，全部爬完）</label>
      <input type="text" id="batchLimit" placeholder="如 10，则每个任务最多爬 10 个；留空则全部爬完">
      <label>目标列表（每行一个，最多 20 行）</label>
      <textarea id="batchText" placeholder="27285332742&#10;某抖音号或主页链接&#10;..."></textarea>
    </div>

    <label>存储位置（下载的视频、图片、评论 CSV 都保存在这里）</label>
    <div class="outdirrow">
      <input type="text" id="outdir" placeholder="留空 = 保存在脚本目录的 downloads 文件夹；也可填如 D:\抖音素材">
      <button type="button" id="btnBrowse" onclick="browse()">浏览…</button>
    </div>

    <div class="loginrow">
      <span id="ckState" class="ck">检查中…</span>
      <button id="btnLogin" onclick="login()">一键登录抖音，自动获取 Cookie</button>
      <span class="why">弹出浏览器扫码登录，Cookie 自动保存，不用手动复制</span>
    </div>

    <details style="margin-top:10px">
      <summary class="hint" style="cursor:pointer; user-select:none">高级选项：手动粘贴 cookie（用了登录按钮就不用管这里）</summary>
      <textarea id="cookie" placeholder="留空则自动使用 cookies.txt；只有在别处已有 cookie 才需要粘贴" style="margin-top:8px"></textarea>
    </details>

    <div class="row">
      <label style="margin:0" id="limitLabel">数量限制</label>
      <input type="text" id="limit" placeholder="全部" style="width:110px">
      <button id="btnStart" onclick="start()">开始爬取</button>
      <button id="btnStop" onclick="stop()" disabled>停止</button>
      <span id="status" class="end">空闲</span>
    </div>
    <div class="row">
      <span class="hint">提示：任务会弹出浏览器窗口，如遇验证码请在窗口里手动完成，脚本会自动继续。</span>
    </div>
  </div>

  <div class="logpanel">
    <div class="loghead">
      <span class="dots"></span>
      <span class="logtitle">运行日志</span>
      <span class="loghint">LIVE OUTPUT</span>
    </div>
    <div id="logbox"></div>
  </div>
  </div><!-- /layout -->
</div>

<script>
const $ = id => document.getElementById(id);
let cursor = 0;

// 搜索排序：general=综合排序 / most_like=点赞最多，选择持久化
let searchSort = localStorage.getItem("dy_sort") || "general";
if (searchSort !== "general" && searchSort !== "most_like") searchSort = "general";

function setSort(s) {
  searchSort = s;
  localStorage.setItem("dy_sort", s);
  paintSort();
}

function paintSort() {
  $("sortGeneral").classList.toggle("active", searchSort === "general");
  $("sortLike").classList.toggle("active", searchSort === "most_like");
}

function onMode() {
  const m = $("mode").value;
  $("dateRow").style.display = m === "user" ? "flex" : "none";
  $("liveRow").style.display = m === "live" ? "block" : "none";
  $("batchRow").style.display = m === "batch" ? "block" : "none";
  const showSort = m === "search" || (m === "batch" && $("batchType").value === "search");
  $("sortRow").style.display = showSort ? "flex" : "none";
  $("searchTip").style.display = showSort ? "block" : "none";
  const isBatch = m === "batch";
  $("queryLabel").style.display = isBatch ? "none" : "block";
  $("query").style.display = isBatch ? "none" : "block";
  // 批量模式用批量区里自己的「每个任务爬取数量」，隐藏底部的数量框避免两个框混淆
  $("limitLabel").style.display = isBatch ? "none" : "";
  $("limit").style.display = isBatch ? "none" : "";
  const labels = {
    user:     ["目标（抖音号 / 主页链接 / App 分享文案）", "例如：27285332742 或 https://v.douyin.com/xxxx/", "数量限制", "全部"],
    single:   ["视频链接 / App 分享文案", "粘贴视频链接或分享文案，如 https://v.douyin.com/xxxx/", "数量限制", "1"],
    search:   ["搜索关键词", "如：家常菜、篮球教程、旅行 vlog（若弹验证请在浏览器窗口手动完成）", "数量限制", "全部"],
    comments: ["视频链接 / App 分享文案", "粘贴视频链接，评论将导出为 CSV", "评论数量上限", "全部"],
    live:     ["直播间链接 / 房间号 / 抖音号 / 主页链接", "开播中：https://live.douyin.com/123456 或纯数字房间号&#10;未开播（定时录）：抖音号或昵称、主页链接、App 分享文案，到点自动等开播", "录制时长（分钟，留空=不限）", "不限"],
    batch:    ["", "", "每个任务的数量限制", "全部"]
  }[m] || ["目标", "", "数量限制", "全部"];
  $("queryLabel").textContent = labels[0];
  $("query").placeholder = labels[1];
  $("limitLabel").textContent = labels[2];
  $("limit").placeholder = labels[3];
  $("btnStart").textContent =
    { user: "开始爬取", single: "开始下载", search: "开始搜索",
      comments: "开始抓取评论", batch: "开始批量任务", live: "开始录制" }[m] || "开始任务";
  localStorage.setItem("dy_mode", m);
}

async function post(url, data) {
  const r = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data || {})
  });
  return r.json();
}

async function start() {
  const m = $("mode").value;
  localStorage.setItem("dy_outdir", $("outdir").value);
  const res = await post("/start", {
    mode: m,
    query: m === "batch" ? $("batchText").value : $("query").value,
    cookie: $("cookie").value,
    limit: m === "batch" ? $("batchLimit").value : $("limit").value,
    since: $("since").value,
    until: $("until").value,
    startat: $("startat").value,
    batchType: $("batchType").value,
    outdir: $("outdir").value,
    sort: searchSort,
    danmode: $("danmode") ? $("danmode").value : "clean"
  });
  if (res.error) { setStatus(res.error, "err"); }
  else { $("logbox").textContent = ""; cursor = 0; poll(); }
}

async function stop() {
  const res = await post("/stop", {});
  if (res.error) setStatus(res.error, "err");
}

async function login() {
  const res = await post("/login", {});
  if (res.error) { setStatus(res.error, "err"); return; }
  $("logbox").textContent = ""; cursor = 0; poll();
}

// 弹出系统文件夹选择窗口，选完自动填入（用户取消则不变）
async function browse() {
  const btn = $("btnBrowse");
  btn.disabled = true;
  try {
    const r = await (await fetch("/browse", {method: "POST"})).json();
    if (r.path) {
      $("outdir").value = r.path;
      localStorage.setItem("dy_outdir", r.path);
    } else if (r.error) {
      setStatus(r.error, "err");
    }
  } catch (e) { /* 窗口被强关等异常静默 */ }
  btn.disabled = false;
}

async function refreshCookie() {
  try {
    const s = await (await fetch("/cookie_status")).json();
    const el = $("ckState"), btn = $("btnLogin");
    if (s.ready) {
      el.textContent = "Cookie 就绪，可以正常使用";
      el.className = "ck ok";
      btn.textContent = "重新登录（更新 Cookie）";
    } else if (s.exists) {
      el.textContent = "Cookie 缺少登录信息，建议重新登录";
      el.className = "ck warn";
      btn.textContent = "一键登录抖音，自动获取 Cookie";
    } else {
      el.textContent = "还没有登录，点右边按钮扫码即可";
      el.className = "ck bad";
      btn.textContent = "一键登录抖音，自动获取 Cookie";
    }
  } catch (e) { /* 服务未就绪时静默 */ }
}

// err 提示具有粘性：几秒内轮询不得覆盖，否则错误一闪而过等于没提示
let statusStickyUntil = 0;

function setStatus(text, cls) {
  const el = $("status");
  el.textContent = text;
  el.className = cls || "end";
  statusStickyUntil = cls === "err" ? Date.now() + 4000 : 0;
}

async function poll() {
  try {
    const r = await fetch("/log?after=" + cursor);
    const s = await r.json();
    if (s.lines.length) {
      const box = $("logbox");
      const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
      box.textContent += s.lines.join("\\n") + "\\n";
      if (stick) box.scrollTop = box.scrollHeight;
      cursor = s.total;
    }
    if (s.running) { setStatus("运行中…", "run"); }
    else if (Date.now() < statusStickyUntil) { /* 保持错误提示 */ }
    else if (s.exit_code === null) { setStatus("空闲", "end"); }
    else if (s.stopped) { setStatus("已停止", "end"); }
    else if (s.exit_code === 0) { setStatus("已完成", "run"); }
    else { setStatus("已结束（退出码 " + s.exit_code + "），详情看日志", "err"); }
    $("btnStart").disabled = s.running;
    $("btnStop").disabled = !s.running;
    $("btnLogin").disabled = s.running;
  } catch (e) { /* 服务未就绪时静默 */ }
}
setInterval(poll, 1000);
poll();
setInterval(refreshCookie, 5000);
refreshCookie();

// 恢复上次的功能类型和存储位置
(function restore() {
  const sm = localStorage.getItem("dy_mode");
  const so = localStorage.getItem("dy_outdir");
  if (sm) { $("mode").value = sm; }
  if (so) { $("outdir").value = so; }
  onMode();
  paintSort();
})();

// 存储位置输入完立即记住，刷新/下次打开不用重填
$("outdir").addEventListener("change",
  () => localStorage.setItem("dy_outdir", $("outdir").value));
</script>
</body>
</html>"""

# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 页面持续迭代，禁止浏览器缓存，保证刷新总能拿到最新版
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/log"):
            after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
            self._send(200, json.dumps(runner.state(after), ensure_ascii=False))
        elif self.path.startswith("/cookie_status"):
            self._send(200, json.dumps(cookie_status(), ensure_ascii=False))
        else:
            self._send(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            data = {}
        if self.path == "/start":
            err = runner.start(data.get("mode"), data.get("query"),
                               data.get("cookie"), data.get("limit"),
                               data.get("since"), data.get("until"),
                               data.get("startat"),
                               data.get("batchType"), data.get("outdir"),
                               data.get("sort"), data.get("danmode"))
            self._send(200, json.dumps({"error": err}, ensure_ascii=False))
        elif self.path == "/login":
            err = runner.start("login", None, None, None)
            self._send(200, json.dumps({"error": err}, ensure_ascii=False))
        elif self.path == "/browse":
            self._send(200, json.dumps(browse_folder(), ensure_ascii=False))
        elif self.path == "/stop":
            err = runner.stop()
            self._send(200, json.dumps({"error": err}, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *args):  # 关闭默认访问日志
        pass


def main():
    server = None
    port = PORT
    for p in range(PORT, PORT + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if not server:
        print("无法绑定端口（8765-8784 均被占用）")
        sys.exit(1)

    url = f"http://127.0.0.1:{port}"
    print(f"爬虫界面已启动: {url}  （关闭本窗口即退出）")
    # DY_NO_BROWSER=1 时不自动开浏览器（测试/后台启动用）
    if os.environ.get("DY_NO_BROWSER") != "1":
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
