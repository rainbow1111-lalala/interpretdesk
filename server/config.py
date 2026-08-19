"""集中放可调参数。凭证只从环境变量读。"""
import os
from dataclasses import dataclass
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
SESSIONS_DIR = DATA_DIR / "sessions"
# 两种运行形态。留空是公网多人模式（按会话隔离）；设成 1 是本机单人模式，自己开会时用，
# 数据仍然落在 data/ 原来的位置。对外部署时绝对不能开这个开关，开了就是所有人共用一套底稿。
SINGLE_USER_ENV = "MI_SINGLE_USER"

# 上传限额。公网上的上传接口没有上限，等于让任何人把磁盘塞满，服务停摆连带别人的会议。
# 一份会前底稿几十万字也就几兆，一次 20 兆、一个会话总共 60 兆足够宽松。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_SESSION_BYTES = 60 * 1024 * 1024
# 体积限额挡不住一堆小文件：几百份几十 KB 的文本同样能把向量索引撑爆、把提炼拖垮。
# 一场会的会前材料十份足够宽松，真实用过的两份合计七万多字。
MAX_SESSION_DOCS = 10


def single_user() -> bool:
    return os.environ.get(SINGLE_USER_ENV, "").strip() in ("1", "true", "yes")
# 会话多久没动就算过期，清理任务据此删目录。会前上传底稿、会后导出纪要，七天够用
SESSION_TTL_S = 7 * 24 * 3600


@dataclass(frozen=True)
class Workspace:
    """一个会话独占的存储根。

    多人同时用这套程序时，隔离全靠这个对象：底稿、索引、摘要、设置、会议库、回收站
    一律从 root 派生，模块里不许再出现进程级的固定路径常量。函数一律显式收
    Workspace，不走隐式上下文，因为串号是这类系统最致命的缺陷，宁可多传一个参数，
    也不要一个读错了当场没人发现的隐式全局。
    """

    root: Path

    @classmethod
    def for_session(cls, sid: str) -> "Workspace":
        # 本机单人模式下所有请求共用 data/ 本身，也就是这套程序原来的布局。这样自己开会时
        # 现有底稿、设置、会议库原地可用，不必迁移；公网多人模式才按会话分目录。
        return cls(DATA_DIR) if single_user() else cls(SESSIONS_DIR / sid)

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    @property
    def index(self) -> Path:
        return self.root / "index.json"

    @property
    def trash(self) -> Path:
        return self.root / "trash"

    @property
    def brief(self) -> Path:
        return self.root / "context.json"

    @property
    def settings(self) -> Path:
        return self.root / "settings.json"

    @property
    def db(self) -> Path:
        return self.root / "meetings.db"

    @property
    def minutes(self) -> Path:
        return self.root / "minutes"


def api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(f"环境变量 {API_KEY_ENV} 未设置")
    return key
