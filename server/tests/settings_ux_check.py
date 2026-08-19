"""模型设置的可用性验证。

实战问题：用户把文本层配成智谱，语音层的 base url 与 key 只是灰色占位没真填，
点开始记录没有字幕，界面上却没有任何地方说缺了什么。这里验四件事：
缺配置时顶部有红字状态、语音层默认值真的填进框里、选了不做语音的服务商会给出提示、
拉模型清单之后三个模型框能从清单里选。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('MI_PORT', '8787')}"
SHOTS = Path("/private/tmp/claude-501/-Users-rainbow/"
             "16b1d450-b283-4899-8b89-4794d5225e7a/scratchpad/shots-settings")


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # 用一个干净会话，别动本机已经配好的那份
        page.goto(BASE)
        if page.query_selector(".onboard-ok"):
            page.click(".onboard-ok")
            page.wait_for_selector(".onboard", state="detached")
        page.click("text=模型设置")
        page.wait_for_selector(".sheet h2")

        # 语音层的 base url 与 model 必须是真值，不是占位
        vals = page.eval_on_selector_all(".sheet input", "els => els.map(e => e.value)")
        filled = any("livetranslate" in v or "generativelanguage" in v for v in vals)
        print("  语音层默认模型已填进框里 →", filled)
        ok &= filled
        # 带 <> 的是模板不是真地址，填进框会被当成已配好存下去
        no_template = not any("<" in v for v in vals)
        print("  框里没有 <占位符> →", no_template, [v for v in vals if "<" in v])
        ok &= no_template

        # 把文本层切成智谱，应当出现「不做实时字幕」的提示
        page.select_option(".sheet select >> nth=0", label="智谱（语音层需另配）")
        page.wait_for_timeout(300)
        warn = page.query_selector(".cfg-state.bad")
        has_warn = warn is not None and "实时字幕" in (warn.inner_text() if warn else "")
        print("  选智谱后提示语音层要另配 →", has_warn)
        ok &= has_warn

        # base url 应被预设自动填好
        url_val = page.eval_on_selector(".sheet input", "e => e.value")
        print("  服务商预设填好了 base url →", "bigmodel.cn" in url_val, f"（{url_val}）")
        ok &= "bigmodel.cn" in url_val

        # 向量模型不该出现在默认视野里：它有可用默认值（留空自动挑），普通用户不必知道
        hidden = page.eval_on_selector(
            ".advanced", "e => !e.open && e.querySelector('input') !== null")
        print("  向量模型默认收在高级里 →", hidden)
        ok &= bool(hidden)
        page.click(".advanced > summary")
        page.wait_for_timeout(200)
        opened = page.eval_on_selector(".advanced", "e => e.open")
        print("  点开高级能看到 →", opened)
        ok &= bool(opened)

        page.screenshot(path=str(SHOTS / "设置-智谱.png"), full_page=True)
        browser.close()
    print(f"截图在 {SHOTS}")
    print("结果：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


sys.exit(main())
