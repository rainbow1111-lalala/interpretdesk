import { useState } from "react";
import type { ContextInfo } from "./types";

export function ContextSheet({
  info,
  onClose,
  onUploaded,
}: {
  info: ContextInfo | null;
  onClose: () => void;
  onUploaded: (info: ContextInfo) => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [note, setNote] = useState("");
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  const submit = async () => {
    if (busy || (files.length === 0 && !note.trim())) return;
    setBusy(true);
    setError("");
    setDone("");
    const body = new FormData();
    files.forEach((f) => body.append("files", f));
    body.append("note", note);
    try {
      const r = await fetch("/api/context", { method: "POST", body });
      if (!r.ok) {
        const detail = await r.json().then((j) => j?.detail).catch(() => "");
        throw new Error(detail || `服务端返回 ${r.status}`);
      }
      const next = (await r.json()) as ContextInfo;
      onUploaded(next);
      const bad = next.sources.filter((x) => x.includes("失败"));
      setDone(
        `读好了：底稿共 ${next.docs.length} 份原文，术语锁定 ${next.glossarySize} 条，` +
          `原文索引 ${next.indexChunks} 块，会议中可按内容检索。`,
      );
      if (bad.length > 0) setError(`这些文件没读进来：${bad.join("；")}`);
      setFiles([]);
      setNote("");
    } catch (e) {
      setError(`读取失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="sheet-bg" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <h2>会议底稿</h2>
        <p className="hint">
          上传这场会议的背景文件，我会读出当事人、争点和术语译法。术语表会用来校正字幕里的译法，
          也会作为拟英文回复的依据。
        </p>

        <label
          className={over ? "drop over" : "drop"}
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setOver(false);
            setFiles([...files, ...Array.from(e.dataTransfer.files)]);
          }}
        >
          把 PDF、Word 或 txt 拖进来，或者点这里选文件
          <input
            type="file"
            multiple
            accept=".pdf,.docx,.txt,.md"
            style={{ display: "none" }}
            onChange={(e) => setFiles([...files, ...Array.from(e.target.files ?? [])])}
          />
        </label>

        {files.map((f, i) => (
          <div className="file-row" key={`${f.name}-${i}`}>
            <span style={{ flex: 1 }}>{f.name}</span>
            <span className="label">{Math.max(1, Math.round(f.size / 1024))} KB</span>
            <button className="mini" onClick={() => setFiles(files.filter((_, j) => j !== i))}>
              移除
            </button>
          </div>
        ))}

        <textarea
          value={note}
          placeholder="口述补充：这次会议要谈什么，我方立场和底线是什么"
          onChange={(e) => setNote(e.target.value)}
        />

        <div style={{ display: "flex", gap: 8, marginTop: 12, alignItems: "center" }}>
          <button className="send" disabled={busy || (files.length === 0 && !note.trim())} onClick={submit}>
            {busy ? "正在读，长文件要等一会" : "读进来"}
          </button>
          <button
            className="mini"
            onClick={async () => {
              const r = await fetch("/api/context/reload", { method: "POST" });
              if (r.ok) onUploaded((await r.json()) as ContextInfo);
            }}
          >
            重新读取 data/context.json
          </button>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="mini" onClick={onClose}>
            关闭
          </button>
        </div>

        {error && <p className="notice">{error}</p>}
        {done && <p className="ok-note">{done}</p>}

        {info && info.docs.length > 0 && (
          <div className="brief">
            <dl>
              <dt>已存原文（上传是累加，不会覆盖）</dt>
              <dd>
                {info.docs.map((d) => (
                  <div className="file-row" key={d.name}>
                    <span style={{ flex: 1 }}>{d.name}</span>
                    <span className="label">{d.chars.toLocaleString()} 字</span>
                    <button
                      className="mini"
                      onClick={async () => {
                        const r = await fetch(
                          `/api/context/docs/${encodeURIComponent(d.name)}`,
                          { method: "DELETE" },
                        );
                        if (r.ok) {
                          onUploaded((await r.json()) as ContextInfo);
                          setDone("删掉了。摘要和索引要重新点读进来才会更新。");
                        }
                      }}
                    >
                      删除
                    </button>
                  </div>
                ))}
                <div className="row">
                  <button
                    className="mini"
                    onClick={async () => {
                      // 会前底稿是手工整理的，清空前问一句。原文仍会留在 data/trash 可取回
                      if (
                        !window.confirm(
                          "清空全部底稿？原文和摘要会挪进 data/trash 保留，可以取回。",
                        )
                      ) {
                        return;
                      }
                      const r = await fetch("/api/context", { method: "DELETE" });
                      if (r.ok) {
                        onUploaded((await r.json()) as ContextInfo);
                        setDone("底稿已清空，原文和摘要挪进了 data/trash，需要时可取回。");
                      }
                    }}
                  >
                    清空全部底稿
                  </button>
                </div>
              </dd>
            </dl>
          </div>
        )}

        {info && (info.matter || info.terms.length > 0) && (
          <div className="brief">
            <dl>
              {info.myPosition && (
                <>
                  <dt>我方立场与底线</dt>
                  <dd>{info.myPosition}</dd>
                </>
              )}
              {info.matter && (
                <>
                  <dt>事项</dt>
                  <dd>{info.matter}</dd>
                </>
              )}
              {info.parties.length > 0 && (
                <>
                  <dt>当事人</dt>
                  <dd>
                    {info.parties.map((p, i) => (
                      <div key={i}>
                        {p.en}　{p.zh}
                        {p.role ? `　${p.role}` : ""}
                      </div>
                    ))}
                  </dd>
                </>
              )}
              {info.issues.length > 0 && (
                <>
                  <dt>争点</dt>
                  <dd>
                    {info.issues.map((s, i) => (
                      <div key={i}>{s}</div>
                    ))}
                  </dd>
                </>
              )}
              {info.terms.length > 0 && (
                <>
                  <dt>术语锁定（{info.glossarySize} 条替换规则）</dt>
                  <dd>
                    <div className="terms">
                      {info.terms.map((t, i) => (
                        <div key={i} style={{ display: "contents" }}>
                          <span className="en">{t.en}</span>
                          <span className="zh">{t.zh}</span>
                        </div>
                      ))}
                    </div>
                  </dd>
                </>
              )}
              {info.sources.length > 0 && (
                <>
                  <dt>来源</dt>
                  <dd>{info.sources.join("、")}</dd>
                </>
              )}
            </dl>
          </div>
        )}
      </div>
    </div>
  );
}
