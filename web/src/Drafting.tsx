import { useEffect, useRef, useState } from "react";
import { copyText } from "./copy";
import type { Draft } from "./types";

const QUICK: { label: string; instruction: string }[] = [
  { label: "回这一段", instruction: "针对对方最后这段话，帮我拟一段英文回复。" },
  { label: "追问", instruction: "针对对方最后这段话，帮我拟一句英文追问，把关键点问清楚。" },
  { label: "这句什么意思", instruction: "对方最后这段话里有哪些说法值得我留意，用中文点出来。" },
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
  onAsk,
  onRefine,
}: {
  drafts: Draft[];
  busy: boolean;
  onAsk: (instruction: string) => void;
  onRefine: (draft: Draft) => void;
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

  useEffect(() => {
    const box = threadRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [drafts]);

  const send = (instruction: string) => {
    const value = instruction.trim();
    if (!value || busy) return;
    onAsk(value);
    setText("");
  };

  return (
    <>
      <div className="thread" ref={threadRef}>
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
            <div className="ask">{d.auto ? "自动建议 · 回应对方最后这段话" : d.instruction}</div>
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
                  </div>
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
            <button key={q.label} className="chip" disabled={busy} onClick={() => send(q.instruction)}>
              {q.label}
            </button>
          ))}
          <span className="spacer" />
          <button className="send" disabled={busy || !text.trim()} onClick={() => send(text)}>
            {busy ? "正在写" : "发出"}
          </button>
        </div>
      </div>
    </>
  );
}
