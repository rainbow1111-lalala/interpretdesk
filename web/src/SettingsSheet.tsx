import { useEffect, useState } from "react";

type Engine = { base_url: string; api_key: string; model: string; has_key?: boolean };

type SettingsData = {
  text: Engine;
  text_model_strong: string;
  embed_model: string;
  context_full_chars: number;
  speech_engine: string;
  speech: Engine;
  target_lang: string;
  chunk_seconds: number;
};

type EngineMeta = {
  label: string;
  implemented?: boolean;
  single_stream?: boolean;
  note?: string;
  defaults: { base_url: string; model: string };
};

type TestResult = { ok: boolean; error?: string; reply?: string; seconds?: number; url?: string };

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <label className="field">
      <span className="label">{label}</span>
      <input
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        autoComplete="off"
      />
    </label>
  );
}

export function SettingsSheet({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [data, setData] = useState<SettingsData | null>(null);
  const [engines, setEngines] = useState<Record<string, EngineMeta>>({});
  const [textKey, setTextKey] = useState("");
  const [speechKey, setSpeechKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [tests, setTests] = useState<Record<string, TestResult | "running">>({});
  const [models, setModels] = useState<string[]>([]);

  useEffect(() => {
    fetch("/api/settings")
      .then((r) => r.json())
      .then((j) => {
        setData(j.settings);
        setEngines(j.engines);
      })
      .catch(() => {});
  }, []);

  const patch = (fn: (d: SettingsData) => SettingsData) => setData((d) => (d ? fn(d) : d));

  const save = async (): Promise<boolean> => {
    if (!data) return false;
    setSaving(true);
    try {
      const body = {
        ...data,
        text: { ...data.text, api_key: textKey },
        speech: { ...data.speech, api_key: speechKey },
      };
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) return false;
      const j = await r.json();
      setData(j.settings);
      setTextKey("");
      setSpeechKey("");
      onSaved();
      return true;
    } finally {
      setSaving(false);
    }
  };

  const runTest = async (target: string) => {
    // 先存再测，否则测的是上一次存进去的配置
    if (!(await save())) {
      setTests((t) => ({ ...t, [target]: { ok: false, error: "保存失败，没测" } }));
      return;
    }
    setTests((t) => ({ ...t, [target]: "running" }));
    try {
      const r = await fetch("/api/settings/test", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ target }),
      });
      const outcome = (await r.json()) as TestResult;
      setTests((t) => ({ ...t, [target]: outcome }));
    } catch (e) {
      setTests((t) => ({
        ...t,
        [target]: { ok: false, error: e instanceof Error ? e.message : String(e) },
      }));
    }
  };

  const loadModels = async () => {
    if (!(await save())) return;
    const r = await fetch("/api/settings/models");
    setModels(((await r.json()).models ?? []) as string[]);
  };

  if (!data) return null;

  const meta = engines[data.speech_engine];

  const result = (key: string) => {
    const t = tests[key];
    if (!t) return null;
    if (t === "running") return <p className="test">正在测…</p>;
    if (t.ok)
      return (
        <p className="test ok">
          已连通{t.seconds ? `，耗时 ${t.seconds} 秒` : ""}
          {t.reply ? `，回复“${t.reply}”` : ""}
        </p>
      );
    return <p className="test bad">{t.error || "没通"}</p>;
  };

  return (
    <div className="sheet-bg" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <h2>模型设置</h2>

        <div className="brief" style={{ borderTop: "none", paddingTop: 0 }}>
          <dl>
            <dt>文本模型（底稿提炼、字幕译文、拟稿）</dt>
            <dd>
              <Field
                label="base url"
                value={data.text.base_url}
                placeholder="https://xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
                onChange={(v) => patch((d) => ({ ...d, text: { ...d.text, base_url: v } }))}
              />
              <Field
                label="api key"
                type="password"
                value={textKey}
                placeholder={data.text.has_key ? "已填，留空表示不改" : "粘贴 key"}
                onChange={setTextKey}
              />
              <Field
                label="model name 快档（会议中拟稿用，要快）"
                value={data.text.model}
                placeholder="qwen-flash"
                onChange={(v) => patch((d) => ({ ...d, text: { ...d.text, model: v } }))}
              />
              <Field
                label="model name 强档（底稿提炼与换强模型重写，留空则同快档）"
                value={data.text_model_strong}
                placeholder="qwen-max"
                onChange={(v) => patch((d) => ({ ...d, text_model_strong: v }))}
              />
              <Field
                label="向量模型（底稿原文检索用，走同一个端点）"
                value={data.embed_model}
                placeholder="text-embedding-v4"
                onChange={(v) => patch((d) => ({ ...d, embed_model: v }))}
              />
              <Field
                label="底稿原文直接进上下文的字数上限（超出部分改用检索补）"
                value={String(data.context_full_chars)}
                placeholder="20000"
                onChange={(v) =>
                  patch((d) => ({ ...d, context_full_chars: Number(v.replace(/\D/g, "")) || 0 }))
                }
              />
              <div className="row">
                <button className="mini" onClick={() => runTest("text")}>
                  测试快档
                </button>
                <button className="mini" onClick={() => runTest("text_strong")}>
                  测试强档
                </button>
                <button className="mini" onClick={loadModels}>
                  拉模型清单
                </button>
              </div>
              {result("text")}
              {result("text_strong")}
              {models.length > 0 && (
                <p className="hint" style={{ margin: "8px 0 0" }}>
                  这个 key 能用的模型：{models.join("、")}
                </p>
              )}
            </dd>

            <dt>语音引擎（实时字幕）</dt>
            <dd>
              <label className="field">
                <span className="label">引擎</span>
                <select
                  value={data.speech_engine}
                  onChange={(e) => {
                    const key = e.target.value;
                    const d = engines[key]?.defaults;
                    patch((s) => ({
                      ...s,
                      speech_engine: key,
                      speech: {
                        ...s.speech,
                        base_url: s.speech.base_url || d?.base_url || "",
                        model: d?.model || s.speech.model,
                      },
                    }));
                  }}
                >
                  {Object.entries(engines).map(([key, m]) => (
                    <option key={key} value={key} disabled={!m.implemented}>
                      {m.label}
                      {m.implemented ? "" : "（未接线）"}
                    </option>
                  ))}
                </select>
              </label>
              {meta?.note && (
                <p className="hint" style={{ margin: "8px 0 0" }}>
                  {meta.note}
                  {meta.single_stream ? "" : " 这一档不是单流，译文会再慢半拍。"}
                </p>
              )}
              <Field
                label="base url"
                value={data.speech.base_url}
                placeholder="https://xxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
                onChange={(v) => patch((d) => ({ ...d, speech: { ...d.speech, base_url: v } }))}
              />
              <Field
                label="api key"
                type="password"
                value={speechKey}
                placeholder={data.speech.has_key ? "已填，留空表示不改" : "粘贴 key"}
                onChange={setSpeechKey}
              />
              <Field
                label="model name"
                value={data.speech.model}
                placeholder="qwen3.5-livetranslate-flash-realtime"
                onChange={(v) => patch((d) => ({ ...d, speech: { ...d.speech, model: v } }))}
              />
              <Field
                label="译文语言"
                value={data.target_lang}
                placeholder="zh-CN"
                onChange={(v) => patch((d) => ({ ...d, target_lang: v }))}
              />
              <div className="row">
                <button className="mini" onClick={() => runTest("speech")}>
                  测试连通
                </button>
              </div>
              {result("speech")}
            </dd>
          </dl>
        </div>

        <div className="row" style={{ marginTop: 20 }}>
          <button className="send" disabled={saving} onClick={save}>
            {saving ? "正在存" : "保存"}
          </button>
          <span className="spacer" style={{ flex: 1 }} />
          <button className="mini" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
