"""FastAPI 入口：一条 WebSocket 走实时字幕，几个 HTTP 接口走背景与拟稿。

只服务本机单人使用，所以最近字幕直接放进内存单例，不做多会话隔离。
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import (FastAPI, File, Form, HTTPException, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import (config, context_store, drafting, export_doc, llm, minutes,
               qwen_live, retrieval, store)
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
    """当前这场会议的内存状态。"""

    def __init__(self) -> None:
        self.context = context_store.load()
        self.settings = settings_mod.load()
        self.turns: list[dict] = []
        # 后台预取的原文片段，拟稿时直接用，不在关键路径上再做检索
        self.excerpts: list[dict] = []
        self.meeting_id: int | None = None

    def reset_meeting(self, title: str = "") -> int:
        self.turns = []
        self.excerpts = []
        self.meeting_id = store.start_meeting(title)
        return self.meeting_id


state = State()


@app.middleware("http")
async def log_arrival(request, call_next):
    """请求一进来就记一笔。排查上传失败时，先看这条日志有没有，
    有就是服务端的问题，没有就是浏览器侧断的。"""
    if request.method != "GET":
        log.info("收到请求 %s %s，content-length=%s", request.method, request.url.path,
                 request.headers.get("content-length", "?"))
    return await call_next(request)


@app.get("/api/health")
async def health() -> dict:
    cfg = state.settings
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
async def get_settings() -> dict:
    engines = {key: {k: v for k, v in meta.items() if k != "defaults"} | 
               {"defaults": meta["defaults"]}
               for key, meta in settings_mod.SPEECH_ENGINES.items()}
    return {"settings": state.settings.redacted(), "engines": engines,
            "targetLangs": settings_mod.TARGET_LANGS,
            "replyLangs": settings_mod.REPLY_LANGS}


@app.get("/api/langs")
async def get_langs() -> dict:
    """顶栏切换语种用，比整份设置轻。"""
    return {"targetLangs": settings_mod.TARGET_LANGS,
            "replyLangs": settings_mod.REPLY_LANGS,
            "targetLang": state.settings.target_lang,
            "replyLang": state.settings.reply_lang}


@app.post("/api/settings")
async def post_settings(patch: dict) -> dict:
    state.settings = settings_mod.save(state.settings, patch)
    return {"settings": state.settings.redacted()}


@app.get("/api/settings/models")
async def get_models() -> dict:
    """拉服务商的模型清单，填 model name 时当候选用。"""
    return {"models": await llm.list_models(state.settings.text)}


@app.post("/api/settings/test")
async def test_settings(payload: dict) -> dict:
    """真发一次请求，不猜。"""
    target = payload.get("target") or "text"
    cfg = state.settings
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
        return await gemini_probe()
    return {"ok": False, "error": "未知语音引擎"}


async def gemini_probe() -> dict:
    """Gemini 那条留着当备用，测法与百炼一致：连上去发 setup 看认不认。"""
    import json as _json
    import time as _time

    import websockets

    from .live_client import build_setup
    started = _time.monotonic()
    try:
        url = f"{config.LIVE_URL}?key={config.api_key()}"
        async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
            await ws.send(_json.dumps(build_setup(state.settings.target_lang, None)))
            first = _json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if "setupComplete" in first:
                return {"ok": True, "seconds": round(_time.monotonic() - started, 2),
                        "event": "setupComplete"}
            return {"ok": False, "error": _json.dumps(first, ensure_ascii=False)[:260]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}：{exc}"[:260]}


@app.get("/api/context")
async def get_context() -> dict:
    ctx = state.context
    return {"matter": ctx.matter, "parties": ctx.parties, "issues": ctx.issues,
            "terms": ctx.terms, "myPosition": ctx.my_position, "sources": ctx.sources,
            "glossarySize": len(ctx.glossary()),
            "docs": retrieval.list_docs(), "indexChunks": retrieval.index_size()}


@app.post("/api/context")
async def post_context(files: list[UploadFile] = File(default=[]),
                       note: str = Form(default="")) -> dict:
    tmpdir = Path(tempfile.mkdtemp(prefix="mi-ctx-"))
    try:
        paths = []
        for f in files:
            if not f.filename:
                continue
            dest = tmpdir / Path(f.filename).name
            dest.write_bytes(await f.read())
            paths.append(dest)
        state.context = await context_store.build(paths, note)
    except Exception as exc:
        log.exception("底稿提炼失败")
        raise HTTPException(status_code=502,
                            detail=f"{type(exc).__name__}：{exc}"[:300]) from exc
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return await get_context()


@app.post("/api/context/reload")
async def reload_context() -> dict:
    """底稿存在 data/context.json，手改过之后按这里重新读，不必重新上传文件。"""
    state.context = context_store.load()
    return await get_context()


@app.delete("/api/context")
async def clear_context() -> dict:
    """整份清掉：底稿摘要、已存原文、向量索引一起。

    原文与摘要都挪进 data/trash/<时间戳>/，不真删。有过一次教训：会前底稿被一条 DELETE
    清掉之后无从恢复，只能重新整理重新上传。
    """
    bin_dir = retrieval.clear_docs()
    if bin_dir and context_store.BRIEF_PATH.exists():
        shutil.copy2(context_store.BRIEF_PATH, bin_dir / "context.json")
    state.context = context_store.MeetingContext()
    context_store.save(state.context)
    return await get_context()


@app.delete("/api/context/docs/{name}")
async def delete_doc(name: str) -> dict:
    """删掉某一份原文。摘要不会自动重算，需要重新点读进来。"""
    retrieval.remove_doc(name)
    return await get_context()


@app.post("/api/context/prefetch")
async def prefetch_excerpts(payload: dict) -> dict:
    """右栏打字停顿时预取原文片段。

    refresh_excerpts 跟着对方的话走，这里跟着我正在打的问题走。跑在打字停顿里，
    等按下发出时片段已经是热的，检索始终不占拟稿关键路径。
    """
    query = (payload.get("query") or "").strip()
    cfg = state.settings
    if not query or len(retrieval.all_text(10 ** 9)) <= cfg.context_full_chars:
        return {"chunks": len(state.excerpts)}
    recent = " ".join((t.get("src") or "") for t in state.turns[-1:]).strip()
    try:
        state.excerpts = await retrieval.search(cfg.text, cfg.embed_model,
                                                f"{query} {recent}".strip(), k=8)
    except Exception as exc:
        log.warning("按输入预取片段失败：%s：%s", type(exc).__name__, exc)
    return {"chunks": len(state.excerpts)}


@app.post("/api/context/search")
async def search_docs(payload: dict) -> dict:
    """手动查原文，也用来验证索引是否可用。"""
    query = (payload.get("query") or "").strip()
    if not query:
        return {"hits": []}
    cfg = state.settings
    try:
        hits = await retrieval.search(cfg.text, cfg.embed_model, query,
                                      int(payload.get("k") or 3))
        return {"hits": hits}
    except Exception as exc:
        raise HTTPException(status_code=502,
                            detail=f"{type(exc).__name__}：{exc}"[:300]) from exc


@app.post("/api/draft")
async def post_draft(payload: dict) -> StreamingResponse:
    instruction = (payload.get("instruction") or "").strip()
    history = payload.get("history") or []
    quality = payload.get("quality") or "fast"
    if not instruction:
        return StreamingResponse(iter(["data: \n\n"]), media_type="text/event-stream")

    async def gen():
        try:
            async for chunk in drafting.stream_draft(
                    state.context, state.turns, history, instruction, quality,
                    state.excerpts):
                yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            log.exception("拟稿失败")
            msg = f"拟稿失败：{type(exc).__name__} {exc}"[:300]
            yield f"data: {json.dumps({'error': msg}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/meetings")
async def get_meetings() -> list[dict]:
    return store.list_meetings()


@app.get("/api/meetings/{meeting_id}/export.md")
async def export_meeting(meeting_id: int) -> PlainTextResponse:
    """只要逐句记录，不含纪要正文。"""
    return PlainTextResponse(store.export_markdown(meeting_id),
                             media_type="text/markdown; charset=utf-8")


@app.post("/api/meetings/{meeting_id}/minutes")
async def make_minutes(meeting_id: int) -> dict:
    """让强档模型整理纪要。不赶时间，用强档，返回 markdown 供预览。"""
    try:
        text = await minutes.generate(meeting_id, state.context)
    except Exception as exc:
        log.exception("生成纪要失败")
        raise HTTPException(status_code=502,
                            detail=f"{type(exc).__name__}：{exc}"[:300]) from exc
    return {"markdown": text, "title": minutes.title_of(text),
            "full": export_doc.full_markdown(meeting_id, text)}


@app.get("/api/meetings/{meeting_id}/minutes")
async def get_minutes(meeting_id: int) -> dict:
    text = minutes.load(meeting_id)
    return {"markdown": text, "title": minutes.title_of(text) if text else "",
            "full": export_doc.full_markdown(meeting_id, text) if text else ""}


def _download_name(meeting_id: int, suffix: str) -> str:
    title = minutes.title_of(minutes.load(meeting_id), "会议纪要")[:30]
    stamp = datetime.now().strftime("%y%m%d")
    return f"【{stamp}】{title}.{suffix}"


@app.get("/api/meetings/{meeting_id}/minutes.md")
async def minutes_md(meeting_id: int) -> PlainTextResponse:
    text = export_doc.full_markdown(meeting_id)
    if not text.strip():
        raise HTTPException(status_code=404, detail="这场会议还没有记录")
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8",
                             headers={"content-disposition":
                                      f"attachment; filename*=UTF-8''"
                                      f"{quote(_download_name(meeting_id, 'md'))}"})


@app.get("/api/meetings/{meeting_id}/minutes.docx")
async def minutes_docx(meeting_id: int) -> FileResponse:
    text = export_doc.full_markdown(meeting_id)
    if not text.strip():
        raise HTTPException(status_code=404, detail="这场会议还没有记录")
    path = config.DATA_DIR / "minutes" / f"{meeting_id}.docx"
    await asyncio.to_thread(export_doc.to_docx, text, path)
    return FileResponse(path, filename=_download_name(meeting_id, "docx"),
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".wordprocessingml.document")


@app.get("/api/meetings/{meeting_id}/minutes.pdf")
async def minutes_pdf(meeting_id: int) -> FileResponse:
    text = export_doc.full_markdown(meeting_id)
    if not text.strip():
        raise HTTPException(status_code=404, detail="这场会议还没有记录")
    docx_path = config.DATA_DIR / "minutes" / f"{meeting_id}.docx"
    await asyncio.to_thread(export_doc.to_docx, text, docx_path)
    try:
        pdf_path = await asyncio.to_thread(export_doc.to_pdf, docx_path,
                                           config.DATA_DIR / "minutes")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300]) from exc
    return FileResponse(pdf_path, filename=_download_name(meeting_id, "pdf"),
                        media_type="application/pdf")


async def refresh_excerpts() -> None:
    """会议进行中在后台更新原文片段。

    放在后台是为了把向量往返移出拟稿的关键路径。原文全部装进上下文时不必检索，直接清空。
    """
    cfg = state.settings
    stored = len(retrieval.all_text(10 ** 9))
    if stored <= cfg.context_full_chars:
        state.excerpts = []
        return
    query = " ".join((t.get("src") or "") for t in state.turns[-2:]).strip()
    if not query:
        return
    try:
        # 预取在后台，捞 8 块和捞 3 块对首字延迟没有区别，多捞提高命中率
        state.excerpts = await retrieval.search(cfg.text, cfg.embed_model, query, k=8)
    except Exception as exc:
        log.warning("预取原文片段失败：%s：%s", type(exc).__name__, exc)


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    await ws.accept()
    send_lock = asyncio.Lock()
    want_audio = False

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
            state.turns.append({"src": merged_src, "dst": merged_dst,
                                "srcLang": event.get("srcLang", "")})
            state.turns[:] = state.turns[-200:]
            if state.meeting_id and merged_src:
                store.add_turn(state.meeting_id, event["turnId"], merged_src, merged_dst,
                               event.get("srcLang", ""))
            asyncio.create_task(refresh_excerpts())
        await send(event)

    # 百炼给全文、Gemini 给增量，语义不同，这里按引擎选
    cumulative = state.settings.speech_engine == "qwen_livetranslate"
    builder = TurnBuilder(on_turn, state.context.glossary(), cumulative=cumulative)

    async def on_live(event: dict) -> None:
        kind = event.get("type")
        if kind == "src":
            await builder.add("src", event["text"], event.get("lang", ""),
                              bool(event.get("commit")))
        elif kind == "dst":
            await builder.add("dst", event["text"], event.get("lang", ""),
                              bool(event.get("commit")))
        elif kind == "turn_complete":
            await builder.close()
        elif kind == "audio":
            if want_audio:
                await send(event)
        else:
            await send(event)

    cfg = state.settings
    if cfg.speech_engine == "qwen_livetranslate":
        client = qwen_live.QwenLiveClient(on_live, cfg.speech, cfg.speech.model,
                                         cfg.target_lang)
    else:
        client = LiveTranslateClient(on_live, cfg.target_lang)
    state.reset_meeting()
    await send({"type": "meeting", "meetingId": state.meeting_id,
                "glossarySize": len(state.context.glossary())})

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
                    state.context = context_store.load()
                    builder.set_glossary(state.context.glossary())
                    await send({"type": "meeting", "meetingId": state.meeting_id,
                                "glossarySize": len(state.context.glossary())})
    except WebSocketDisconnect:
        pass
    finally:
        # 先让模型把最后一句吐完再收摊，否则尾巴会丢
        client.stop()
        idler.cancel()
        # 模型在收到音频结束后还要几秒才吐完，固定等一段，不用空闲判据（它会在
        # 模型吐字的自然间隙里误判成已经结束，把最后半句切掉）
        await asyncio.sleep(3.5)
        await builder.close()
        runner.cancel()
        await asyncio.gather(runner, idler, return_exceptions=True)
        log.info("会议结束：音频 token %d，输出 token %d",
                 client.audio_tokens, client.response_tokens)


# 前端构建产物在 web/dist，构建过就直接由后端伺服，开发时走 vite
DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(DIST / "index.html")
