import { useEffect, useState } from "react";
import type { ContextInfo, Profile } from "./types";

const BLANK_PROFILE: Profile = { identity: "", goal: "", facts: "", noCommit: "" };

// 四项都由使用者自己填。程序不从材料里提炼身份，也不按行业惯例替他补一个：
// 猜错身份的代价是整场会的口径都偏了。
const PROFILE_FIELDS: { key: keyof Profile; label: string; placeholder: string }[] = [
  { key: "identity", label: "我的身份",
    placeholder: "例如：某制造企业的采购经理，代表买方" },
  { key: "goal", label: "本场目标",
    placeholder: "例如：把单价压到十二元以内，账期争取到六十天" },
  { key: "facts", label: "已确认的事实",
    placeholder: "可以直接引用的事实，例如：上一批单价十四元，年采购量约三十万件" },
  { key: "noCommit", label: "不可承诺的事项",
    placeholder: "例如：不承诺全年采购量，不确认交付日期，不代表总部表态" },
];

export function ContextSheet({
  info,
  profile,
  onProfileSaved,
  onClose,
  onUploaded,
}: {
  info: ContextInfo | null;
  profile: Profile | null;
  onProfileSaved: (p: Profile) => void;
  onClose: () => void;
  // briefingReset 表示这一次是换掉或清空了底稿，不是给同一场会补材料
  onUploaded: (info: ContextInfo, briefingReset?: boolean) => void;
}) {
  const [draftProfile, setDraftProfile] = useState<Profile>(profile ?? BLANK_PROFILE);
  const [profileState, setProfileState] = useState<"idle" | "saving" | "saved">("idle");
  const [provider, setProvider] = useState("");

  useEffect(() => {
    setDraftProfile(profile ?? BLANK_PROFILE);
  }, [profile]);

  // 材料会发给哪一家，写实填进去而不是写一句套话。存在哪、交给谁处理，是两件事。
  useEffect(() => {
    fetch("/api/settings")
      .then((r) => r.json())
      .then((d) => {
        try {
          setProvider(new URL(d.settings?.text?.base_url ?? "").hostname);
        } catch {
          setProvider("");
        }
      })
      .catch(() => {});
  }, []);

  const saveProfile = async () => {
    setProfileState("saving");
    try {
      const r = await fetch("/api/conversation/profile", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(draftProfile),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || `服务端返回 ${r.status}`);
      onProfileSaved(d.profile);
      setProfileState("saved");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setProfileState("idle");
    }
  };
  const [files, setFiles] = useState<File[]>([]);
  const [note, setNote] = useState("");
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");
  // 底稿是一场会一份。默认换新，免得上一场的材料留在里面混进拟稿；同一场会分批补材料时
  // 手动切到追加
  const [replace, setReplace] = useState(true);
  const existing = info?.docs.length ?? 0;

  const submit = async () => {
    if (busy || (files.length === 0 && !note.trim())) return;
    if (replace && existing > 0) {
      const names = (info?.docs ?? []).map((d) => d.name).join("、");
      if (
        !window.confirm(
          `换成新底稿？现有 ${existing} 份原文（${names}）会挪进回收站保留，可以取回。` +
            `\n\n如果这是同一场会补材料，点取消，改选「补进现有底稿」。`,
        )
      ) {
        return;
      }
    }
    setBusy(true);
    setError("");
    setDone("");
    const body = new FormData();
    files.forEach((f) => body.append("files", f));
    body.append("note", note);
    body.append("replace", replace ? "true" : "false");
    try {
      const r = await fetch("/api/context", { method: "POST", body });
      if (!r.ok) {
        const detail = await r.json().then((j) => j?.detail).catch(() => "");
        throw new Error(detail || `服务端返回 ${r.status}`);
      }
      const next = (await r.json()) as ContextInfo;
      onUploaded(next, replace && existing > 0);
      const bad = next.sources.filter((x) => x.includes("失败"));
      setDone(
        `${replace && existing > 0 ? "换好了（旧底稿在回收站可取回）：" : "读好了："}` +
          `底稿共 ${next.docs.length} 份原文，术语锁定 ${next.glossarySize} 条，` +
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

        <section className="profile">
          <h3>会前交代</h3>
          <p className="hint">
            这几项由你自己填。不填我就什么都不假设，不会替你安一个身份或立场。
          </p>
          {PROFILE_FIELDS.map((f) => (
            <label className="profile-row" key={f.key}>
              <span>{f.label}</span>
              <textarea
                rows={2}
                value={draftProfile[f.key]}
                placeholder={f.placeholder}
                onChange={(e) => {
                  setDraftProfile({ ...draftProfile, [f.key]: e.target.value });
                  setProfileState("idle");
                }}
              />
            </label>
          ))}
          <button className="chip" disabled={profileState === "saving"} onClick={saveProfile}>
            {profileState === "saving" ? "正在存…" : profileState === "saved" ? "已存下" : "保存会前交代"}
          </button>
        </section>

        <h3>背景文件</h3>
        <p className="hint">
          上传这场会议的背景文件，我会读出当事人、争点和术语译法。术语表会用来校正字幕里的译法，
          也会作为拟回复的依据。
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

        {existing > 0 && (
          <div className="row" style={{ marginTop: 10, gap: 16, flexWrap: "wrap" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input
                type="radio"
                checked={replace}
                onChange={() => setReplace(true)}
              />
              换成新底稿（换一场会，旧的挪进回收站）
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input
                type="radio"
                checked={!replace}
                onChange={() => setReplace(false)}
              />
              补进现有底稿（同一场会加材料）
            </label>
          </div>
        )}

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
            重新读取这场会议的摘要
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
              <dt>已存原文（默认换新，可在上方改成补进现有底稿）</dt>
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
                      // 会前底稿是手工整理的，清空前问一句。原文仍会留在回收站可取回
                      if (
                        !window.confirm(
                          "清空全部底稿？原文和摘要会挪进回收站保留，可以取回。",
                        )
                      ) {
                        return;
                      }
                      const r = await fetch("/api/context", { method: "DELETE" });
                      if (r.ok) {
                        onUploaded((await r.json()) as ContextInfo, true);
                        setDone(
                          "底稿已清空：原文、摘要、术语表、原文索引、检索片段和拟稿记录都清了，" +
                            "原文和摘要挪进回收站可取回。",
                        );
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

        <details className="storage">
          <summary>这些材料存在哪、交给谁处理</summary>
          <p>
            <b>存在哪：</b>
            上传的原文和提炼出的摘要，存在本程序服务器上这场会议自己的目录里，与你的其他
            会议分开，也与别人的会议分开。删除会先挪进回收站，可以取回。
          </p>
          <p>
            <b>交给谁处理：</b>
            提炼底稿、拟稿、生成纪要时，材料的相关部分会发给你在「模型设置」里填的那个模型
            服务商{provider ? `（当前是 ${provider}）` : ""}，由他们的服务器处理。不想外发的
            材料不要上传。
          </p>
        </details>

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
