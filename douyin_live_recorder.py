#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抖音直播录制器 —— 录制纯净画面（无弹幕 / 评论 / 在线人数 / 礼物特效等任何 UI）

原理：
  弹幕、评论、在线人数、点赞数、礼物特效全都是网页渲染出来的"叠加层"，
  不在视频流里。直播的原始流（http-flv）就是主播摄像头推出去的纯净画面+声音。
  本脚本用 playwright 打开直播间，拦截播放器自己发出的拉流地址，
  然后直接把流写到本地文件 —— 录出来的就是原始画质，零 UI 元素。

用法：
  1. 交互模式（逐项问你）:
       python douyin_live_recorder.py
  2. 命令行模式:
       python douyin_live_recorder.py --url "https://live.douyin.com/123456789" --cookie-file cookies.txt
       python douyin_live_recorder.py --room 123456789
       python douyin_live_recorder.py --url ... --minutes 90              # 录 90 分钟
       python douyin_live_recorder.py --url ... --start 20:00 --minutes 120   # 今晚 8 点开录，录到 10 点
       python douyin_live_recorder.py --url "https://www.douyin.com/user/xxx" --start 20:00
           # 主播还没开播（拿不到直播间号）也能定时：先打开主播主页等开播，
           # 检测到「直播中」入口后自动找到直播间并开始录制
           # 也支持只填抖音号/昵称（自动解析主页），或 App 分享文案

说明：
  - 时间按分钟指定（--minutes），也可以只定时开始（--start），到点自动开录
  - 主播没开播时会自动等待，每 60 秒刷新检查一次，开播自动开始录
  - 直播中途网络断了会自动重连，分多个 part 文件继续录
  - 主播下播（页面显示"直播已结束"）会自动停止录制并收尾，不会空等到期
  - "主播暂时离开"不算下播，会继续等待主播回来
  - 录制期间页面自动切到空白页（弹幕/动画/视频解码全部停掉），不会越录越卡
  - 录完如果有 ffmpeg（系统 PATH 或首次安装自带的 imageio-ffmpeg），
    会把 .flv 自动转封装成 .mp4（无损、秒完成）
  - 完全没有 ffmpeg 也能用，产出 .flv 文件（VLC / PotPlayer 可直接播放）

依赖（已预装在专用环境里）: playwright, requests, imageio-ffmpeg（内置 ffmpeg）
仅供个人学习研究使用，请勿二次传播录制内容。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

try:
    import ctypes
    import ctypes.wintypes
except ImportError:
    ctypes = None  # 非 Windows 环境用不到 gdigrab，这里只做兜底

try:
    import requests
    from playwright.sync_api import sync_playwright
except ImportError:
    print("缺少运行依赖：playwright / requests")
    print("请双击「打开爬虫界面.bat」，它会自动帮你安装；")
    print("或手动执行: pip install requests playwright && playwright install chromium")
    sys.exit(1)

# 与浏览器保持一致的 UA（拉流时也用同一个，降低风控概率）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 观看视角模式：浏览器窗口钉在这个位置/大小（逻辑像素），gdigrab 只采集
# 其中播放器的真实区域（运行时动态定位，含弹幕层）
VIEW_WIN = (30, 30, 1300, 820)

# 「停止」标记文件：网页界面点「停止」时放置，录制器轮询到后优雅收尾退出
_STOP_FLAG = Path(__file__).parent / "stop.flag"

# 交互判定：网页外壳（DY_UNATTENDED=1）或 stdin 不可读时按无人值守处理
INTERACTIVE = (os.environ.get("DY_UNATTENDED") != "1"
               and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())


_LOG_FILE = Path(__file__).parent / "logs" / f"{datetime.now():%Y%m%d}.log"


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    # 同时落盘到 logs/日期.log，事后可查（界面内存日志关掉就没了）
    try:
        _LOG_FILE.parent.mkdir(exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def pause(msg):
    if INTERACTIVE:
        input(msg)
    else:
        log(f"(无人值守模式，跳过人工确认) {msg.strip()}")


def parse_cookie_string(raw):
    raw = raw.strip().strip('"').strip("'")
    cookies = {}
    for pair in raw.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


def load_cookies(cookie_str=None, cookie_file=None):
    if cookie_file:
        p = Path(cookie_file)
        if not p.exists():
            log(f"找不到 cookie 文件: {p}")
            sys.exit(1)
        lines = [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        if not lines:
            log(f"cookie 文件是空的: {p}")
            sys.exit(1)
        cookie_str = max(lines, key=len)
    if not cookie_str:
        log("未提供 cookies")
        sys.exit(1)
    return parse_cookie_string(cookie_str)


def parse_when(s):
    """解析定时输入 -> Unix 时间戳。

    支持: '20:00' / '20:00:30' / '2026-10-11 20:00' / '2026-10-11 20:00:00'
    纯时刻（不带日期）按今天算，已过则自动算明天。
    解析失败返回 (None, 错误说明)。
    """
    s = s.strip()
    now = datetime.now()
    for fmt, has_date in (("%Y-%m-%d %H:%M:%S", True),
                          ("%Y-%m-%d %H:%M", True),
                          ("%H:%M:%S", False),
                          ("%H:%M", False)):
        try:
            t = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if not has_date:
            t = t.replace(year=now.year, month=now.month, day=now.day)
            if t <= now:
                t += timedelta(days=1)
        elif t <= now:
            return None, f"指定时间已过去: {s}"
        return t.timestamp(), None
    return None, f"认不出这个时间「{s}」，请写 20:00 这样的时刻，或 2026-10-11 20:00 完整时间"


def ffmpeg_cmd():
    """返回可用的 ffmpeg 命令：优先系统 PATH，找不到就用
    imageio-ffmpeg 包内置的 ffmpeg（首次 bat 安装会装上），都没有返回 None
    """
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def has_ffmpeg():
    return ffmpeg_cmd() is not None


def os_display_scale():
    """主屏的 Windows 显示缩放倍率（1.0 / 1.25 / 1.5 ...），失败返回 1.0。

    浏览器窗口坐标（JS 里的 screenX 等）是逻辑像素（DIP），
    而 gdigrab 在 DPI 感知模式下按物理像素采集，两者相差这个倍率。
    """
    if ctypes is None:
        return 1.0
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def flv_to_mp4(flv_path):
    """用 ffmpeg 无损转封装 flv -> mp4，成功则删掉 flv，返回 mp4 路径"""
    if not has_ffmpeg():
        return None
    mp4 = flv_path.with_suffix(".mp4")
    try:
        r = subprocess.run(
            [ffmpeg_cmd(), "-y", "-loglevel", "error",
             "-i", str(flv_path), "-c", "copy", str(mp4)],
            capture_output=True, timeout=600)
    except Exception as e:
        log(f"  ffmpeg 转封装出错: {e}")
        return None
    if r.returncode == 0 and mp4.exists() and mp4.stat().st_size > 10240:
        flv_path.unlink()
        return mp4
    # 失败时清掉残留的半成品 mp4，保留原 .flv
    try:
        mp4.unlink(missing_ok=True)
    except Exception:
        pass
    log("  ffmpeg 转封装失败，保留原 .flv 文件")
    return None


def check_disk_free(outdir, min_gb=5.0):
    """开录前检查磁盘剩余空间，不足则拒绝开录（原画直播约 1~2GB/小时）"""
    try:
        free = shutil.disk_usage(outdir).free
    except OSError as e:
        log(f"磁盘空间检查失败（{e}），跳过检查继续执行")
        return True
    if free < min_gb * 1024 ** 3:
        log(f"磁盘剩余空间不足 {min_gb:.0f}GB（仅剩 {free / 1024 ** 3:.1f}GB），"
            "拒绝开录以免写到一半写坏文件；请清理磁盘后重试")
        return False
    return True


# 清晰度选择：flv_pull_url 里的档位键（FULL_HD1=原画 HD1=超清 SD1=高清 SD2=标清），
# 首选档缺失时按列表顺序就近回落
QUALITY_KEYS = {
    "origin":   ["FULL_HD1", "HD1", "SD1", "SD2"],
    "super":    ["HD1", "FULL_HD1", "SD1", "SD2"],
    "high":     ["SD1", "HD1", "FULL_HD1", "SD2"],
    "standard": ["SD2", "SD1", "HD1", "FULL_HD1"],
}


class LiveRecorder:
    def __init__(self, cookies, outdir, duration=0, headless=False, start_ts=None,
                 danmode="clean", quality="origin"):
        self.cookies = cookies
        self.outdir = Path(outdir)
        self.duration = duration      # 最长录制秒数，0 = 不限
        self.headless = headless
        self.start_ts = start_ts      # 定时开始的时间戳，None = 立即开始
        self.danmode = danmode or "clean"   # clean=纯净原始流 / view=观看视角(含弹幕)
        self.quality = quality if quality in QUALITY_KEYS else "origin"

        self.stream_url = None        # 拦截到的拉流地址
        self.stream_kind = None       # "flv" / "m3u8"
        self.t_start = None           # 本次任务开始时间
        self._room_url = None         # 直播间真实地址（重连用）
        self._stop = False
        self._finished = []           # 已录/在录的文件，任何退出路径统一收尾

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---------- 浏览器 ----------

    def start_browser(self):
        log("启动浏览器...")
        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        vp = {"width": 1600, "height": 900}
        if self.danmode == "view":
            # 观看视角：窗口钉在固定位置，gdigrab 按同一区域采集画面；
            # 小屏幕（如 1366x768）放不下固定窗口时收缩窗口与视口，避免弹幕被切掉
            w, h = VIEW_WIN[2], VIEW_WIN[3]
            if ctypes is not None:
                scale = os_display_scale()   # 顺便把进程设为 DPI 感知
                scr_w = ctypes.windll.user32.GetSystemMetrics(0) / scale
                scr_h = ctypes.windll.user32.GetSystemMetrics(1) / scale
                w = min(w, int(scr_w) - VIEW_WIN[0] - 10)
                h = min(h, int(scr_h) - VIEW_WIN[1] - 10)
                if (w, h) != (VIEW_WIN[2], VIEW_WIN[3]):
                    log(f"屏幕偏小，观看视角窗口收缩为 {w}x{h}")
            args += [f"--window-position={VIEW_WIN[0]},{VIEW_WIN[1]}",
                     f"--window-size={w},{h}"]
            vp = {"width": max(960, w - 20), "height": max(540, h - 120)}
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=args,
        )
        self._context = self._browser.new_context(
            user_agent=UA,
            viewport=vp,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        # 任何页面发出的拉流请求都能收到
        self._context.on("request", self._on_request)
        self._page = self._context.new_page()

        # 先访问一次抖音拿域，再注入 cookies，刷新生效
        self._page.goto("https://www.douyin.com/",
                        wait_until="domcontentloaded", timeout=60000)
        cookie_list = [{"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                       for k, v in self.cookies.items()]
        self._context.add_cookies(cookie_list)
        self._page.reload(wait_until="domcontentloaded")
        time.sleep(2)

        if "sessionid" not in self.cookies and "sessionid_ss" not in self.cookies:
            log("⚠️ cookies 里没有 sessionid，未登录状态看部分直播可能受限")

    def stop_browser(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ---------- 拉流地址拦截 ----------

    def _on_request(self, req):
        url = req.url
        if self.stream_url and self.stream_kind == "flv":
            return  # 已经拿到 flv，不再覆盖
        if ".flv" in url and ("douyin" in url or "zjcdn" in url or "bytecdn" in url):
            self.stream_url, self.stream_kind = url, "flv"
        elif ".m3u8" in url and ("douyin" in url or "zjcdn" in url or "bytecdn" in url):
            if self.stream_kind is None:   # m3u8 只是兜底，flv 优先
                self.stream_url, self.stream_kind = url, "m3u8"

    def wait_for_stream(self, timeout=45):
        """等播放器发出拉流请求，返回 (kind, url) 或 (None, None)"""
        self.stream_url, self.stream_kind = None, None
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._poll_stop()
            if self.stream_url:
                return self.stream_kind, self.stream_url
            time.sleep(0.5)
        return None, None

    def _streams_from_page(self):
        """从页面内嵌数据里挖出全部带签名 flv 地址，按清晰度从高到低排序。

        抖音网页播放器可能走 WebRTC（拦不到 flv 请求），但各清晰度的
        flv 地址仍写在页面数据里，正则挖出来直接用即可。
        """
        try:
            html = self._page.content()
        except Exception:
            return []
        # 页面 JSON 里 & 被转义成 \u0026，不还原的话正则会截断签名参数
        html = html.replace("\\u0026", "&")
        urls = re.findall(r'https?://[^"\'\\\s<>]+?\.flv\?[^"\'\\\s<>]{10,}', html)
        seen, out = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)

        def rank(u):
            m = re.search(r'_([a-z0-9]{2,4})\.flv', u)
            tag = m.group(1) if m else ""
            order = {"or7": 0, "or5": 1, "or4": 2, "uhd": 3,
                     "hd": 4, "sd": 5, "ld": 6}
            return order.get(tag, 3)   # 无清晰度后缀的按超清对待

        return sorted(out, key=rank)

    # 播放器 store 里的分档拉流地址表（键见 QUALITY_KEYS）
    _STORE_FLV_JS = """
() => {
  try {
    const info = window.__STORE__ && window.__STORE__.roomStore
      && window.__STORE__.roomStore.roomInfo;
    const urls = (info && info.room && info.room.stream_url
                  && info.room.stream_url.flv_pull_url)
                 || (info && info.web_stream_url && info.web_stream_url.flv_pull_url)
                 || null;
    return urls || null;
  } catch (e) { return null; }
}
"""

    def _stream_from_store(self):
        """从播放器数据直取所选档位的 flv 地址（缺档就近回落），拿不到返回 None"""
        try:
            urls = self._page.evaluate(self._STORE_FLV_JS)
        except Exception:
            return None
        if not isinstance(urls, dict):
            return None
        for key in QUALITY_KEYS.get(self.quality, QUALITY_KEYS["origin"]):
            u = urls.get(key)
            if u and ".flv?" in u:
                return u
        return None

    # ---------- 直播间 ----------

    @staticmethod
    def _is_direct_room(query):
        """输入是否直接指向直播间：直播间链接或纯数字房间号"""
        q = query.strip()
        return bool(re.search(r"live\.douyin\.com/(?:\d+|live_web_rid=\d+)", q)
                    or re.fullmatch(r"\d{5,}", q))

    @staticmethod
    def _extract_url(query):
        """从输入里提取 URL（App 分享文案里挖出第一个链接）"""
        m = re.search(r"https?://[^\s\"'<>，。；]+", query.strip())
        return m.group(0) if m else None

    def _resolve_profile(self, name):
        """把纯文字抖音号/昵称解析成主播主页链接。

        优先走 www.douyin.com/@抖音号 直达主页（无风控）；失败再退到
        抖音搜索页（type=user）找人，取文案里含关键词的第一条结果。
        """
        name = name.strip().lstrip("@")
        log(f"输入的是抖音号/昵称，先解析出主播主页: {name}")
        try:
            self._page.goto(f"https://www.douyin.com/@{quote(name)}",
                            wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            if "/user/" in self._page.url:
                log("已通过 @抖音号 直达主播主页")
                return self._page.url
        except Exception:
            pass
        log("@主页直达没命中，改走抖音搜索页找人...")
        try:
            self._page.goto(
                f"https://www.douyin.com/search/{quote(name)}?type=user",
                wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"打开搜索页失败: {type(e).__name__}: {e}")
            return None
        time.sleep(3)
        self._check_captcha()
        cands = self._page.evaluate("""
() => {
  const out = [];
  document.querySelectorAll('a[href*="/user/"]').forEach(a => {
    out.push({href: a.href, text: (a.innerText || '').trim().slice(0, 60)});
  });
  return out;
}
""")
        if not cands:
            log("搜索结果里找不到用户链接（风控 / 无此人 / 页面结构变化）")
            return None
        best = next((c for c in cands if name in c["text"]), cands[0])
        log(f"搜索到候选用户: {best['text'] or '无文字'}")
        return best["href"]

    def _wait_for_live(self, query):
        """目标不是直播间而是主播主页/分享文案/抖音号：等开播并解析直播间。

        每 120 秒刷新主页，检测到「直播中」入口（指向 live.douyin.com 的
        链接）后返回直播间地址；短链直接跳进直播间的也一并识别。
        """
        url = self._extract_url(query)
        if not url:
            # 纯文字：当作抖音号/昵称，解析出主页链接
            url = self._resolve_profile(query)
            if not url:
                log("解析不出主播主页。请填直播间链接、主播主页链接"
                    "（douyin.com/user/...）、App 分享文案或抖音号")
                sys.exit(1)
        log(f"目标不是直播间，先打开主播主页等开播: {url}")
        while True:
            self._poll_stop()
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"打开主页失败: {type(e).__name__}: {e}，60 秒后重试")
                time.sleep(60)
                continue
            time.sleep(3)
            self._check_captcha()
            info = self._page.evaluate("""
() => {
  const cands = [];
  document.querySelectorAll('a[href*="live.douyin.com/"]').forEach(a => {
    const m = a.href.match(/live\\.douyin\\.com\\/(\\d+)/);
    if (m) cands.push({href: a.href, text: (a.innerText || '').trim().slice(0, 60)});
  });
  return {cands, url: location.href,
          hasVideo: !!document.querySelector('video')};
}
""")
            # 短链/主页链接直接落在了直播间页面
            if re.search(r"live\.douyin\.com/\d+", info["url"]):
                log("已直接进入直播间页面")
                return info["url"]
            # URL 指向直播域且页面在播视频：特殊形式的直播播放页
            if ("live.douyin.com" in info["url"] or "/live/" in info["url"]) \
                    and info["hasVideo"]:
                log("检测到直播播放页")
                return info["url"]
            cands = info["cands"]
            if cands:
                # 优先选文案带「直播」的入口，避免点到推荐位里别人的直播间
                best = next((c for c in cands if "直播" in c["text"]), cands[0])
                log(f"检测到主播已开播（入口文案: {best['text'] or '无文字'}）")
                return best["href"]
            log("主播还没开播，120 秒后重新检查（不想等可停止/Ctrl+C 退出）...")
            # 分段睡眠：保证「停止」请求随时能被响应
            for _ in range(24):
                self._poll_stop()
                time.sleep(5)

    def open_room(self, query):
        """打开直播间页面，返回房间号（拿不到就返回 'live'）"""
        query = query.strip()
        # live.douyin.com/数字 或 live_web_rid=数字 都归一化成标准数字地址
        m = (re.search(r"live\.douyin\.com/(\d+)", query)
             or re.search(r"live_web_rid=(\d+)", query))
        if m:
            url = f"https://live.douyin.com/{m.group(1)}"
        elif re.fullmatch(r"\d{5,}", query):
            url = f"https://live.douyin.com/{query}"
        elif query.startswith("http"):
            url = query  # 可能是 @昵称 形式或短链，打开后等重定向
        else:
            url = f"https://live.douyin.com/{query}"

        log(f"打开直播间: {url}")
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        self._mute()
        m = re.search(r"live\.douyin\.com/(\d+)", self._page.url)
        room_id = m.group(1) if m else "live"
        # 记住真实房间地址，供录制期间（页面已切空白）重连时重新打开
        self._room_url = self._page.url if "live.douyin.com" in self._page.url else url
        log(f"房间号: {room_id}")
        return room_id

    def _park_page(self):
        """录制期间把页面切到空白页：停掉弹幕渲染/礼物动画/视频解码，避免越录越卡"""
        try:
            self._page.goto("about:blank", wait_until="commit", timeout=30000)
        except Exception:
            pass

    def _reopen_room(self):
        """重连前重新打开直播间页面"""
        try:
            self._page.goto(self._room_url, wait_until="domcontentloaded",
                            timeout=60000)
            time.sleep(2)
            self._mute()
        except Exception:
            pass

    def _mute(self):
        """把浏览器里的直播静音（我们单独拉流录制，不需要页面出声）"""
        try:
            self._page.evaluate(
                "document.querySelectorAll('video').forEach(v=>{v.muted=true;v.volume=0;})")
        except Exception:
            pass

    def _page_text(self):
        try:
            return self._page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            return ""

    def _check_captcha(self):
        txt = self._page_text()
        if any(k in txt for k in ("验证码", "安全验证", "拖动滑块", "滑动验证")):
            log("❗ 检测到验证页面！请在弹出的浏览器窗口里手动完成验证")
            pause("  完成后回到这里按回车继续...")

    def _looks_not_live(self):
        txt = self._page_text()
        return any(k in txt for k in ("直播已结束", "暂无直播", "离开一下，稍后再来",
                                      "主播暂时离开", "未开播"))

    def _live_ended(self):
        """直播是否已确定结束（区别于主播暂时离开）"""
        return "直播已结束" in self._page_text()

    # ---------- 录制 ----------

    def _time_up(self):
        if not self.duration or self.t_start is None:
            return False
        return (time.time() - self.t_start) >= self.duration

    @staticmethod
    def _fmt_remain(sec):
        sec = int(max(0, sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return (f"{h}小时{m}分{s}秒" if h else
                f"{m}分{s}秒" if m else f"{s}秒")

    def _poll_stop(self):
        """检查「停止」请求（界面点停止会放标记文件），收到则抛出中断。

        抛 KeyboardInterrupt 复用统一收尾路径：各层先把手上文件的收尾做掉
        （下载部分改名 / 给 ffmpeg 发 q 写完 mp4 索引），run() 的 finally
        再把所有片段转封装。
        """
        if not self._stop:
            try:
                self._stop = _STOP_FLAG.exists()
            except Exception:
                self._stop = False
            if self._stop:
                log("收到「停止」请求，正在收尾...")
                try:
                    _STOP_FLAG.unlink()
                except Exception:
                    pass
        if self._stop:
            raise KeyboardInterrupt
        return False

    def _finalize(self):
        """收尾：把已录片段无损转封装成 mp4（任何退出路径都会走到这里）"""
        if not self._finished:
            return
        log("=" * 60)
        log("录制结束，处理文件...")
        final_files = []
        for f in self._finished:
            if not f.exists():
                continue
            if f.stat().st_size < 10240:
                f.unlink()   # 录坏了的小碎片直接删
                continue
            if f.suffix == ".flv":
                mp4 = flv_to_mp4(f)
                if mp4:
                    log(f"  ✓ {mp4.name}（{mp4.stat().st_size/1048576:.1f} MB，"
                        f"已从 flv 无损转封装）")
                    final_files.append(mp4)
                    continue
            final_files.append(f)
            log(f"  ✓ {f.name}（{f.stat().st_size/1048576:.1f} MB）")
        if final_files:
            log(f"共 {len(final_files)} 个文件，保存在: {self.outdir}")
            if self.danmode == "view":
                log("画面为播放器视角（含弹幕等界面元素），声音来自原始直播流")
            else:
                log("录制的是直播原始流：无弹幕、无评论、无在线人数、无任何界面元素")
        else:
            log("没有可保留的录制文件")
        log("=" * 60)

    def wait_for_start(self):
        """定时等待：到点前 60 秒返回（留出启动浏览器的时间）"""
        while True:
            self._poll_stop()
            remain = self.start_ts - time.time()
            if remain <= 60:
                return
            log(f"定时开始 {datetime.fromtimestamp(self.start_ts).strftime('%H:%M:%S')}"
                f"，还剩 {self._fmt_remain(remain - 60)}（之后自动启动浏览器开始录）")
            time.sleep(min(30, remain - 60))

    def record_flv(self, url, base_path):
        """把 http-flv 流写进本地文件，返回 (字节数, 是否正常结束)"""
        headers = {"User-Agent": UA,
                   "Referer": "https://live.douyin.com/",
                   "Accept": "*/*"}
        tmp = base_path.with_suffix(".flv.part")
        size = 0
        last_report = time.time()
        last_stopchk = time.time()
        try:
            with requests.get(url, headers=headers, stream=True,
                              timeout=(10, 30)) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            f.write(chunk)
                            size += len(chunk)
                        if time.time() - last_report >= 15:
                            log(f"  录制中... 已录 {size/1048576:.1f} MB")
                            last_report = time.time()
                        if time.time() - last_stopchk >= 2:
                            self._poll_stop()   # 收到停止请求会抛 KeyboardInterrupt
                            last_stopchk = time.time()
                        if self._time_up():
                            break
        finally:
            if tmp.exists() and tmp.stat().st_size > 0:
                tmp.rename(base_path.with_suffix(".flv"))
        return size

    def record_m3u8(self, url, base_path):
        """兜底：只有 hls 流且本机有 ffmpeg 时，直接用 ffmpeg 录成 mp4"""
        mp4 = base_path.with_suffix(".mp4")
        headers = "Referer: https://live.douyin.com/\r\n"
        cmd = [ffmpeg_cmd(), "-y", "-loglevel", "error",
               "-user_agent", UA, "-headers", headers,
               "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", str(mp4)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            while proc.poll() is None and not self._time_up():
                self._poll_stop()
                time.sleep(2)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
        if mp4.exists() and mp4.stat().st_size > 10240:
            return mp4.stat().st_size
        return 0

    # ---------- 观看视角录制 ----------

    _PLAYER_RECT_JS = """
() => {
  const v = document.querySelector('video');
  if (!v) return null;
  const r = v.getBoundingClientRect();
  if (r.width < 100 || r.height < 100) return null;
  return {left: r.left, top: r.top, width: r.width, height: r.height};
}
"""

    def _find_chrome_hwnd(self):
        """按页面标题找到浏览器顶层窗口句柄（窗口标题包含页面标题）。

        为什么不用 window.screenX：Playwright 用模拟视口时这些值是假的
        （实测会返回 -21333 之类的乱值），必须从操作系统侧拿真实坐标。
        """
        if ctypes is None:
            return None
        try:
            title = self._page.title()
        except Exception:
            return None
        if not title:
            return None
        user32 = ctypes.windll.user32
        matches = []

        def cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if title in buf.value:
                        matches.append(hwnd)
            return True

        CMPFUNC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                     ctypes.c_void_p)
        try:
            user32.EnumWindows(CMPFUNC(cb), None)
        except Exception:
            return None
        if not matches:
            return None
        # 多个窗口标题都匹配时，取面积最大的（主窗口）
        best, best_area = None, 0
        rect = ctypes.wintypes.RECT()
        for h in matches:
            if user32.GetWindowRect(h, ctypes.byref(rect)):
                area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                if area > best_area:
                    best, best_area = h, area
        return best

    def _viewport_origin(self, hwnd):
        """拿网页视口原点的屏幕物理像素坐标。

        Chromium 主窗口客户区包含标签栏/地址栏，而 getBoundingClientRect
        是相对网页视口的。Chromium 有个子窗口 Chrome_RenderWidgetHostHWND
        对应网页渲染区，对它做 ClientToScreen 得到的就是视口原点。
        拿不到子窗口时退回主窗口客户区原点（会偏移一个工具栏高度）。
        """
        user32 = ctypes.windll.user32
        target = []

        def child_cb(child, _):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(child, buf, 64)
            if buf.value == "Chrome_RenderWidgetHostHWND":
                target.append(child)
            return True

        CMPFUNC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                     ctypes.c_void_p)
        try:
            user32.EnumChildWindows(hwnd, CMPFUNC(child_cb), None)
        except Exception:
            pass
        pt = ctypes.wintypes.POINT(0, 0)
        probe = target[0] if target else hwnd
        if not user32.ClientToScreen(probe, ctypes.byref(pt)):
            return None
        return (pt.x, pt.y)

    def _set_browser_topmost(self, top):
        """把浏览器窗口置顶（top=True）或取消置顶（top=False）。

        gdigrab 抓的是屏幕上该区域的实际像素，任何窗口盖住浏览器都会被
        录进去，因此每段开录前必须置顶；录完恢复普通层级，不干扰日常使用。
        """
        if ctypes is None:
            return
        try:
            hwnd = self._find_chrome_hwnd()
            if not hwnd:
                log("⚠️ 未找到浏览器窗口句柄，无法置顶，请勿遮挡弹出的窗口")
                return
            user32 = ctypes.windll.user32
            if top:
                user32.SetForegroundWindow(hwnd)
            user32.SetWindowPos(hwnd, -1 if top else -2, 0, 0, 0, 0,
                                0x0001 | 0x0002 | 0x0040)  # SWP_NOSIZE|NOMOVE|SHOWWINDOW
            if top:
                log("浏览器窗口已置顶，录制期间请勿在该窗口内做任何操作")
        except Exception:
            pass

    def _player_capture_rect(self, grab_scale):
        """算出播放器（画面+弹幕层，两者重合）在屏幕上的 gdigrab 采集区域。

        坐标换算：Playwright 强制 deviceScaleFactor=1，页面 CSS 像素就是
        逻辑像素（DIP）；视口原点由 Win32 ClientToScreen 提供（物理像素），
        元素偏移再乘 grab_scale（显示缩放）换算成物理像素。宽高取偶，
        避免 x264/yuv420p 报奇数尺寸错误。取不到播放器返回 None。
        """
        info = None
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                info = self._page.evaluate(self._PLAYER_RECT_JS)
            except Exception:
                info = None
            if info:
                break
            time.sleep(1)
        if not info:
            return None

        hwnd = self._find_chrome_hwnd()
        if hwnd is None:
            return None
        origin = self._viewport_origin(hwnd)
        if origin is None:
            return None
        x = origin[0] + round(info["left"] * grab_scale)
        y = origin[1] + round(info["top"] * grab_scale)
        w = round(info["width"] * grab_scale) // 2 * 2
        h = round(info["height"] * grab_scale) // 2 * 2
        if w < 320 or h < 240:
            return None
        return (x, y, w, h)

    def _probe_grab_scale(self):
        """探测 ffmpeg gdigrab 的坐标体系：物理像素还是 DPI 虚拟化的逻辑像素。

        先把本进程设为 DPI 感知拿到主屏物理宽度，再让 ffmpeg 抓一帧全屏，
        对比抓出来的宽度：等于物理宽度说明 ffmpeg 按物理像素采集（乘显示
        缩放），等于逻辑宽度说明被 DPI 虚拟化（乘 1）。探测失败按物理处理。
        """
        scale = os_display_scale()
        if ctypes is None:
            return scale
        try:
            phys_w = ctypes.windll.user32.GetSystemMetrics(0)  # SM_CXSCREEN
        except Exception:
            return scale
        probe = Path(os.environ.get("TEMP", ".")) / "_gdigrab_probe.png"
        try:
            subprocess.run(
                [ffmpeg_cmd(), "-y", "-loglevel", "error", "-f", "gdigrab",
                 "-i", "desktop", "-frames", "1", str(probe)],
                capture_output=True, timeout=30)
            with open(probe, "rb") as fh:
                head = fh.read(24)
            probe.unlink(missing_ok=True)
            w = int.from_bytes(head[16:20], "big")
            if abs(w - phys_w) <= 2:
                return scale
            if abs(w - round(phys_w / scale)) <= 2:
                return 1.0
        except Exception:
            pass
        log("⚠️ 无法确认 ffmpeg 屏幕坐标体系，按物理像素处理")
        return scale

    # 档位叶子元素定位：返回可见的档位按钮/菜单项中心坐标（tier 为空查当前按钮）。
    # 只认播放器区域内的同名元素，避免误点弹幕/评论里的相同文字。
    _TIER_RECT_JS = """
(tier) => {
  const TS = ['原画','蓝光','超清','高清','标清'];
  const vis = e => e.offsetParent !== null || e.getClientRects().length;
  const vid = document.querySelector('video');
  const vr = vid ? vid.getBoundingClientRect() : null;
  const leaf = [...document.querySelectorAll('div,span')].filter(e => {
    const t = (e.textContent || '').trim();
    if (!(tier ? t === tier : TS.includes(t)) || !vis(e)) return false;
    if ([...e.children].some(c => TS.includes((c.textContent || '').trim()))) return false;
    if (vr) {
      const r = e.getBoundingClientRect();
      const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
      if (cx < vr.x || cx > vr.x + vr.width || cy < vr.y || cy > vr.y + vr.height) return false;
    }
    return true;
  });
  if (!leaf.length) return null;
  const r = leaf[0].getBoundingClientRect();
  return {t: leaf[0].textContent.trim(), x: r.x + r.width / 2, y: r.y + r.height / 2};
}
"""

    def _switch_player_quality(self):
        """把网页播放器切到最高档（原画→蓝光→超清），尽力而为，失败不影响录制。

        播放器默认放标清且清晰度菜单靠悬停展开，JS click() 触发不了，
        必须用真实鼠标事件：悬停档位按钮等菜单出现，再点最高档。
        """
        try:
            # 控制栏默认隐藏，档位按钮不可见时先悬停播放器中央唤出控制栏再找，
            # 带重试；页面刚加载时控件可能尚未渲染
            cur = None
            deadline = time.time() + 6
            while time.time() < deadline and not cur:
                cur = self._page.evaluate(self._TIER_RECT_JS, None)
                if not cur:
                    vr = self._page.evaluate(
                        "() => { const v = document.querySelector('video');"
                        " if (!v) return null; const r = v.getBoundingClientRect();"
                        " return {x: r.x + r.width / 2, y: r.y + r.height / 2}; }")
                    if vr:
                        self._page.mouse.move(vr["x"], vr["y"])
                    time.sleep(1.0)
            if not cur:
                log("⚠️ 未找到播放器清晰度控件，保持默认画质录制")
                return
            self._page.mouse.move(cur["x"], cur["y"])
            time.sleep(0.8)
            # 按所选档位优先点击，缺档时向更高档回落、再向低档回落
            tiers = {"origin": ["原画", "蓝光", "超清"],
                     "super": ["超清", "蓝光", "原画"],
                     "high": ["高清", "超清", "蓝光", "原画"],
                     "standard": ["标清", "高清", "超清", "蓝光", "原画"]
                     }.get(self.quality, ["原画", "蓝光", "超清"])
            for tier in tiers:
                r = self._page.evaluate(self._TIER_RECT_JS, tier)
                if r:
                    self._page.mouse.move(r["x"], r["y"])
                    time.sleep(0.3)
                    self._page.mouse.click(r["x"], r["y"])
                    time.sleep(2.0)
                    # 鼠标移开，避免控制栏一直显示在录制画面里
                    self._page.mouse.move(10, 10)
                    log(f"播放器清晰度已切到「{r['t']}」")
                    return
            self._page.mouse.move(10, 10)  # 没有更高档，移开鼠标收起菜单
        except Exception:
            pass

    def _record_viewmode(self, room_id):
        """观看视角录制：录浏览器窗口画面（含弹幕/礼物特效）+ 原始流声音。

        与纯净模式不同，页面必须保持打开且不被遮挡（录的就是这块屏幕区域），
        因此资源占用更高、画质为播放分辨率而非原始流分辨率。
        一个 ffmpeg 进程同时采集屏幕和直播流声音，直接输出 mp4。
        """
        if not has_ffmpeg():
            log("❗观看视角录制需要 ffmpeg（用于屏幕采集），未检测到 ffmpeg")
            log("   重新运行「打开爬虫界面.bat」会自动装上，或改用「纯净原始流」模式")
            return
        vx, vy, vw, vh = VIEW_WIN
        grab_scale = self._probe_grab_scale()
        log(f"观看视角模式：只录播放器画面（含弹幕），声音来自原始直播流")
        log("⚠️ 录制期间请不要移动、最小化或遮挡弹出的浏览器窗口")
        # 网页播放器默认放标清，先切到最高档再录，避免录出来糊
        self._switch_player_quality()
        # 页面取消静音，和平时看直播一样（录制声音仍直接取自原始流，互不影响）
        try:
            self._page.evaluate(
                "document.querySelectorAll('video').forEach(v=>{v.muted=false;v.volume=1;})")
        except Exception:
            pass

        part, fails, recorded = 1, 0, 0
        while fails < 3 and not self._time_up():
            self._check_captcha()
            log(f"寻找直播流（作为声音来源）...（第 {part} 段）")
            _, url = self.wait_for_stream(20)
            if not url:
                cands = self._streams_from_page()
                if cands:
                    url = cands[0]
            if not url:
                if self._looks_not_live():
                    if self._live_ended():
                        log("主播已下播，结束录制")
                        break
                    log("主播当前没在直播，每 60 秒刷新等待开播...")
                    wait_until = time.time() + 60
                    while time.time() < wait_until and not self._time_up():
                        self._poll_stop()
                        time.sleep(2)
                    self._reopen_room()
                    # 重开页面后播放器回到默认标清，需重新切到最高档
                    self._switch_player_quality()
                    continue
                log("拿不到直播流地址（观看视角的声音需要从直播流提取）")
                self._check_captcha()
                pause("  请看一眼浏览器窗口确认状态，按回车重试...")
                fails += 1
                continue

            # 每段录制前重新定位播放器（页面重开/布局变化后位置会变）
            region = self._player_capture_rect(grab_scale)
            if region:
                vx, vy, vw, vh = region
                log(f"已定位播放器区域：({vx},{vy}) {vw}x{vh}（不含浏览器标签栏/地址栏）")
            else:
                # VIEW_WIN 是窗口钉扎用的逻辑像素（DIP），同样要换算成 gdigrab 坐标
                vx, vy, vw, vh = (int(v * grab_scale) // 2 * 2 for v in VIEW_WIN)
                log(f"⚠️ 未能定位播放器，退回录制整窗区域 ({vx},{vy}) {vw}x{vh}")

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            mp4 = self.outdir / f"{room_id}_{stamp}_part{part}_观看视角.mp4"
            self._finished.append(mp4)   # 先登记，任何退出路径统一收尾
            if self.t_start is None:
                self.t_start = time.time()
                if self.duration:
                    log(f"⏱ 开始计时：录 {self.duration // 60} 分钟")
            log(f"✅ 开始观看视角录制 → {mp4.name}")
            self._set_browser_topmost(True)   # 开录前置顶，防止被遮挡录到别的窗口
            cmd = [ffmpeg_cmd(), "-y", "-loglevel", "error",
                   "-f", "gdigrab", "-framerate", "30",
                   "-offset_x", str(vx), "-offset_y", str(vy),
                   "-video_size", f"{vw}x{vh}", "-i", "desktop",
                   "-user_agent", UA,
                   "-headers", "Referer: https://live.douyin.com/\r\n",
                   "-i", url,
                   "-map", "0:v", "-map", "1:a",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                   "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-b:a", "160k",
                   str(mp4)]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)

            def stop_ffmpeg():
                # 必须发 q 让 ffmpeg 正常收尾（直接 kill 会丢 mp4 索引导致文件损坏）
                if proc.poll() is None:
                    try:
                        proc.stdin.write(b"q")
                        proc.stdin.flush()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=15)
                    except Exception:
                        proc.kill()

            last_report = time.time()
            try:
                while proc.poll() is None and not self._time_up():
                    time.sleep(3)
                    if mp4.exists() and time.time() - last_report >= 30:
                        log(f"  录制中... {mp4.stat().st_size / 1048576:.1f} MB")
                        last_report = time.time()
                    if self._live_ended():
                        log("页面显示直播已结束，停止录制")
                        break
                    self._check_captcha()
                    self._poll_stop()
            finally:
                stop_ffmpeg()   # 无论正常结束还是中断，都让 ffmpeg 写完 mp4 索引
                self._set_browser_topmost(False)   # 录完恢复普通窗口层级

            size = mp4.stat().st_size if mp4.exists() else 0
            if size >= 102400:
                recorded += 1
                log(f"  本段录制 {size / 1048576:.1f} MB")
                if self._time_up():
                    break
                # 用户要求：流一断就收尾，不再找流重连
                log("流断开了，直接收尾（按设定不再找流重连）")
                break
            else:
                if mp4.exists() and size < 10240:
                    mp4.unlink()
                if recorded:
                    log("本段没有录到有效数据，判定直播已结束，收尾")
                    break
                fails += 1
                log(f"本段没有录到有效数据（{fails}/3），重试...")

        if not recorded:
            log("观看视角没有录到任何内容，退出")

    # ---------- 主流程 ----------

    def run(self, query):
        self.outdir.mkdir(parents=True, exist_ok=True)

        if self.duration:
            dur_txt = f"录 {self.duration // 60} 分钟"
        elif INTERACTIVE:
            dur_txt = "不限时长（Ctrl+C 停止）"
        else:
            dur_txt = "不限时长（可在网页界面点「停止」结束）"
        if self.start_ts:
            dur_txt += f"；定时开始 {datetime.fromtimestamp(self.start_ts).strftime('%m-%d %H:%M')}"
        log(f"录制计划: {dur_txt}")
        if not has_ffmpeg():
            log("提示: 本机没装 ffmpeg，录制结果为 .flv（VLC/PotPlayer 可播放）；"
                "装上 ffmpeg 可自动转成 .mp4")

        if self.start_ts:
            log("进入定时等待" + ("，不想等就 Ctrl+C 退出" if INTERACTIVE else ""))
            self.wait_for_start()
        if not check_disk_free(self.outdir):
            return
        # duration 从真正拿到流、开始录制那一刻才起算（启动浏览器/等开播不占时长）
        self.t_start = None

        self.start_browser()
        try:
            if self._is_direct_room(query):
                room_id = self.open_room(query)
            else:
                # 主页链接/分享文案：先等开播，解析出直播间再录
                room_url = self._wait_for_live(query)
                room_id = self.open_room(room_url)
            # 观看视角走独立流程（录屏幕画面，不下载原始流文件）
            if self.danmode == "view":
                self._record_viewmode(room_id)
                return
            fails = 0
            part = 1
            while fails < 3 and not self._time_up():
                self._check_captcha()
                log(f"寻找直播流...（第 {part} 段）")
                # 优先直取播放器数据里的最高档（原画）；播放器默认放标清，
                # 直接拦截播放器请求会录到低档画面
                su = self._stream_from_store()
                if su:
                    kind, url = "flv", su
                    log("已从播放器数据直取最高画质（原画）拉流地址")
                else:
                    kind, url = self.wait_for_stream(20)

                if not url:
                    # 播放器走 WebRTC 时拦不到 flv 请求，改从页面数据里挖
                    cands = self._streams_from_page()
                    if cands:
                        kind, url = "flv", cands[0]
                        m = re.search(r'_([a-z0-9]{2,4})\.flv', url)
                        tag = m.group(1) if m else "默认"
                        log(f"从页面解析出 {len(cands)} 路拉流地址，"
                            f"选用最高画质档（{tag}）")

                if not url:
                    if self._finished:
                        log("已录到的流中断后未能恢复，判定主播已下播，结束录制并收尾")
                        break
                    if self._looks_not_live():
                        log("主播当前没在直播。脚本会一直等，每 60 秒刷新检查，开播后自动开始录"
                            + ("（不想等就 Ctrl+C 退出）" if INTERACTIVE
                               else "（可在网页界面点「停止」结束）"))
                        wait_until = time.time() + 60
                        while time.time() < wait_until and not self._time_up():
                            self._poll_stop()
                            time.sleep(2)
                        self._reopen_room()
                        continue
                    log("页面里既没拦到直播流，也挖不到拉流地址"
                        "（可能未开播 / 弹了验证 / 直播间结构变化）")
                    self._check_captcha()
                    pause("  请看一眼浏览器窗口确认状态，按回车重试...")
                    fails += 1
                    continue

                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                base = self.outdir / f"{room_id}_{stamp}_part{part}"
                if self.t_start is None:
                    self.t_start = time.time()   # 从这里才开始计时长
                    if self.duration:
                        log(f"⏱ 开始计时：录 {self.duration // 60} 分钟")
                log(f"✅ 已拿到{'视频流' if kind == 'flv' else 'HLS 流'}，开始录制 → {base.name}.*")
                log("   （录制的是直播原始流，无弹幕、无评论、无在线人数等任何界面元素）")
                # 录制只依赖拉流地址，页面切空白，停掉弹幕/动画/视频解码的开销
                self._park_page()
                raw = base.with_suffix(".flv" if kind == "flv" else ".mp4")
                self._finished.append(raw)   # 先登记，任何退出路径统一收尾
                try:
                    if kind == "flv":
                        size = self.record_flv(url, base)
                    else:
                        if not has_ffmpeg():
                            log("只拦到 HLS 流且本机没有 ffmpeg，无法录制。"
                                "请安装 ffmpeg 后重试")
                            break
                        size = self.record_m3u8(url, base)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    log(f"流中断: {type(e).__name__}: {e}")
                    size = 0

                if size >= 102400:
                    log(f"  本段录制 {size/1048576:.1f} MB")
                    fails = 0
                    part += 1
                    if self._time_up():
                        break
                    # 用户要求：流一断就收尾打包，不再找流重连
                    log("流断开了，直接收尾转封装（按设定不再找流重连）")
                    break
                else:
                    if raw.exists() and raw.stat().st_size < 10240:
                        raw.unlink()
                    if self._finished:
                        log("本段没有录到有效数据，判定直播已结束，收尾转封装")
                        break
                    fails += 1
                    log(f"本段没有录到有效数据（{fails}/3），重试...")

            if not self._finished:
                log("没有录到任何内容，退出")
        finally:
            # 任何退出路径（正常结束 / Ctrl+C / 界面停止 / 异常）都统一转封装收尾
            self.stop_browser()
            self._finalize()


def salvage_leftover_flv(outdir):
    """崩溃兜底：把上次异常退出（进程被强杀/断电等）残留的 flv 转封装。

    正常路径都会在 run() 的 finally 里收尾，但强杀/断电收不到任何信号，
    这里在每次启动时扫描输出目录做补救；.part 半成品也一并改名转封装。
    """
    outdir = Path(outdir)
    if not outdir.is_dir():
        return
    leftovers = sorted(outdir.glob("*.flv.part")) + sorted(outdir.glob("*.flv"))
    if not leftovers:
        return
    log("=" * 60)
    log(f"发现 {len(leftovers)} 个上次残留的录制文件，自动转封装收尾...")
    for f in leftovers:
        if f.suffix == ".part":
            target = f.with_suffix("")   # xxx.flv.part -> xxx.flv
            try:
                f.rename(target)
            except OSError as e:
                log(f"  ✗ {f.name} 改名失败: {e}")
                continue
            f = target
        if f.stat().st_size < 10240:
            f.unlink()
            log(f"  ✗ {f.name} 几乎是空的，已删除")
            continue
        mp4 = flv_to_mp4(f)
        if mp4:
            log(f"  ✓ {mp4.name}（{mp4.stat().st_size / 1048576:.1f} MB）")
        else:
            log(f"  ✗ {f.name} 转封装失败，原文件保留")
    log("残留文件处理完毕")
    log("=" * 60)


def _selftest():
    """快速自检：ffmpeg / 屏幕采集 / cookies / 网络是否可用（不录）。

    「录不上」时先跑这个，快速定位是环境问题还是抖音页面问题。
    """
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok = ok and good
        log(f"{'✅' if good else '❌'} {name}{('：' + detail) if detail else ''}")

    # 1. ffmpeg（转封装 / 观看视角录屏都靠它）
    report("ffmpeg", has_ffmpeg(),
           "" if has_ffmpeg() else "未找到（重跑「打开爬虫界面.bat」自动安装）")

    # 2. 屏幕采集（观看视角模式依赖 gdigrab 抓屏）
    probe = Path(os.environ.get("TEMP", ".")) / "_recorder_selftest.png"
    try:
        subprocess.run([ffmpeg_cmd(), "-y", "-loglevel", "error", "-f", "gdigrab",
                        "-i", "desktop", "-frames", "1", str(probe)],
                       capture_output=True, timeout=30)
        good = probe.exists() and probe.stat().st_size > 10240
        report("屏幕采集（gdigrab 抓帧）", good)
    except Exception as e:
        report("屏幕采集（gdigrab 抓帧）", False, str(e)[:80])
    finally:
        probe.unlink(missing_ok=True)

    # 3. cookies
    ck = Path(__file__).parent / "cookies.txt"
    good = ck.exists() and ck.stat().st_size > 50
    report("cookies 文件", good,
           str(ck) if good else f"缺失（{ck}），未登录也能录大部分直播，但部分直播间受限")

    # 4. 浏览器 + 直播站可达
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            pg = b.new_context(user_agent=UA).new_page()
            pg.goto("https://live.douyin.com/", wait_until="domcontentloaded",
                    timeout=30000)
            report("直播站可达（浏览器内核正常）", True)
            b.close()
    except Exception as e:
        report("直播站可达（浏览器内核正常）", False, str(e)[:100])
    return ok


def main():
    parser = argparse.ArgumentParser(description="抖音直播录制器（纯净画面，无弹幕评论等 UI）")
    parser.add_argument("--url", help="直播间链接（live.douyin.com/数字 或 live.douyin.com/@昵称）")
    parser.add_argument("--room", help="或：纯房间号（直播链接里那串数字）")
    parser.add_argument("--cookie", help="整串 cookie 字符串")
    parser.add_argument("--cookie-file", help="cookie 文件路径（默认自动找脚本目录下的 cookies.txt）")
    parser.add_argument("--minutes", type=int, default=0,
                        help="录多少分钟（默认 0 = 不限，Ctrl+C 停止）")
    parser.add_argument("--start", help="定时开始，如 20:00 或 2026-10-11 20:00（默认立即开始）")
    parser.add_argument("--outdir", default=str(Path(__file__).parent / "downloads" / "live"),
                        help="输出目录（默认 脚本目录/downloads/live）")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（不弹浏览器窗口，出验证时无法手动处理，不建议）")
    parser.add_argument("--danmode", choices=["clean", "view"], default="clean",
                        help="录制视角：clean=纯净原始流(无弹幕) view=观看视角(画面含弹幕，录屏)")
    parser.add_argument("--selftest", action="store_true",
                        help="快速自检：ffmpeg/屏幕采集/cookies/网络是否可用（不录）")
    parser.add_argument("--quality", choices=["origin", "super", "high", "standard"],
                        default="origin",
                        help="录制画质（默认 origin=原画）：super=超清 high=高清 standard=标清")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(0 if _selftest() else 1)

    # 崩溃兜底：先处理上次异常退出残留的录制文件（定时任务也不受影响）
    salvage_leftover_flv(args.outdir)

    query = args.url or args.room
    if not query:
        if not INTERACTIVE:
            log("未提供 --url 或 --room（无人值守模式无法交互输入），退出")
            sys.exit(1)
        print("=" * 60)
        print("抖音直播录制器（录制纯净画面，无弹幕/评论/人数等 UI）")
        print("=" * 60)
        print("提示：未开播时可填主播主页链接、抖音号/昵称或 App 分享文案，开播自动找到直播间")
        query = input("请输入直播间链接/房间号/抖音号/主播主页链接: ").strip()
        if not query:
            print("未输入，退出")
            sys.exit(1)
        if not args.start:
            s = input("几点开始录（如 20:00，直接回车 = 现在就开始）: ").strip()
            if s:
                args.start = s
        if not args.minutes:
            m = input("录多久（分钟，如 90；直接回车 = 不限，Ctrl+C 可随时停）: ").strip()
            if m.isdigit():
                args.minutes = int(m)
            elif m:
                print(f"「{m}」不是数字，按不限时长处理")
        if not args.danmode or args.danmode == "clean":
            d = input("录制视角：1=纯净原始流(无弹幕) 2=观看视角(画面含弹幕)（回车=1）: ").strip()
            if d == "2":
                args.danmode = "view"

    start_ts = None
    if args.start:
        start_ts, err = parse_when(args.start)
        if err:
            log(err)
            sys.exit(1)
        log(f"定时开始: {datetime.fromtimestamp(start_ts).strftime('%Y-%m-%d %H:%M:%S')}")

    cookie_file = args.cookie_file
    if not cookie_file and not args.cookie:
        default = Path(__file__).parent / "cookies.txt"
        if default.exists():
            cookie_file = str(default)

    # 未登录也能录制大部分直播，所以 cookie 缺失只提示、不退出
    if args.cookie:
        cookies = load_cookies(args.cookie, None)
    elif cookie_file:
        cookies = load_cookies(None, cookie_file)
    else:
        cookies = {}
        log("未提供 cookies：将以未登录状态尝试录制（部分直播可能受限）")
    log(f"cookies 解析出 {len(cookies)} 个字段")

    # 清掉上次可能残留的停止标记（避免一启动就被误判为要停止）
    try:
        _STOP_FLAG.unlink(missing_ok=True)
    except Exception:
        pass

    recorder = LiveRecorder(cookies, args.outdir,
                            duration=(args.minutes or 0) * 60,
                            headless=args.headless, start_ts=start_ts,
                            danmode=args.danmode, quality=args.quality)
    try:
        recorder.run(query)
    except KeyboardInterrupt:
        print()
        log("手动停止。已录片段已收尾转封装，文件保留在输出目录")


if __name__ == "__main__":
    main()
