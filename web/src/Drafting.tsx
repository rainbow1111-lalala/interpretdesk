import { useEffect, useRef, useState } from "react";
import { copyText } from "./copy";
import type { Directive, Draft } from "./types";

// mode 告诉后端这一次要的是拟稿还是问我。原来后端在提示词末尾无条件要求「英文正文加
// ---ZH--- 对照」，把「问含义就中文简答」那条规则压死了，问「这句什么意思」也会回英文
const QUICK: { label: string; instruction: string; mode: "draft" | "ask" }[] = [
  { label: "回这一段", mode: "draft",
    instruction: "针对对方最后这段话，帮我拟一段英文回复。" },
  { label: "追问", mode: "draft",
    instruction: "针对对方最后这段话，帮我拟一句英文追问，把关键点问清楚。" },
  { label: "这句什么意思", mode: "ask",
    instruction: "对方最后这段话里有哪些说法值得我留意，用中文点出来。" },
];

function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="mini"
      onClick={async () => {
        if (await copyText(text)) {
          setDone(true);
          window.setTimeout(() => setDone(false), 1400);
        }
      }}
    >
      {done ? "已复制" : label}
    </button>
  );
}

export function Drafting({
  drafts,
  busy,
  directives,
  onAsk,
  onRefine,
  onSaveDirective,
}: {
  drafts: Draft[];
  busy: boolean;
  directives: Directive[];
  onAsk: (instruction: string, mode?: "draft" | "ask") => void;
  onRefine: (draft: Draft) => void;
  onSaveDirective: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const threadRef = useRef<HTMLDivElement>(null);
  const prefetchTimer = useRef<number | undefined>(undefined);

  // 打字停顿 300 毫秒就按当前输入预取原文片段，等按下发出时片段已经是热的。
  // 检索跑在打字的间隙里，不占拟稿关键路径
  const prefetch = (value: string) => {
    window.clearTimeout(prefetchTimer.current);
    const query = value.trim();
    if (query.length < 4) return;
    prefetchTimer.current = window.setTimeout(() => {
      fetch("/api/context/prefetch", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ query }),
      }).catch(() => {});
    }, 300);
  };

  // 贴底跟随：停在底部时新卡片照常把视线带下去；一旦往上翻去看之前的话就停住，
  // 不再被新卡片拽走，翻回底部自动恢复跟随。会中翻回看上一版建议时被弹走过
  const stick = useRef(true);

  const onThreadScroll = () => {
    const box = threadRef.current;
    if (!box) return;
    // 留 40 像素余量：流式写字时高度一直在长，严格等于底部几乎不成立
    stick.current = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  };

  useEffect(() => {
    const box = threadRef.current;
    if (box && stick.current) box.scrollTop = box.scrollHeight;
  }, [drafts]);

  const send = (instruction: string, mode?: "draft" | "ask") => {
    const value = instruction.trim();
    // 正在写自动稿时照样放行。手打的要求必须能当场打断，等它写完再接受就晚了：
    // 屏幕已经翻过去，答的还是上一句。旧流由 App 里的序号挡掉，不会盖住新稿。
    if (!value) return;
    // 自由输入不传 mode，分不清是拟稿还是问话，交给模型自己认
    onAsk(value, mode);
    setText("");
  };

  return (
    <>
      <div className="thread" ref={threadRef} onScroll={onThreadScroll}>
        {drafts.length === 0 && (
          <div className="empty" style={{ padding: "24px 0 0" }}>
            <p>
              让我帮你组织英文。说要求就行，比如“回这一段，但先不接受价格调整”。
            </p>
          </div>
        )}

        {drafts.map((d) => {
          // 助手判断我的要求与底稿立场冲突时会追一行「提示：」，这一行要跳出来，别混在译文里
          const noteAt = d.zh.search(/(^|\n)\s*提示[：:]/);
          const zhMain = noteAt >= 0 ? d.zh.slice(0, noteAt).trim() : d.zh;
          const zhNote = noteAt >= 0 ? d.zh.slice(noteAt).replace(/^\s*/, "") : "";
          return (
          <div key={d.id}>
            <div className="ask">
              {d.auto ? `自动建议 · 回应：${d.answering ?? "对方最后这段话"}` : d.instruction}
            </div>
            <div className="card" style={{ marginTop: 10 }}>
              {d.error ? (
                <p className="en" style={{ paddingBottom: 16, fontSize: 15 }}>
                  {d.error}
                </p>
              ) : (
                <>
                  <p className="en">{d.en || "…"}</p>
                  {zhMain && <p className="zh">{zhMain}</p>}
                  {zhNote && <p className="note">{zhNote}</p>}
                  <div className="actions">
                    <CopyButton text={d.en} label="复制英文" />
                    {zhMain && <CopyButton text={zhMain} label="复制中文" />}
                    <span className="spacer" />
                    {d.done && !d.refined && (
                      <button className="mini" onClick={() => onRefine(d)}>
                        换强模型重写
                      </button>
                    )}
                    {d.refined && <span className="label">已精修</span>}
                    {!d.auto && d.instruction.trim() && (
                      <button className="mini" onClick={() => onSaveDirective(d.instruction.trim())}>
                        存为会中指示
                      </button>
                    )}
                  </div>
                  {d.evidence && (d.evidence.verified.length > 0
                    || d.evidence.pending.length > 0
                    || d.evidence.todo.length > 0) && (
                    <details className="evidence">
                      <summary>
                        依据 {d.evidence.verified.length} 条已核对
                        {d.evidence.pending.length > 0
                          ? ` · 未核实 ${d.evidence.pending.length} 条` : ""}
                        {d.evidence.todo.length > 0
                          ? ` · 待确认 ${d.evidence.todo.length} 条` : ""}
                      </summary>
                      {d.evidence.verified.map((it, i) => (
                        <p className="ev-ok" key={`v${i}`}>
                          <b>{it.doc || it.tag}</b>「{it.quote}」
                          {it.note && <span className="ev-note">　{it.note}</span>}
                        </p>
                      ))}
                      {d.evidence.pending.map((it, i) => (
                        <p className="ev-bad" key={`p${i}`}>
                          未核实：「{it.quote}」<span className="ev-note">　{it.reason}</span>
                        </p>
                      ))}
                      {d.evidence.todo.map((x, i) => (
                        <p className="ev-todo" key={`t${i}`}>待确认：{x}</p>
                      ))}
                    </details>
                  )}
                </>
              )}
            </div>
          </div>
          );
        })}
      </div>

      <div className="composer">
        <textarea
          value={text}
          placeholder="要我怎么说？回车发出，Shift＋回车换行"
          onChange={(e) => {
            setText(e.target.value);
            prefetch(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(text);
            }
          }}
        />
        <div className="row">
          {QUICK.map((q) => (
            <button key={q.label} className="chip"
                    onClick={() => send(q.instruction, q.mode)}>
              {q.label}
            </button>
          ))}
          <span className="spacer" />
          <button className="send" disabled={!text.trim()} onClick={() => send(text)}>
            {busy ? "打断并发出" : "发出"}
          </button>
        </div>
      </div>
    </>
  );
}
