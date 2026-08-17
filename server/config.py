"""集中放可调参数。凭证只从环境变量读。"""
import os
from pathlib import Path

API_KEY_ENV = "GEMINI_API_KEY"

# 实时同传：音频进，源语转写＋目标语转写＋目标语音频出
LIVE_MODEL = "models/gemini-3.5-live-translate-preview"
LIVE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
# 拟稿与背景摘要。实测首字延迟（2026-08-17）：3.1-flash-lite 1.6s，3.5-flash 4.1s，
# 3.7-flash 当时返回 503。会议里先要快，慢档留给「精修」按钮。
FAST_MODEL = "gemini-3.1-flash-lite"
GOOD_MODEL = "gemini-3.5-flash"
TEXT_MODEL = FAST_MODEL
TEXT_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:{method}"

TARGET_LANG = "zh-CN"
SAMPLE_RATE = 16000
CHUNK_MS = 100
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MS // 1000  # 3200

# 断句：多久没有新转写就认为这一段说完了
TURN_IDLE_CLOSE_S = 1.8
TURN_HARD_CLOSE_S = 4.0
TURN_MAX_CHARS = 400

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "meetings.db"


def api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(f"环境变量 {API_KEY_ENV} 未设置")
    return key
