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


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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
                 since=None, until=None):
        self.cookies = cookies
        self.outdir = Path(outdir)
        self.headless = headless
        self.limit = limit
        # 时间段过滤（"YYYY-MM-DD"，None 表示不限）
        self.since_ts = self._date_ts(since) if since else None
        self.until_ts = self._date_ts(until) + 86399 if until else None

        self.works = {}          # aweme_id -> aweme dict
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
                data = resp.json()
                aweme_list = data.get("aweme_list") or []
                new_cnt = 0
                for aw in aweme_list:
                    awid = aw.get("aweme_id")
                    if awid and awid not in self.works:
                        self.works[awid] = aw
                        new_cnt += 1
                if data.get("has_more") in (0, False, "0", None):
                    if new_cnt == 0:
                        self.has_more = False
                self._last_post_resp = time.time()
            elif API_DETAIL in url and self.want_detail:
                data = resp.json()
                aw = data.get("aweme_detail") or {}
                # 页面可能自动跳到推荐视频，只收目标视频，避免下错
                if (aw.get("aweme_id")
                        and (self.expect_vid is None
                             or aw["aweme_id"] == self.expect_vid)):
                    self.works[aw["aweme_id"]] = aw
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
                        self.works[awid] = aw
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
                cid = c.get("cid")
                if cid and cid not in self._comment_seen:
                    self._comment_seen.add(cid)
                    # 采集时就压缩成 CSV 所需字段：原始评论 dict 含用户资料等，
                    # 每条几 KB，几万条评论会累积上百 MB 内存
                    self.comments.append(self.summarize_comment(c))
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
        for i, sec in enumerate(candidates, 1):
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
                log("  ✅ 抖音号精确匹配")
                return self.profile
            if nick == account:
                log("  ✅ 昵称匹配")
                return self.profile

        if fallback:
            log(f"⚠️ 没有精确匹配到抖音号「{account}」，将使用搜索排名第一的用户: "
                f"@{fallback.get('unique_id')}（{fallback.get('nickname')}）")
            pause("  如果这不是你要的用户，请停止任务；确认无误就继续")
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
            self._page.screenshot(path=str(self.outdir / "验证页面截图.png"))
        except Exception:
            pass
        try:
            input("  完成后回到这里按回车继续...")
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
                        return
                except Exception:
                    return
            log("⚠️ 验证长时间未解除，继续尝试（数据可能不全）")

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
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for t in dl:
                futures[pool.submit(self._download_one, t["url"], t["path"], session)] = t
            for fut in as_completed(futures):
                t = futures[fut]
                status, size = fut.result()
                if status == "fail" and t.get("fallback") and t["fallback"] != t["url"]:
                    status, size = self._download_one(t["fallback"], t["path"], session)
                done += 1
                mark = {"ok": "✓", "skip": "已存在", "fail": "✗"}.get(status, status)
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
        rows, json_rows = [], []
        for t in tasks:
            files_rel = []
            for f in t["files"]:
                try:
                    files_rel.append(str(f.relative_to(self.user_dir)))
                except ValueError:
                    files_rel.append(str(f))
            row = self.summarize_work(t["aweme"], files_rel)
            rows.append(row)
            json_rows.append(row)

        csv_path = self.user_dir / "元数据.csv"
        if rows:
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
        (self.user_dir / "works.json").write_text(
            json.dumps(json_rows, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"元数据已保存: {csv_path}")
        return csv_path

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

            self.collect_works()
            if not self.works:
                log("一个作品都没收集到，退出。建议检查 cookies 是否有效。")
                return

            tasks = self.build_tasks()
            self.download_all(tasks)
            self.save_meta(tasks)

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
            log(f"完成，保存位置: {self.user_dir}")
        finally:
            self.stop_browser()

    # ---------- 关键词搜索模式 ----------

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
            # 综合排序=0，点赞最多=2（页面请求回显验证过）
            st = "2" if sort == "most_like" else "0"
            sort_name = "点赞最多" if sort == "most_like" else "综合排序"
            url = (f"https://www.douyin.com/search/{quote(query)}"
                   f"?type=video&sort_type={st}")
            log(f"打开搜索页（排序：{sort_name}）: {url}")
            log("搜索模式：若浏览器里弹出安全验证，请手动完成，"
                "脚本检测到验证解除后会自动继续")
            self._page.goto(url, wait_until="domcontentloaded", timeout=60000)

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
                self._check_captcha()
                n = len(self.works)
                if n == last_n:
                    stable += 1
                    if stable >= 8:  # 连续约 16 秒无新数据：加载完了
                        break
                else:
                    stable = 0
                    last_n = n
                    log(f"已收集 {n} 个搜索结果...")
                # 真实鼠标滚轮滚动加载更多（与探测脚本同款，实测有效）
                self._page.mouse.move(800, 450)
                self._page.mouse.wheel(0, random.uniform(900, 1600))
                time.sleep(random.uniform(1.5, 2.5))

            if not self.works:
                log("搜索结果一条都没收到。可能：验证未完成 / cookies 失效 / 关键词无结果")
                return

            tasks = self.build_tasks()
            self.download_all(tasks)
            self.save_meta(tasks)
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
        return {
            "cid": c.get("cid"),
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

    def run_comments(self, query):
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
                log("评论接口没有返回数据（cookies 失效 / 视频无评论 / 页面结构变化），退出")
                return

            # 边抓边写盘：新评论随时追加进临时 CSV，中途崩溃/停止不丢已抓数据；
            # comments 里已是压缩过的行 dict，直接写，不要再做二次转换
            csv_dir = self.outdir / "comments"
            csv_dir.mkdir(exist_ok=True)
            tmp_csv = csv_dir / f"评论_{vid}_进行中.csv"
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
                    log(f"  已收集 {len(self.comments)} 条评论")
                last_n = len(self.comments)

            if self.limit:
                self.comments = self.comments[:self.limit]
            rows = sorted(self.comments, key=lambda r: (r["点赞"] or 0), reverse=True)
            csv_path = csv_dir / f"评论_{vid}_{len(rows)}条.csv"
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            tmp_csv.unlink(missing_ok=True)
            log("=" * 60)
            log(f"评论抓取完成: 共 {len(rows)} 条（按点赞数排序）")
            log(f"  保存位置: {csv_path}")
            log("=" * 60)
        finally:
            self.stop_browser()


# ---------------------------------------------------------------- 命令行入口

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
    parser.add_argument("--outdir", default=str(Path(__file__).parent / "downloads"),
                        help="输出目录（默认 脚本目录/downloads）")
    parser.add_argument("--headless", action="store_true", help="无头模式（不弹浏览器窗口，出验证时无法手动处理，不建议）")
    args = parser.parse_args()

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

    cookies = load_cookies(args.cookie, args.cookie_file)
    if not cookies:
        print("cookies 解析结果为空，退出")
        sys.exit(1)
    log(f"cookies 解析出 {len(cookies)} 个字段")

    spider = DouyinSpider(cookies, args.outdir, headless=args.headless,
                          limit=args.limit, since=args.since, until=args.until)
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
