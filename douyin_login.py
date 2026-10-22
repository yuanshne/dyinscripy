#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抖音登录助手 —— 弹出浏览器扫码登录，自动把 Cookie 保存到 cookies.txt

给不懂技术的使用者：
  运行后会弹出浏览器并打开抖音网页，用手机抖音 App 扫码（或短信）登录；
  登录成功后脚本自动检测到并保存 Cookie，之后爬虫/直播录制就能直接用，
  全程不需要手动复制粘贴任何东西。

登录状态会持久化保存在本目录的 browser_profile 里：扫一次码后，以后再运行
本脚本会直接复用已登录状态（并顺带刷新 cookies.txt），不用重复扫码。

用法：
  python douyin_login.py                  # 保存到脚本目录的 cookies.txt
  python douyin_login.py --out 我的.txt    # 保存到指定文件
  python douyin_login.py --timeout 600    # 最多等 10 分钟（默认 5 分钟）

网页界面里的「一键登录抖音」按钮就是调用本脚本。
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

def wait_enter():
    """双击运行时停一下让用户看到结果；被脚本/网页调用（无键盘输入）时跳过"""
    try:
        input("按回车键关闭...")
    except (EOFError, OSError):
        pass


try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("缺少运行依赖：playwright")
    print("请双击「打开爬虫界面.bat」，它会自动帮你安装；")
    print("或手动执行: pip install playwright && playwright install chromium")
    wait_enter()
    sys.exit(1)

# 与爬虫/录制器保持一致，降低风控概率
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# exe 模式下 __file__ 指向 _internal，日志/配置目录要对齐 exe 同级（与其他脚本一致）
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

# 持久化浏览器配置目录：登录态存在这里，扫一次码后长期复用
PROFILE_DIR = BASE_DIR / "browser_profile"

_LOG_FILE = BASE_DIR / "logs" / f"{datetime.now():%Y%m%d}.log"


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"), flush=True)
    try:
        _LOG_FILE.parent.mkdir(exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def cookies_ready(names):
    """已登录（有 sessionid）且带基础风控字段（msToken / ttwid）才算就绪"""
    has_login = bool(names & {"sessionid", "sessionid_ss"})
    has_anti = bool(names & {"msToken", "ttwid"})
    return has_login, has_anti


def seed_profile(ctx, cookie_file):
    """首次启用持久化配置时，把已有 cookies.txt 导入 profile，免去重新扫码。

    旧版登录助手生成的 cookies.txt 往往还没过期，直接导入可省一次扫码；
    导入失败不报错，退回正常扫码流程。
    """
    try:
        text = cookie_file.read_text(encoding="utf-8")
    except OSError:
        return 0
    cookies = []
    for chunk in text.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#") or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        if not name.strip() or not value.strip():
            continue
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".douyin.com",
            "path": "/",
            # 不写过期时间会变成会话 Cookie（关浏览器即丢），这里给 30 天
            "expires": time.time() + 30 * 86400,
            "secure": True,
        })
    if not cookies:
        return 0
    try:
        ctx.add_cookies(cookies)
        return len(cookies)
    except Exception:
        return 0


def main():
    ap = argparse.ArgumentParser(description="抖音登录助手：扫码登录并自动保存 Cookie")
    ap.add_argument("--out", default=None, help="Cookie 保存路径（默认：脚本目录 cookies.txt）")
    ap.add_argument("--timeout", type=int, default=300, help="等待登录的最长秒数（默认 300）")
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else BASE_DIR / "cookies.txt"
    out_path = out_path.resolve()

    first_run = not PROFILE_DIR.exists()
    log("正在打开浏览器，请稍候...")
    with sync_playwright() as pw:
        try:
            # 持久化配置：登录态写在 browser_profile 里，下次直接复用
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                user_agent=UA,
                viewport={"width": 1280, "height": 860},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
        except Exception as e:
            log(f"浏览器启动失败：{e}")
            log("如果提示配置目录被占用，请先关掉上次遗留的登录浏览器窗口再重试。")
            wait_enter()
            sys.exit(1)
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        if first_run:
            # 旧版生成的 cookies.txt 若仍有效，直接导入，省掉一次扫码
            n = seed_profile(ctx, BASE_DIR / "cookies.txt")
            if n:
                log(f"已导入现有 cookies.txt 的 {n} 个字段到本地登录配置，无需重新扫码")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                  timeout=60000)

        log("请在浏览器窗口里登录抖音（扫码 / 短信验证码都可以）。")
        wait_hint = (f"{args.timeout // 60} 分钟" if args.timeout >= 60
                     else f"{args.timeout} 秒")
        log(f"登录成功后我会自动保存，最多等你 {wait_hint}...")

        deadline = time.time() + args.timeout
        cookie_list = None
        while time.time() < deadline:
            names = {c["name"] for c in ctx.cookies("https://www.douyin.com")}
            has_login, has_anti = cookies_ready(names)
            if has_login:
                if not has_anti:
                    # 刚登录成功，msToken/ttwid 可能还没发下来，再等一会儿
                    time.sleep(3)
                    continue
                # 稳定一下再取最终值
                time.sleep(2)
                cookie_list = ctx.cookies("https://www.douyin.com")
                break
            # 浏览器被用户直接关掉就视为放弃
            try:
                if not ctx.pages:
                    break
                page.evaluate("1")   # 探测页面还活着
            except Exception:
                break
            time.sleep(2)

        if not cookie_list:
            log("未能获取 Cookie（超时未登录，或浏览器被提前关闭），没有改动任何文件。")
            try:
                ctx.close()
            except Exception:
                pass
            wait_enter()
            sys.exit(1)

        # 保存：先备份旧文件，再写入新 Cookie
        if out_path.exists():
            bak = out_path.with_name(out_path.name + ".bak")
            try:
                bak.write_bytes(out_path.read_bytes())
                log(f"已把旧的 {out_path.name} 备份为 {bak.name}")
            except OSError:
                pass
        pairs = "; ".join(f"{c['name']}={c['value']}" for c in cookie_list)
        out_path.write_text(
            f"# 由登录助手自动生成（{datetime.now().strftime('%Y-%m-%d %H:%M')}）\n"
            f"{pairs}\n",
            encoding="utf-8",
        )
        log(f"Cookie 已保存到: {out_path}")
        log("搞定！回到网页界面就能正常使用了（cookie 一栏留空即可）。")

        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
