"""几个候选模型并排跑同一组现场提问，比首字延迟、全文延迟和实际答案。

会议现场看重的三件事，公开榜单一件都答不了：首字要压在两秒内、长上下文里要扣住对方
刚问的那一句、中英口语要像人在会上说话。所以别挑榜单，拿自己的底稿和自己真问过的问题
测一轮。

用法：
    .venv/bin/python -m server.tests.model_bench                    # 只比本机现有快档与强档
    .venv/bin/python -m server.tests.model_bench 候选.json 提问.txt  # 比自己列的候选

候选.json 形如（放在仓库外，别提交，里面有 key）：
    [{"label": "百炼 max", "base_url": "https://.../compatible-mode/v1",
      "api_key": "sk-...", "model": "qwen3.8-max"}]
提问.txt 一行一个问题，空行忽略。默认用三条通用问题，换成你真问过的更准。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

PROJ = "/Users/rainbow/AIwork/03-AI项目/meeting-interpreter"
sys.path.insert(0, PROJ)

from server import config, context_store, drafting, llm, retrieval  # noqa: E402
from server import settings as settings_mod  # noqa: E402

WS = config.Workspace(config.DATA_DIR)

DEFAULT_QUESTIONS = [
    "对方刚问这样安排的合规依据是什么，帮我拟一段英文回复。",
    "对方问落地会遇到哪些实际障碍，帮我拟一段英文回复。",
    "对方要我们给一个时间表，帮我拟一段英文回复。",
]


def candidates(argv: list[str]) -> list[dict]:
    if len(argv) > 1 and Path(argv[1]).exists():
        return json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    cfg = settings_mod.load(WS)
    if not cfg.text.ready():
        return []
    return [
        {"label": f"快档 {cfg.text.model}", "base_url": cfg.text.base_url,
         "api_key": cfg.text.api_key, "model": cfg.text.model},
        {"label": f"强档 {cfg.strong_model()}", "base_url": cfg.text.base_url,
         "api_key": cfg.text.api_key, "model": cfg.strong_model()},
    ]


def questions(argv: list[str]) -> list[str]:
    if len(argv) > 2 and Path(argv[2]).exists():
        lines = Path(argv[2]).read_text(encoding="utf-8").splitlines()
        return [x.strip() for x in lines if x.strip()]
    return DEFAULT_QUESTIONS


async def one(cand: dict, prompt: str, system: str) -> tuple[float, float, str]:
    engine = settings_mod.Engine(base_url=cand["base_url"], api_key=cand.get("api_key", ""),
                                model=cand["model"])
    t0 = time.monotonic()
    first = 0.0
    out = ""
    async for chunk in llm.stream(engine, cand["model"], prompt, system, temperature=0.4):
        if not out and chunk.strip():
            first = time.monotonic() - t0
        out += chunk
    return first, time.monotonic() - t0, out


async def main() -> int:
    cands = candidates(sys.argv)
    if not cands:
        print("没有可测的候选：本机没配文本模型，也没给候选文件")
        return 1
    qs = questions(sys.argv)
    cfg = settings_mod.load(WS)
    ctx = context_store.load(WS)
    full = retrieval.all_text(WS, cfg.context_full_chars) if cfg.context_full_chars else ""
    system = drafting.system_prompt(cfg.reply_lang)
    print(f"底稿常驻 {len(full)} 字（context_full_chars={cfg.context_full_chars}），"
          f"候选 {len(cands)} 个，问题 {len(qs)} 条\n")

    rows = []
    for q in qs:
        prompt = drafting.build_prompt(ctx, [], [], q, None, full, cfg.reply_lang)
        print(f"— 问题：{q}")
        for c in cands:
            try:
                first, total, out = await one(c, prompt, system)
                rows.append((c["label"], first, total))
                head = out.strip().replace("\n", " ")[:150]
                print(f"  {c['label']}：首字 {first:.2f}s 全文 {total:.2f}s")
                print(f"    {head}")
            except Exception as exc:
                print(f"  {c['label']}：失败 {type(exc).__name__} {exc}"[:200])
        print()

    print("汇总（首字/全文，秒）")
    for label in dict.fromkeys(r[0] for r in rows):
        hit = [r for r in rows if r[0] == label]
        f = sum(r[1] for r in hit) / len(hit)
        t = sum(r[2] for r in hit) / len(hit)
        print(f"  {label}：首字均值 {f:.2f}，全文均值 {t:.2f}，样本 {len(hit)}")
    print("\n延迟只是一半，另一半是答得对不对，上面每条的正文自己读。")
    return 0


sys.exit(asyncio.run(main()))
