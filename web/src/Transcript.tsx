import { useEffect, useRef } from "react";
import type { Entry, LiveEntry } from "./types";

function stamp(ts: number): string {
  const d = new Date(ts * 1000);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
}

export function Transcript({
  entries,
  live,
  running,
}: {
  entries: Entry[];
  live: LiveEntry | null;
  running: boolean;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const onScroll = () => {
      pinned.current = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
    };
    box.addEventListener("scroll", onScroll, { passive: true });
    return () => box.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const box = boxRef.current;
    if (box && pinned.current) box.scrollTop = box.scrollHeight;
  }, [entries, live]);

  const nothing = entries.length === 0 && !live;

  return (
    <div className="record" ref={boxRef}>
      {nothing && (
        <div className="empty">
          <p>
            {running
              ? "已经在听了。对方一开口，原话和中文译文就会逐段落在这里。"
              : "开始记录后，对方的原话与中文译文会逐段落在这里，供你随时回看。"}
          </p>
        </div>
      )}

      {entries.map((entry) => (
        <div className="entry" key={entry.turnId}>
          <div className="stamp">{stamp(entry.ts)}</div>
          <div>
            {entry.pairs.map((pair, i) => (
              <div className="pair" key={i}>
                <p className="source">{pair.src}</p>
                {!entry.echo && pair.dst && <p className="target arrive">{pair.dst}</p>}
              </div>
            ))}
          </div>
        </div>
      ))}

      {live && (live.src || live.dst) && (
        <div className="entry speaking" key={`live-${live.turnId}`}>
          <div className="stamp">
            <span className="seal-square" aria-hidden />
            <span>正在说</span>
          </div>
          <div>
            <p className="source">{live.src}</p>
            {!live.echo && live.dst && <p className="target">{live.dst}</p>}
          </div>
        </div>
      )}
    </div>
  );
}
