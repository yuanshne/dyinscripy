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

说明：
  - 时间按分钟指定（--minutes），也可以只定时开始（--start），到点自动开录
  - 主播没开播时会自动等待，每 60 秒刷新检查一次，开播自动开始录
  - 直播中途网络断了会自动重连，分多个 part 文件继续录
  - 主播下播（页面显示"直播已结束"）会自动停止录制并收尾，不会空等到期
  - "主播暂时离开"不算下播，会继续等待主播回来
  - 录制期间页面自动切到空白页（弹幕/动画/视频解码全部停掉），不会越录越卡
  - 录完如果本机装有 ffmpeg，会把 .flv 自动转封装成 .mp4（无损、秒完成）
  - 没有 ffmpeg 也能用，产出 .flv 文件（VLC / PotPlayer 可直接播放）

依赖（已预装在专用环境里）: playwright, requests
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

# 观看视角模式：浏览器窗口钉在这个位置/大小，gdigrab 按同一屏幕区域采集
VIEW_WIN = (30, 30, 1300, 820)

# 交互判定：网页外壳（DY_UNATTENDED=1）或 stdin 不可读时按无人值守处理
INTERACTIVE = (os.environ.get("DY_UNATTENDED") != "1"
               and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def flv_to_mp4(flv_path):
    """用 ffmpeg 无损转封装 flv -> mp4，成功则删掉 flv，返回 mp4 路径"""
    if not has_ffmpeg():
        return None
    mp4 = flv_path.with_suffix(".mp4")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(flv_path), "-c", "copy", str(mp4)],
            capture_output=True, timeout=600)
    except Exception as e:
        log(f"  ffmpeg 转封装出错: {e}")
        return None
    if r.returncode == 0 and mp4.exists() and mp4.stat().st_size > 10240:
        flv_path.unlink()
        return mp4
    log("  ffmpeg 转封装失败，保留原 .flv 文件")
    return None


class LiveRecorder:
    def __init__(self, cookies, outdir, duration=0, headless=False, start_ts=None,
                 danmode="clean"):
        self.cookies = cookies
        self.outdir = Path(outdir)
        self.duration = duration      # 最长录制秒数，0 = 不限
        self.headless = headless
        self.start_ts = start_ts      # 定时开始的时间戳，None = 立即开始
        self.danmode = danmode or "clean"   # clean=纯净原始流 / view=观看视角(含弹幕)

        self.stream_url = None        # 拦截到的拉流地址
        self.stream_kind = None       # "flv" / "m3u8"
        self.t_start = None           # 本次任务开始时间
        self._room_url = None         # 直播间真实地址（重连用）
        self._stop = False

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    # ---------- 浏览器 ----------

    def start_browser(self):
        log("启动浏览器...")
        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        if self.danmode == "view":
            # 观看视角：窗口钉在固定位置，gdigrab 按同一区域采集画面
            args += [f"--window-position={VIEW_WIN[0]},{VIEW_WIN[1]}",
                     f"--window-size={VIEW_WIN[2]},{VIEW_WIN[3]}"]
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=args,
        )
        vp = ({"width": 1280, "height": 700} if self.danmode == "view"
              else {"width": 1600, "height": 900})
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

    # ---------- 直播间 ----------

    def open_room(self, query):
        """打开直播间页面，返回房间号（拿不到就返回 'live'）"""
        query = query.strip()
        m = re.search(r"live\.douyin\.com/(\d+)", query)
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

    def wait_for_start(self):
        """定时等待：到点前 60 秒返回（留出启动浏览器的时间）"""
        while True:
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
        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-user_agent", UA, "-headers", headers,
               "-i", url, "-c", "copy", "-bsf:a", "aac_adtstoasc", str(mp4)]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            while proc.poll() is None and not self._time_up():
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

    def _record_viewmode(self, room_id):
        """观看视角录制：录浏览器窗口画面（含弹幕/礼物特效）+ 原始流声音。

        与纯净模式不同，页面必须保持打开且不被遮挡（录的就是这块屏幕区域），
        因此资源占用更高、画质为播放分辨率而非原始流分辨率。
        一个 ffmpeg 进程同时采集屏幕和直播流声音，直接输出 mp4。
        """
        if not has_ffmpeg():
            log("❗观看视角录制需要 ffmpeg（用于屏幕采集），未检测到 ffmpeg")
            log("   请先安装 ffmpeg，或改用「纯净原始流」模式")
            return
        vx, vy, vw, vh = VIEW_WIN
        log(f"观看视角模式：录制屏幕区域 ({vx},{vy}) {vw}x{vh}，画面含弹幕等界面元素")
        log("⚠️ 录制期间请不要移动、最小化或遮挡弹出的浏览器窗口")
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
                        time.sleep(2)
                    self._reopen_room()
                    continue
                log("拿不到直播流地址（观看视角的声音需要从直播流提取）")
                self._check_captcha()
                pause("  请看一眼浏览器窗口确认状态，按回车重试...")
                fails += 1
                continue

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            mp4 = self.outdir / f"{room_id}_{stamp}_part{part}_观看视角.mp4"
            if self.t_start is None:
                self.t_start = time.time()
                if self.duration:
                    log(f"⏱ 开始计时：录 {self.duration // 60} 分钟")
            log(f"✅ 开始观看视角录制 → {mp4.name}")
            cmd = ["ffmpeg", "-y", "-loglevel", "error",
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
            while proc.poll() is None and not self._time_up():
                time.sleep(3)
                if mp4.exists() and time.time() - last_report >= 30:
                    log(f"  录制中... {mp4.stat().st_size / 1048576:.1f} MB")
                    last_report = time.time()
                if self._live_ended():
                    log("页面显示直播已结束，停止录制")
                    break
                self._check_captcha()
            stop_ffmpeg()

            size = mp4.stat().st_size if mp4.exists() else 0
            if size >= 102400:
                recorded += 1
                log(f"  本段录制 {size / 1048576:.1f} MB")
                part += 1
                fails = 0
                if self._time_up():
                    break
                log("流断开了，尝试重连（主播下播的话会自动结束）...")
                self._reopen_room()
                if self._live_ended():
                    log("主播已下播，结束录制")
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
            return
        log("=" * 60)
        log(f"观看视角录制完成！共 {recorded} 段，保存在: {self.outdir}")
        log("画面含弹幕等界面元素（与网页观看一致），声音来自原始直播流")
        log("=" * 60)

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
        # duration 从真正拿到流、开始录制那一刻才起算（启动浏览器/等开播不占时长）
        self.t_start = None

        self.start_browser()
        finished = []
        try:
            room_id = self.open_room(query)
            # 观看视角走独立流程（录屏幕画面，不下载原始流文件）
            if self.danmode == "view":
                self._record_viewmode(room_id)
                return
            fails = 0
            part = 1
            while fails < 3 and not self._time_up():
                self._check_captcha()
                log(f"寻找直播流...（第 {part} 段）")
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
                    # 已经录到内容：流断了且找不回来，大概率主播已下播。
                    # 直接收尾转封装，不再反复找流干等（用户明确要求的行为）
                    if finished:
                        log("已录到的流中断后未能恢复，判定主播已下播，"
                            "结束录制并收尾转封装")
                        break
                    if self._looks_not_live():
                        log("主播当前没在直播。脚本会一直等，每 60 秒刷新检查，开播后自动开始录"
                            + ("（不想等就 Ctrl+C 退出）" if INTERACTIVE
                               else "（可在网页界面点「停止」结束）"))
                        wait_until = time.time() + 60
                        while time.time() < wait_until and not self._time_up():
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
                    finished.append(raw)
                    fails = 0
                    part += 1
                    if self._time_up():
                        break
                    log("流断开了，尝试自动重连（主播下播的话会自动结束录制）...")
                    self._reopen_room()
                    if self._live_ended():
                        log("主播已下播（直播已结束），提前结束录制并收尾")
                        break
                else:
                    if raw.exists() and raw.stat().st_size < 10240:
                        raw.unlink()
                    if finished:
                        # 本段几乎没录到数据且前面已有内容：流已断，直接收尾
                        log("本段没有录到有效数据，判定直播已结束，收尾转封装")
                        break
                    fails += 1
                    log(f"本段没有录到有效数据（{fails}/3），重试...")

            # ------- 收尾：转封装 -------
            if not finished:
                log("没有录到任何内容，退出")
                return
            log("=" * 60)
            log("录制结束，处理文件...")
            final_files = []
            for f in finished:
                if f.suffix == ".flv":
                    mp4 = flv_to_mp4(f)
                    if mp4:
                        log(f"  ✓ {mp4.name}（{mp4.stat().st_size/1048576:.1f} MB，已从 flv 无损转封装）")
                        final_files.append(mp4)
                        continue
                final_files.append(f)
                log(f"  ✓ {f.name}（{f.stat().st_size/1048576:.1f} MB）")
            log(f"共 {len(final_files)} 个文件，保存在: {self.outdir}")
            log("录制的是直播原始流：无弹幕、无评论、无在线人数、无任何界面元素")
            log("=" * 60)
        finally:
            self.stop_browser()


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
    args = parser.parse_args()

    query = args.url or args.room
    if not query:
        if not INTERACTIVE:
            log("未提供 --url 或 --room（无人值守模式无法交互输入），退出")
            sys.exit(1)
        print("=" * 60)
        print("抖音直播录制器（录制纯净画面，无弹幕/评论/人数等 UI）")
        print("=" * 60)
        query = input("请输入直播间链接或房间号: ").strip()
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

    recorder = LiveRecorder(cookies, args.outdir,
                            duration=(args.minutes or 0) * 60,
                            headless=args.headless, start_ts=start_ts,
                            danmode=args.danmode)
    try:
        recorder.run(query)
    except KeyboardInterrupt:
        print()
        log("手动停止。正在收尾（浏览器已关，文件保留），重新运行脚本即可再次录制")


if __name__ == "__main__":
    main()
