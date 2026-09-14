import { useState } from "react";
import { AudioCapture, type SourceMode } from "./audio";
import type { ContextInfo, Profile } from "./types";

type Props = {
  source: SourceMode;
  micId: string;
  micLabel: string;
  ctxInfo: ContextInfo | null;
  profile: Profile | null;
  hasMeeting: boolean;
};

/**
 * 开会前的三项检查：真的收到声音了吗、字幕服务连得上吗、材料准备到什么程度。
 *
 * 这三件事以前只能等开会开到一半才发现不对。实战翻过一次车：共享面板里选错了对象，
 * 音频 token 一直是 0，界面上没有任何地方说得出问题在哪，会已经开起来了。
 */
export function Readiness({ source, micId, micLabel, ctxInfo, profile, hasMeeting }: Props) {
  const [mic, setMic] = useState<{ state: "idle" | "testing" | "done"; peak: number }>({
    state: "idle",
    peak: 0,
  });
  const [sub, setSub] = useState<{ state: "idle" | "testing" | "done"; ok?: boolean; detail?: string }>(
    { state: "idle" },
  );

  const testMic = async () => {
    setMic({ state: "testing", peak: 0 });
    const cap = new AudioCapture();
    let top = 0;
    try {
      await cap.start(source, (_pcm, p) => {
        if (p > top) top = p;
      }, () => undefined, micId || undefined);
      await new Promise((r) => setTimeout(r, 5000));
    } catch (e) {
      cap.stop();
      setMic({ state: "done", peak: -1 });
      setSub((s) => ({ ...s, detail: e instanceof Error ? e.message : String(e) }));
      return;
    }
    cap.stop();
    setMic({ state: "done", peak: top });
  };

  const testSubtitle = async () => {
    setSub({ state: "testing" });
    try {
      const r = await fetch("/api/settings/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ target: "speech" }),
      });
      const d = await r.json();
      setSub({ state: "done", ok: Boolean(d.ok),
               detail: d.ok ? `已连通，用时 ${d.seconds} 秒` : String(d.error ?? "连不上") });
    } catch (e) {
      setSub({ state: "done", ok: false, detail: e instanceof Error ? e.message : String(e) });
    }
  };

  const docs = ctxInfo?.docs ?? [];
  const chars = docs.reduce((n, d) => n + d.chars, 0);
  const filled = profile
    ? (["identity", "goal", "facts", "noCommit"] as const).filter((k) => profile[k]?.trim()).length
    : 0;

  return (
    <section className="ready">
      <h3>开会前检查</h3>

      <div className="ready-row">
        <span className="ready-label">实际收音</span>
        <span className="ready-body">
          {mic.state === "idle" && "还没试过。"}
          {mic.state === "testing" && "正在听，请说几句话……"}
          {mic.state === "done" && mic.peak < 0 && "打不开这个声源。"}
          {mic.state === "done" && mic.peak >= 0 && mic.peak < 0.02 && (
            <b className="bad">
              几乎没有声音（峰值 {mic.peak.toFixed(2)}）。
              {source === "mic"
                ? "换一个麦克风试试。"
                : "抓标签页时要在共享面板里选「标签页」并勾上「同时分享标签页音频」。"}
            </b>
          )}
          {mic.state === "done" && mic.peak >= 0.02 && (
            <span className="good">
              收到声音了，峰值 {mic.peak.toFixed(2)}
              {micLabel ? `（${micLabel}）` : ""}
            </span>
          )}
        </span>
        <button className="chip" disabled={mic.state === "testing"} onClick={testMic}>
          试音 5 秒
        </button>
      </div>

      <div className="ready-row">
        <span className="ready-label">字幕返回</span>
        <span className="ready-body">
          {sub.state === "idle" && "还没试过。"}
          {sub.state === "testing" && "正在连语音服务……"}
          {sub.state === "done" && (
            <span className={sub.ok ? "good" : "bad"}>{sub.detail}</span>
          )}
        </span>
        <button className="chip" disabled={sub.state === "testing"} onClick={testSubtitle}>
          测一下
        </button>
      </div>

      <div className="ready-row">
        <span className="ready-label">底稿准备</span>
        <span className="ready-body">
          {!hasMeeting ? (
            <b className="bad">还没有会议。先在「会议」里建一场，材料才有地方存。</b>
          ) : docs.length === 0 && filled === 0 ? (
            <b className="bad">这一场还没有任何材料，也没填会前交代。</b>
          ) : (
            <span>
              {docs.length} 份原文共 {chars.toLocaleString("zh-CN")} 字，
              术语锁定 {ctxInfo?.glossarySize ?? 0} 条，原文索引 {ctxInfo?.indexChunks ?? 0} 块；
              会前交代填了 {filled}/4 项。
            </span>
          )}
        </span>
      </div>
    </section>
  );
}
