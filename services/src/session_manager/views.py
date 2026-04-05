from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"

STATUS_HTML = (_TEMPLATES_DIR / "debug.html").read_text(encoding="utf-8")
CHAT_HTML = (_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
SESSIONS_HTML = (_TEMPLATES_DIR / "sessions.html").read_text(encoding="utf-8")
