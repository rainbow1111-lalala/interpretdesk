import { useCallback, useEffect, useRef, useState } from "react";
import { AudioCapture, listMics, preferredMic, type MicDevice, type SourceMode } from "./audio";
import { ContextSheet } from "./ContextSheet";
import { Drafting } from "./Drafting";
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
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [drafting, setDrafting] = useState(false);
  // 段落收口后右栏自动写建议回复；可关。ws 回调里读不到最新 state，用 ref 镜像
  const [autoReply, setAutoReply] = useState(true);
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
  const running = link === "live" || link === "starting" || link === "reconnecting";

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
  }, [refreshHealth]);

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
  }, [teardown]);

  const start = useCallback(async () => {
    if (startGate.current) return;
    startGate.current = true;
    setNotice("");
    setLink("starting");
    setElapsed(0);
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
            if (autoReplyRef.current && !t.echo && spoken.length >= 12) {
              window.clearTimeout(autoTimer.current);
              autoTimer.current = window.setTimeout(() => {
                if (draftingRef.current || !autoReplyRef.current) return;
                const id = autoDraftId.current ?? draftId.current++;
                autoDraftId.current = id;
                runDraftRef.current?.(AUTO_INSTRUCTION, "fast", id, true);
              }, 250);
            }
            break;
          }
          case "status":
            if (msg.state === "live") setLink("live");
            else if (msg.state === "reconnecting") setLink("reconnecting");
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
        <div className="status">英 → 中</div>
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
                <button className="rec-btn stop" onClick={stop}>
                  <span className="seal-square" aria-hidden />
                  停止记录
                </button>
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
