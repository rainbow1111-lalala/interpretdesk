import { useCallback, useEffect, useState } from "react";

export function MinutesSheet({ meetingId, onClose }: { meetingId: number; onClose: () => void }) {
  const [markdown, setMarkdown] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const generate = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const r = await fetch(`/api/meetings/${meetingId}/minutes`, { method: "POST" });
      if (!r.ok) {
        const detail = await r.json().then((j) => j?.detail).catch(() => "");
        throw new Error(detail || `服务端返回 ${r.status}`);
      }
      setMarkdown(((await r.json()).markdown ?? "") as string);
    } catch (e) {
      setError(`生成失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }, [meetingId]);

  // 停止记录就开始整理，不等指令
  useEffect(() => {
    generate();
  }, [generate]);

  const link = (suffix: string) => `/api/meetings/${meetingId}/minutes.${suffix}`;

  return (
    <div className="sheet-bg" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <h2>会议纪要</h2>

        {busy && <p className="test">正在整理…</p>}
        {error && <p className="notice">{error}</p>}

        <div className="row">
          <span className="label">下载</span>
          <a className="mini" href={link("docx")}>
            Word
          </a>
          <a className="mini" href={link("pdf")}>
            PDF
          </a>
          <a className="mini" href={link("md")}>
            Markdown
          </a>
          <span className="spacer" style={{ flex: 1 }} />
          {!busy && (
            <button className="mini" onClick={generate}>
              重新生成
            </button>
          )}
          <button className="mini" onClick={onClose}>
            关闭
          </button>
        </div>

        {markdown && (
          <div className="brief">
            <pre className="minutes-preview">{markdown}</pre>
          </div>
        )}
      </div>
    </div>
  );
}
