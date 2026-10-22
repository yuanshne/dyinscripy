#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抖音用户作品爬虫（视频 + 图集图片）

原理：用 playwright 驱动真实浏览器，注入你提供的 cookies，
     打开目标用户主页自动滚动加载，拦截浏览器自己发出的接口响应来拿数据，
     从而绕开 a_bogus 签名校验。

用法：
  1. 交互模式（会逐项问你）:
       python douyin_spider.py
  2. 命令行模式:
       python douyin_spider.py --account 某抖音号 --cookie-file cookies.txt
       python douyin_spider.py --url "https://www.douyin.com/user/MS4wLjAB..." --cookie-file cookies.txt
       python douyin_spider.py --account 某抖音号 --cookie-file cookies.txt --limit 50

依赖（已预装在专用环境里，无需再装）: playwright, requests
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
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

# exe 模式下 __file__ 指向 _internal，cookies/logs/下载目录要对齐 exe 同级（与 UI 外壳一致）
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

# 与浏览器保持一致的 UA（下载视频/图片时也要用同一个，降低风控概率）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

API_POST = "/aweme/v1/web/aweme/post/"            # 用户作品列表接口
API_PROFILE = "/aweme/v1/web/user/profile/other/"  # 用户资料接口
API_DETAIL = "/aweme/v1/web/aweme/detail/"         # 单视频详情接口
API_SEARCH = "/aweme/v1/web/search/item/"          # 搜索接口
API_COMMENT = "/aweme/v1/web/comment/list/"        # 评论接口（含回复列表 /reply/）

CAPTCHA_KEYWORDS = ("验证码", "安全验证", "拖动滑块", "滑动验证")

# 验证检测 JS：同时检查页面可见文本与实际可见的验证元素
# （抖音会在 DOM 里预置隐藏的 captcha 容器，必须按尺寸/可见性过滤掉）
_CAPTCHA_JS = """() => {
    const txt = (document.body && document.body.innerText) || '';
    const kws = ['验证码', '安全验证', '拖动滑块', '滑动验证'];
    const textHit = kws.some(k => txt.includes(k));
    let elHit = false;
    const els = document.querySelectorAll('[class*="captcha" i], [id*="captcha" i], iframe[src*="captcha" i]');
    for (const el of els) {
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        if (r.width > 50 && r.height > 50
                && st.display !== 'none' && st.visibility !== 'hidden') {
            elHit = true;
            break;
        }
    }
    return {textHit: textHit, elHit: elHit};
}"""


# ---------------------------------------------------------------- 工具函数

# 后台/无人值守运行时（stdin 不是终端）禁用一切交互等待
# 交互判定：网页外壳（DY_UNATTENDED=1）或 stdin 不可读时按无人值守处理
INTERACTIVE = (os.environ.get("DY_UNATTENDED") != "1"
               and hasattr(sys.stdin, "isatty") and sys.stdin.isatty())


_LOG_FILE = BASE_DIR / "logs" / f"{datetime.now():%Y%m%d}.log"


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Windows PowerShell / older terminals may use GBK, which cannot
        # represent the status icons used by selftest output.
        print(line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"), flush=True)
    # 同时落盘到 logs/日期.log，事后可查（界面内存日志关掉就没了）
    try:
        _LOG_FILE.parent.mkdir(exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def pause(msg):
    """人工确认点：交互模式等待回车，无人值守模式提示后自动继续"""
    if INTERACTIVE:
        try:
            input(f"{msg}（按回车继续）")
        except (EOFError, OSError):
            log(f"(无法读取输入，自动继续) {msg}")
    else:
        log(f"(自动继续) {msg}")


def safe_name(s, maxlen=40):
    """把标题变成安全的文件名片段"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:maxlen] or "无标题")


def _best_video_url(video):
    """从 bit_rate 多档里选码率最高的一档，拿不到返回 None（调用方退回默认档）"""
    rates = [br for br in (video.get("bit_rate") or [])
             if (br.get("play_addr") or {}).get("url_list")]
    if not rates:
        return None
    best = max(rates, key=lambda br: br.get("bit_rate") or 0)
    return best["play_addr"]["url_list"][0]


def _slim_aweme(aw):
    """只保留下载/写元数据/去重真正用到的字段。

    接口返回的 aweme JSON 每条约十几到几十 KB（music、text_extra、
    bit_rate 全档位地址等），大批量采集时全部常驻内存会把占用推高很多。
    裁剪后单条约 1~3KB，且最高码率档在入库时就算好、只留选中那一档，
    后续 _best_video_url / build_tasks / summarize_work 行为不变。
    """
    video = aw.get("video") or {}
    rates = [br for br in (video.get("bit_rate") or [])
             if (br.get("play_addr") or {}).get("url_list")]
    best = max(rates, key=lambda br: br.get("bit_rate") or 0) if rates else None
    return {
        "aweme_id": aw.get("aweme_id"),
        "desc": aw.get("desc"),
        "create_time": aw.get("create_time"),
        "is_top": aw.get("is_top"),
        "statistics": {k: (aw.get("statistics") or {}).get(k)
                       for k in ("digg_count", "comment_count",
                                 "collect_count", "share_count")},
        "author": {k: (aw.get("author") or {}).get(k)
                   for k in ("sec_uid", "unique_id", "nickname")},
        "images": [{"url_list": (img or {}).get("url_list") or []}
                   for img in (aw.get("images") or [])],
        "video": {
            "play_addr": {"url_list": (video.get("play_addr") or {})
                          .get("url_list") or []},
            "download_addr": {"url_list": (video.get("download_addr") or {})
                              .get("url_list") or []},
            "bit_rate": ([{"bit_rate": best.get("bit_rate"),
                           "play_addr": {"url_list": best["play_addr"]["url_list"]}}]
                         if best else []),
        },
    }


def parse_cookie_string(raw):
    """把浏览器复制的整串 cookie（'k=v; k2=v2'）解析成 dict"""
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
        # 跳过注释行（# 开头）和空行，取剩下最长的行当作 cookie 串
        lines = [ln for ln in lines if ln and not ln.startswith("#")]
        if not lines:
            log(f"cookie 文件是空的: {p}")
            sys.exit(1)
        cookie_str = max(lines, key=len)
    if not cookie_str:
        log("未提供 cookies")
        sys.exit(1)
    return parse_cookie_string(cookie_str)


def classify_input(q):
    """识别输入是 sec_uid / 主页链接 / 抖音号"""
    q = q.strip()
    if "MS4wLjAB" in q:
        m = re.search(r"(MS4wLjAB[A-Za-z0-9_-]+)", q)
        return "sec_uid", m.group(1)
    if "douyin.com" in q or "iesdouyin.com" in q:
        return "url", q
    return "account", q


# ---------------------------------------------------------------- 爬虫主体

class DouyinSpider:
    def __init__(self, cookies, outdir, headless=False, limit=None,
                 since=None, until=None, with_comments=False):
        self.cookies = cookies
        self.outdir = Path(outdir)
        self.headless = headless
        self.limit = limit
        self.with_comments = with_comments  # 集成模式：下载作品后逐个抓取评论
        # 时间段过滤（"YYYY-MM-DD"，None 表示不限）
        self.since_ts = self._date_ts(since) if since else None
        self.until_ts = self._date_ts(until) + 86399 if until else None

        self.works = {}          # aweme_id -> aweme dict
        self.target_sec = None   # 目标用户 sec_uid，兜底过滤非目标用户的作品
        self.done_ids = set()    # 上次运行已下载过的 aweme_id（跨次去重）
        self.expect_vid = None   # 单视频/评论模式：只接受这个视频 id 的详情响应
        self.want_detail = True  # 评论模式关掉详情解析：大响应同步 json() 会阻塞
                                 # playwright 事件循环，导致懒加载的评论请求发不出
        self.only_comment = False  # 评论模式置 True：回调只处理评论接口，其余一律跳过
        self.has_more = True
        self.profile = None      # 用户资料 dict
        # 评论抓取状态
        self.comments = []           # 按接口返回顺序的评论（只留 CSV 所需字段）
        self._comment_seen = set()   # 已收到的 cid
        self._pending_comment_resps = []  # 回调线程入队、主线程解析的评论响应
        self.comment_has_more = True
        self._last_comment_resp = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    @staticmethod
    def _date_ts(s):
        return int(datetime.strptime(s, "%Y-%m-%d").timestamp())

    # ---------- 浏览器 ----------

    def start_browser(self):
        log("启动浏览器...")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent=UA,
            # 1920 宽屏下视频页评论区布局不同，滚轮触发序列会失效；
            # 1600x900 是实测评论区懒加载可正常触发的尺寸
            viewport={"width": 1600, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        # 抹掉 navigator.webdriver 标记
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        # 注册接口拦截（挂在 context 级，任何页面发出的响应都能收到）
        self._context.on("response", self._on_response)
        self._page = self._context.new_page()

        # 先访问一次抖音拿到域，再注入 cookies，最后刷新生效
        self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
        cookie_list = [{"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                       for k, v in self.cookies.items()]
        self._context.add_cookies(cookie_list)
        self._page.reload(wait_until="domcontentloaded")
        time.sleep(2)

        if "sessionid" not in self.cookies and "sessionid_ss" not in self.cookies:
            log("⚠️ cookies 里没有 sessionid，可能不是登录状态，后续可能拿不到完整数据或触发验证")

    def stop_browser(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ---------- 接口拦截 ----------

    def _on_response(self, resp):
        try:
            url = resp.url
            if self.only_comment:
                # 回调线程里绝不读 body：resp.json() 会同步等 body、阻塞事件
                # 循环，连带卡死页面自己发出的懒加载请求。先把响应存起来，
                # 由主线程 run_comments 的等待循环里调用 _drain_comments() 解析
                if API_COMMENT in url:
                    self._pending_comment_resps.append(resp)
                return
            if API_PROFILE in url:
                data = resp.json()
                user = (data.get("data") or {}).get("user") or data.get("user")
                if user and user.get("sec_uid"):
                    self.profile = {
                        "sec_uid": user.get("sec_uid"),
                        "unique_id": user.get("unique_id") or user.get("short_id"),
                        "nickname": user.get("nickname"),
                        "signature": user.get("signature"),
                        "follower_count": user.get("follower_count"),
                        "aweme_count": user.get("aweme_count"),
                    }
            elif API_POST in url:
                # 作品列表接口随时都收：主页一打开就会发出第一页（含全部
                # 作品），此时可能还没进收集阶段，靠时间门禁会把它误杀掉，
                # 导致滚动时只请求到空的第二页、一个作品都收不到。
                # 候选用户主页的污染由 resolve_by_account 的快照法隔离
                data = resp.json()
                aweme_list = data.get("aweme_list") or []
                new_cnt = 0
                for aw in aweme_list:
                    awid = aw.get("aweme_id")
                    if awid and awid not in self.works:
                        # 兜底校验：接口数据里带作者 sec_uid 且与目标不符的直接丢弃
                        au = (aw.get("author") or {}).get("sec_uid")
                        if self.target_sec and au and au != self.target_sec:
                            continue
                        self.works[awid] = _slim_aweme(aw)
                        new_cnt += 1
                if data.get("has_more") in (0, False, "0", None):
                    if new_cnt == 0:
                        self.has_more = False
                self._last_post_resp = time.time()
            elif API_DETAIL in url and self.want_detail:
                data = resp.json()
                aw = data.get("aweme_detail") or {}
                # 详情接口只服务单视频模式（expect_vid 精确匹配）。主页模式
                # 下 expect_vid 为 None，此时任何详情响应都是干扰——用户主页
                # 自动弹开的视频浮层会预加载"相关推荐"视频（别人家视频）的
                # 详情，收进来就会下载错的作品，还置 has_more=False 中断收集
                if self.expect_vid is None:
                    return
                if aw.get("aweme_id") == self.expect_vid:
                    self.works[aw["aweme_id"]] = _slim_aweme(aw)
                    self.has_more = False
                    self._last_post_resp = time.time()
            elif API_SEARCH in url:
                # 搜索结果页的卡片里混着直播/商品等推广卡，
                # 只收带可下载视频地址的条目
                data = resp.json()
                new_cnt = 0
                for card in data.get("data") or []:
                    aw = (card or {}).get("aweme_info") or {}
                    awid = aw.get("aweme_id")
                    if (awid and awid not in self.works
                            and (aw.get("video") or {}).get("play_addr", {}).get("url_list")):
                        self.works[awid] = _slim_aweme(aw)
                        new_cnt += 1
                if new_cnt:
                    self._last_post_resp = time.time()
            elif API_COMMENT in url:
                self._pending_comment_resps.append(resp)
        except Exception:
            pass  # 非 JSON 响应（图片/脚本等）直接忽略

    def _drain_comments(self):
        """主线程解析排队的评论响应（此时 body 已就绪，json() 不会卡事件循环）"""
        while self._pending_comment_resps:
            # 排队期间可能已经一次收到多页响应，达到数量上限就丢弃余下的
            if self.limit and len(self.comments) >= self.limit:
                self._pending_comment_resps.clear()
                return
            resp = self._pending_comment_resps.pop(0)
            try:
                data = resp.json()
            except Exception as e:
                log(f"评论响应不是 JSON（{type(e).__name__}），跳过")
                continue
            log(f"评论响应 status_code={data.get('status_code')}, "
                f"comments={len(data.get('comments') or [])}, has_more={data.get('has_more')}")
            if data.get("comments"):
                self._last_comment_resp = time.time()
            for c in data.get("comments") or []:
                # 顶级评论响应里还内嵌回复预览（reply_comment 字段，
                # 展开前页面自动带出的前几条回复），一并收集，
                # 剩余未带出的回复由 _expand_reply_phase 点击展开后并入
                for item in [c] + (c.get("reply_comment") or []):
                    cid = item.get("cid")
                    if cid and cid not in self._comment_seen:
                        self._comment_seen.add(cid)
                        # 采集时就压缩成 CSV 所需字段：原始评论 dict 含用户
                        # 资料等，每条几 KB，几万条评论会累积上百 MB 内存
                        self.comments.append(self.summarize_comment(item))
            if data.get("has_more") in (0, False, "0"):
                self.comment_has_more = False

    # ---------- 定位用户 ----------

    def open_user_page(self, sec_uid):
        """打开用户主页并等待资料返回（接口拦截优先，DOM 提取兜底）"""
        url = f"https://www.douyin.com/user/{sec_uid}"
        log(f"打开用户主页: {url}")
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)

        deadline = time.time() + 15
        while time.time() < deadline and not self.profile:
            time.sleep(0.5)
        self._check_captcha()

        if not self.profile:
            log("接口没返回用户资料（资料可能走页面渲染），从页面内容提取...")
            dom_info = self._profile_from_dom()
            if dom_info:
                dom_info["sec_uid"] = sec_uid
                self.profile = dom_info

        if not self.profile:
            pause("  请在浏览器窗口里看一下（有弹窗就手动处理）")
            deadline = time.time() + 10
            while time.time() < deadline and not self.profile:
                time.sleep(0.5)
            if not self.profile:
                self.profile = self._profile_from_dom()

        if not self.profile:
            log("仍拿不到用户资料，继续尝试爬作品（资料可能拿不到）")
        return self.profile

    def _profile_from_dom(self):
        """资料接口拦不到时，从主页可见文本里提取昵称/抖音号"""
        try:
            info = self._page.evaluate("""() => {
                let nick = null, uid = null;
                const h1 = document.querySelector('h1');
                if (h1) nick = h1.textContent.trim();
                const m = (document.body.innerText || '').match(/抖音号[：:]\\s*([A-Za-z0-9_.\\-]+)/);
                if (m) uid = m[1];
                return {nickname: nick, unique_id: uid};
            }""")
            if info and (info.get("nickname") or info.get("unique_id")):
                return {"sec_uid": None, "unique_id": info.get("unique_id"),
                        "nickname": info.get("nickname"), "signature": None,
                        "follower_count": None, "aweme_count": None}
        except Exception:
            pass
        return None

    def resolve_by_account(self, account):
        """抖音号 -> sec_uid：搜索页找候选用户，逐个打开比对抖音号"""
        # 先在首页停留+滚动预热，降低搜索页触发风控的概率
        log("首页预热（降低搜索风控概率）...")
        self._page.mouse.wheel(0, random.randint(600, 1400))
        time.sleep(random.uniform(1.0, 2.0))
        self._page.mouse.wheel(0, random.randint(600, 1400))
        time.sleep(random.uniform(1.0, 2.0))

        search_url = f"https://www.douyin.com/search/{quote(account)}?type=user"
        log(f"通过搜索定位抖音号「{account}」...")
        self._page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
        self._page.mouse.wheel(0, 2000)
        time.sleep(2)
        self._check_captcha()

        hrefs = self._page.eval_on_selector_all(
            'a[href*="/user/"]', "els => els.map(e => e.href)")
        candidates = []
        for h in hrefs:
            m = re.search(r"/user/([A-Za-z0-9_-]+)", h)
            if m and m.group(1) not in candidates:
                candidates.append(m.group(1))
        # MS4wLjAB 开头的（真实 sec_uid）排前面
        candidates.sort(key=lambda c: not c.startswith("MS4wLjAB"))
        candidates = candidates[:8]
        if not candidates:
            log("搜索页没找到任何用户候选，换个网络环境重试，或直接用主页链接方式输入")
            sys.exit(1)
        log(f"搜索到 {len(candidates)} 个候选用户，逐个核对抖音号...")

        fallback = None
        last_prev_ids = set()
        for i, sec in enumerate(candidates, 1):
            # 进候选页前快照：每个候选主页都会发出自己的作品列表接口，
            # 不隔离的话前面候选（如搜索推荐里的其他账号）的作品会混进
            # 目标用户的作品池，最后被一起下载
            last_prev_ids = set(self.works)
            self.profile = None
            self._page.goto(f"https://www.douyin.com/user/{sec}",
                            wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + 8
            while time.time() < deadline and not self.profile:
                time.sleep(0.4)
            if not self.profile:
                # 资料接口走页面渲染拦不到，从 DOM 提取兜底
                dom = self._profile_from_dom()
                if dom:
                    dom["sec_uid"] = sec
                    self.profile = dom
            if not self.profile:
                log(f"  候选 {i}: 拿不到资料，跳过")
                continue
            if fallback is None:
                fallback = self.profile
            uid = self.profile.get("unique_id") or ""
            nick = self.profile.get("nickname") or ""
            log(f"  候选 {i}: @{uid}（{nick}）")
            if uid == account:
                # 只保留该候选页面访问期间收到的作品（目标主页自己的第一页
                # 作品列表），此前其他候选页面的数据全部丢弃
                self.works = {k: v for k, v in self.works.items()
                              if k not in last_prev_ids}
                log("  ✅ 抖音号精确匹配")
                return self.profile
            if nick == account:
                self.works = {k: v for k, v in self.works.items()
                              if k not in last_prev_ids}
                log("  ✅ 昵称匹配")
                return self.profile

        if fallback:
            log(f"⚠️ 没有精确匹配到抖音号「{account}」，将使用搜索排名第一的用户: "
                f"@{fallback.get('unique_id')}（{fallback.get('nickname')}）")
            pause("  如果这不是你要的用户，请停止任务；确认无误就继续")
            # 同理只保留最后访问的候选页面的作品（collect_works 滚动的是当前页）
            self.works = {k: v for k, v in self.works.items()
                          if k not in last_prev_ids}
            self.profile = fallback
            return fallback
        log("所有候选用户都拿不到资料（大概率 cookies 失效），退出")
        sys.exit(1)

    def resolve_user(self, query):
        kind, value = classify_input(query)
        if kind == "sec_uid":
            return self.open_user_page(value)
        if kind == "url":
            m = re.search(r"/user/([A-Za-z0-9_-]+)", value)
            if not m:
                # 可能是 v.douyin.com 短链，跟随重定向解析
                try:
                    r = requests.head(value, allow_redirects=True, timeout=15,
                                      headers={"User-Agent": UA})
                    final = r.url
                    m = re.search(r"/user/([A-Za-z0-9_-]+)", final)
                except Exception:
                    m = None
                if not m:
                    log(f"链接里解析不出用户 id: {value}")
                    log("请直接复制浏览器地址栏里 douyin.com/user/ 开头的完整链接")
                    sys.exit(1)
            sec = m.group(1)
            if sec.startswith("MS4wLjAB"):
                return self.open_user_page(sec)
            log("链接里是旧版数字 id，无法直接定位，请改用抖音号或含 MS4wLjAB 的主页链接")
            sys.exit(1)
        return self.resolve_by_account(value)

    # ---------- 滚动收集作品 ----------

    def _check_captcha(self):
        """只在页面可见文本/验证元素里找验证特征，避免误匹配 JS 源码"""
        hit = None
        try:
            hit = self._page.evaluate(_CAPTCHA_JS)
        except Exception:
            return
        if not (hit and (hit.get("textHit") or hit.get("elHit"))):
            return
        if not hit.get("elHit"):
            # 纯文本命中可能是视频标题等内容误报（搜索结果里就可能含
            # 「验证」字样），2 秒后复查，仍在才算真验证
            time.sleep(2)
            try:
                hit = self._page.evaluate(_CAPTCHA_JS)
            except Exception:
                return
            if not (hit and (hit.get("textHit") or hit.get("elHit"))):
                return
        log("❗ 检测到验证页面！请在弹出的浏览器窗口里手动完成验证")
        try:
            input("  完成后回到这里按回车继续...")
            return True
        except (EOFError, OSError):
            # 有 tty 却读不到输入（后台任务/管道环境）：在浏览器窗口里等人工处理，
            # 期间轮询验证是否已解除，最多 5 分钟
            deadline = time.time() + 300
            while time.time() < deadline:
                time.sleep(5)
                try:
                    still = self._page.evaluate(_CAPTCHA_JS)
                    if not (still and (still.get("textHit") or still.get("elHit"))):
                        log("验证已解除，继续")
                        return True
                except Exception:
                    return True
            log("⚠️ 验证长时间未解除，继续尝试（数据可能不全）")
            return True
        return False

    def collect_works(self):
        total_hint = (self.profile or {}).get("aweme_count")
        if total_hint:
            log(f"该用户共 {total_hint} 个作品，开始滚动收集...")
        else:
            log("开始滚动收集作品...")

        self._last_post_resp = time.time()
        last_n = -1
        stable = 0
        while True:
            if self.limit and len(self.works) >= self.limit:
                log(f"已达 --limit {self.limit}，停止收集")
                break
            if not self.has_more and len(self.works) == last_n:
                log("接口已无更多数据")
                break
            # 作品按时间倒序排列：非置顶作品里已出现早于起始日期的，再滚也不会有区间内的了
            if self.since_ts is not None and len(self.works) > 0:
                non_top_ct = [w.get("create_time") or 0
                              for w in self.works.values() if not w.get("is_top")]
                if non_top_ct and min(non_top_ct) < self.since_ts:
                    log("已滚动到早于起始日期的作品，停止收集")
                    break
            if time.time() - self._last_post_resp > 45 and len(self.works) == 0:
                log("长时间没收到作品接口数据（可能 cookies 失效 / 弹验证 / 网络问题）")
                self._check_captcha()
                pause("  请检查浏览器窗口，处理后再继续")
                self._last_post_resp = time.time()
            self._page.mouse.wheel(0, 4000)
            time.sleep(random.uniform(1.2, 2.2))
            self._check_captcha()

            if len(self.works) == last_n:
                stable += 1
                if stable >= 6:
                    if self.has_more:
                        log("⚠️ 连续多次滚动没有新数据，但接口显示还有更多，结果可能不全")
                    break
            else:
                stable = 0
                log(f"  已收集 {len(self.works)} 个作品")
            last_n = len(self.works)

        log(f"收集完成，共 {len(self.works)} 个作品")

    # ---------- 下载 ----------

    def _make_session(self):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": "https://www.douyin.com/"})
        s.cookies.update(self.cookies)
        return s

    def _download_one(self, url, path, session):
        if path.exists() and path.stat().st_size > 10240:
            return ("skip", path.stat().st_size)
        last_err = None
        for attempt in range(3):
            try:
                with session.get(url, timeout=60, stream=True) as r:
                    r.raise_for_status()
                    tmp = path.with_suffix(path.suffix + ".part")
                    size = 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            if chunk:
                                f.write(chunk)
                                size += len(chunk)
                    if size < 1024:
                        raise RuntimeError(f"文件过小({size}B)，可能被风控")
                    tmp.rename(path)
                    return ("ok", size)
            except Exception as e:
                last_err = e
                time.sleep(2 + attempt * 2)
        log(f"  ❌ 下载失败 {path.name}: {last_err}")
        return ("fail", 0)

    def build_tasks(self):
        """把作品转成下载任务 [(类型, aweme_idx, url, 保存路径)]"""
        tasks = []
        works = list(self.works.values())
        works.sort(key=lambda w: w.get("create_time") or 0, reverse=True)
        # 时间段过滤
        works = [w for w in works
                 if (self.since_ts is None or (w.get("create_time") or 0) >= self.since_ts)
                 and (self.until_ts is None or (w.get("create_time") or 0) <= self.until_ts)]
        if self.limit:
            works = works[:self.limit]
        for idx, aw in enumerate(works, 1):
            ct = aw.get("create_time")
            date = datetime.fromtimestamp(ct).strftime("%Y%m%d") if ct else "unknown"
            title = safe_name(aw.get("desc"))
            base = f"{idx:03d}_{date}_{title}"

            video = aw.get("video") or {}
            images = aw.get("images") or []
            if images:
                # 图集作品优先按图片下载（抖音会给图集附带视频版，但用户要的是原图）
                for n, img in enumerate(images, 1):
                    urls = img.get("url_list") or []
                    if not urls:
                        continue
                    url = urls[-1]  # 最后一档通常是原图
                    path = self.images_dir / f"{base}_p{n}.jpg"
                    tasks.append({"type": "图片", "url": url, "path": path, "aweme": aw,
                                  "files": [path], "fallback": None})
            elif video.get("play_addr", {}).get("url_list"):
                # 从多档码率里选最高档（默认 play_addr 往往是中低档，看着比 App 糊）
                url = _best_video_url(video) or video["play_addr"]["url_list"][0]
                path = self.videos_dir / f"{base}.mp4"
                fallback_url = ((video.get("download_addr") or {}).get("url_list") or [None])[0]
                tasks.append({"type": "视频", "url": url, "path": path, "aweme": aw,
                              "files": [path], "fallback": fallback_url})
            else:
                tasks.append({"type": "其他", "url": None, "path": None, "aweme": aw,
                              "files": [], "fallback": None})
        return tasks

    def download_all(self, tasks):
        session = self._make_session()
        dl = [t for t in tasks if t["url"]]
        log(f"开始下载：{len(dl)} 个文件（视频 {sum(1 for t in dl if t['type']=='视频')}，"
            f"图片 {sum(1 for t in dl if t['type']=='图片')}）")

        done = 0
        # 并发压到 2：下载请求也带 cookie，宁可慢一点也别触发风控
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            for t in dl:
                futures[pool.submit(self._download_one, t["url"], t["path"], session)] = t
            for fut in as_completed(futures):
                t = futures[fut]
                status, size = fut.result()
                if status == "fail" and t.get("fallback") and t["fallback"] != t["url"]:
                    status, size = self._download_one(t["fallback"], t["path"], session)
                done += 1
                # Keep direct progress output compatible with legacy GBK consoles.
                mark = {"ok": "[OK]", "skip": "[SKIP]", "fail": "[FAIL]"}.get(status, status)
                print(f"  [{done}/{len(dl)}] {mark} {t['path'].name} "
                      f"({size/1024/1024:.2f} MB)", flush=True)
        return session

    # ---------- 结果输出 ----------

    def summarize_work(self, aw, files_rel):
        ct = aw.get("create_time")
        st = aw.get("statistics") or {}
        video = aw.get("video") or {}
        kind = "图集" if aw.get("images") else ("视频" if video.get("play_addr") else "其他")
        return {
            "aweme_id": aw.get("aweme_id"),
            "类型": kind,
            "置顶": bool(aw.get("is_top")),
            "标题": aw.get("desc") or "",
            "发布时间": datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M:%S") if ct else "",
            "点赞": st.get("digg_count"), "评论": st.get("comment_count"),
            "收藏": st.get("collect_count"), "分享": st.get("share_count"),
            "保存文件": " | ".join(files_rel),
        }

    def save_meta(self, tasks):
        # 去重元数据是内部数据，不放下载目录：存到 downloads/.meta/<用户>/
        # works.json，累积合并（按 aweme_id 去重，新记录覆盖同 id 旧记录）
        json_rows = []
        for t in tasks:
            files_rel = []
            for f in t["files"]:
                try:
                    files_rel.append(str(f.relative_to(self.user_dir)))
                except ValueError:
                    files_rel.append(str(f))
            json_rows.append(self.summarize_work(t["aweme"], files_rel))

        meta_dir = self.user_dir.parent / ".meta" / self.user_dir.name
        meta_dir.mkdir(parents=True, exist_ok=True)
        json_path = meta_dir / "works.json"
        old_rows = []
        if json_path.exists():
            try:
                old_rows = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                old_rows = []
        new_ids = {r.get("aweme_id") for r in json_rows if r.get("aweme_id")}
        merged = [r for r in old_rows if r.get("aweme_id") not in new_ids] + json_rows
        json_path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        return json_path

    # ---------- 主流程 ----------

    def run(self, query):
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.start_browser()
        try:
            profile = self.resolve_user(query)
            if profile:
                log("=" * 60)
                log(f"已定位用户: {profile.get('nickname')}  "
                    f"(@{profile.get('unique_id')})  "
                    f"粉丝 {profile.get('follower_count')}  作品 {profile.get('aweme_count')}")
                log("=" * 60)
                if profile.get("unique_id") and classify_input(query)[0] == "account":
                    if profile["unique_id"] != query:
                        log(f"⚠️ 注意: 定位到的抖音号是 @{profile['unique_id']}，与输入「{query}」不一致")

            safe_user = safe_name((profile or {}).get("unique_id") or
                                  (profile or {}).get("nickname") or "douyin_user", 50)
            self.user_dir = self.outdir / safe_user
            self.videos_dir = self.user_dir / "videos"
            self.images_dir = self.user_dir / "images"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            self.images_dir.mkdir(parents=True, exist_ok=True)
            self.target_sec = (profile or {}).get("sec_uid")

            # 跨次去重用的已下载作品 id（收集完成后统一比对剔除）。
            # 元数据存 .meta 隐藏目录；旧版本存在用户目录下的 works.json
            # 自动迁移过去，元数据.csv 不再生成
            meta_path = self.outdir / ".meta" / safe_user / "works.json"
            legacy_path = self.user_dir / "works.json"
            if not meta_path.exists() and legacy_path.exists():
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                legacy_path.replace(meta_path)
            if meta_path.exists():
                try:
                    for row in json.loads(meta_path.read_text(encoding="utf-8")):
                        if row.get("aweme_id"):
                            self.done_ids.add(row["aweme_id"])
                except Exception as e:
                    log(f"读取上次元数据失败（{type(e).__name__}），跳过跨次去重")

            self.collect_works()
            # 去重前快照全部作品：评论阶段要覆盖所有作品（含上次已下载过的，
            # 否则先跑过一次不带评论的下载、再勾评论重跑时会一个都不抓）
            vids_all = self._sorted_vid_list(self.works)

            # 跨次去重：上次保存过元数据的作品（按 aweme_id）本次不再下载，
            # 避免重跑时序号位移导致同名视频换个文件名重复下载
            if self.done_ids:
                dups = [k for k in self.works if k in self.done_ids]
                for k in dups:
                    del self.works[k]
                if dups:
                    log(f"跨次去重：{len(dups)} 个作品上次已下载过，本次自动跳过")

            if not self.works:
                if vids_all:
                    # 没有新作品要下载，但勾选了评论：仍对全部作品补抓评论
                    self._comments_phase(vids_all)
                else:
                    log("一个作品都没收集到，退出。建议检查 cookies 是否有效。")
                return

            tasks = self.build_tasks()
            self.download_all(tasks)
            self.save_meta(tasks)
            self._comments_phase(vids_all)

            n_video = sum(1 for t in tasks if t["type"] == "视频")
            n_img = sum(1 for t in tasks if t["type"] == "图片")
            n_other = sum(1 for t in tasks if t["type"] == "其他")
            n_gallery = len({t["aweme"]["aweme_id"] for t in tasks if t["type"] == "图片"})
            log("=" * 60)
            log("全部完成！")
            log(f"  作品总数: {len(tasks)}（视频 {n_video} / 图集作品 {n_gallery} / 其他 {n_other}，图片共 {n_img} 张）")
            log(f"  保存位置: {self.user_dir}")
            log("=" * 60)
        finally:
            self.stop_browser()


    # ---------- 单视频模式 ----------

    @staticmethod
    def parse_video_id(query):
        """从链接/分享文案里解析视频 id"""
        m = re.search(r"/(?:video|note)/(\d+)", query)
        if m:
            return m.group(1)
        m = re.search(r"https?://\S+", query)  # 分享文案里的短链
        if m:
            try:
                r = requests.head(m.group(0), allow_redirects=True, timeout=15,
                                  headers={"User-Agent": UA})
                m2 = re.search(r"/(?:video|note)/(\d+)", r.url)
                if m2:
                    return m2.group(1)
            except Exception:
                pass
        return None

    def run_single(self, query):
        vid = self.parse_video_id(query)
        if not vid:
            log("解析不出视频 id，请粘贴含 /video/ 或 /note/ 的链接或 App 分享文案")
            sys.exit(1)
        self.expect_vid = vid
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.start_browser()
        try:
            url = f"https://www.douyin.com/video/{vid}"
            log(f"打开视频页: {url}")
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + 15
            while time.time() < deadline and not self.works:
                time.sleep(0.5)
            self._check_captcha()
            if not self.works:
                pause("  没拿到视频数据，请在浏览器窗口检查（有弹窗手动处理）")
                deadline = time.time() + 10
                while time.time() < deadline and not self.works:
                    time.sleep(0.5)
            if not self.works:
                log("拿不到视频详情（可能 cookies 失效或视频已删除），退出")
                return

            aw = next(iter(self.works.values()))
            author = aw.get("author") or {}
            folder = safe_name(author.get("unique_id") or
                               author.get("nickname") or vid, 50)
            self.user_dir = self.outdir / f"单个视频_{folder}_{vid}"
            self.videos_dir = self.user_dir / "videos"
            self.images_dir = self.user_dir / "images"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            self.images_dir.mkdir(parents=True, exist_ok=True)

            tasks = self.build_tasks()
            self.download_all(tasks)
            self.save_meta(tasks)
            self._comments_phase([(vid,
                                   (aw.get("desc") or "").replace("\n", " "),
                                   aw.get("create_time") or 0)])
            log(f"完成，保存位置: {self.user_dir}")
        finally:
            self.stop_browser()

    # ---------- 关键词搜索模式 ----------

    def _warmup_home(self, seconds=3.0):
        """首页停留+自然滚动，模拟真人浏览轨迹，降低搜索触发风控概率。
        新会话第一步就直跳搜索页是最典型的机器人特征（run_search 旧实现），
        与 locate_user 里已验证有效的预热逻辑保持一致。"""
        log("首页预热（模拟真人浏览，降低搜索风控概率）...")
        try:
            self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                            timeout=60000)
        except Exception:
            return
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._page.mouse.move(random.randint(300, 1200), random.randint(200, 700))
            self._page.mouse.wheel(0, random.randint(400, 1200))
            time.sleep(random.uniform(0.8, 1.6))

    def _human_search(self, query):
        """真人式搜索路径：点搜索框 -> 输入关键词 -> 回车 -> 等结果页，
        替代旧的直跳 /search/关键词 URL（带 Referer 与会话轨迹，风控分更低）。"""
        box = None
        try:
            box = self._page.wait_for_selector(
                'input[placeholder*="搜索"], [contenteditable="true"]',
                timeout=8000)
        except Exception:
            box = None
        if box is None:
            # 搜索框找不到就退回直跳（总比卡死强）
            log("没定位到搜索框，退回直接打开搜索页")
            self._page.goto(f"https://www.douyin.com/search/{quote(query)}?type=video",
                            wait_until="domcontentloaded", timeout=60000)
            return
        try:
            box.click()
            time.sleep(random.uniform(0.5, 1.0))
            self._page.keyboard.type(query, delay=random.uniform(80, 180))
            time.sleep(random.uniform(0.6, 1.2))
            self._page.keyboard.press("Enter")
        except Exception:
            log("搜索框交互失败，退回直接打开搜索页")
            self._page.goto(f"https://www.douyin.com/search/{quote(query)}?type=video",
                            wait_until="domcontentloaded", timeout=60000)
            return
        time.sleep(random.uniform(3.0, 5.0))
        # 从综合结果页点「视频」标签（等价旧 URL 的 type=video）
        try:
            tab = self._page.wait_for_selector(
                'div:has(> span:text-is("视频")), a[href*="type=video"]',
                timeout=5000)
            if tab:
                tab.click()
                time.sleep(random.uniform(2.0, 3.0))
        except Exception:
            pass

    def run_search(self, query, sort="general"):
        """关键词搜索模式。

        工作方式：打开搜索结果页自动滚动收集，结果页卡片里混着直播/商品等
        推广卡，只收带可下载视频地址的条目。
        排序：general=综合排序(sort_type=0) / most_like=点赞最多(sort_type=2)。
        实测 URL 带 sort_type 时页面发出的搜索接口请求会原样回显该参数，
        说明排序参数被页面识别；sort_field 则会被忽略。
        重要：搜索页大概率触发平台安全验证（点选/滑块），脚本不会也无法
        自动绕过——检测到验证会停下等待，请在弹出的浏览器窗口里手动完成，
        脚本检测到验证解除后自动继续收集。
        """
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.start_browser()
        try:
            log("搜索模式：若浏览器里弹出安全验证，请手动完成，"
                "脚本检测到验证解除后会自动继续")
            # 先首页预热再走真人式搜索路径（点搜索框输入回车），
            # 新会话直跳搜索页是最典型的机器人特征，风控概率显著更高
            self._warmup_home()
            self._human_search(query)
            # 真人路径会落到精选页（type=general），且页面结构与
            # 视频搜索页不同、结果接口拦不稳；此处会话轨迹已建立，
            # 再归一到标准视频搜索 URL（排序参数页面会回显验证过）
            st = "2" if sort == "most_like" else "0"
            url = (f"https://www.douyin.com/search/{quote(query)}"
                   f"?type=video&sort_type={st}")
            log(f"归一到视频搜索页（排序：{'点赞最多' if sort == 'most_like' else '综合排序'}）")
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(random.uniform(3.0, 5.0))
            log(f"搜索就绪: {self._page.url}")

            self.user_dir = self.outdir / f"搜索_{safe_name(query, 30)}"
            self.videos_dir = self.user_dir / "videos"
            self.images_dir = self.user_dir / "images"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            self.images_dir.mkdir(parents=True, exist_ok=True)

            self._last_post_resp = time.time()
            last_n = -1
            stable = 0
            while True:
                if self.limit and len(self.works) >= self.limit:
                    log(f"已达 --limit {self.limit}，停止收集")
                    break
                if self._check_captcha():
                    # 验证刚解除：直接高频滚动会二次触发，先回首页预热冷却
                    self._warmup_home(seconds=10.0)
                    self._human_search(query)
                n = len(self.works)
                if n == last_n:
                    stable += 1
                    if stable >= 12:  # 连续约 36 秒无新数据：加载完了
                        break
                else:
                    stable = 0
                    # 搜索页比主页风控更严：每多收集 10 个歇 15~30 秒
                    if n // 10 > last_n // 10:
                        rest = random.uniform(15, 30)
                        log(f"  已收集 {n} 个，歇 {rest:.0f} 秒降低请求密度...")
                        time.sleep(rest)
                    last_n = n
                    log(f"已收集 {n} 个搜索结果...")
                # 真实鼠标滚轮滚动加载更多；搜索页降速（3~5 秒/次）
                self._page.mouse.move(random.randint(500, 1100), random.randint(300, 600))
                self._page.mouse.wheel(0, random.uniform(700, 1400))
                time.sleep(random.uniform(3.0, 5.0))

            if not self.works:
                log("搜索结果一条都没收到。可能：验证未完成 / cookies 失效 / 关键词无结果")
                return

            tasks = self.build_tasks()
            self.download_all(tasks)
            self.save_meta(tasks)
            self._comments_phase(self._sorted_vid_list(self.works))
            log("=" * 60)
            log(f"搜索完成！共 {len(tasks)} 个作品，保存位置: {self.user_dir}")
            log("=" * 60)
        finally:
            self.stop_browser()

    # ---------- 视频评论抓取模式 ----------

    def _scroll_comment_panel(self):
        """触发评论区加载更多。

        实测结论：直接改容器 scrollTop / window.scrollBy 都不会触发翻页，
        必须把真实鼠标滚轮打在评论列表元素上才会请求下一页。
        先用 JS 把评论列表滚进视野，再对列表中心发真实 wheel。
        """
        pos = self._page.evaluate("""() => {
            const list = document.querySelector('[data-e2e="comment-list"]');
            if (!list) return null;
            const sc = list.closest('[class*="route-scroll-container"]')
                       || document.scrollingElement;
            const r0 = list.getBoundingClientRect();
            if (r0.top > window.innerHeight * 0.6 || r0.bottom > window.innerHeight) {
                sc.scrollTop += Math.min(r0.top - 100, 900);
            }
            const r = list.getBoundingClientRect();
            return {x: r.x + r.width / 2,
                    y: Math.max(60, Math.min(r.y + r.height / 2, window.innerHeight - 60))};
        }""")
        if not pos:
            return False
        self._page.mouse.move(pos["x"], pos["y"])
        for _ in range(3):
            self._page.mouse.wheel(0, 800)
            time.sleep(random.uniform(0.6, 1.0))
        return True

    @staticmethod
    def summarize_comment(c):
        u = c.get("user") or {}
        ct = c.get("create_time")
        rid = c.get("reply_id")
        return {
            "cid": c.get("cid"),
            "类型": "回复" if rid and str(rid) != "0" else "评论",
            "评论内容": (c.get("text") or "").replace("\r", " ").replace("\n", " "),
            "昵称": u.get("nickname") or "",
            "抖音号": u.get("unique_id") or u.get("short_id") or "",
            "主页": (f"https://www.douyin.com/user/{u['sec_uid']}"
                     if u.get("sec_uid") else ""),
            "IP属地": c.get("ip_label") or "",
            "点赞": c.get("digg_count"),
            "回复数": c.get("reply_comment_total"),
            "评论时间": datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M:%S") if ct else "",
        }

    def _expand_reply_phase(self):
        """顶级评论抓完后，逐个点击「展开N条回复」加载子回复。

        回复接口 /comment/list/reply/ 的 URL 含顶级评论接口前缀，
        会被同一个响应拦截器收进队列，由 _drain_comments 统一解析去重，
        无需另写回复解析逻辑。
        注意：评论列表是虚拟渲染，只有滚进视野的条目挂在 DOM 上；顶级
        翻页结束后视口停在列表底部，此时 DOM 里只有末尾几条的展开控件。
        必须自底向上逐屏回滚，把沿途出现的展开控件逐个点掉，回滚到顶
        且滚不动了才结束。
        """
        log("开始展开子回复...")
        stall = 0
        last_top = -1
        for _ in range(800):   # 点击+滚屏总步数硬上限，防异常 DOM 死循环
            if self.limit and len(self.comments) >= self.limit:
                return
            pos = self._page.evaluate("""() => {
                const list = document.querySelector('[data-e2e="comment-list"]');
                if (!list) return null;
                const els = [...list.querySelectorAll('div,span')]
                    .filter(e => !e.children.length &&
                                 /^展开\\s*\\d*\\s*条?回复/.test(e.textContent.trim()));
                if (!els.length) return null;
                // 即时滚动：容器是 smooth 滚动时默认 scrollIntoView 会让
                // 元素持续移动，随后读到的坐标是过期的，点击会落空
                els[0].scrollIntoView({block: 'center', behavior: 'instant'});
                const r = els[0].getBoundingClientRect();
                return {x: r.x + r.width / 2, y: r.y + r.height / 2};
            }""")
            if not pos:
                # 当前视野没有展开控件：向上滚一屏，让上一段评论进 DOM
                sc = self._page.evaluate("""() => {
                    const list = document.querySelector('[data-e2e="comment-list"]');
                    if (!list) return null;
                    const box = list.closest('[class*="route-scroll-container"]')
                                || document.scrollingElement;
                    const r = list.getBoundingClientRect();
                    return {top: box.scrollTop,
                            x: r.x + r.width / 2,
                            y: Math.max(60, Math.min(r.y + r.height / 2,
                                                     window.innerHeight - 60))};
                }""")
                if not sc or sc["top"] <= 2 or sc["top"] == last_top:
                    log(f"子回复展开完成，共收集 {len(self.comments)} 条")
                    return
                last_top = sc["top"]
                self._page.mouse.move(sc["x"], sc["y"])
                self._page.mouse.wheel(0, -900)
                time.sleep(random.uniform(0.8, 1.2))
                continue
            before = len(self.comments)
            n0 = before
            try:
                self._page.mouse.move(pos["x"], pos["y"])
                self._page.mouse.click(pos["x"], pos["y"])
            except Exception as e:
                log(f"点击展开回复失败（{type(e).__name__}），跳过剩余回复")
                return
            # 等回复响应进来；还在持续增长就延长等待（大线程分多页拉取）
            deadline = time.time() + 3
            while time.time() < deadline:
                time.sleep(0.3)
                self._drain_comments()
                if len(self.comments) > n0:
                    n0 = len(self.comments)
                    deadline = time.time() + 2
            self._check_captcha()
            # 用点击前的快照判断本轮是否真有增长；n0 在循环里被刷新，
            # 拿它比较会恒为假，导致每点一次就误判失败、8 次后提前收工
            stall = 0 if len(self.comments) > before else stall + 1
            if stall >= 8:
                log("连续多次展开没有新回复响应，停止展开")
                return

    def _reply_sweep_phase(self):
        """展开后把评论列表再从头到尾滚一遍，补拉大回复线程的剩余回复。

        大线程（如几十条回复）展开时页面只自动拉前几页，剩余回复要等
        线程内容滚进视野才继续请求；用顶级评论的回复数合计作为目标数，
        缺口还在且滚动仍有增长时多滚几遍，最多 3 遍。
        """
        def _reply_gap():
            top_n = sum(r["回复数"] or 0 for r in self.comments if r["类型"] == "评论")
            got_n = sum(1 for r in self.comments if r["类型"] == "回复")
            return top_n - got_n

        for sweep in range(3):
            gap = _reply_gap()
            if gap <= 0:
                return
            log(f"回复缺口 {gap} 条，第 {sweep + 1}/3 遍滚动补拉...")
            n0 = len(self.comments)
            ok = self._page.evaluate("""() => {
                const list = document.querySelector('[data-e2e="comment-list"]');
                if (!list) return false;
                const box = list.closest('[class*="route-scroll-container"]')
                            || document.scrollingElement;
                box.scrollTop = 0;
                return true;
            }""")
            if not ok:
                return
            time.sleep(1.0)
            idle = 0
            last_top = -1
            for _ in range(200):
                self._drain_comments()
                vp = self._page.viewport_size
                self._page.mouse.move(vp["width"] / 2, vp["height"] / 2)
                self._page.mouse.wheel(0, 700)
                time.sleep(random.uniform(0.8, 1.1))
                self._drain_comments()
                top = self._page.evaluate("""() => {
                    const list = document.querySelector('[data-e2e="comment-list"]');
                    if (!list) return -1;
                    const box = list.closest('[class*="route-scroll-container"]')
                                || document.scrollingElement;
                    return box.scrollTop;
                }""")
                if _reply_gap() <= 0:
                    return
                if top == last_top or top <= 2:
                    idle += 1
                    if idle >= 3:
                        break   # 已滚到列表末尾
                else:
                    idle = 0
                last_top = top
            if len(self.comments) == n0:
                log("本轮滚动没有补到新回复，停止补拉")
                return
        log(f"补拉结束（回复缺口 {_reply_gap()} 条，多为平台已过滤内容）")

    def run_comments(self, query):
        """独立评论模式：只抓单个视频的评论，不下载作品"""
        vid = self.parse_video_id(query)
        if not vid:
            log("解析不出视频 id，请粘贴含 /video/ 或 /note/ 的链接或 App 分享文案")
            sys.exit(1)
        self.expect_vid = vid
        self.want_detail = False   # 只抓评论，别在回调里解析详情大响应
        self.only_comment = True   # 回调只处理评论接口（详见 _on_response 注释）
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.start_browser()
        try:
            self._comments_one_video(vid, self.outdir / "comments")
        finally:
            self.stop_browser()

    def _comments_one_video(self, vid, csv_dir, csv_base=None):
        """抓取单个视频的评论并导出 CSV（浏览器需已启动）。

        独立评论模式与集成在视频爬取里的评论阶段共用本方法。
        csv_base 为输出文件名主体（不含条数后缀），缺省用 评论_{vid}。
        返回抓到的评论条数；出错时抛异常由调用方决定是否继续。
        """
        # 每个视频独立的评论状态（集成模式下连续抓多个视频）
        self.comments = []
        self._comment_seen = set()
        self._pending_comment_resps = []
        self.comment_has_more = True
        csv_base = csv_base or f"评论_{vid}"

        url = f"https://www.douyin.com/video/{vid}"
        log(f"打开视频页抓取评论: {url}")
        self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        self._last_comment_resp = time.time()
        deadline = time.time() + 30
        # 评论区是懒加载且位于视频下方：需要先用页面级滚轮把评论区滚进
        # 视野，再对列表发真实滚轮，评论接口才会发出。页面组件挂载时机
        # 有波动，单次触发不可靠，改为每 5 秒重复触发直到有数据（实测验证）
        last_kick = 0.0
        while time.time() < deadline and not self.comments:
            time.sleep(0.5)
            self._drain_comments()
            if self.comments:
                break
            if time.time() - last_kick >= 5:
                vp = self._page.viewport_size
                self._page.mouse.move(vp["width"] / 2, vp["height"] / 2)
                self._page.mouse.wheel(0, 1200)
                time.sleep(1.2)
                if self._scroll_comment_panel():
                    log("已滚动到评论区并发送滚轮触发...")
                last_kick = time.time()
        self._check_captcha()
        if not self.comments:
            pause("  没等到评论数据，请在浏览器窗口确认状态（可能要点开评论区）")
            deadline = time.time() + 10
            while time.time() < deadline and not self.comments:
                time.sleep(0.5)
                self._drain_comments()
        if not self.comments:
            log("评论接口没有返回数据（cookies 失效 / 视频无评论 / 页面结构变化）")
            return 0

        # 边抓边写盘：新评论随时追加进临时 CSV，中途崩溃/停止不丢已抓数据；
        # comments 里已是压缩过的行 dict，直接写，不要再做二次转换
        csv_dir.mkdir(parents=True, exist_ok=True)
        tmp_csv = csv_dir / f"{csv_base}_进行中.csv"
        tmp_csv.unlink(missing_ok=True)   # 清掉上次可能残留的进行中文件
        written = 0

        def _flush_new():
            """把新增评论追加写入临时 CSV（表头在文件为空时写入）"""
            nonlocal written
            cap = self.limit if self.limit else None
            new = self.comments[written:cap]
            if not new:
                return
            with open(tmp_csv, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(new[0].keys()))
                if f.tell() == 0:
                    w.writeheader()
                w.writerows(new)
            written += len(new)

        _flush_new()   # 首批评论先落盘

        # 滚动加载更多
        last_n, stable = -1, 0
        while True:
            self._drain_comments()
            _flush_new()
            if self.limit and len(self.comments) >= self.limit:
                log(f"已达 --limit {self.limit}，停止收集")
                break
            if not self.comment_has_more and len(self.comments) == last_n:
                log("评论区已到末尾")
                break
            n = self._scroll_comment_panel()
            if n == 0:
                log("页面上找不到可滚动的评论容器，无法加载更多")
            time.sleep(random.uniform(1.5, 2.5))
            self._check_captcha()
            if len(self.comments) == last_n:
                stable += 1
                if stable >= 6:
                    log("连续多轮滚动没有新评论，停止收集")
                    break
            else:
                stable = 0
                # 每多收集 50 条评论随机歇一会，降低请求密度防风控
                if last_n >= 0 and len(self.comments) // 50 > last_n // 50:
                    rest = random.uniform(4, 8)
                    log(f"  已收集 {len(self.comments)} 条，歇 {rest:.0f} 秒降低请求密度...")
                    time.sleep(rest)
                log(f"  已收集 {len(self.comments)} 条评论")
            last_n = len(self.comments)

        # 顶级评论抓完后展开子回复（回复响应复用同一拦截器，cid 去重后并入）
        if not (self.limit and len(self.comments) >= self.limit):
            self._expand_reply_phase()
            _flush_new()
            self._reply_sweep_phase()
            _flush_new()

        if self.limit:
            self.comments = self.comments[:self.limit]
        rows = sorted(self.comments, key=lambda r: (r["点赞"] or 0), reverse=True)
        csv_path = csv_dir / f"{csv_base}_{len(rows)}条.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        tmp_csv.unlink(missing_ok=True)
        log(f"评论抓取完成: {len(rows)} 条（按点赞数排序）→ {csv_path.name}")
        return len(rows)

    def _sorted_vid_list(self, works):
        """作品 dict -> [(vid, 标题, 发布时间戳)]，按发布时间倒序（与下载顺序一致）"""
        ws = sorted(works.values(),
                    key=lambda w: w.get("create_time") or 0, reverse=True)
        return [(w.get("aweme_id"), (w.get("desc") or "").replace("\n", " "),
                 w.get("create_time") or 0)
                for w in ws if w.get("aweme_id")]

    def _comments_phase(self, vid_titles):
        """集成模式评论采集：对本次涉及的作品逐个抓评论（已有结果的跳过）。

        在视频/图片下载完成后调用；only_comment 切换回调只处理评论接口，
        作品收集此时已结束，互不干扰。
        """
        if not self.with_comments or not vid_titles:
            return
        log("=" * 60)
        log(f"进入评论采集阶段：共 {len(vid_titles)} 个作品"
            "（已有评论 CSV 的作品自动跳过）")
        self.only_comment = True
        self.want_detail = False
        self.limit = None          # 评论不沿用作品数量限制，全量抓取
        csv_dir = self.user_dir / "comments"
        done = skip = fail = 0
        seen_bases = {}
        for i, (vid, title, ts) in enumerate(vid_titles, 1):
            if not vid:
                continue
            # 评论文件名与视频文件同款风格（日期_标题），方便人工对应；
            # 同日同名作品（如每日固定栏目）加视频 id 尾缀避免重名
            date = datetime.fromtimestamp(ts).strftime("%Y%m%d") if ts else "unknown"
            csv_base = f"评论_{date}_{safe_name(title)}"
            if csv_base in seen_bases:
                csv_base = f"{csv_base}_{vid[-6:]}"
            seen_bases[csv_base] = vid
            if list(csv_dir.glob(f"{csv_base}_*条.csv")):
                skip += 1
                continue
            log(f"[{i}/{len(vid_titles)}] 抓取评论: {title[:36]}")
            try:
                self._comments_one_video(vid, csv_dir, csv_base)
                done += 1
            except Exception as e:
                fail += 1
                log(f"  ❌ 评论抓取失败（{type(e).__name__}: {e}），跳过该作品继续")
            if i < len(vid_titles):
                rest = random.uniform(3, 6)
                log(f"  歇 {rest:.0f} 秒降低请求密度...")
                time.sleep(rest)
        log(f"评论采集阶段结束：本次抓取 {done} 个，已有跳过 {skip} 个，失败 {fail} 个")
        log(f"  保存位置: {csv_dir}")


# ---------------------------------------------------------------- 命令行入口

def _selftest():
    """快速自检：依赖 / cookies / 网络 / 页面结构是否兼容（不跑完整任务）。

    抖音改版后先跑这个，能快速分辨「环境坏了」还是「页面结构变了」。
    """
    ok = True

    def report(name, good, detail=""):
        nonlocal ok
        ok = ok and good
        log(f"{'✅' if good else '❌'} {name}{('：' + detail) if detail else ''}")

    # 1. Python 依赖
    try:
        import playwright.sync_api  # noqa: F401
        import requests  # noqa: F401
        report("Python 依赖（playwright / requests）", True)
    except Exception as e:
        report("Python 依赖", False, f"{e}（重跑「打开爬虫界面.bat」自动安装）")
        return ok

    # 2. cookies
    ck = BASE_DIR / "cookies.txt"
    good = ck.exists() and ck.stat().st_size > 50
    report("cookies 文件", good,
           str(ck) if good else f"缺失（{ck}），先运行 douyin_login.py 登录")
    if not good:
        return ok

    # 3. 浏览器内核 + 网络 + 页面结构探测
    raw = ck.read_text(encoding="utf-8").strip()
    cookie_str = max(raw.splitlines(), key=len)
    cookies = [{"name": k.strip(), "value": v.strip(), "domain": ".douyin.com", "path": "/"}
               for k, v in (p.split("=", 1) for p in cookie_str.split(";") if "=" in p)]
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(user_agent=UA)
            ctx.add_cookies(cookies)
            pg = ctx.new_page()
            pg.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            report("抖音可达（浏览器内核正常）", True)
            # 结构探测：推荐流页面应内嵌作品数据（aweme 标记），改版后这些标记会消失
            pg.goto("https://www.douyin.com/?recommend=1",
                    wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            html = pg.content()
            struct = any(m in html for m in ('"aweme', "sec_uid", '"desc":'))
            report("页面数据结构探测", struct,
                   "" if struct else "页面里找不到作品数据标记，抖音可能已改版，爬虫功能大概率受影响")
            b.close()
    except Exception as e:
        report("浏览器 / 网络", False, str(e)[:100])
    return ok


def main():
    parser = argparse.ArgumentParser(description="抖音作品爬虫")
    parser.add_argument("--account", help="抖音号（个人主页显示的那个号）")
    parser.add_argument("--url", help="或：用户主页链接（含 sec_uid）")
    parser.add_argument("--video", help="或：单个视频链接/分享文案")
    parser.add_argument("--search", help="或：关键词搜索（弹验证时需在浏览器窗口手动完成）")
    parser.add_argument("--sort", choices=["general", "most_like"], default="general",
                        help="搜索结果排序：general=综合排序（默认），most_like=点赞最多")
    parser.add_argument("--comments", help="或：抓取该视频的评论，导出 CSV（视频链接/分享文案）")
    parser.add_argument("--cookie", help="整串 cookie 字符串")
    parser.add_argument("--cookie-file", help="或：cookie 文件路径（文件里粘贴整串 cookie）")
    parser.add_argument("--limit", type=int, help="只爬前 N 个作品")
    parser.add_argument("--since", help="只爬该日期（含）之后的作品，格式 YYYY-MM-DD")
    parser.add_argument("--until", help="只爬该日期（含）之前的作品，格式 YYYY-MM-DD")
    parser.add_argument("--outdir", default=str(BASE_DIR / "downloads"),
                        help="输出目录（默认 脚本目录/downloads）")
    parser.add_argument("--headless", action="store_true", help="无头模式（不弹浏览器窗口，出验证时无法手动处理，不建议）")
    parser.add_argument("--with-comments", action="store_true",
                        help="下载作品后逐个打开视频页抓取评论（导出 CSV），"
                             "仅对 --account/--url/--video/--search 生效")
    parser.add_argument("--selftest", action="store_true",
                        help="快速自检：依赖/cookies/网络/页面结构是否兼容（不跑任务）")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(0 if _selftest() else 1)

    for date_arg in (args.since, args.until):
        if date_arg:
            try:
                datetime.strptime(date_arg, "%Y-%m-%d")
            except ValueError:
                log(f"日期格式错误: {date_arg}，应为 YYYY-MM-DD")
                sys.exit(1)

    query = args.account or args.url
    if not query and not args.video and not args.comments and not args.search:
        if not INTERACTIVE:
            log("未提供 --account/--url/--video/--search/--comments（无人值守模式无法交互输入），退出")
            sys.exit(1)
        print("=" * 60)
        print("抖音作品爬虫")
        print("=" * 60)
        query = input("请输入抖音号/主页链接/视频链接: ").strip()
        if not query:
            print("未输入，退出")
            sys.exit(1)

    # 未显式提供 cookie 时自动找脚本/exe 同目录下的 cookies.txt（与录制器一致）
    cookie_file = args.cookie_file
    if not cookie_file and not args.cookie:
        default_ck = BASE_DIR / "cookies.txt"
        if default_ck.exists():
            cookie_file = str(default_ck)

    cookies = load_cookies(args.cookie, cookie_file)
    if not cookies:
        print("cookies 解析结果为空，退出")
        sys.exit(1)
    log(f"cookies 解析出 {len(cookies)} 个字段")

    spider = DouyinSpider(cookies, args.outdir, headless=args.headless,
                          limit=args.limit, since=args.since, until=args.until,
                          with_comments=args.with_comments)
    try:
        if args.search:
            spider.run_search(args.search, sort=args.sort)
        elif args.video:
            spider.run_single(args.video)
        elif args.comments:
            spider.run_comments(args.comments)
        else:
            if not query:
                log("请提供 --account 或 --url")
                sys.exit(1)
            spider.run(query)
    except KeyboardInterrupt:
        print("\n手动中断。已收集的数据不完整，可重新运行（已下载的文件会自动跳过）")


if __name__ == "__main__":
    main()
