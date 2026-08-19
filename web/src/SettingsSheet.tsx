import { useEffect, useState } from "react";

type Engine = { base_url: string; api_key: string; model: string; has_key?: boolean };

/** 服务商预设。只预填 base url，不写死模型名：模型名会过时，用「拉模型清单」从端点真拉。
 *  speech 标的是这家能不能做实时字幕，用来提醒用户语音层要不要另找一家。 */
const PROVIDERS: { label: string; base_url: string; speech: boolean }[] = [
  { label: "阿里云百炼", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", speech: true },
  { label: "智谱", base_url: "https://open.bigmodel.cn/api/paas/v4", speech: false },
  { label: "DeepSeek", base_url: "https://api.deepseek.com/v1", speech: false },
  { label: "OpenAI", base_url: "https://api.openai.com/v1", speech: false },
];

function providerOf(baseUrl: string) {
  const u = (baseUrl || "").toLowerCase();
  if (u.includes("aliyuncs.com")) return PROVIDERS[0];
  return PROVIDERS.find((p) => u.startsWith(p.base_url.toLowerCase().slice(0, 28)));
}

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
  list,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  list?: string;
}) {
  return (
    <label className="field">
      <span className="label">{label}</span>
      <input
        type={type}
        value={value}
        list={list}
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
  const [health, setHealth] = useState<{ ok: boolean; detail: string } | null>(null);

  const refreshHealth = () =>
    fetch("/api/health").then((r) => r.json()).then(setHealth).catch(() => {});

  useEffect(() => {
    fetch("/api/settings")
      .then((r) => r.json())
      .then((j) => {
        const eng = j.engines?.[j.settings.speech_engine];
        // 默认值要真的填进去。原来只当灰字占位摆着，用户以为已经配好了，
        // 结果点开始记录没有字幕（实战反馈）
        setData({
          ...j.settings,
          speech: {
            ...j.settings.speech,
            base_url: j.settings.speech.base_url || eng?.defaults?.base_url || "",
            model: j.settings.speech.model || eng?.defaults?.model || "",
          },
        });
        setEngines(j.engines);
      })
      .catch(() => {});
    refreshHealth();
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
      refreshHealth();
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

        {health && (
          <p className={health.ok ? "cfg-state ok" : "cfg-state bad"}>
            {health.ok
              ? "文本模型与实时字幕都已配好，可以开会了。"
              : `${health.detail}。缺文本模型则拟稿不出，缺语音则点开始记录不会有字幕。`}
          </p>
        )}

        <div className="brief" style={{ borderTop: "none", paddingTop: 0 }}>
          <dl>
            <dt>文本模型（底稿提炼、字幕译文、拟稿）</dt>
            <dd>
              <label className="field">
                <span className="label">服务商（选了自动填 base url）</span>
                <select
                  value={providerOf(data.text.base_url)?.label ?? ""}
                  onChange={(e) => {
                    const p = PROVIDERS.find((x) => x.label === e.target.value);
                    if (p) patch((d) => ({ ...d, text: { ...d.text, base_url: p.base_url } }));
                  }}
                >
                  <option value="">自定义</option>
                  {PROVIDERS.map((p) => (
                    <option key={p.label} value={p.label}>
                      {p.label}
                      {p.speech ? "" : "（不做实时字幕）"}
                    </option>
                  ))}
                </select>
              </label>
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
                list="mi-models"
                value={data.text.model}
                placeholder="qwen-flash"
                onChange={(v) => patch((d) => ({ ...d, text: { ...d.text, model: v } }))}
              />
              <Field
                label="model name 强档（底稿提炼与换强模型重写，留空则同快档）"
                list="mi-models"
                value={data.text_model_strong}
                placeholder="qwen-max"
                onChange={(v) => patch((d) => ({ ...d, text_model_strong: v }))}
              />
              <Field
                label="向量模型（底稿原文检索用，走同一个端点）"
                list="mi-models"
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
                <>
                  <datalist id="mi-models">
                    {models.map((m) => (
                      <option key={m} value={m} />
                    ))}
                  </datalist>
                  <p className="hint" style={{ margin: "8px 0 0" }}>
                    已拉到 {models.length} 个模型，上面三个框点一下就能选。
                  </p>
                </>
              )}
            </dd>

            <dt>语音引擎（实时字幕）</dt>
            <dd>
              {/* 实时字幕是另一条链路，文本层配好了它也不会自己工作。用智谱、DeepSeek、
                  OpenAI 做文本层的人，这一层必须另外找一家，界面上要直说 */}
              {providerOf(data.text.base_url)?.speech === false && (
                <p className="cfg-state bad" style={{ marginTop: 0 }}>
                  {providerOf(data.text.base_url)?.label} 没有可用于实时字幕的接口。
                  这一层要另配阿里云百炼或 Gemini 的 key，不然只有拟稿没有字幕。
                </p>
              )}
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
                placeholder={meta?.defaults?.base_url}
                onChange={(v) => patch((d) => ({ ...d, speech: { ...d.speech, base_url: v } }))}
              />
              <Field
                label="api key"
                type="password"
                value={speechKey}
                placeholder={data.speech.has_key ? "已填，留空表示不改" : "粘贴 key"}
                onChange={setSpeechKey}
              />
              {textKey && !speechKey && (
                <button
                  className="mini"
                  style={{ marginBottom: 10 }}
                  onClick={() => setSpeechKey(textKey)}
                >
                  和文本层用同一个 key
                </button>
              )}
              <Field
                label="model name"
                value={data.speech.model}
                placeholder={meta?.defaults?.model}
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
