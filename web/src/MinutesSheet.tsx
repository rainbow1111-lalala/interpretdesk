import { useState } from "react";

type Props = {
  meetingId: number;
  turns: number;
  onClose: () => void;
};

export function MinutesSheet({ meetingId, turns, onClose }: Props) {
  const [markdown, setMarkdown] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const generate = async () => {
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
  };

  const link = (suffix: string) => `/api/meetings/${meetingId}/minutes.${suffix}`;

  return (
    <div className="sheet-bg" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <h2>会议纪要</h2>
        <p className="hint">
          这场会议记录了 {turns} 段对话。纪要由强档模型整理，含会议标题、时间、参会人和按议题
          归类的讨论内容；文末附完整的逐句原话与译文，那一部分由程序直接从记录生成，不经模型改写。
        </p>

        <div className="row">
          <button className="send" disabled={busy} onClick={generate}>
            {busy ? "正在整理，约需十几秒" : markdown ? "重新生成" : "生成会议纪要"}
          </button>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="mini" onClick={onClose}>
            关闭
          </button>
        </div>

        {error && <p className="notice">{error}</p>}

        <div className="row" style={{ marginTop: 16 }}>
          <span className="label">下载</span>
          <a className="mini" href={link("md")}>
            Markdown
          </a>
          <a className="mini" href={link("docx")}>
            Word
          </a>
          <a className="mini" href={link("pdf")}>
            PDF
          </a>
        </div>
        <p className="hint" style={{ margin: "8px 0 0" }}>
          没有生成纪要时，下载的文件只含逐句记录。Word 与 PDF 按仿宋小四、行距 26 的成稿格式排版。
        </p>

        {markdown && (
          <div className="brief">
            <dl>
              <dt>预览</dt>
              <dd>
                <pre className="minutes-preview">{markdown}</pre>
              </dd>
            </dl>
          </div>
        )}
      </div>
    </div>
  );
}
