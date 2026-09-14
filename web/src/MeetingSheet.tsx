import { useState } from "react";
import type { ContextInfo, MeetingSummary, Profile } from "./types";

function when(ts: number): string {
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, "0")}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}

type Props = {
  meetings: MeetingSummary[];
  meetingId: number | null;
  canAdopt: boolean;
  docCount: number;
  running: boolean;
  onClose: () => void;
  onSwitched: (payload: {
    meetingId: number;
    context: ContextInfo;
    profile: Profile | null;
    turns?: { turnId: number; src: string; dst: string; srcLang: string; ts: number }[];
    fresh: boolean;
  }) => void;
};

/**
 * 会议列表：新建一场、或者回到某一场。
 *
 * 底稿是一场会一份。以前换会议靠手工换底稿，换漏了上一场的客户材料就跟着进了这一场的
 * 拟稿上下文。现在材料跟着会议走，切换会议就是整套换掉。
 */
export function MeetingSheet({
  meetings, meetingId, canAdopt, docCount, running, onClose, onSwitched,
}: Props) {
  const [title, setTitle] = useState("");
  const [adopt, setAdopt] = useState(canAdopt);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const call = async (url: string, body?: unknown) => {
    setBusy(true);
    setError("");
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body ?? {}),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || `服务端返回 ${r.status}`);
      return data;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      setBusy(false);
    }
  };

  const create = async () => {
    const d = await call("/api/meetings", { title: title.trim(), adoptExisting: adopt });
    if (!d) return;
    onSwitched({ meetingId: d.meetingId, context: d.context,
                 profile: d.conversation?.profile ?? null, turns: [], fresh: true });
    onClose();
  };

  const resume = async (id: number) => {
    const d = await call(`/api/meetings/${id}/resume`);
    if (!d) return;
    onSwitched({ meetingId: d.meetingId, context: d.context,
                 profile: d.conversation?.profile ?? null, turns: d.turns, fresh: false });
    onClose();
  };

  return (
    <div className="sheet-bg" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <header className="sheet-head">
          <h2>会议</h2>
          <button className="chip" onClick={onClose}>
            关闭
          </button>
        </header>

        <section className="mtg-new">
          <h3>新建一场</h3>
          <input
            value={title}
            placeholder="会议名称，留空就用日期"
            onChange={(e) => setTitle(e.target.value)}
          />
          {canAdopt && (
            <label className="mtg-adopt">
              <input type="checkbox" checked={adopt} onChange={(e) => setAdopt(e.target.checked)} />
              沿用现在这份底稿（{docCount} 份原文，会复制一份进新会议，原处保留）
            </label>
          )}
          <button className="chip primary" disabled={busy || running} onClick={create}>
            建一场新的
          </button>
          {running && <p className="mtg-hint">正在录音，停止之后才能新建或切换。</p>}
        </section>

        <section className="mtg-list">
          <h3>回到某一场</h3>
          {meetings.length === 0 && <p className="mtg-hint">还没有会议。</p>}
          {meetings.map((m) => (
            <div className={m.id === meetingId ? "mtg-row cur" : "mtg-row"} key={m.id}>
              <div className="mtg-meta">
                <b>{m.title}</b>
                <span>
                  {when(m.startedAt)}　{m.turns} 段
                  {m.docCount > 0 ? `　材料 ${m.docCount} 份` : "　无留存材料"}
                  {m.hasProfile ? "　已填会前交代" : ""}
                  {m.hasMinutes ? "　纪要已出" : ""}
                </span>
              </div>
              {m.id === meetingId ? (
                <span className="mtg-cur">当前这场</span>
              ) : (
                <button className="chip" disabled={busy || running} onClick={() => resume(m.id)}>
                  回到这场
                </button>
              )}
            </div>
          ))}
          <p className="mtg-hint">
            回到某一场会换成那一场自己的底稿与会前交代。早于分场存储的会议没有留存材料，
            笔录仍在。
          </p>
        </section>

        {error && <p className="sheet-err">{error}</p>}
      </div>
    </div>
  );
}
