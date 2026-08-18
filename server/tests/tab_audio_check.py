"""真实标签页音频端到端：不桩任何东西，验证用户现场失败的那条路。

开两个标签页：源页用 WebAudio 循环播放 fixtures/en.pcm 的真实语音；应用页点「开始记录」，
Chrome 由 --auto-select-tab-capture-source-by-title 启动参数自动选中源页（等价于用户在共享
面板里选了那个标签页并开了音频），随后音频走 AudioWorklet、WebSocket、百炼实时模型，断言
字幕真的上屏。需要后端在 8787 跑着、web 已构建。用系统 python 运行：

    python3 -m server.tests.tab_audio_check
"""
from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8787"
PCM = Path(__file__).resolve().parent / "fixtures" / "en.pcm"
LOG = Path(__file__).resolve().parents[2] / "data" / "server.log"
TITLE = "MI-AUDIO-SOURCE"

SOURCE_HTML = """<!doctype html><html><head><title>%s</title></head><body>audio source
<script>
const raw = atob("%s");
const n = raw.length / 2;
const bytes = Uint8Array.from(raw, c => c.charCodeAt(0));
const dv = new DataView(bytes.buffer);
const ctx = new AudioContext({sampleRate: 16000});
const buf = ctx.createBuffer(1, n, 16000);
const ch = buf.getChannelData(0);
for (let i = 0; i < n; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
const src = ctx.createBufferSource();
src.buffer = buf;
src.loop = true;
src.connect(ctx.destination);
src.start();
</script></body></html>"""


def last_meeting_tokens() -> int | None:
    """服务日志里最后一行「会议结束」的音频 token 数。"""
    if not LOG.exists():
        return None
    hits = re.findall(r"会议结束：音频 token (\d+)", LOG.read_text(encoding="utf-8"))
    return int(hits[-1]) if hits else None


def main() -> int:
    if not PCM.exists():
        print("缺 fixtures/en.pcm，先跑一次 replay 生成")
        return 1
    b64 = base64.b64encode(PCM.read_bytes()).decode()

    with sync_playwright() as p:
        # 无头模式不支持 getDisplayMedia，必须有头跑；标签页捕获在 Chrome 内部完成，
        # 不需要 macOS 屏幕录制权限
        browser = p.chromium.launch(headless=False, args=[
            "--autoplay-policy=no-user-gesture-required",
            f"--auto-select-tab-capture-source-by-title={TITLE}",
        ])
        ctx = browser.new_context(viewport={"width": 1500, "height": 950})

        source = ctx.new_page()
        source.set_content(SOURCE_HTML % (TITLE, b64))
        source.wait_for_timeout(500)

        app = ctx.new_page()
        app.goto(BASE)
        app.wait_for_selector(".rec-btn")
        app.click(".rec-btn")

        # 共享被自动授予后应进入同传状态；拿不到音轨时会弹 notice 报错
        try:
            app.wait_for_selector("text=正在同传", timeout=20000)
        except Exception:
            notice = app.locator(".notice").all_text_contents()
            print("没进入同传状态，页面提示：", notice or "(无)")
            browser.close()
            return 1
        print("已进入同传，音频来自另一个真实标签页")

        # 真实模型转写真实语音，等第一段字幕（进行中或已收口都算）
        try:
            app.wait_for_selector(".entry", timeout=60000)
        except Exception:
            notice = app.locator(".notice").all_text_contents()
            print("60 秒没等到字幕，页面提示：", notice or "(无)")
            browser.close()
            return 1
        text = app.locator(".entry").first.inner_text()
        print(f"字幕已上屏（{len(text)} 字）")

        # 成功判据到此已满足：真实标签页音频进来、真实模型转写、字幕上屏。
        # 之后的停止与关闭只是清理：Playwright 自带 Chromium 在活跃标签页捕获下
        # 截图或关闭时偶发崩溃，那是测试工具的毛病，不算链路失败
        try:
            app.click(".rec-btn.stop")
            app.wait_for_timeout(1500)
            browser.close()
        except Exception as exc:
            print(f"清理阶段测试浏览器异常（不影响结论）：{type(exc).__name__}")

    tokens = last_meeting_tokens()
    if tokens is not None:
        print(f"服务端记账：最近一场会议音频 token {tokens}")
    ok = len(text.strip()) > 0
    print("结果：" + ("通过，标签页音频链路真实可用" if ok else "不通过"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
