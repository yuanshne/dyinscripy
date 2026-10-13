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
import base64
import gzip
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

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


# 拉流档位键（FULL_HD1=原画 HD1=超清 SD1=高清 SD2=标清），
# 首选原画，缺失时按列表顺序就近回落
QUALITY_KEYS = ["FULL_HD1", "HD1", "SD1", "SD2"]


# ---------- 弹幕接口 protobuf 最小解析（clean_dm 模式） ----------
# 不引入完整 proto 定义，只按字段号取需要的值，遇到不认识的结构自然跳过。
# 结构参照抖音 webcast 公开 proto：PushFrame{payloadType=7,payload=8}
# -> gzip -> Response{messagesList=1} -> Message{method=1,payload=2}
# -> ChatMessage{common=1,user=2,content=3}；Common.createTime=4，User.nickName=3

def _pb_varint(buf, i):
    r = s = 0
    while True:
        b = buf[i]
        i += 1
        r |= (b & 0x7F) << s
        if not b & 0x80:
            return r, i
        s += 7
        if s > 63:
            raise ValueError("varint too long")


def _pb_fields(buf):
    """遍历 protobuf 字段，yield (字段号, wire_type, 值)；varint 为 int，其余为 bytes。"""
    i, n = 0, len(buf)
    while i < n:
        key, i = _pb_varint(buf, i)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _pb_varint(buf, i)
        elif wt == 2:
            ln, i = _pb_varint(buf, i)
            if i + ln > n:
                raise ValueError("length overflow")
            v = buf[i:i + ln]
            i += ln
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        else:
            raise ValueError(f"wire type {wt}")
        yield fno, wt, v


def _pb_get(buf, fno):
    """取第一个指定字段的值，取不到返回 None。"""
    if not buf:
        return None
    for f, _wt, v in _pb_fields(buf):
        if f == fno:
            return v
    return None


def _pb_extract_messages(frame):
    """弹幕推送帧 -> [(method, (服务端时刻, 昵称, 内容)), ...]，只取聊天消息。

    解析器保证不抛异常：坏帧/未知结构返回空列表，由调用方放心使用。
    """
    try:
        return _pb_extract_messages_inner(frame)
    except Exception:
        return []


def _pb_extract_messages_inner(frame):
    payload = _pb_get(frame, 8)          # PushFrame.payload
    if payload is None:
        payload = _pb_get(frame, 7)      # 兼容 payloadType=7/payload=8 的旧排布
    if not payload:
        return []
    ptype = _pb_get(frame, 7)
    if isinstance(ptype, bytes) and ptype not in (b"msg", b""):
        return []                        # 只处理 msg 类型推送
    try:
        payload = gzip.decompress(payload)
    except Exception:
        pass                             # 未压缩的帧直接解
    out = []
    for f, wt, v in _pb_fields(payload):
        if f != 1 or wt != 2:            # Response.messagesList
            continue
        method = _pb_get(v, 1)           # Message.method
        if method != b"WebcastChatMessage":
            continue
        mpayload = _pb_get(v, 2)         # Message.payload (ChatMessage)
        if not mpayload:
            continue
        common = _pb_get(mpayload, 1) or b""
        user = _pb_get(mpayload, 2) or b""
        content = _pb_get(mpayload, 3) or b""
        create_ts = _pb_get(common, 4)   # Common.createTime（秒级时间戳）
        nick = _pb_get(user, 3)          # User.nickName
        out.append(("WebcastChatMessage",
                    (int(create_ts or 0),
                     nick.decode("utf-8", "ignore") if isinstance(nick, bytes) else "",
                     content.decode("utf-8", "ignore") if isinstance(content, bytes) else "")))
    return out


class LiveRecorder:
    def __init__(self, cookies, outdir, duration=0, headless=False, start_ts=None,
                 danmode="clean"):
        self.cookies = cookies
        self.outdir = Path(outdir)
        self.duration = duration      # 最长录制秒数，0 = 不限
        self.headless = headless
        self.start_ts = start_ts      # 定时开始的时间戳，None = 立即开始
        self.danmode = danmode or "clean"   # clean=纯净原始流 / clean_dm=纯净流+弹幕TXT

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

        # clean_dm 模式：弹幕接口采集（(本地接收时刻, 服务端时刻, 昵称, 内容) 列表）
        self._danmaku = []
        self._dm_warned = False       # 钩子未生效只告警一次
        self._part_t0 = {}            # 片段文件路径 -> 该段开始时刻（本地时间戳）

    # ---------- 浏览器 ----------

    def start_browser(self):
        log("启动浏览器...")
        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        vp = {"width": 1600, "height": 900}
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
        # clean_dm：页面加载前注入弹幕钩子（包装 WebSocket/Worker，旁听弹幕接口）
        if self.danmode == "clean_dm":
            self._context.add_init_script(self._DM_HOOK_JS)
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
        for key in QUALITY_KEYS:
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

    # ---------- 纯净流+弹幕（clean_dm）----------
    #
    # 弹幕从抖音直播的 IM WebSocket 接口拿：通过页面 JS 钩子包装
    # WebSocket / Worker 构造器，旁听页面（含 Web Worker 内）建立的
    # 弹幕连接，截获二进制 protobuf 帧并最小化解析，
    # 只取 WebcastChatMessage（观众发言）。签名由页面自己完成，
    # 我们是被动的旁听者，不额外发请求，抗接口签名变动。
    #
    # 为什么不用 Playwright 的 websocket 事件：实测抖音把弹幕 IM
    # 连接建在 Web Worker 里，Playwright 的事件层收不到 Worker 内
    # 建立的连接，只能在页面里挂钩子转发出来。

    # 页面加载前注入：包装 WebSocket（只挂钩 douyin 域的连接），
    # 二进制帧转 base64 存入 window.__dmFrames；
    # 包装 Worker：先在 Worker 里装同样的钩子再 importScripts 原脚本，
    # Worker 内收到的帧 base64 转发回主线程同一个缓冲。
    _DM_HOOK_JS = r"""
(() => {
  if (window.__dmHook) return;
  window.__dmHook = true;
  window.__dmFrames = [];
  const frameToB64 = (data) => {
    let buf;
    if (data instanceof ArrayBuffer) buf = new Uint8Array(data);
    else if (data instanceof Blob) {
      data.arrayBuffer().then(ab => frameToB64(ab)).catch(() => {});
      return;
    } else if (data && data.buffer instanceof ArrayBuffer && data.byteLength !== undefined) {
      buf = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    } else return;
    try {
      let s = '';
      for (let i = 0; i < buf.length; i += 0x8000)
        s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
      window.__dmFrames.push(btoa(s));
    } catch (e) {}
  };
  const isDouyin = (u) => { try { return String(u).includes('douyin'); } catch (e) { return false; } };

  const NativeWS = window.WebSocket;
  const PatchedWS = function (url, proto) {
    const ws = proto === undefined ? new NativeWS(url) : new NativeWS(url, proto);
    if (isDouyin(url)) {
      try { ws.addEventListener('message', e => frameToB64(e.data)); } catch (e) {}
    }
    return ws;
  };
  PatchedWS.prototype = NativeWS.prototype;
  try { PatchedWS.CONNECTING = 0; PatchedWS.OPEN = 1; PatchedWS.CLOSING = 2; PatchedWS.CLOSED = 3; } catch (e) {}
  window.WebSocket = PatchedWS;

  // Worker 包装：blob 代理脚本先装钩子再加载原脚本（module worker 无法代理，放行）
  const NativeWorker = window.Worker;
  window.Worker = function (url, opts) {
    if (opts && opts.type === 'module') return new NativeWorker(url, opts);
    let abs;
    try { abs = new URL(url, location.href).href; } catch (e) { return new NativeWorker(url, opts); }
    try {
      const shim =
        'var __push=function(data){' +
        'try{var buf;' +
        'if(data instanceof ArrayBuffer)buf=new Uint8Array(data);' +
        'else if(data instanceof Blob){data.arrayBuffer().then(function(ab){__push(ab);}).catch(function(){});return;}' +
        'else if(data&&data.buffer instanceof ArrayBuffer&&data.byteLength!==undefined)' +
        'buf=new Uint8Array(data.buffer,data.byteOffset,data.byteLength);' +
        'else return;' +
        "var s='';for(var i=0;i<buf.length;i+=0x8000)s+=String.fromCharCode.apply(null,buf.subarray(i,i+0x8000));" +
        'self.postMessage({__dm:btoa(s)});' +
        '}catch(e){}};' +
        'var NativeWS=self.WebSocket;' +
        'self.WebSocket=function(u,p){' +
        'var ws=p===undefined?new NativeWS(u):new NativeWS(u,p);' +
        "try{if(String(u).indexOf('douyin')>=0)ws.addEventListener('message',function(e){__push(e.data);});}catch(e){}" +
        'return ws;};' +
        'self.WebSocket.prototype=NativeWS.prototype;';
      const blob = new Blob([shim + 'importScripts(' + JSON.stringify(abs) + ');'],
                            {type: 'text/javascript'});
      const w = new NativeWorker(URL.createObjectURL(blob));
      w.addEventListener('message', e => {
        if (e && e.data && typeof e.data.__dm === 'string') window.__dmFrames.push(e.data.__dm);
      });
      return w;
    } catch (e) {
      return new NativeWorker(url, opts);
    }
  };
})();
"""

    # 排空帧缓冲 + 顺带暂停播放（一次 evaluate 干两件事，每 2 秒调一次）
    _DM_DRAIN_JS = """
() => {
  const frames = window.__dmFrames || [];
  window.__dmFrames = [];
  document.querySelectorAll('video').forEach(v => { v.pause(); v.muted = true; v.volume = 0; });
  return {hook: !!window.__dmHook, frames: frames};
}
"""

    def _drain_danmaku(self):
        """把页面钩子缓冲的弹幕帧搬回 Python 解析，顺带维持页面暂停。

        钩子未生效时告警一次；页面正在跳转/失败时静默跳过，不影响录制。
        """
        if self.danmode != "clean_dm" or self._page is None:
            return
        try:
            res = self._page.evaluate(self._DM_DRAIN_JS)
        except Exception:
            return
        if not res:
            return
        if not res.get("hook") and not self._dm_warned:
            self._dm_warned = True
            log("⚠️ 弹幕钩子未生效（页面可能限制了脚本注入），本次录制可能采不到弹幕")
        for b64 in res.get("frames") or []:
            try:
                frame = base64.b64decode(b64)
            except Exception:
                continue
            for _method, (create_ts, nick, content) in _pb_extract_messages(frame):
                if not content:
                    continue
                recv = time.time()
                # 服务端 create_time 缺失/异常时退回本地接收时刻
                ts = create_ts if 1_500_000_000 < create_ts < 4_000_000_000 else recv
                self._danmaku.append((recv, ts, nick, content))

    def _write_txt_files(self, pairs):
        """收尾时按片段写弹幕 TXT：相对秒 + 绝对时间 + 昵称 + 内容。"""
        t0s = sorted(self._part_t0.values())
        for raw, final in pairs:
            t0 = self._part_t0.get(str(raw))
            if t0 is None:
                continue
            target = final or raw
            if not target or not Path(str(target)).exists():
                continue
            nxt = next((v for v in t0s if v > t0), None)
            msgs = [m for m in self._danmaku if t0 <= m[0] and (nxt is None or m[0] < nxt)]
            if not msgs:
                log(f"  （{Path(str(target)).name} 时段内未采集到弹幕，跳过弹幕文件）")
                continue
            txt = Path(str(target)).with_suffix(".txt")
            try:
                with open(txt, "w", encoding="utf-8") as f:
                    f.write(f"# 纯净流弹幕记录（来源：抖音直播弹幕接口）\n")
                    f.write(f"# 片段: {Path(str(target)).name}\n")
                    f.write(f"# 片段开始: {datetime.fromtimestamp(t0):%Y-%m-%d %H:%M:%S}\n")
                    f.write("# 列: 相对时间(秒)\t绝对时间\t昵称\t内容\n")
                    for recv, create, nick, content in msgs:
                        rel = max(0.0, recv - t0)
                        abs_s = datetime.fromtimestamp(create).strftime("%Y-%m-%d %H:%M:%S")
                        f.write(f"{rel:.2f}\t{abs_s}\t{nick}\t{content}\n")
                log(f"  ✓ {txt.name}（弹幕记录，{len(msgs)} 条，含相对/绝对时间）")
            except OSError as e:
                log(f"  ✗ 弹幕文件写入失败 {txt.name}: {e}")

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
        pairs = []   # (登记时的片段路径, 收尾后的最终文件)，clean_dm 生成弹幕字幕用
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
                    pairs.append((f, mp4))
                    continue
            final_files.append(f)
            pairs.append((f, f))
            log(f"  ✓ {f.name}（{f.stat().st_size/1048576:.1f} MB）")
        if final_files:
            log(f"共 {len(final_files)} 个文件，保存在: {self.outdir}")
            if self.danmode == "clean_dm":
                log("录制的是直播原始流：无弹幕、无界面元素；弹幕已从接口同步采集为同名 .txt 文件")
            else:
                log("录制的是直播原始流：无弹幕、无评论、无在线人数、无任何界面元素")
            if self.danmode == "clean_dm":
                self._write_txt_files(pairs)
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
                            extra = (f"，已采集弹幕 {len(self._danmaku)} 条"
                                     if self.danmode == "clean_dm" else "")
                            log(f"  录制中... 已录 {size/1048576:.1f} MB{extra}")
                            last_report = time.time()
                        if time.time() - last_stopchk >= 2:
                            self._poll_stop()   # 收到停止请求会抛 KeyboardInterrupt
                            self._drain_danmaku()   # clean_dm：拉弹幕帧 + 维持页面暂停
                            last_stopchk = time.time()
                        if self._time_up():
                            break
        finally:
            self._drain_danmaku()
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
                self._drain_danmaku()   # clean_dm：拉弹幕帧 + 维持页面暂停
                time.sleep(2)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
            self._drain_danmaku()
        if mp4.exists() and mp4.stat().st_size > 10240:
            return mp4.stat().st_size
        return 0

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
                log("   （录制的是直播原始流，无弹幕、无评论、无在线人数等任何界面元素）"
                    if self.danmode != "clean_dm" else
                    "   （录制直播原始流；弹幕接口监听中，收尾生成同名弹幕 TXT）")
                if self.danmode == "clean_dm":
                    # 页面留在直播间，JS 钩子旁听弹幕接口；evaluate 顺带暂停播放
                    self._drain_danmaku()
                else:
                    # 录制只依赖拉流地址，页面切空白，停掉弹幕/动画/视频解码的开销
                    self._park_page()
                raw = base.with_suffix(".flv" if kind == "flv" else ".mp4")
                self._finished.append(raw)   # 先登记，任何退出路径统一收尾
                if self.danmode == "clean_dm":
                    self._part_t0[str(raw)] = time.time()   # 该段弹幕时间从 0 起算的基准
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

    # 1. ffmpeg（转封装靠它）
    report("ffmpeg", has_ffmpeg(),
           "" if has_ffmpeg() else "未找到（重跑「打开爬虫界面.bat」自动安装）")

    # 2. cookies
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
    parser.add_argument("--danmode", choices=["clean", "clean_dm"], default="clean",
                        help="录制视角：clean=纯净原始流(无弹幕) "
                             "clean_dm=纯净流+弹幕(原始流+弹幕接口采集的TXT记录，含相对/绝对时间)")
    parser.add_argument("--selftest", action="store_true",
                        help="快速自检：ffmpeg/cookies/网络是否可用（不录）")
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
            d = input("录制视角：1=纯净原始流(无弹幕) "
                      "2=纯净流+弹幕(原始流+接口采集的弹幕TXT)（回车=1）: ").strip()
            if d == "2":
                args.danmode = "clean_dm"

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
                            danmode=args.danmode)
    try:
        recorder.run(query)
    except KeyboardInterrupt:
        print()
        log("手动停止。已录片段已收尾转封装，文件保留在输出目录")


if __name__ == "__main__":
    main()
