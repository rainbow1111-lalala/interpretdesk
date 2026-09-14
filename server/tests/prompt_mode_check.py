"""问我含义时不许再拟英文稿。

实战问题：界面上「这句什么意思」按的是问询，回来的却是英文回复加 ---ZH--- 中文对照。
成因是提示词末尾无条件要求「正文用目标语言写完再给中文对照」，把系统提示里「问含义就中文
简答」那条压过去了。修法是按 mode 分支。两层验：拼提示词（确定性）与真发模型。
"""
import asyncio
import sys

PROJ = "/Users/rainbow/AIwork/03-AI项目/meeting-interpreter"
sys.path.insert(0, PROJ)

from server import config, drafting, llm  # noqa: E402
from server import settings as settings_mod  # noqa: E402
from server.context_store import MeetingContext  # noqa: E402

CTX = MeetingContext(matter="某跨境交易谈判", my_position="按字段逐一识别，不按数量豁免")
TR = [{"src": "We'd want a carve-out for ordinary course disclosures.",
       "dst": "我们希望对正常经营过程中的披露作除外安排。"}]
ASK = "carve-out 是什么意思"
DRAFT = "针对对方最后这段话，帮我拟一段英文回复。"


def check_prompt() -> bool:
    ok = True
    ask_p, _ = drafting.build_prompt(CTX, TR, [], ASK, mode="ask")
    draft_p, _ = drafting.build_prompt(CTX, TR, [], DRAFT, mode="draft")
    free_p, _ = drafting.build_prompt(CTX, TR, [], ASK)
    checks = {
        "ask 明确要中文简答": "用中文简短回答" in ask_p,
        "ask 不再要求 ---ZH---": "写完另起一行写 ---ZH---" not in ask_p,
        "draft 仍强制目标语言与对照": "写完另起一行写 ---ZH---" in draft_p,
        "自由输入两种情形都写明": ("我要的是拟稿时" in free_p and "我问的是含义" in free_p),
    }
    for name, passed in checks.items():
        print(("  ok   " if passed else "  FAIL ") + name)
        ok &= passed
    return ok


async def check_model() -> bool:
    ws = config.Workspace(config.DATA_DIR)
    cfg = settings_mod.load(ws)
    if not cfg.text.ready():
        print("  跳过 模型侧验证：本机没配文本模型")
        return True
    system = drafting.system_prompt(cfg.reply_lang)
    ok = True
    for mode, q, want_zh in (("ask", ASK, False), ("draft", DRAFT, True)):
        prompt, _ = drafting.build_prompt(CTX, TR, [], q, None, "", cfg.reply_lang, None, mode)
        out = ""
        async for chunk in llm.stream(cfg.text, cfg.strong_model(), prompt, system,
                                      temperature=0.4):
            out += chunk
        got_zh = "---ZH---" in out
        passed = got_zh == want_zh
        print(("  ok   " if passed else "  FAIL ")
              + f"[{mode}] 含 ---ZH--- {got_zh}（应为 {want_zh}）")
        print(f"       {out.strip()[:110]}")
        ok &= passed
    return ok


async def main() -> int:
    print("一、拼提示词（确定性）")
    ok = check_prompt()
    print("二、真发模型")
    ok &= await check_model()
    print("结果：" + ("通过" if ok else "不通过"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
