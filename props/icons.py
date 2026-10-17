"""The sites' own icons, embedded as data URIs so the pages work offline: a toggle shows a site's icon in place of its name."""
import base64
from pathlib import Path

DIR = Path(__file__).with_name("img")
_TYPES = {".png": "image/png", ".webp": "image/webp"}


def icon(name: str) -> str:
    """props/img/<name> as a data URI."""
    path = DIR / name
    return f"data:{_TYPES[path.suffix]};base64,{base64.b64encode(path.read_bytes()).decode()}"
