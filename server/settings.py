"""模型接入设置，存本机 data/settings.json，界面上可改，不重启生效。

分两层。文本层走 OpenAI 兼容的 /v1/chat/completions，负责底稿提炼、字幕译文、右栏拟稿，
几乎所有服务商都兼容这个协议。语音层没有统一标准，所以做成可选引擎，见 SPEECH_ENGINES。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config

# 设置按会话存，见 config.Workspace.settings。每个人填自己的 API key，谁也用不了谁的额度

# 语音引擎。single_stream 表示音频进、源语与目标语文本一起出，不必再走一次翻译。
SPEECH_ENGINES: dict[str, dict[str, Any]] = {
    "openai_chunk": {
        "label": "转写分片（OpenAI 兼容 /v1/audio/transcriptions）",
        "implemented": False,
        "single_stream": False,
        "note": "兼容面最广，本地 whisper、Groq、硅基流动、OpenAI 都能填。译文由文本层翻译，"
                "比单流多滞后半秒左右。",
        "defaults": {"base_url": "https://api.openai.com/v1", "model": "whisper-1"},
    },
    "qwen_livetranslate": {
        "label": "通义千问 LiveTranslate（阿里云百炼实时 WebSocket）",
        "implemented": True,
        "single_stream": True,
        "note": "",
        "defaults": {
            "base_url": "https://<workspace>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "model": "qwen3.5-livetranslate-flash-realtime",
        },
    },
    "gemini_live": {
        "label": "Gemini Live Translate（单流，已实测可用）",
        "implemented": True,
        "single_stream": True,
        "note": "音频进、双语文本与译文语音一起出，实测首句 3.5 秒、之后滞后 1.5 秒。",
        "defaults": {
            "base_url": ("wss://generativelanguage.googleapis.com/ws/"
                         "google.ai.generativelanguage.v1beta.GenerativeService."
                         "BidiGenerateContent"),
            "model": "models/gemini-3.5-live-translate-preview",
        },
    },
}


# 字幕译文可选语种。取自阿里云百炼 LiveTranslate 支持的音频加文本输出语种，这里列常用的。
# 源语言不设，由模型自动识别，所以日译中、韩译中、法译中这些只要把目标语设成中文即可。
TARGET_LANGS: list[dict[str, str]] = [
    {"code": "zh", "label": "中文"},
    {"code": "en", "label": "英文"},
    {"code": "ja", "label": "日文"},
    {"code": "ko", "label": "韩文"},
    {"code": "fr", "label": "法文"},
    {"code": "de", "label": "德文"},
    {"code": "es", "label": "西班牙文"},
    {"code": "pt", "label": "葡萄牙文"},
    {"code": "ru", "label": "俄文"},
    {"code": "it", "label": "意大利文"},
    {"code": "ar", "label": "阿拉伯文"},
    {"code": "th", "label": "泰文"},
    {"code": "vi", "label": "越南文"},
    {"code": "id", "label": "印尼文"},
]

# 右栏拟稿用哪种语言写。与字幕译文分开：字幕译成我看得懂的，回复用我要说出口的那种语言。
REPLY_LANGS: list[dict[str, str]] = [
    {"code": "English", "label": "英文"},
    {"code": "日本語", "label": "日文"},
    {"code": "한국어", "label": "韩文"},
    {"code": "Français", "label": "法文"},
    {"code": "Deutsch", "label": "德文"},
    {"code": "Español", "label": "西班牙文"},
    {"code": "中文", "label": "中文"},
]

@dataclass
class Engine:
    base_url: str = ""
    api_key: str = ""
    model: str = ""

    def ready(self) -> bool:
        return bool(self.base_url and self.model)


@dataclass
class Settings:
    # 文本层：快档负责会议中拟稿，强档负责底稿提炼与「换强模型重写」
    text: Engine = field(default_factory=Engine)
    text_model_strong: str = ""
    # 底稿原文检索用的向量模型，走文本层同一个端点与 key
    embed_model: str = "text-embedding-v4"
    # 拟稿时直接放进上下文的底稿原文字数上限。超出的部分改由向量检索补相关片段。
    # 实测首字延迟：1.5k 字 1.6 秒，19.3k 字 1.8 到 3.3 秒，40k 字 5.05 秒。会议里首字超过
    # 3 秒就难用。定两万五是为了让两万四千字的法规要点对照完整进上下文，不在半截被切断；
    # 原文全文是提示词的稳定前缀，同一场会里第二次拟稿起走服务端上下文缓存，边际代价小。
    context_full_chars: int = 25_000
    # 语音层
    speech_engine: str = "qwen_livetranslate"
    speech: Engine = field(default_factory=Engine)
    target_lang: str = "zh-CN"
    # 拟稿写成哪种语言。中文对照始终附在后面
    reply_lang: str = "English"
    # 转写分片模式下每片多长，越短越快但越容易切断句子
    chunk_seconds: float = 4.0

    @property
    def single_stream(self) -> bool:
        return bool(SPEECH_ENGINES.get(self.speech_engine, {}).get("single_stream"))

    def strong_model(self) -> str:
        return self.text_model_strong or self.text.model

    def redacted(self) -> dict:
        """给界面看的，key 只回显是否已填，不回显内容。"""
        data = asdict(self)
        for section in ("text", "speech"):
            data[section]["api_key"] = ""
            data[section]["has_key"] = bool(getattr(self, section).api_key)
        return data


def load(ws: config.Workspace) -> Settings:
    if not ws.settings.exists():
        return Settings()
    try:
        raw = json.loads(ws.settings.read_text(encoding="utf-8"))
    except Exception:
        return Settings()
    return Settings(
        text=Engine(**{k: v for k, v in (raw.get("text") or {}).items()
                       if k in {"base_url", "api_key", "model"}}),
        text_model_strong=raw.get("text_model_strong", "") or "",
        embed_model=raw.get("embed_model") or "text-embedding-v4",
        context_full_chars=int(raw.get("context_full_chars") or 25_000),
        speech_engine=raw.get("speech_engine") or "qwen_livetranslate",
        speech=Engine(**{k: v for k, v in (raw.get("speech") or {}).items()
                         if k in {"base_url", "api_key", "model"}}),
        target_lang=raw.get("target_lang") or "zh-CN",
        reply_lang=raw.get("reply_lang") or "English",
        chunk_seconds=float(raw.get("chunk_seconds") or 4.0),
    )


def save(ws: config.Workspace, current: Settings, patch: dict) -> Settings:
    """按界面提交的内容合并。api_key 留空表示不改动，不是清空。"""
    for section in ("text", "speech"):
        incoming = (patch.get(section) or {})
        engine: Engine = getattr(current, section)
        engine.base_url = (incoming.get("base_url", engine.base_url) or "").strip()
        engine.model = (incoming.get("model", engine.model) or "").strip()
        key = (incoming.get("api_key") or "").strip()
        if key:
            engine.api_key = key
        elif incoming.get("clear_key"):
            engine.api_key = ""
    if "text_model_strong" in patch:
        current.text_model_strong = (patch.get("text_model_strong") or "").strip()
    if patch.get("embed_model"):
        current.embed_model = patch["embed_model"].strip()
    if patch.get("context_full_chars"):
        current.context_full_chars = max(0, min(200_000, int(patch["context_full_chars"])))
    if patch.get("speech_engine") in SPEECH_ENGINES:
        current.speech_engine = patch["speech_engine"]
    if patch.get("target_lang"):
        current.target_lang = patch["target_lang"].strip()
    if patch.get("reply_lang"):
        current.reply_lang = patch["reply_lang"].strip()
    if patch.get("chunk_seconds"):
        current.chunk_seconds = max(1.5, min(10.0, float(patch["chunk_seconds"])))

    ws.root.mkdir(parents=True, exist_ok=True)
    ws.settings.write_text(
        json.dumps(asdict(current), ensure_ascii=False, indent=2), encoding="utf-8")
    ws.settings.chmod(0o600)  # 里面有 api key
    return current
