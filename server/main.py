"""FastAPI 入口：一条 WebSocket 走实时字幕，几个 HTTP 接口走背景与拟稿。

多人同时用，按会话隔离。每个浏览器拿一个 cookie 里的会话 id，底稿、索引、设置、会议库
各存各的，见 config.Workspace。这里不留任何进程级的单例状态，串号就是泄露别人的客户材料。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (config, context_store, drafting, evidence, export_doc, llm,
               minutes, qwen_live, retrieval, store)
from . import settings as settings_mod
from .live_client import LiveTranslateClient
from .segmenter import TurnBuilder

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("meeting-interpreter")

app = FastAPI(title="meeting-interpreter")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class State:
    """一个会话的内存状态。会话之间不共享任何东西。

    工作区有两层，分清楚哪一层管什么是这个类唯一要紧的事：

    - `sws` 会话根：模型设置、SQLite 会议库、纪要目录、活动会议指针。这些属于这个人，
      不属于某一场会。
    - `mws` 当前这场会议：上传的原文、提炼出的摘要、向量索引、会前交代与有效指示。
      换一场会就整个换掉。还没有会议时是 None。

    属性名从原来的 `ws` 改成 `sws`，是为了让所有旧调用点当场报 AttributeError，逼着逐个
    点位重新判断该用哪一层。留一个 `ws` 兼容属性等于给串号留后门。
    """

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.sws = config.Workspace.for_session(sid)
        self.settings = settings_mod.load(self.sws)
        self.seen_at = time.time()
        self.turns: list[dict] = []
        # 后台预取的原文片段，拟稿时直接用，不在关键路径上再做检索
        self.excerpts: list[dict] = []
        self.meeting_id: int | None = None
        self.mws: config.Workspace | None = None
        self.context = context_store.MeetingContext()
        self.conversation: dict = store._blank_conversation()
        # 正在跑的字幕流水线。它在连接建立时拿的是当时的术语表快照，底稿一变必须推给它，
        # 否则会中换掉或清空底稿之后，字幕还在按已经删掉的底稿改译法
        self.builder: TurnBuilder | None = None
        # 同一个浏览器同时只允许一路录音。这是权威的那一层，浏览器侧的互斥只管体验。
        self.live_ws: WebSocket | None = None
        self.live_since = 0.0
        self.adopt_active()

    def adopt_active(self) -> None:
        """进程重启或新建 State 时，把上次那场会接回来。"""
        meeting_id = store.load_active(self.sws)
        if meeting_id is None:
            return
        self.bind_meeting(meeting_id, load_turns=True)

    def bind_meeting(self, meeting_id: int, load_turns: bool = False) -> None:
        """把内存状态整体切到某一场会议上。

        材料、摘要、交代、指示、预取片段、字幕术语表六处必须一起换，漏一处就是上一场的
        内容渗进这一场。
        """
        self.meeting_id = meeting_id
        self.mws = self.sws.for_meeting(meeting_id)
        self.context = context_store.load(self.mws)
        self.conversation = store.load_conversation(self.mws)
        if load_turns:
            self.turns = store.get_turns(self.sws, meeting_id)[-200:]
        self.drop_briefing_traces()

    def materials(self) -> config.Workspace:
        """要读写这一场会议材料的地方。没有会议时调用方必须先挡下来，不许退回会话根。"""
        if self.mws is None:
            raise HTTPException(status_code=409,
                                detail="还没有会议。先新建一场会议，或者继续上一场。")
        return self.mws

    def drop_briefing_traces(self) -> None:
        """底稿变了，把内存里跟着旧底稿走的东西一并换掉。

        底稿在磁盘上换掉不等于这一场会就干净了：预取片段是旧原文的切块，术语表在字幕
        流水线里另有一份快照。删底稿时这两处不清，旧底稿的内容会继续出现在字幕和拟稿里。
        """
        self.excerpts = []
        if self.builder is not None:
            self.builder.set_glossary(self.context.glossary())


SESSION_COOKIE = "mi_sid"
# 会话 id 会拼进文件路径，校验必须严格。放行 ../ 一类就等于让人读别人的底稿
_SID_OK = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_sessions: dict[str, State] = {}
# 本机单人模式下所有请求都归到这一个会话，数据落在 data/ 原位，见 config.single_user
SOLO_SID = "solo"


def _session(sid: str) -> State:
    s = _sessions.get(sid)
    if s is None:
        s = _sessions[sid] = State(sid)
    s.seen_at = time.time()
    return s


def current(request: Request) -> State:
    """取这个浏览器的会话。id 由中间件发放并校验过，这里只认它。"""
    return _session(request.state.sid)


@app.middleware("http")
async def log_arrival(request, call_next):
    """发放并认领会话 id，顺带记一笔请求。

    排查上传失败时先看这条日志有没有，有就是服务端的问题，没有就是浏览器侧断的。
    """
    if config.single_user():
        request.state.sid = SOLO_SID
        fresh = False
        sid = SOLO_SID
    else:
        sid = request.cookies.get(SESSION_COOKIE, "")
        fresh = not _SID_OK.match(sid)
        if fresh:
            sid = secrets.token_urlsafe(24)
        request.state.sid = sid
    if request.method != "GET":
        log.info("收到请求 %s %s，content-length=%s", request.method, request.url.path,
                 request.headers.get("content-length", "?"))
    response = await call_next(request)
    if fresh:
        # 在中间件里种 cookie，流式回包与文件下载也照样带上。
        # 这个 cookie 就是取底稿的凭据，跑在 HTTPS 上时必须带 secure，否则中间人能拿走它，
        # 进而读到用户的会议材料。反向代理后面看 X-Forwarded-Proto 才判得准。
        https = (request.url.scheme == "https"
                 or request.headers.get("x-forwarded-proto", "") == "https")
        response.set_cookie(SESSION_COOKIE, sid, max_age=config.SESSION_TTL_S,
                            httponly=True, samesite="lax", path="/", secure=https)
    return response


@app.get("/api/health")
async def health(s: State = Depends(current)) -> dict:
    cfg = s.settings
    meta = settings_mod.SPEECH_ENGINES.get(cfg.speech_engine, {})
    missing = []
    if not cfg.text.ready():
        missing.append("文本模型")
    if not meta.get("implemented"):
        missing.append(f"语音引擎（{meta.get('label', cfg.speech_engine)} 未接线）")
    elif cfg.speech_engine != "gemini_live" and not cfg.speech.ready():
        missing.append("语音模型")
    ok = not missing
    detail = "还没配：" + "、".join(missing) if missing else ""
    return {"ok": ok, "detail": detail,
            "speechEngine": cfg.speech_engine, "speechModel": cfg.speech.model,
            "textModel": cfg.text.model, "textModelStrong": cfg.strong_model()}


@app.get("/api/settings")
async def get_settings(s: State = Depends(current)) -> dict:
    engines = {key: {k: v for k, v in meta.items() if k != "defaults"} | 
               {"defaults": meta["defaults"]}
               for key, meta in settings_mod.SPEECH_ENGINES.items()}
    return {"settings": s.settings.redacted(), "engines": engines,
            "targetLangs": settings_mod.TARGET_LANGS,
            "replyLangs": settings_mod.REPLY_LANGS}


@app.get("/api/langs")
async def get_langs(s: State = Depends(current)) -> dict:
    """顶栏切换语种用，比整份设置轻。"""
    return {"targetLangs": settings_mod.TARGET_LANGS,
            "replyLangs": settings_mod.REPLY_LANGS,
            "targetLang": s.settings.target_lang,
            "replyLang": s.settings.reply_lang}


@app.post("/api/settings")
async def post_settings(patch: dict, s: State = Depends(current)) -> dict:
    s.settings = settings_mod.save(s.sws, s.settings, patch)
    return {"settings": s.settings.redacted()}


@app.get("/api/settings/models")
async def get_models(s: State = Depends(current)) -> dict:
    """拉服务商的模型清单，填 model name 时当候选用。"""
    return {"models": await llm.list_models(s.settings.text)}


@app.post("/api/settings/test")
async def test_settings(payload: dict, s: State = Depends(current)) -> dict:
    """真发一次请求，不猜。"""
    target = payload.get("target") or "text"
    cfg = s.settings
    if target == "text":
        if not cfg.text.ready():
            return {"ok": False, "error": "base URL 或 model name 还没填"}
        return await llm.probe(cfg.text, cfg.text.model)
    if target == "text_strong":
        if not cfg.text.ready():
            return {"ok": False, "error": "base URL 或 model name 还没填"}
        return await llm.probe(cfg.text, cfg.strong_model())
    meta = settings_mod.SPEECH_ENGINES.get(cfg.speech_engine, {})
    if not meta.get("implemented"):
        return {"ok": False, "error": f"{meta.get('label', cfg.speech_engine)} 还没接线"}
    if cfg.speech_engine == "qwen_livetranslate":
        return await qwen_live.probe(cfg.speech, cfg.speech.model, cfg.target_lang)
    if cfg.speech_engine == "gemini_live":
        return await gemini_probe(s)
    return {"ok": False, "error": "未知语音引擎"}


async def gemini_probe(s: State) -> dict:
    """Gemini 那条留着当备用，测法与百炼一致：连上去发 setup 看认不认。"""
    import json as _json
    import time as _time

    import websockets

    from .live_client import build_setup
    started = _time.monotonic()
    try:
        url = f"{config.LIVE_URL}?key={config.api_key()}"
        async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
            await ws.send(_json.dumps(build_setup(s.settings.target_lang, None)))
            first = _json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if "setupComplete" in first:
                return {"ok": True, "seconds": round(_time.monotonic() - started, 2),
                        "event": "setupComplete"}
            return {"ok": False, "error": _json.dumps(first, ensure_ascii=False)[:260]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}：{exc}"[:260]}


@app.get("/api/context")
async def get_context(s: State = Depends(current)) -> dict:
    return _context_payload(s)


def _context_payload(s: State) -> dict:
    """几个改底稿的接口都要回这份，抽出来免得互相调用还要转一手会话。

    还没有会议时不报错，回一份空壳，界面照常渲染并提示先新建会议。只有要写材料的接口
    才拦（见 State.materials）。
    """
    ctx = s.context
    mws = s.mws
    return {"matter": ctx.matter, "parties": ctx.parties, "issues": ctx.issues,
            "terms": ctx.terms, "myPosition": ctx.my_position, "sources": ctx.sources,
            "glossarySize": len(ctx.glossary()),
            "meetingId": s.meeting_id,
            "docs": retrieval.list_docs(mws) if mws else [],
            "indexChunks": retrieval.index_size(mws) if mws else 0}


@app.post("/api/context")
async def post_context(files: list[UploadFile] = File(default=[]),
                       note: str = Form(default=""),
                       replace: bool = Form(default=False),
                       s: State = Depends(current)) -> dict:
    """replace=True 换一场会（旧原文挪进回收站），False 给同一场会补材料。"""
    mws = s.materials()
    tmpdir = Path(tempfile.mkdtemp(prefix="mi-ctx-"))
    try:
        paths = []
        total = 0
        for f in files:
            if not f.filename:
                continue
            blob = await f.read()
            total += len(blob)
            if total > config.MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"这一批文件超过 {config.MAX_UPLOAD_BYTES // 1024 // 1024} MB，"
                           f"请分批上传或先删掉用不上的文件")
            dest = tmpdir / Path(f.filename).name
            dest.write_bytes(blob)
            paths.append(dest)
        # 单次限额挡不住反复上传，一场会的总量也要看住
        stored_paths = list(mws.docs.glob("*.txt")) if mws.docs.exists() else []
        stored = sum(p.stat().st_size for p in stored_paths)
        # replace=True 旧原文进回收站，只数本次这批
        kept = 0 if replace else len(stored_paths)
        if kept + len(paths) > config.MAX_SESSION_DOCS:
            raise HTTPException(
                status_code=413,
                detail=f"一场会议最多存 {config.MAX_SESSION_DOCS} 份底稿，"
                       f"当前已存 {kept} 份、本次 {len(paths)} 份。"
                       f"请先删掉用不上的，或选「换成新底稿」")
        if not replace and stored + total > config.MAX_SESSION_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"这场会议已存的底稿加上本次上传超过 "
                       f"{config.MAX_SESSION_BYTES // 1024 // 1024} MB，"
                       f"请先删掉用不上的文件，或选「换成新底稿」")
        # 材料按会议分开存之后，上面两道都成了单场的限额，会话总量就此没有上限。
        # 开几十场会照样能把磁盘塞满，所以再看一眼这个会话所有会议的总占用。
        root = mws.meeting_data
        session_total = sum(f.stat().st_size for f in root.rglob("*.txt")) if root.exists() else 0
        if session_total + total > config.MAX_SESSION_TOTAL_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"这个会话所有会议的材料合计超过 "
                       f"{config.MAX_SESSION_TOTAL_BYTES // 1024 // 1024} MB，"
                       f"请先清掉用不上的历史会议材料")
        s.context = await context_store.build(mws, paths, note, replace)
        s.drop_briefing_traces()
    except HTTPException:
        # 超限一类的 413 要原样回给用户，别被下面裹成 502，否则界面提示会答非所问
        raise
    except Exception as exc:
        log.exception("底稿提炼失败")
        raise HTTPException(status_code=502,
                            detail=f"{type(exc).__name__}：{exc}"[:300]) from exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return _context_payload(s)


@app.post("/api/context/reload")
async def reload_context(s: State = Depends(current)) -> dict:
    """摘要存在会话目录的 context.json，手改过之后按这里重新读，不必重新上传文件。"""
    s.context = context_store.load(s.materials())
    return _context_payload(s)


@app.delete("/api/context")
async def clear_context(s: State = Depends(current)) -> dict:
    """整份清掉：底稿摘要、已存原文、向量索引一起。

    原文与摘要都挪进 data/trash/<时间戳>/，不真删。有过一次教训：会前底稿被一条 DELETE
    清掉之后无从恢复，只能重新整理重新上传。
    """
    mws = s.materials()
    bin_dir = retrieval.clear_docs(mws)
    if bin_dir and mws.brief.exists():
        shutil.copy2(mws.brief, bin_dir / "context.json")
    s.context = context_store.MeetingContext()
    context_store.save(mws, s.context)
    s.drop_briefing_traces()
    return _context_payload(s)


@app.delete("/api/context/docs/{name}")
async def delete_doc(name: str, s: State = Depends(current)) -> dict:
    """删掉某一份原文。摘要不会自动重算，需要重新点读进来。"""
    retrieval.remove_doc(s.materials(), name)
    s.drop_briefing_traces()
    return _context_payload(s)


@app.post("/api/context/prefetch")
async def prefetch_excerpts(payload: dict, s: State = Depends(current)) -> dict:
    """右栏打字停顿时预取原文片段。

    refresh_excerpts 跟着对方的话走，这里跟着我正在打的问题走。跑在打字停顿里，
    等按下发出时片段已经是热的，检索始终不占拟稿关键路径。
    """
    query = (payload.get("query") or "").strip()
    cfg = s.settings
    mws = s.mws
    if (not query or mws is None
            or len(retrieval.all_text(mws, 10 ** 9)) <= cfg.context_full_chars):
        return {"chunks": len(s.excerpts)}
    recent = " ".join((t.get("src") or "") for t in s.turns[-1:]).strip()
    try:
        s.excerpts = await retrieval.search(mws, cfg.text, cfg.embed_model,
                                            f"{query} {recent}".strip(), k=8)
    except Exception as exc:
        log.warning("按输入预取片段失败：%s：%s", type(exc).__name__, exc)
    return {"chunks": len(s.excerpts)}


@app.post("/api/context/search")
async def search_docs(payload: dict, s: State = Depends(current)) -> dict:
    """手动查原文，也用来验证索引是否可用。"""
    query = (payload.get("query") or "").strip()
    if not query:
        return {"hits": []}
    cfg = s.settings
    try:
        hits = await retrieval.search(s.materials(), cfg.text, cfg.embed_model, query,
                                      int(payload.get("k") or 3))
        return {"hits": hits}
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail=f"{type(exc).__name__}：{exc}"[:300]) from exc


@app.post("/api/draft")
async def post_draft(payload: dict, s: State = Depends(current)) -> StreamingResponse:
    instruction = (payload.get("instruction") or "").strip()
    history = payload.get("history") or []
    # 会中指示由用户显式保存，存在这场会议的 conversation.json 里，不从聊天记录反推。
    # 原来是前端从拟稿卡片猜出来再发上来的，猜错了就是按被推翻的旧方案继续答。
    directives = [str(d.get("text", "")).strip()
                  for d in (s.conversation.get("directives") or [])
                  if str(d.get("text", "")).strip()]
    mws = s.materials()
    quality = payload.get("quality") or "fast"
    # draft=要我拟稿，ask=问我含义或让我判断形势，空=自由输入，分不清就让模型自己认
    mode = payload.get("mode") if payload.get("mode") in ("draft", "ask") else ""
    if not instruction:
        return StreamingResponse(iter(["data: \n\n"]), media_type="text/event-stream")

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"

    async def gen():
        """正文一个字都不缓冲地往外流，依据区留到最后核验完再发。

        只有一处例外：依据区的起始标记可能被模型切在两个 chunk 中间（「---依」加「据---」），
        所以永远压着最后几个字符不发，等下一块到了再判断。几个字符的延迟肉眼不可见，
        漏出半截标记却会直接出现在用户看到的正文里。
        """
        hold = len(evidence.REF_MARK) - 1
        buf = ""          # 还没确定能不能发的尾巴
        tail = ""         # 进入依据区之后的全部文本
        in_ref = False
        sources: dict[str, dict] = {}
        profile = s.conversation.get("profile") or {}
        try:
            async for chunk in drafting.stream_draft(
                    mws, s.context, s.turns, history, instruction, quality,
                    s.excerpts, directives, mode, profile, sources):
                if in_ref:
                    tail += chunk
                    continue
                buf += chunk
                cut = buf.find(evidence.REF_MARK)
                if cut >= 0:
                    head, tail = buf[:cut], buf[cut + len(evidence.REF_MARK):]
                    in_ref = True
                    if head:
                        yield sse({"text": head})
                    continue
                if len(buf) > hold:
                    yield sse({"text": buf[:-hold]})
                    buf = buf[-hold:]
            if not in_ref and buf:
                yield sse({"text": buf})
        except Exception as exc:
            log.exception("拟稿失败")
            msg = f"拟稿失败：{type(exc).__name__} {exc}"[:300]
            yield sse({"error": msg})
        else:
            if in_ref:
                ref_text, todo_text = tail, ""
                if evidence.TODO_MARK in tail:
                    ref_text, todo_text = tail.split(evidence.TODO_MARK, 1)
                yield sse({"evidence": evidence.parse_and_verify(
                    ref_text, todo_text, sources)})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _meeting_row(s: State, row: dict) -> dict:
    """列表里每一行要让人看出这是哪一场：有没有材料、有没有纪要、是不是当前这场。"""
    mid = row["id"]
    mws = s.sws.for_meeting(mid)
    docs = len(list(mws.docs.glob("*.txt"))) if mws.docs.exists() else 0
    conv = store.load_conversation(mws) if mws.conversation.exists() else {}
    profile = conv.get("profile") or {}
    return {**row, "docCount": docs,
            "hasProfile": any(str(v).strip() for v in profile.values()),
            "hasMinutes": (mws.minutes / f"{mid}.md").exists(),
            "active": mid == s.meeting_id}


@app.get("/api/meetings")
async def get_meetings(s: State = Depends(current)) -> list[dict]:
    return [_meeting_row(s, row) for row in store.list_meetings(s.sws)]


@app.get("/api/meetings/active")
async def get_active_meeting(s: State = Depends(current)) -> dict:
    """页面加载时调一次，把上一场会接回来。没有会议时 meetingId 为 null。"""
    if s.meeting_id is None:
        return {"meetingId": None, "canAdopt": _can_adopt(s),
                "context": _context_payload(s), "conversation": s.conversation}
    return {"meetingId": s.meeting_id, "canAdopt": False,
            "context": _context_payload(s), "conversation": s.conversation,
            "turns": store.get_turns(s.sws, s.meeting_id)}


def _can_adopt(s: State) -> bool:
    """首场会议可以沿用会话根上已经准备好的材料。

    判据是这个会话有没有走过分场流程，不是会议库里有没有行：旧代码每次连 WebSocket 就建
    一场会议，库里早堆了一批空行，拿它判会永远判成不是首场。
    """
    if s.sws.meeting_data.exists():
        return False
    return s.sws.brief.exists() or (s.sws.docs.exists()
                                    and any(s.sws.docs.glob("*.txt")))


def _adopt_session_root(s: State, mws: config.Workspace) -> bool:
    """把会话根上的既有材料复制一份进这场新会议。

    复制而不是搬走：会话根的原件原样留着，从此成为只读存档，之后所有上传、删除、清空都
    只作用于会议目录。这样既满足「不迁移或覆盖旧材料」，也让 Workspace 保持成一条没有
    分支的路径推导规则，不必在隔离的关键路径上加回退逻辑。
    """
    copied = False
    if s.sws.docs.exists():
        mws.docs.mkdir(parents=True, exist_ok=True)
        for src in s.sws.docs.glob("*.txt"):
            shutil.copy2(src, mws.docs / src.name)
            copied = True
    for attr in ("brief", "index"):
        src = getattr(s.sws, attr)
        if src.exists():
            dst = getattr(mws, attr)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied = True
    return copied


@app.post("/api/meetings")
async def create_meeting(payload: dict | None = None,
                         s: State = Depends(current)) -> dict:
    """新建一场会议。首场可以沿用已有材料，之后每一场都是空白的。"""
    payload = payload or {}
    title = str(payload.get("title") or "").strip()
    adopt = bool(payload.get("adoptExisting")) and _can_adopt(s)
    meeting_id = store.start_meeting(s.sws, title)
    mws = s.sws.for_meeting(meeting_id)
    mws.root.mkdir(parents=True, exist_ok=True)
    adopted = await asyncio.to_thread(_adopt_session_root, s, mws) if adopt else False
    store.save_active(s.sws, meeting_id)
    s.turns = []
    s.bind_meeting(meeting_id)
    return {"meetingId": meeting_id, "adopted": adopted,
            "context": _context_payload(s), "conversation": s.conversation,
            "meetings": [_meeting_row(s, row) for row in store.list_meetings(s.sws)]}


@app.post("/api/meetings/{meeting_id}/resume")
async def resume_meeting(meeting_id: int, s: State = Depends(current)) -> dict:
    """恢复历史会议，用的是那一场自己的材料，当前底稿一概不带过去。"""
    if s.live_ws is not None:
        raise HTTPException(status_code=409,
                            detail="正在录音，先停止记录再切换会议。")
    if not store.meeting_exists(s.sws, meeting_id):
        raise HTTPException(status_code=404, detail="没有这场会议")
    store.save_active(s.sws, meeting_id)
    s.bind_meeting(meeting_id, load_turns=True)
    return {"meetingId": meeting_id, "context": _context_payload(s),
            "conversation": s.conversation, "turns": s.turns,
            "hasMaterials": bool(s.context.sources or s.context.matter)}


@app.get("/api/conversation")
async def get_conversation(s: State = Depends(current)) -> dict:
    return s.conversation


@app.put("/api/conversation/profile")
async def put_profile(payload: dict, s: State = Depends(current)) -> dict:
    """会前交代由用户本人填写，不从材料里提炼，也不替他补默认身份。"""
    mws = s.materials()
    keys = ("identity", "goal", "facts", "noCommit")
    s.conversation["profile"] = {k: str(payload.get(k) or "").strip() for k in keys}
    store.save_conversation(mws, s.conversation)
    return {"profile": s.conversation["profile"]}


@app.put("/api/conversation/directives")
async def put_directives(payload: dict, s: State = Depends(current)) -> dict:
    """整表提交。会中指示只认用户显式保存的这一份，不从聊天记录猜。"""
    mws = s.materials()
    items = []
    for raw in (payload.get("directives") or [])[:50]:
        text = str(raw.get("text") if isinstance(raw, dict) else raw or "").strip()
        if text:
            items.append({"text": text[:500],
                          "savedAt": float(raw.get("savedAt") or time.time())
                          if isinstance(raw, dict) else time.time()})
    s.conversation["directives"] = items
    store.save_conversation(mws, s.conversation)
    return {"directives": items}


@app.get("/api/meetings/{meeting_id}/export.md")
async def export_meeting(meeting_id: int,
                         s: State = Depends(current)) -> PlainTextResponse:
    """只要逐句记录，不含纪要正文。"""
    return PlainTextResponse(store.export_markdown(s.sws, meeting_id),
                             media_type="text/markdown; charset=utf-8")


def _meeting_ws(s: State, meeting_id: int) -> config.Workspace:
    """按 URL 里的编号取工作区，不看当前活动的是哪一场。

    纪要与导出可以针对任意一场历史会议。库与纪要目录挂在共享根，所以这个工作区同时给出
    正确的库和那一场自己的底稿；用 s.context（当前这场的底稿）给历史会议出纪要，就是把
    这一场的立场写进上一场的记录里。
    """
    return s.sws.for_meeting(meeting_id)


@app.post("/api/meetings/{meeting_id}/minutes")
async def make_minutes(meeting_id: int, s: State = Depends(current)) -> dict:
    """让强档模型整理纪要。不赶时间，用强档，返回 markdown 供预览。"""
    # 还在录音就不许出纪要：尾句可能还没落库，出来的纪要会少最后一段。
    if s.live_ws is not None and s.meeting_id == meeting_id:
        raise HTTPException(status_code=409,
                            detail="这场会还在录音。先停止记录，等服务端确认收尾完成。")
    mws = _meeting_ws(s, meeting_id)
    ctx = s.context if s.meeting_id == meeting_id else context_store.load(mws)
    try:
        text = await minutes.generate(mws, meeting_id, ctx)
    except Exception as exc:
        log.exception("生成纪要失败")
        raise HTTPException(status_code=502,
                            detail=f"{type(exc).__name__}：{exc}"[:300]) from exc
    return {"markdown": text, "title": minutes.title_of(text),
            "full": export_doc.full_markdown(mws, meeting_id, text)}


@app.get("/api/meetings/{meeting_id}/minutes")
async def get_minutes(meeting_id: int, s: State = Depends(current)) -> dict:
    mws = _meeting_ws(s, meeting_id)
    text = minutes.load(mws, meeting_id)
    return {"markdown": text, "title": minutes.title_of(text) if text else "",
            "full": export_doc.full_markdown(mws, meeting_id, text) if text else ""}


def _download_name(s: State, meeting_id: int, suffix: str) -> str:
    title = minutes.title_of(minutes.load(_meeting_ws(s, meeting_id), meeting_id),
                             "会议纪要")[:30]
    stamp = datetime.now().strftime("%y%m%d")
    return f"【{stamp}】{title}.{suffix}"


@app.get("/api/meetings/{meeting_id}/minutes.md")
async def minutes_md(meeting_id: int,
                     s: State = Depends(current)) -> PlainTextResponse:
    text = export_doc.full_markdown(_meeting_ws(s, meeting_id), meeting_id)
    if not text.strip():
        raise HTTPException(status_code=404, detail="这场会议还没有记录")
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                             headers={"content-disposition":
                                      f"attachment; filename*=UTF-8''"
                                      f"{quote(_download_name(s, meeting_id, 'md'))}"})


@app.get("/api/meetings/{meeting_id}/minutes.docx")
async def minutes_docx(meeting_id: int, s: State = Depends(current)) -> FileResponse:
    mws = _meeting_ws(s, meeting_id)
    text = export_doc.full_markdown(mws, meeting_id)
    if not text.strip():
        raise HTTPException(status_code=404, detail="这场会议还没有记录")
    path = mws.minutes / f"{meeting_id}.docx"
    await asyncio.to_thread(export_doc.to_docx, text, path)
    return FileResponse(path, filename=_download_name(s, meeting_id, "docx"),
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".wordprocessingml.document")


@app.get("/api/meetings/{meeting_id}/minutes.pdf")
async def minutes_pdf(meeting_id: int, s: State = Depends(current)) -> FileResponse:
    mws = _meeting_ws(s, meeting_id)
    text = export_doc.full_markdown(mws, meeting_id)
    if not text.strip():
        raise HTTPException(status_code=404, detail="这场会议还没有记录")
    docx_path = mws.minutes / f"{meeting_id}.docx"
    await asyncio.to_thread(export_doc.to_docx, text, docx_path)
    try:
        pdf_path = await asyncio.to_thread(export_doc.to_pdf, docx_path, mws.minutes)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    return FileResponse(pdf_path, filename=_download_name(s, meeting_id, "pdf"),
                        media_type="application/pdf")


async def refresh_excerpts(s: State) -> None:
    """会议进行中在后台更新原文片段。

    放在后台是为了把向量往返移出拟稿的关键路径。原文全部装进上下文时不必检索，直接清空。
    """
    cfg = s.settings
    if s.mws is None:
        s.excerpts = []
        return
    stored = len(retrieval.all_text(s.mws, 10 ** 9))
    if stored <= cfg.context_full_chars:
        s.excerpts = []
        return
    query = " ".join((t.get("src") or "") for t in s.turns[-2:]).strip()
    if not query:
        return
    try:
        # 预取在后台，捞 8 块和捞 3 块对首字延迟没有区别，多捞提高命中率
        s.excerpts = await retrieval.search(s.mws, cfg.text, cfg.embed_model, query, k=8)
    except Exception as exc:
        log.warning("预取原文片段失败：%s：%s", type(exc).__name__, exc)


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    # 中间件不管 WebSocket，这里自己认 cookie。页面必然先发过 HTTP 请求，cookie 一定在
    if config.single_user():
        sid = SOLO_SID
    else:
        sid = ws.cookies.get(SESSION_COOKIE, "")
        if not _SID_OK.match(sid):
            await ws.close(code=4001)
            return
    s = _session(sid)
    await ws.accept()
    send_lock = asyncio.Lock()
    want_audio = False

    async def refuse(code: str, detail: str, close_code: int) -> None:
        try:
            await ws.send_text(json.dumps({"type": "error", "code": code,
                                           "detail": detail}, ensure_ascii=False))
        except Exception:
            pass
        await ws.close(code=close_code)

    # 没有会议就不许录。原来是每连一次 WebSocket 就凭空建一场，笔录因此散落在一堆
    # 没人认领的会议行里，也没法把材料归到某一场。
    if s.meeting_id is None:
        await refuse("no_meeting", "还没有会议。先新建一场会议，或者继续上一场。", 4003)
        return
    # 同一个浏览器同时只允许一路录音。两个标签页同时录，两条流会抢同一个断句器，
    # 落库的段落互相穿插。这是权威的那一层，浏览器侧的互斥只管提示得快一点。
    if s.live_ws is not None and time.time() - s.live_since < 6 * 3600:
        await refuse("busy", "这个浏览器已经有一场会议在录音了。", 4002)
        return
    s.live_ws, s.live_since = ws, time.time()

    async def send(obj: dict) -> None:
        async with send_lock:
            try:
                await ws.send_text(json.dumps(obj, ensure_ascii=False))
            except Exception:
                pass

    async def on_turn(event: dict) -> None:
        """收口的段落落库，并留在内存里供右栏拟稿引用。"""
        if event.get("type") == "turn":
            merged_src = " ".join(p["src"] for p in event["pairs"] if p["src"]).strip()
            merged_dst = " ".join(p["dst"] for p in event["pairs"] if p["dst"]).strip()
            s.turns.append({"src": merged_src, "dst": merged_dst,
                            "srcLang": event.get("srcLang", "")})
            s.turns[:] = s.turns[-200:]
            if s.meeting_id and merged_src:
                store.add_turn(s.sws, s.meeting_id, event["turnId"], merged_src,
                               merged_dst, event.get("srcLang", ""))
            asyncio.create_task(refresh_excerpts(s))
        await send(event)

    # 百炼给全文、Gemini 给增量，语义不同，这里按引擎选
    cumulative = s.settings.speech_engine == "qwen_livetranslate"
    builder = TurnBuilder(on_turn, s.context.glossary(), cumulative=cumulative)
    # 登记给会话，底稿一变就能把新术语表推过来，见 drop_briefing_traces
    s.builder = builder

    async def on_live(event: dict) -> None:
        kind = event.get("type")
        if kind == "src":
            await builder.add("src", event["text"], event.get("lang", ""),
                              bool(event.get("commit")))
        elif kind == "dst":
            await builder.add("dst", event["text"], event.get("lang", ""),
                              bool(event.get("commit")))
        elif kind == "turn_complete":
            # 这段话真正结束，段内已上屏偏移清零；中途切卡的 close 不能带 final
            await builder.close(final=True)
        elif kind == "audio":
            if want_audio:
                await send(event)
        else:
            await send(event)

    cfg = s.settings
    if cfg.speech_engine == "qwen_livetranslate":
        client = qwen_live.QwenLiveClient(on_live, cfg.speech, cfg.speech.model,
                                         cfg.target_lang)
    else:
        client = LiveTranslateClient(on_live, cfg.target_lang)
    # 会议由 /api/meetings 显式建立，连接这里只是加入当前这一场，不再凭空新建
    s.excerpts = []
    await send({"type": "meeting", "meetingId": s.meeting_id,
                "glossarySize": len(s.context.glossary())})

    runner = asyncio.create_task(client.run())
    idler = asyncio.create_task(builder.watch_idle())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if data := msg.get("bytes"):
                client.feed(data)
            elif text := msg.get("text"):
                try:
                    cmd = json.loads(text)
                except json.JSONDecodeError:
                    continue
                action = cmd.get("type")
                if action == "stop":
                    break
                if action == "pause":
                    # 暂停时把当前这段收口，恢复后从新的一段开始
                    await builder.close()
                    await send({"type": "status", "state": "paused"})
                if action == "resume":
                    await send({"type": "status", "state": "live"})
                if action == "audio":
                    want_audio = bool(cmd.get("on"))
                if action == "reload_context":
                    s.context = context_store.load(s.mws)
                    builder.set_glossary(s.context.glossary())
                    await send({"type": "meeting", "meetingId": s.meeting_id,
                                "glossarySize": len(s.context.glossary())})
    except WebSocketDisconnect:
        pass
    finally:
        # 收口。原来是固定睡 3.5 秒再收摊，而实测百炼的原话比译文晚 6.6 秒到，
        # 固定等就是在赌尾句能不能赶上；现在等服务端的结束信号，等到立刻走，
        # 等不到最多等 SETTLE_MAX_S，无论哪种都要落库之后才告诉前端结束。
        began = time.monotonic()
        client.stop()
        await send({"type": "status", "state": "settling"})
        settled = False
        try:
            await asyncio.wait_for(client.finished.wait(), timeout=config.SETTLE_MAX_S)
            settled = True
        except asyncio.TimeoutError:
            log.warning("等语音服务确认结束超时（%.1f 秒），按现有内容收口",
                        config.SETTLE_MAX_S)
        # 结束信号到了，零星片段可能还在路上，等断句器真的安静下来
        while (time.monotonic() - began < config.SETTLE_MAX_S
               and builder.open
               and time.monotonic() - builder.last_at < config.SETTLE_QUIET_S):
            await asyncio.sleep(0.15)
        idler.cancel()
        await builder.close(final=True)
        runner.cancel()
        await asyncio.gather(runner, idler, return_exceptions=True)
        if s.builder is builder:
            s.builder = None
        if s.live_ws is ws:
            s.live_ws = None
        stored = len(store.get_turns(s.sws, s.meeting_id)) if s.meeting_id else 0
        await send({"type": "ended", "meetingId": s.meeting_id, "turns": stored,
                    "settled": settled,
                    "seconds": round(time.monotonic() - began, 2)})
        # 给上面这条消息一个出网的机会，随后返回会关掉连接
        await asyncio.sleep(0.05)
        log.info("会议结束：音频 token %d，输出 token %d，收口 %.1f 秒，确认=%s",
                 client.audio_tokens, client.response_tokens,
                 time.monotonic() - began, settled)


async def _sweep_sessions() -> None:
    """定期清掉过期会话。

    别人上传的底稿不该无限期留在服务器上，磁盘也会一直涨。内存里的会话对象与磁盘目录
    一起清，判断只看最后活跃时间。正在开会的会话每收一段话都会刷新活跃时间，不会被误删。
    """
    while True:
        await asyncio.sleep(3600)
        cutoff = time.time() - config.SESSION_TTL_S
        try:
            for sid, sess in list(_sessions.items()):
                if sess.seen_at < cutoff and sess.builder is None:
                    _sessions.pop(sid, None)
            if config.SESSIONS_DIR.exists():
                for d in config.SESSIONS_DIR.iterdir():
                    if not d.is_dir() or d.name in _sessions:
                        continue
                    if d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                        log.info("清掉过期会话目录 %s", d.name)
        except Exception as exc:
            log.warning("会话清理出错：%s：%s", type(exc).__name__, exc)


@app.on_event("startup")
async def _start_sweeper() -> None:
    asyncio.create_task(_sweep_sessions())


# 前端构建产物在 web/dist，构建过就直接由后端伺服，开发时走 vite
DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(DIST / "index.html")
