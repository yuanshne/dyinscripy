#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抖音登录助手 —— 弹出浏览器扫码登录，自动把 Cookie 保存到 cookies.txt

给不懂技术的使用者：
  运行后会弹出浏览器并打开抖音网页，用手机抖音 App 扫码（或短信）登录；
  登录成功后脚本自动检测到并保存 Cookie，之后爬虫/直播录制就能直接用，
  全程不需要手动复制粘贴任何东西。

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


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def cookies_ready(names):
    """已登录（有 sessionid）且带基础风控字段（msToken / ttwid）才算就绪"""
    has_login = bool(names & {"sessionid", "sessionid_ss"})
    has_anti = bool(names & {"msToken", "ttwid"})
    return has_login, has_anti


def main():
    ap = argparse.ArgumentParser(description="抖音登录助手：扫码登录并自动保存 Cookie")
    ap.add_argument("--out", default=None, help="Cookie 保存路径（默认：脚本目录 cookies.txt）")
    ap.add_argument("--timeout", type=int, default=300, help="等待登录的最长秒数（默认 300）")
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else Path(__file__).parent / "cookies.txt"
    out_path = out_path.resolve()

    log("正在打开浏览器，请稍候...")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 860},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
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
                if not browser.is_connected():
                    break
                page.evaluate("1")   # 探测页面还活着
            except Exception:
                break
            time.sleep(2)

        if not cookie_list:
            log("未能获取 Cookie（超时未登录，或浏览器被提前关闭），没有改动任何文件。")
            try:
                browser.close()
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
            browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
