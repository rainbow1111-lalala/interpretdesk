"""手机端布局验证：iPhone 尺寸下不横向溢出、两栏上下都在、声源只剩麦克风。

手机浏览器没有 getDisplayMedia，这里把它删掉来模拟，验证界面确实不再给「会议标签页」。
需要先 `cd web && npm run build`，后端在 8787 上跑着。用系统 python 跑。
"""
from __future__ import annotations

import os
import tempfile
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = f"http://127.0.0.1:{os.environ.get('MI_PORT', '8787')}"
SHOTS = Path(os.environ.get("MI_SHOTS")
             or (Path(tempfile.gettempdir()) / "mi-shots-mobile"))

# 手机上没有屏幕共享；麦克风给一条真轨，免得点开始就报错
NO_TAB = """
delete MediaDevices.prototype.getDisplayMedia;  // 方法在原型上，删实例属性没用
navigator.mediaDevices.getUserMedia = async () => {
  const ctx = new AudioContext();
  const osc = ctx.createOscillator();
  const dest = ctx.createMediaStreamDestination();
  osc.connect(dest); osc.start();
  return dest.stream;
};
navigator.mediaDevices.enumerateDevices = async () => [
  { deviceId: "builtin", kind: "audioinput", label: "iPhone 麦克风", groupId: "g1" },
];
"""

DEVICES = [("iPhone 14", 390, 844), ("iPhone SE", 375, 667)]


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, w, h in DEVICES:
            page = browser.new_page(viewport={"width": w, "height": h},
                                    device_scale_factor=3, is_mobile=True,
                                    has_touch=True)
            page.add_init_script(NO_TAB)
            page.goto(BASE)
            # 首次进入会弹使用提示：先验它在、再关掉，后面的布局断言才量得到真实控件
            page.wait_for_selector(".onboard")
            first_open = page.eval_on_selector(".onboard", "e => e.innerText.length > 60")
            page.screenshot(path=str(SHOTS / f"{name}-首次提示.png"))
            page.click(".onboard-ok")
            page.wait_for_selector(".onboard", state="detached")
            page.reload()
            page.wait_for_selector(".workspace")
            again = page.query_selector(".onboard") is None
            page.wait_for_timeout(600)
            page.screenshot(path=str(SHOTS / f"{name}.png"))

            over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            # 容器不溢出不等于里面的控件没被切掉：iPhone 14 上录音条宽度是 390，
            # 但「开始记录」被切在左边缘、设备下拉被切在右边缘，断言只查容器会漏掉
            def fits(sel: str) -> bool:
                return page.eval_on_selector(sel, """(e, w) => {
                    const r = e.getBoundingClientRect();
                    return r.left >= -1 && r.right <= w + 1 && r.width > 0;
                }""", w)
            def visible_in_viewport(sel: str) -> bool:
                return page.eval_on_selector(sel, """(e, h) => {
                    const r = e.getBoundingClientRect();
                    return r.bottom <= h + 1 && r.top >= -1 && r.height > 0;
                }""", h)
            cols = page.eval_on_selector_all(
                ".workspace > .column", "els => els.map(e => Math.round(e.getBoundingClientRect().height))")
            recw = page.eval_on_selector(".recorder", "e => Math.round(e.getBoundingClientRect().width)")
            opts = page.eval_on_selector_all(".recorder select option", "els => els.map(e => e.textContent)")
            checks = {
                f"[{name}] 首次进入弹出使用提示": first_open,
                f"[{name}] 点知道了后刷新不再弹": again,
                f"[{name}] 无横向溢出": over <= 0,
                f"[{name}] 笔录与拟稿上下都在且各有高度": len(cols) == 2 and all(c > 120 for c in cols),
                f"[{name}] 录音条没有超出屏宽": recw <= w,
                f"[{name}] 开始记录键完整可见": fits(".rec-btn"),
                f"[{name}] 声源下拉完整可见": fits(".recorder select"),
                f"[{name}] 拟稿输入框没被录音条盖住": (
                    fits(".composer textarea") and visible_in_viewport(".composer textarea")),
                f"[{name}] 发出键没被盖住": visible_in_viewport(".composer .send"),
                f"[{name}] 声源只剩麦克风": all("标签页" not in (o or "") for o in opts),
            }
            for label, passed in checks.items():
                print(("  ok   " if passed else "  FAIL ") + label)
                ok &= passed
            print(f"       两栏高度 {cols}，录音条宽 {recw}，声源选项 {opts}")
            page.close()
        browser.close()
    print(f"截图在 {SHOTS}")
    print("结果：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


sys.exit(main())
