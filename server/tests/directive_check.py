"""会中指示是否一直有效。

实战翻车：会上口头说了不用某个方案，右栏后面几张卡还围着那个方案答。成因是自动建议每出
一张卡就往对话历史里塞两条，手打的指示很快被历史窗口挤掉。这里两层验：
一是拼提示词时指示确实单列且不受窗口挤压（确定性，不调模型）；
二是真发一次模型请求，看它是否照新方向答、不再提被推翻的方案（要配好文本模型）。
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

PROJ = "/Users/rainbow/AIwork/03-AI项目/meeting-interpreter"
sys.path.insert(0, PROJ)

from server import config, drafting, settings as settings_mod  # noqa: E402
from server.context_store import MeetingContext  # noqa: E402

# 全部用虚构情节，不放任何真实客户信息
DIRECTIVE = "放弃走本地审核员那个方案，后面别再提它，改成全部在境外完成。"
AUTO = "对方刚说完左边这段话。直接回应对方刚才问的那个问题，帮我拟一段英文回复。"
QUESTION = "对方刚问：这样安排是不是为了保护本地那家子公司？帮我拟一段英文回复。"

TRANSCRIPT = [
    {"src": "So the local reviewer would flag it first.", "dst": "所以本地审核员会先标记出来。"},
    {"src": "Is that arrangement meant to protect the local subsidiary?",
     "dst": "这样安排是为了保护本地那家子公司吗？"},
]
CTX = MeetingContext(
    matter="某跨境业务安排",
    parties=[{"en": "Our Client", "zh": "我方客户", "role": "买方"}],
    issues=["审核环节放在境内还是境外"],
    terms=[],
    my_position="审核全部在境外完成，境内不参与",
    sources=["虚构测试底稿"])


def flooded_history() -> list[dict]:
    """指示之后又出了八张自动卡，共十八条。

    窗口开多大都不是办法：会开得够久，手打的指示总会被挤出去。所以这里故意造一个超过
    当前窗口的长度，验的是「指示单独一节、不受窗口影响」，而不是「窗口够不够大」。
    """
    h = [{"role": "user", "text": DIRECTIVE}, {"role": "model", "text": "好的，明白。"}]
    for i in range(8):
        h.append({"role": "user", "text": AUTO})
        h.append({"role": "model", "text": f"The local reviewer would handle step {i}."})
    return h


def check_prompt() -> bool:
    ok = True
    hist = flooded_history()
    p, _ = drafting.build_prompt(CTX, TRANSCRIPT, hist, QUESTION, directives=[DIRECTIVE])
    checks = {
        "指示单列成节": "【会中指示" in p,
        "指示原文在提示词里": DIRECTIVE in p,
        "指示排在我的要求之前": p.index(DIRECTIVE) < p.rindex("【我的要求】"),
        "系统提示写明冲突时以会中指示为准": "以会中指示为准" in drafting.system_prompt("English"),
    }
    # 不传 directives 时（旧行为）指示确实会被历史窗口挤掉，这条证明成因判断没错
    old, _ = drafting.build_prompt(CTX, TRANSCRIPT, hist, QUESTION)
    checks["不传指示时确会被挤掉（成因复现）"] = DIRECTIVE not in old
    for name, passed in checks.items():
        print(("  ok   " if passed else "  FAIL ") + name)
        ok &= passed
    return ok


async def check_model() -> bool:
    src = settings_mod.load(config.Workspace(config.DATA_DIR))
    if not src.text.ready():
        print("  跳过 模型侧验证：本机没配文本模型")
        return True
    tmp = Path(tempfile.mkdtemp(prefix="mi-directive-"))
    ws = config.Workspace(tmp / "ws")
    settings_mod.save(ws, settings_mod.Settings(), {
        "text": {"base_url": src.text.base_url, "api_key": src.text.api_key,
                 "model": src.text.model}})
    try:
        out = ""
        async for chunk in drafting.stream_draft(
                ws, CTX, TRANSCRIPT, flooded_history(), QUESTION, "fast",
                directives=[DIRECTIVE]):
            out += chunk
        print("  模型实答：" + out.strip().replace("\n", " ")[:220])
        low = out.lower()
        # 断言的是「改按新方向答」，不是「一个字都不许提旧方案」。对方直接问到旧方案时
        # 照实说明为什么不再采用是正常的，把不提旧方案当硬指标会逼出回避式的回答
        moved = any(w in low or w in out for w in ("offshore", "outside", "境外"))
        print(("  ok   " if moved else "  FAIL ") + "回复按新方向作答")
        old_hit = [w for w in ("local reviewer", "本地审核员", "onshore review")
                   if w in low or w in out]
        print(f"  参考 旧方案是否被提及：{old_hit or '未提及'}（提及不算失败）")
        return moved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main() -> int:
    print("一、拼提示词（确定性）")
    ok = check_prompt()
    print("二、真发模型")
    ok &= await check_model()
    print("结果：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
