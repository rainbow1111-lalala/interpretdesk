"""界面验证：把真实事件序列喂进已构建的前端，截图确认渲染。

不依赖真实屏幕共享，桩掉 getDisplayMedia 与 /ws/live，只验证界面与音频管线的接线。
需要先 `cd web && npm run build`，并让后端在 8787 上跑着。用系统 python 运行（playwright
装在系统解释器上，不进项目依赖）：

    python3 -m server.tests.ui_check
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
SHOTS = Path("/private/tmp/claude-501/-Users-rainbow/"
             "e14a2e43-5cb9-4694-842f-4e8f380bab4f/scratchpad/shots")

# 用真实回放里收到过的内容，不编造
TURNS = [
    {"type": "turn", "turnId": 0, "srcLang": "en", "dstLang": "zh-CN", "echo": False,
     "ts": 1786_000_000,
     "pairs": [
         {"src": "Good morning.", "dst": "早上好。"},
         {"src": "Before we turn to the indemnity clause, I want to confirm whether your "
                 "client accepts the revised representations and warranties in section 4.2.",
          "dst": "在我们讨论赔偿条款之前，我想确认一下您的客户是否接受修改后的陈述与保证，"
                 "也就是第 4.2 条。"},
     ]},
    {"type": "turn", "turnId": 1, "srcLang": "en", "dstLang": "zh-CN", "echo": False,
     "ts": 1786_000_019,
     "pairs": [
         {"src": "If not, we would need to revisit the escrow amount.",
          "dst": "如果不接受，我们就需要重新讨论第三方托管金额。"},
     ]},
    {"type": "turn", "turnId": 2, "srcLang": "zh", "dstLang": "zh-CN", "echo": True,
     "ts": 1786_000_034,
     "pairs": [
         {"src": "我们这边的意见是，第四条第二款的保证条款范围过宽，建议限定在卖方明知的范围内。",
          "dst": "我们这边的意见是，第四条第二款的保证条款范围过宽，建议限定在卖方明知的范围内。"},
     ]},
]

LIVE_PARTIAL = {
    "type": "partial", "turnId": 3, "srcLang": "en", "dstLang": "zh-CN", "echo": False,
    "src": "On the indemnity cap, our instruction is that fifteen per cent is the ceiling,",
    "dst": "关于赔偿上限，我们得到的指示是百分之十五就是上限，",
}

# 桩掉屏幕采集：给一条真实存在的音轨，让 AudioWorklet 与 WebSocket 的接线照常跑起来
FAKE_CAPTURE = """
navigator.mediaDevices.getDisplayMedia = async () => {
  const ctx = new AudioContext();
  const osc = ctx.createOscillator();
  osc.frequency.value = 220;
  const dest = ctx.createMediaStreamDestination();
  const gain = ctx.createGain();
  gain.gain.value = 0.05;
  osc.connect(gain).connect(dest);
  osc.start();
  return dest.stream;
};
"""


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    frames = {"count": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000},
                               device_scale_factor=2)
        page.add_init_script(FAKE_CAPTURE)

        def handle_ws(route):
            # 页面要等 onopen 之后才挂 onmessage，所以等它送出第一帧音频再回灌事件
            def on_page_message(msg):
                if isinstance(msg, str):
                    return
                frames["count"] += 1
                if frames["count"] != 1:
                    return
                route.send(json.dumps({"type": "meeting", "meetingId": 1,
                                       "glossarySize": 18}))
                route.send(json.dumps({"type": "status", "state": "live"}))
                for turn in TURNS:
                    route.send(json.dumps(turn))
                route.send(json.dumps(LIVE_PARTIAL))

            route.on_message(on_page_message)

        page.route_web_socket("**/ws/live", handle_ws)

        page.goto(BASE)
        page.wait_for_selector(".rec-btn")
        page.screenshot(path=str(SHOTS / "01-待机.png"))
        print("待机界面已截图")

        page.click(".rec-btn")
        page.wait_for_selector("text=正在同传", timeout=10000)
        page.wait_for_selector(".entry.speaking", timeout=10000)
        page.wait_for_timeout(1200)
        page.screenshot(path=str(SHOTS / "02-同传中.png"))
        entries = page.locator(".entry").count()
        print(f"笔录条目 {entries} 条，收到音频帧 {frames['count']} 帧")

        # 对方段落收口后右栏应自动出建议回复（真实走 /api/draft）
        page.wait_for_selector("text=自动建议", timeout=15000)
        page.wait_for_function(
            "() => { const c = document.querySelectorAll('.card .zh');"
            " return c.length >= 1 && c[0].textContent.trim().length > 0; }",
            timeout=60000)
        print("自动建议卡片已出现并写出内容")
        # 先填字（空输入框时发出按钮本来就是灰的），再等自动稿写完、按钮恢复可用
        page.fill(".composer textarea", "对方要把赔偿上限压到 15%，帮我回一段顶回去。")
        page.wait_for_selector(".send:not([disabled])", timeout=60000)
        page.click(".send")
        page.wait_for_function(
            "() => document.querySelectorAll('.ask').length >= 2", timeout=60000)
        page.wait_for_function(
            "() => { const c = document.querySelectorAll('.card .zh');"
            " return c.length >= 2 && [...c].every(x => x.textContent.trim()); }",
            timeout=60000)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "03-拟稿.png"))
        print("手动拟稿卡片已渲染（与自动建议并存）")

        page.click(".topbar .chip")
        page.wait_for_selector(".sheet h2")
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "04-会议底稿.png"))
        print("会议底稿抽屉已截图")

        errors = page.evaluate("window.__errors || []")
        browser.close()

    ok = entries >= 4 and frames["count"] > 0
    print(f"\n音频帧是否真的发出：{'是' if frames['count'] else '否'}")
    print("结果：" + ("通过" if ok else "不通过"))
    if errors:
        print("页面错误：", errors)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
