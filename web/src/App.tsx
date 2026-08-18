import { useCallback, useEffect, useRef, useState } from "react";
import { AudioCapture, listMics, preferredMic, type MicDevice, type SourceMode } from "./audio";
import { ContextSheet } from "./ContextSheet";
import { Drafting } from "./Drafting";
import { MinutesSheet } from "./MinutesSheet";
import { SettingsSheet } from "./SettingsSheet";
import { Transcript } from "./Transcript";
import type { ContextInfo, Draft, Entry, LinkState, LiveEntry } from "./types";

const SOURCE_LABEL: Record<SourceMode, string> = {
  tab: "会议标签页",
  mic: "麦克风（公放/现场）",
  both: "混合声源",
};

const STATE_LABEL: Record<LinkState, string> = {
  idle: "待机",
  starting: "接入中",
  live: "正在同传",
  paused: "已暂停",
  reconnecting: "重连中",
  error: "已中断",
};

function clock(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// 自动建议回复用的固定指令。改这句话就是改自动回应的口径。
// 显式要求 ---ZH--- 分隔：快档模型偶尔漏写，中文对照就不显示了
const AUTO_INSTRUCTION =
  "对方刚说完左边这段话。结合会议底稿，帮我拟一段可以直接说的英文回复。" +
  "必须先输出英文，然后单独一行写 ---ZH---，再给中文对照，两部分都不能省。";

const TODAY = new Date().toLocaleDateString("zh-CN", {
  year: "numeric",
  month: "long",
  day: "numeric",
});

export default function App() {
  const [link, setLink] = useState<LinkState>("idle");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [live, setLive] = useState<LiveEntry | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [peak, setPeak] = useState(0);
  const [source, setSource] = useState<SourceMode>("tab");
  // 麦克风设备可选：macOS 连续互通会把 iPhone 设成系统默认输入，必须能指定本机麦克风
  const [mics, setMics] = useState<MicDevice[]>([]);
  const [micId, setMicId] = useState(() => localStorage.getItem("mi-mic-id") ?? "");
  const [zoom, setZoom] = useState(1);
  const [notice, setNotice] = useState("");
  const [sheetOpen, setSheetOpen] = useState(false);
  const [ctxInfo, setCtxInfo] = useState<ContextInfo | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [health, setHealth] = useState<{ ok: boolean; detail: string } | null>(null);
  const [langs, setLangs] = useState<{
    targetLangs: { code: string; label: string }[];
    replyLangs: { code: string; label: string }[];
    targetLang: string;
    replyLang: string;
  } | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [drafting, setDrafting] = useState(false);
  // 段落收口后右栏自动写建议回复；可关。ws 回调里读不到最新 state，用 ref 镜像
  const [autoReply, setAutoReply] = useState(true);
  const [meetingId, setMeetingId] = useState<number | null>(null);
  const [minutesOpen, setMinutesOpen] = useState(false);
  // 暂停时不再往上送音频。用 ref 是因为音频回调建立在 start 里，拿不到最新的 state
  const pausedRef = useRef(false);
  // 当前这张自动卡在回应哪一句，供卡片显示
  const answeringRef = useRef("");
  // stop 里要知道这场会有没有内容，读 state 会拿到闭包里的旧值，用 ref 跟着走
  const entryCount = useRef(0);
  const autoReplyRef = useRef(true);
  const draftingRef = useRef(false);
  const autoDraftId = useRef<number | null>(null);
  const autoTimer = useRef<number | undefined>(undefined);
  // ws 回调建立在 start 里，而 runDraft 声明在 start 之后，经 ref 转一道避免引用顺序问题
  const runDraftRef = useRef<
    ((i: string, q: "fast" | "good", r?: number, a?: boolean) => void) | null
  >(null);

  const wsRef = useRef<WebSocket | null>(null);
  const capRef = useRef<AudioCapture | null>(null);
  const draftId = useRef(1);
  // setLink 是异步生效的，连点两下「开始记录」会在按钮还没变灰前跑两遍 start，
  // 弹出两个共享面板。用 ref 同步挡住重入
  const startGate = useRef(false);
  const running = link !== "idle" && link !== "error";

  useEffect(() => {
    if (source === "tab" || mics.length > 0) return;
    listMics()
      .then((list) => {
        setMics(list);
        setMicId((cur) =>
          cur && list.some((d) => d.id === cur) ? cur : preferredMic(list),
        );
      })
      .catch(() => setNotice("拿不到麦克风列表，检查系统设置里 Chrome 的麦克风权限。"));
  }, [source, mics.length]);

  useEffect(() => {
    if (micId) localStorage.setItem("mi-mic-id", micId);
  }, [micId]);

  useEffect(() => {
    autoReplyRef.current = autoReply;
  }, [autoReply]);
  useEffect(() => {
    draftingRef.current = drafting;
  }, [drafting]);

  useEffect(() => {
    entryCount.current = entries.length;
  }, [entries]);

  const refreshHealth = useCallback(() => {
    fetch("/api/health")
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetch("/api/context")
      .then((r) => r.json())
      .then(setCtxInfo)
      .catch(() => {});
    refreshHealth();
    fetch("/api/langs")
      .then((r) => r.json())
      .then(setLangs)
      .catch(() => {});
  }, [refreshHealth]);

  // 语种改动立刻存。字幕译文语种在会话建立时就定了，所以录音中不给改
  const setLang = useCallback(async (field: "target_lang" | "reply_lang", value: string) => {
    setLangs((prev) =>
      prev ? { ...prev, [field === "target_lang" ? "targetLang" : "replyLang"]: value } : prev,
    );
    await fetch("/api/settings", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    }).catch(() => {});
  }, []);

  useEffect(() => {
    if (link !== "live" && link !== "reconnecting") return;
    const t = window.setInterval(() => setElapsed((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [link]);

  const teardown = useCallback(() => {
    capRef.current?.stop();
    capRef.current = null;
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "stop" }));
      ws.close();
    }
    wsRef.current = null;
    setLive(null);
    setPeak(0);
  }, []);

  const stop = useCallback(() => {
    teardown();
    setLink("idle");
    pausedRef.current = false;
    // 停下来就问一句要不要出纪要，会议刚结束是整理的最佳时机
    if (entryCount.current > 0) setMinutesOpen(true);
  }, [teardown]);

  const togglePause = useCallback(() => {
    const next = !pausedRef.current;
    pausedRef.current = next;
    setLink(next ? "paused" : "live");
    setPeak(0);
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: next ? "pause" : "resume" }));
    }
  }, []);

  const start = useCallback(async () => {
    if (startGate.current) return;
    startGate.current = true;
    setNotice("");
    setLink("starting");
    setElapsed(0);
    pausedRef.current = false;
    setEntries([]);
    try {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      await new Promise<void>((resolve, reject) => {
        ws.onopen = () => resolve();
        ws.onerror = () => reject(new Error("连不上本机服务，确认后端已经启动。"));
      });

      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data as string);
        switch (msg.type) {
          case "partial":
            setLive({
              turnId: msg.turnId,
              src: msg.src,
              dst: msg.dst,
              srcLang: msg.srcLang,
              echo: msg.echo,
            });
            break;
          case "turn": {
            setEntries((prev) => [...prev, msg as Entry]);
            setLive((prev) => (prev && prev.turnId === msg.turnId ? null : prev));
            // 对方讲完一段外语，右栏自动给一版建议回复。同一张「自动」卡原地更新不刷屏。
            // 只等 250 毫秒去抖连续收口：检索片段是后台预取、拿现成的，多等换不来新片段
            const t = msg as Entry;
            const spoken = t.pairs.map((p) => p.src).join(" ").trim();
            // 对方讲中文还是外语都照常拟稿，听到中文不代表不用回应
            if (autoReplyRef.current && spoken.length >= 12) {
              window.clearTimeout(autoTimer.current);
              // 正在写上一版时不能把这次触发丢掉，否则卡片会停在旧问题上，
              // 屏幕已经翻过去了它还在答上一句。等写完再补一次。
              const fire = () => {
                if (!autoReplyRef.current) return;
                if (draftingRef.current) {
                  autoTimer.current = window.setTimeout(fire, 300);
                  return;
                }
                const id = autoDraftId.current ?? draftId.current++;
                autoDraftId.current = id;
                answeringRef.current = spoken.slice(0, 40);
                runDraftRef.current?.(AUTO_INSTRUCTION, "fast", id, true);
              };
              autoTimer.current = window.setTimeout(fire, 250);
            }
            break;
          }
          case "status":
            if (msg.state === "live") setLink(pausedRef.current ? "paused" : "live");
            else if (msg.state === "reconnecting") setLink("reconnecting");
            break;
          case "meeting":
            setMeetingId(msg.meetingId ?? null);
            break;
          default:
            break;
        }
      };
      ws.onclose = () => {
        if (capRef.current) {
          setNotice("与本机服务的连接断开了，记录已停止。");
          stop();
        }
      };

      const cap = new AudioCapture();
      await cap.start(
        source,
        (pcm, p) => {
          if (pausedRef.current) {
            setPeak(0);
            return;
          }
          setPeak(p);
          if (ws.readyState === WebSocket.OPEN) ws.send(pcm);
        },
        () => {
          setNotice("共享已经停止，记录也停了。");
          stop();
        },
        micId || undefined,
      );
      capRef.current = cap;
    } catch (e) {
      teardown();
      setLink("idle");
      setNotice(e instanceof Error ? e.message : String(e));
    } finally {
      startGate.current = false;
    }
  }, [source, micId, stop, teardown]);

  const runDraft = useCallback(
    async (instruction: string, quality: "fast" | "good", replaceId?: number, auto = false) => {
      const id = replaceId ?? draftId.current++;
      setDrafting(true);
      setDrafts((prev) => {
        const next: Draft = {
          id,
          instruction,
          answering: auto ? answeringRef.current : undefined,
          en: "",
          zh: "",
          done: false,
          refined: quality === "good",
          auto,
        };
        // replaceId 指向的卡可能还没建（自动建议第一次），没有就追加
        return prev.some((d) => d.id === id)
          ? prev.map((d) => (d.id === id ? next : d))
          : [...prev, next];
      });

      const history = drafts.flatMap((d) => [
        { role: "user", text: d.instruction },
        { role: "model", text: d.en },
      ]);

      try {
        const r = await fetch("/api/draft", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ instruction, history, quality }),
        });
        if (!r.ok || !r.body) throw new Error(`服务端返回 ${r.status}`);
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let full = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";
          for (const line of lines) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (!payload || payload === "[DONE]") continue;
            const parsed = JSON.parse(payload) as { text?: string; error?: string };
            if (parsed.error) throw new Error(parsed.error);
            full += parsed.text ?? "";
            const cut = full.indexOf("---ZH---");
            const en = (cut >= 0 ? full.slice(0, cut) : full).trim();
            const zh = cut >= 0 ? full.slice(cut + 8).trim() : "";
            setDrafts((prev) => prev.map((d) => (d.id === id ? { ...d, en, zh } : d)));
          }
        }
        setDrafts((prev) => prev.map((d) => (d.id === id ? { ...d, done: true } : d)));
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        setDrafts((prev) =>
          prev.map((d) => (d.id === id ? { ...d, done: true, error: `没写出来：${message}` } : d)),
        );
      } finally {
        setDrafting(false);
      }
    },
    [drafts],
  );

  useEffect(() => {
    runDraftRef.current = runDraft;
  }, [runDraft]);

  const bars = [0.06, 0.16, 0.32];

  return (
    <div className="app">
      <header className="topbar">
        <h1>{TODAY} 记录</h1>
        <div className="pipe" />
        <div className="status">
          <span className={`dot ${link === "live" ? "live" : link === "reconnecting" ? "warn" : ""}`} />
          {STATE_LABEL[link]}
        </div>
        {langs && (
          <div className="status">
            <span>自动识别 →</span>
            <select
              value={langs.targetLang.split("-")[0]}
              disabled={running}
              title={running ? "字幕语种在开始记录时就定了，停止后才能改" : "字幕译文语种"}
              onChange={(e) => setLang("target_lang", e.target.value)}
            >
              {langs.targetLangs.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.label}字幕
                </option>
              ))}
            </select>
            <select
              value={langs.replyLang}
              title="拟稿用哪种语言写"
              onChange={(e) => setLang("reply_lang", e.target.value)}
            >
              {langs.replyLangs.map((l) => (
                <option key={l.code} value={l.code}>
                  拟稿用{l.label}
                </option>
              ))}
            </select>
          </div>
        )}
        <span className="spacer" />
        <button className="chip" onClick={() => setSettingsOpen(true)}>
          模型设置
          {health && !health.ok ? "　未配好" : ""}
        </button>
        <button className="chip" onClick={() => setSheetOpen(true)}>
          会议底稿
          {ctxInfo && ctxInfo.sources.length > 0 ? `　${ctxInfo.sources.length} 份` : "　未设置"}
        </button>
      </header>

      <div className="workspace">
        <section className="column">
          <div className="column-head">
            <span className="label">笔录</span>
            <span className="spacer" />
            <button className="mini" onClick={() => setZoom((z) => Math.max(0.85, z - 0.12))}>
              A−
            </button>
            <button className="mini" onClick={() => setZoom((z) => Math.min(1.4, z + 0.12))}>
              A＋
            </button>
          </div>

          {health && !health.ok && (
            <p className="notice" style={{ margin: "0 24px 12px" }}>
              {health.detail}。点右上角“模型设置”填 base URL、API key 和 model name。
            </p>
          )}

          {notice && (
            <p className="notice" style={{ margin: "0 24px 12px" }}>
              {notice}
            </p>
          )}

          <div
            className="record-wrap"
            style={
              {
                "--src-size": `${(18 * zoom).toFixed(1)}px`,
                "--tgt-size": `${(14 * zoom).toFixed(1)}px`,
              } as React.CSSProperties
            }
          >
            <Transcript entries={entries} live={live} running={running} />

            <div className="recorder">
              {running ? (
                <>
                  <button className="rec-btn stop" onClick={stop}>
                    <span className="seal-square" aria-hidden />
                    停止记录
                  </button>
                  <button className="rec-btn pause" onClick={togglePause}>
                    {link === "paused" ? "继续" : "暂停"}
                  </button>
                </>
              ) : (
                <button className="rec-btn" onClick={start}>
                  开始记录
                </button>
              )}
              <span className="timer">{clock(elapsed)}</span>
              <span className="meter" aria-hidden>
                {bars.map((threshold) => (
                  <i
                    key={threshold}
                    className={running && peak > threshold ? "on" : ""}
                    style={{ height: `${Math.min(100, 26 + peak * 240)}%` }}
                  />
                ))}
              </span>
              <div className="pipe" />
              <select
                value={source}
                disabled={running}
                onChange={(e) => setSource(e.target.value as SourceMode)}
              >
                {(Object.keys(SOURCE_LABEL) as SourceMode[]).map((m) => (
                  <option key={m} value={m}>
                    {SOURCE_LABEL[m]}
                  </option>
                ))}
              </select>
              {source !== "tab" && mics.length > 0 && (
                <select
                  value={micId}
                  disabled={running}
                  onChange={(e) => setMicId(e.target.value)}
                  title="用哪个麦克风。iPhone 被系统设成默认输入时在这里换回本机麦克风"
                >
                  {mics.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.label}
                    </option>
                  ))}
                </select>
              )}
            </div>
          </div>
        </section>

        <section className="column draft-col">
          <div className="column-head">
            <span className="label">拟稿</span>
            <span className="spacer" />
            {ctxInfo && ctxInfo.glossarySize > 0 && (
              <span className="label">术语锁定 {ctxInfo.glossarySize} 条</span>
            )}
            <button className="mini" onClick={() => setAutoReply((v) => !v)}>
              {autoReply ? "自动回应 开" : "自动回应 关"}
            </button>
          </div>
          <Drafting
            drafts={drafts}
            busy={drafting}
            onAsk={(instruction) => runDraft(instruction, "fast")}
            onRefine={(d) => runDraft(d.instruction, "good", d.id)}
          />
        </section>
      </div>

      {minutesOpen && meetingId !== null && (
        <MinutesSheet meetingId={meetingId} onClose={() => setMinutesOpen(false)} />
      )}

      {settingsOpen && (
        <SettingsSheet onClose={() => setSettingsOpen(false)} onSaved={refreshHealth} />
      )}

      {sheetOpen && (
        <ContextSheet
          info={ctxInfo}
          onClose={() => setSheetOpen(false)}
          onUploaded={(info) => {
            setCtxInfo(info);
            const ws = wsRef.current;
            if (ws && ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: "reload_context" }));
            }
          }}
        />
      )}
    </div>
  );
}
