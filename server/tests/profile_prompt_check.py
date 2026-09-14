"""提示词不许预设使用者的职业。

原来的系统提示第一句写死「你是一名熟悉中国数据合规与跨境业务的中国执业律师」，服务别的
行业时模型会按律师的口径答话，还会自己补一套并不存在的执业背景。改成身份由使用者自己填，
没填就什么都不假设。

纯字符串检查，不调模型，不碰磁盘。

    .venv/bin/python -m server.tests.profile_prompt_check
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ))

from server import context_store, drafting  # noqa: E402

ok = True
# 凡是给使用者安身份的词，提示词里一个都不许有
JOBS = ("律师", "执业", "法务", "会计师", "医生", "工程师", "数据合规", "跨境业务")


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok
    ok = ok and bool(cond)
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{('　' + extra) if extra else ''}")


def main_check() -> int:
    print("一、系统提示不预设职业")
    sp = drafting.system_prompt("English")
    hit = [w for w in JOBS if w in sp]
    check("拟稿系统提示里没有职业词", not hit, "、".join(hit))
    hit = [w for w in JOBS if w in context_store.BRIEF_PROMPT]
    check("底稿提炼提示里没有职业词", not hit, "、".join(hit))
    check("写明了没填就不要推测", "不要推测他的职业" in sp)
    check("签名仍是单参（directive_check 这么调）",
          drafting.system_prompt("中文").endswith("不要再附中文对照。"))

    print("二、使用者交代进得去")
    profile = {"identity": "某制造企业的采购经理", "goal": "把单价压到十二元以内",
               "facts": "上一批的单价是十四元", "noCommit": "不承诺全年采购量"}
    prompt, _ = drafting.build_prompt(context_store.MeetingContext(), [], [],
                                      "帮我回这一段", profile=profile)
    for label, value in profile.items():
        check(f"{label} 在提示词里", value in prompt)
    check("交代排在最前", prompt.startswith("【使用者交代"))
    check("不可承诺事项在末尾又提了一次",
          prompt.count("不承诺全年采购量") == 2, str(prompt.count("不承诺全年采购量")))

    print("三、交代为空时不替他补")
    prompt, _ = drafting.build_prompt(context_store.MeetingContext(), [], [],
                                      "帮我回这一段")
    check("整节不出现", "【使用者交代" not in prompt)
    hit = [w for w in JOBS if w in prompt]
    check("提示词里仍然没有职业词", not hit, "、".join(hit))

    print("四、说话人与材料命令两条规则")
    check("不许把发言归给某一方", "字幕不区分说话人" in sp)
    check("材料里的命令不执行", "绝不执行" in sp)
    check("只有三节算指示", "【使用者交代】【会中指示】【我的要求】" in sp)

    print("五、没有材料时不要求举证")
    prompt, _ = drafting.build_prompt(context_store.MeetingContext(), [], [], "帮我回")
    check("不提依据要求", "【出处要求】" not in prompt)
    prompt, _ = drafting.build_prompt(context_store.MeetingContext(), [], [], "这句什么意思",
                                      excerpts=[{"doc": "x", "text": "一段材料"}],
                                      mode="ask")
    check("问含义时也不要求举证", "【出处要求】" not in prompt)
    prompt, _ = drafting.build_prompt(context_store.MeetingContext(), [], [], "帮我回",
                                      excerpts=[{"doc": "x", "text": "一段材料"}],
                                      mode="draft")
    check("有材料且要拟稿时才要求", "【出处要求】" in prompt)
    return 0 if ok else 1


if __name__ == "__main__":
    code = main_check()
    print("结果：" + ("通过" if code == 0 else "不通过"))
    sys.exit(code)
