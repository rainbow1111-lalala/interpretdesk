import { useCallback, useEffect, useRef, useState } from "react";
import { AudioCapture, canCaptureTab, listMics, preferredMic, type MicDevice, type SourceMode } from "./audio";
import { ContextSheet } from "./ContextSheet";
import { Drafting } from "./Drafting";
import { MinutesSheet } from "./MinutesSheet";
import { Onboarding } from "./Onboarding";
import { SettingsSheet } from "./SettingsSheet";
import { MeetingSheet } from "./MeetingSheet";
import { Readiness } from "./Readiness";
import { Transcript } from "./Transcript";
import { useSoleRecorder } from "./useSoleRecorder";
import type {
  ContextInfo, Directive, Draft, Entry, Evidence, LinkState, LiveEntry, MeetingSummary,
  Profile,
} from "./types";

// 顺序即下拉框里的顺序。日常用麦克风外放收音，省掉浏览器的共享面板那一步；
// 戴耳机开线上会时对方的声音进不了麦克风，那种场合才需要抓会议标签页。
const SOURCE_LABEL: Record<SourceMode, string> = {
  mic: "麦克风（外放收音）",
  tab: "会议标签页（戴耳机时用）",
  both: "混合声源",
};

const STATE_LABEL: Record<LinkState, string> = {
  idle: "待机",
  starting: "接入中",
  live: "正在同传",
  paused: "已暂停",
  reconnecting: "重连中",
  settling: "正在收尾",
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
  "对方刚说完左边这段话。直接回应对方刚才问的那个问题，像在会上当场接话，" +
  "帮我拟一段可以直接说出口的英文回复。" +
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
  const [source, setSource] = useState<SourceMode>("mic");
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
  const [meetings, setMeetings] = useState<MeetingSummary[]>([]);
  const [meetingOpen, setMeetingOpen] = useState(false);
  const [canAdopt, setCanAdopt] = useState(false);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [directives, setDirectives] = useState<Directive[]>([]);
  const [minutesOpen, setMinutesOpen] = useState(false);
  const { blocked: otherTabRecording, claim, release: releaseRecording } = useSoleRecorder();
  // 暂停时不再往上送音频。用 ref 是因为音频回调建立在 start 里，拿不到最新的 state
  const pausedRef = useRef(false);
  // 当前这张自动卡在回应哪一句，供卡片显示
  const answeringRef = useRef("");
  // stop 里要知道这场会有没有内容，读 state 会拿到闭包里的旧值，用 ref 跟着走
  const entryCount = useRef(0);
  const autoReplyRef = useRef(true);
  const draftingRef = useRef(false);
  const autoTimer = useRef<number | undefined>(undefined);
  // ws 回调建立在 start 里，而 runDraft 声明在 start 之后，经 ref 转一道避免引用顺序问题
  const runDraftRef = useRef<
    ((i: string, q: "fast" | "good", r?: number, a?: boolean) => void) | null
  >(null);

  const wsRef = useRef<WebSocket | null>(null);
  const capRef = useRef<AudioCapture | null>(null);
  const draftId = useRef(1);
  // 停止之后等服务端确认收尾的兜底定时器
  const settleTimer = useRef<number | undefined>(undefined);
  // 正在跑的那条拟稿流。新的要求一来就把旧的掐掉，旧流的字一个都不许写进新稿
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);
  // setLink 是异步生效的，连点两下「开始记录」会在按钮还没变灰前跑两遍 start，
  // 弹出两个共享面板。用 ref 同步挡住重入
  const startGate = useRef(false);
  const running = link !== "idle" && link !== "error";

  useEffect(() => {
    if (source === "tab" || mics.length > 0) return;
    // 只枚举不索权限。授权前拿不到设备名，列表可能为空，这时下拉框不出，
    // 等点「开始记录」时再要一次权限并按名字避开 iPhone
    listMics(false)
      .then((list) => {
        if (list.every((d) => !d.label)) return;
        setMics(list);
        setMicId((cur) =>
          cur && list.some((d) => d.id === cur) ? cur : preferredMic(list),
        );
      })
      .catch(() => undefined);
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
    // 把上一场会接回来：材料、会前交代、已有的笔录一起恢复
    fetch("/api/meetings/active")
      .then((r) => r.json())
      .then((d) => {
        setMeetingId(d.meetingId ?? null);
        setCanAdopt(Boolean(d.canAdopt));
        setCtxInfo(d.context ?? null);
        setProfile(d.conversation?.profile ?? null);
        setDirectives(d.conversation?.directives ?? []);
        if (Array.isArray(d.turns)) {
          setEntries(
            d.turns.map((x: { turnId: number; src: string; dst: string; srcLang: string; ts: number }) => ({
              turnId: x.turnId,
              pairs: [{ src: x.src, dst: x.dst }],
              srcLang: x.srcLang,
              echo: !x.dst,
              ts: x.ts,
            })),
          );
        }
      })
      .catch(() => {});
    fetch("/api/meetings")
      .then((r) => r.json())
      .then(setMeetings)
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

  // 收摊。只在异常路径上直接用，正常停止走 beginStop → ended → finishStop
  const teardown = useCallback(() => {
    capRef.current?.stop();
    capRef.current = null;
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.close();
    wsRef.current = null;
    window.clearTimeout(settleTimer.current);
    releaseRecording();
    setLive(null);
    setPeak(0);
  }, [releaseRecording]);

  // 服务端确认收尾完成（或兜底超时）之后才收场。settled=false 表示没等到确认，
  // 这时纪要可能少最后一句，得说出来而不是闷着。
  const finishStop = useCallback(
    (settled: boolean, turns?: number) => {
      teardown();
      setLink("idle");
      pausedRef.current = false;
      if (!settled) {
        setNotice("服务端没有确认收尾，最后一句可能没收全，纪要仍可生成。");
      }
      // 停下来就问一句要不要出纪要，会议刚结束是整理的最佳时机
      if ((turns ?? entryCount.current) > 0) setMinutesOpen(true);
    },
    [teardown],
  );

  // 点停止：先断音频、告诉服务端停，但连接留着等它把尾句收完。
  // 实测语音服务的原话比译文晚六秒多到，这里一关连接就等于把最后一句扔了。
  const stop = useCallback(() => {
    capRef.current?.stop();
    capRef.current = null;
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      finishStop(true);
      return;
    }
    ws.send(JSON.stringify({ type: "stop" }));
    setLink("settling");
    setPeak(0);
    // 服务端上限八秒，这里给到二十秒，网络再差也够；它没回就自己收场
    window.clearTimeout(settleTimer.current);
    settleTimer.current = window.setTimeout(() => finishStop(false), 20000);
  }, [finishStop]);

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

  // 开不了的两个原因都要写清楚，不能只把按钮置灰让人猜
  const startBlockReason = otherTabRecording
    ? "另一个标签页正在录这场会。"
    : meetingId === null
      ? "还没有会议。点右上角「会议」建一场，可以沿用你已经准备好的材料。"
      : "";
  const canStart = !startBlockReason;

  const start = useCallback(async () => {
    if (startGate.current || !canStart) return;
    startGate.current = true;
    claim();
    setNotice("");
    setLink("starting");
    setElapsed(0);
    pausedRef.current = false;
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
            // 对方讲完一段外语，右栏自动给一版建议回复。每次触发新建一张卡追加进对话流，
            // 旧卡保留可回看，不原地覆盖（实战反馈：上一版建议不能直接消失）。
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
                const id = draftId.current++;
                answeringRef.current = spoken.slice(0, 40);
                // 强档热连接首字实测 1.3-1.5 秒，达到快档同一档位，换更聪明的模型
                runDraftRef.current?.(AUTO_INSTRUCTION, "good", id, true);
              };
              autoTimer.current = window.setTimeout(fire, 250);
            }
            break;
          }
          case "status":
            if (msg.state === "live") setLink(pausedRef.current ? "paused" : "live");
            else if (msg.state === "reconnecting") setLink("reconnecting");
            else if (msg.state === "settling") setLink("settling");
            break;
          case "meeting":
            setMeetingId(msg.meetingId ?? null);
            break;
          case "ended":
            // 服务端已经把尾句落库了，到这里才算真的结束
            finishStop(Boolean(msg.settled), msg.turns);
            break;
          case "error":
            setNotice(msg.detail || "服务端拒绝了这次录音。");
            teardown();
            setLink("idle");
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

      // 进页面时没索权限，所以这里可能还没有设备名。趁这次点击（浏览器认的用户手势）
      // 要一次权限再挑，否则默认设备可能是连续互通的 iPhone，本机什么都收不到
      let useMic = micId;
      if (source !== "tab" && mics.length === 0) {
        const list = await listMics(true);
        setMics(list);
        useMic = micId && list.some((d) => d.id === micId) ? micId : preferredMic(list);
        setMicId(useMic);
      }

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
        useMic || undefined,
      );
      capRef.current = cap;
    } catch (e) {
      teardown();
      setLink("idle");
      setNotice(e instanceof Error ? e.message : String(e));
    } finally {
      startGate.current = false;
    }
  }, [source, micId, mics.length, stop, teardown, canStart, claim]);

  const runDraft = useCallback(
    async (instruction: string, quality: "fast" | "good", replaceId?: number, auto = false,
     mode?: "draft" | "ask") => {
      const id = replaceId ?? draftId.current++;
      // 手打的新要求必须能打断正在跑的自动稿。掐掉旧流并占一个新序号，
      // 旧流剩下的字据此被丢弃，不会盖住新稿。
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const seq = ++seqRef.current;
      const mine = () => seqRef.current === seq;
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
      // 会中指示不再从卡片里反推。猜出来的指示常常是错的：随口问一句也会被当成改口，
      // 真正的改口反而可能被截断窗口挤掉。现在只认用户点过「存为会中指示」的那些，
      // 服务端从这一场的 conversation.json 里读。

      try {
        const r = await fetch("/api/draft", {
          method: "POST",
          headers: { "content-type": "application/json" },
          signal: controller.signal,
          body: JSON.stringify({ instruction, history, quality,
                                 mode: auto ? "draft" : mode }),
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
            const parsed = JSON.parse(payload) as {
              text?: string; error?: string; evidence?: Evidence;
            };
            if (parsed.error) throw new Error(parsed.error);
            // 被新要求掐掉的旧流，剩下的字一律丢弃
            if (!mine()) continue;
            if (parsed.evidence) {
              const ev = parsed.evidence;
              setDrafts((prev) => prev.map((d) => (d.id === id ? { ...d, evidence: ev } : d)));
              continue;
            }
            full += parsed.text ?? "";
            const cut = full.indexOf("---ZH---");
            const en = (cut >= 0 ? full.slice(0, cut) : full).trim();
            const zh = cut >= 0 ? full.slice(cut + 8).trim() : "";
            setDrafts((prev) => prev.map((d) => (d.id === id ? { ...d, en, zh } : d)));
          }
        }
        if (mine()) {
          setDrafts((prev) => prev.map((d) => (d.id === id ? { ...d, done: true } : d)));
        }
      } catch (e) {
        if ((e as { name?: string })?.name === "AbortError") {
          setDrafts((prev) =>
            prev.map((d) => (d.id === id ? { ...d, done: true, error: "已被新的要求中断" } : d)),
          );
        } else {
          const message = e instanceof Error ? e.message : String(e);
          setDrafts((prev) =>
            prev.map((d) => (d.id === id ? { ...d, done: true, error: `没写出来：${message}` } : d)),
          );
        }
      } finally {
        if (mine()) setDrafting(false);
      }
    },
    [drafts],
  );

  useEffect(() => {
    runDraftRef.current = runDraft;
  }, [runDraft]);

  // 会中指示只认用户点过「存为会中指示」的那些，整表提交，服务端存进这一场的会议目录
  const saveDirective = useCallback(
    async (text: string) => {
      const next = [...directives, { text, savedAt: Date.now() / 1000 }];
      setDirectives(next);
      setNotice("已存为会中指示，之后的拟稿都会照它来。");
      try {
        const r = await fetch("/api/conversation/directives", {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ directives: next }),
        });
        const d = await r.json();
        if (r.ok) setDirectives(d.directives);
      } catch {
        setNotice("会中指示没存上，检查一下服务端。");
      }
    },
    [directives],
  );

  const bars = [0.06, 0.16, 0.32];

  return (
    <div className="app">
      <Onboarding />
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
        <button className="chip" onClick={() => setMeetingOpen(true)}>
          会议
          {meetingId ? `　#${meetingId}` : "　未开始"}
        </button>
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

          {link === "idle" && (
            <Readiness
              source={source}
              micId={micId}
              micLabel={mics.find((m) => m.id === micId)?.label ?? ""}
              ctxInfo={ctxInfo}
              profile={profile}
              hasMeeting={meetingId !== null}
            />
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
                  <button className="rec-btn stop" disabled={link === "settling"} onClick={stop}>
                    <span className="seal-square" aria-hidden />
                    {link === "settling" ? "正在收尾…" : "停止记录"}
                  </button>
                  <button className="rec-btn pause" disabled={link === "settling"}
                          onClick={togglePause}>
                    {link === "paused" ? "继续" : "暂停"}
                  </button>
                </>
              ) : (
                <button className="rec-btn" disabled={!canStart} title={startBlockReason}
                        onClick={start}>
                  开始记录
                </button>
              )}
              {!running && otherTabRecording && (
                // 「还没有会议」那条已经写在会前检查面板里了，这里只补面板没覆盖的那一种
                <span className="rec-block">另一个标签页正在录</span>
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
                {(Object.keys(SOURCE_LABEL) as SourceMode[])
                  .filter((m) => m === "mic" || canCaptureTab())
                  .map((m) => (
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
            directives={directives}
            onAsk={(instruction, mode) => runDraft(instruction, "fast", undefined, false, mode)}
            onRefine={(d) => runDraft(d.instruction, "good", d.id)}
            onSaveDirective={saveDirective}
          />
        </section>
      </div>

      {minutesOpen && meetingId !== null && (
        <MinutesSheet meetingId={meetingId} onClose={() => setMinutesOpen(false)} />
      )}

      {settingsOpen && (
        <SettingsSheet onClose={() => setSettingsOpen(false)} onSaved={refreshHealth} />
      )}

      {meetingOpen && (
        <MeetingSheet
          meetings={meetings}
          meetingId={meetingId}
          canAdopt={canAdopt}
          docCount={ctxInfo?.docs.length ?? 0}
          running={running}
          onClose={() => setMeetingOpen(false)}
          onSwitched={({ meetingId: id, context, profile: prof, turns, fresh }) => {
            // 换会议是整套换：材料、会前交代、笔录、拟稿卡片一起换。
            // 拟稿卡片会作为对话历史发给模型，留着上一场的就会继续影响这一场。
            setMeetingId(id);
            setCtxInfo(context);
            setProfile(prof);
            setDrafts([]);
            setCanAdopt(false);
            setEntries(
              (turns ?? []).map((x) => ({
                turnId: x.turnId,
                pairs: [{ src: x.src, dst: x.dst }],
                srcLang: x.srcLang,
                echo: !x.dst,
                ts: x.ts,
              })),
            );
            setNotice(fresh ? "" : "已换成这一场自己的底稿与会前交代。");
            fetch("/api/meetings").then((r) => r.json()).then(setMeetings).catch(() => {});
          }}
        />
      )}

      {sheetOpen && (
        <ContextSheet
          info={ctxInfo}
          profile={profile}
          onProfileSaved={setProfile}
          onClose={() => setSheetOpen(false)}
          onUploaded={(info, briefingReset) => {
            setCtxInfo(info);
            // 换掉或清空底稿等于换一场会。拟稿卡片会作为对话历史发给模型，旧底稿写出来的
            // 那几版留着就会继续影响后面的拟稿，一并清掉；同一场会补材料时不动
            if (briefingReset) setDrafts([]);
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
