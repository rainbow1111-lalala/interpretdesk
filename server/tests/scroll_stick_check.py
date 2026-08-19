"""右栏贴底跟随验证：往上翻看之前的话时，新卡片不许把视线拽回底部。

实战问题：拟稿刷新快，想回看上一版建议，屏幕自己跳到最新那条。
桩掉 /api/draft 用固定回答，跑得快也不烧 token。需要先 `cd web && npm run build`，
并让后端在 8787 上跑着。用系统 python 跑（playwright 装在系统解释器上）：

    python3 -m server.tests.scroll_stick_check
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('MI_PORT', '8787')}"

ANSWER = ("Well, that's a fair point, and we can look at it. " * 12
          + "\n---ZH---\n" + "这一点说得在理，我们可以看看。" * 12)


def sse(text: str) -> str:
    import json
    body = "".join(f"data: {json.dumps({'text': t}, ensure_ascii=False)}\n\n"
                   for t in [text[i:i + 60] for i in range(0, len(text), 60)])
    return body + "data: [DONE]\n\n"


def main() -> int:
    ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 700})
        page.route("**/api/draft", lambda route: route.fulfill(
            status=200, headers={"content-type": "text/event-stream"}, body=sse(ANSWER)))
        page.goto(BASE)
        page.wait_for_selector(".thread")

        box = ".thread"
        # 先攒几张卡片，把右栏撑到能滚动
        for i in range(4):
            page.fill(".composer textarea", f"第 {i + 1} 个问题，帮我拟一段。")
            page.keyboard.press("Enter")
            page.wait_for_timeout(700)
        page.wait_for_timeout(500)

        scrollable = page.eval_on_selector(box, "b => b.scrollHeight > b.clientHeight + 60")
        print("  右栏已可滚动 →", scrollable)
        ok &= bool(scrollable)

        # 停在底部时应当跟随
        page.eval_on_selector(box, "b => { b.scrollTop = b.scrollHeight; }")
        page.fill(".composer textarea", "底部时再问一个。")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1200)
        at_bottom = page.eval_on_selector(
            box, "b => b.scrollHeight - b.scrollTop - b.clientHeight < 60")
        print("  停在底部时新卡片仍自动跟到底 →", at_bottom)
        ok &= bool(at_bottom)

        # 往上翻之后不许被拽走
        page.eval_on_selector(box, "b => { b.scrollTop = 0; }")
        page.wait_for_timeout(300)
        before = page.eval_on_selector(box, "b => b.scrollTop")
        page.fill(".composer textarea", "翻上去之后再问一个。")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        after = page.eval_on_selector(box, "b => b.scrollTop")
        stayed = abs(after - before) < 10
        print(f"  往上翻后新卡片没把视线拽走 → {stayed}（scrollTop {before} → {after}）")
        ok &= stayed

        # 翻回底部应恢复跟随
        page.eval_on_selector(box, "b => { b.scrollTop = b.scrollHeight; }")
        page.wait_for_timeout(300)
        page.fill(".composer textarea", "翻回底部再问一个。")
        page.keyboard.press("Enter")
        page.wait_for_timeout(1500)
        resumed = page.eval_on_selector(
            box, "b => b.scrollHeight - b.scrollTop - b.clientHeight < 60")
        print("  翻回底部后恢复自动跟随 →", resumed)
        ok &= bool(resumed)

        browser.close()
    print("结果：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


sys.exit(main())
