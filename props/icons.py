"""The sites' own icons, as files in site/img/ whose names carry a hash of their bytes: browsers
keep them for a year (site/.htaccess) instead of getting them again inside every page, and a new
icon gets a new name. Relative paths, so the pages still work opened from disk."""
import hashlib
import shutil
from pathlib import Path

DIR = Path(__file__).with_name("img")


def _name(path: Path) -> str:
    return f"{path.stem}.{hashlib.blake2b(path.read_bytes(), digest_size=4).hexdigest()}{path.suffix}"


def icon(name: str) -> str:
    """props/img/<name> as the page's path to it: img/<name stem>.<hash><suffix>."""
    return f"img/{_name(DIR / name)}"


def publish(out: Path) -> None:
    """Every icon into out/img/ under its hashed name; the ones no longer made, removed."""
    folder = out / "img"
    folder.mkdir(parents=True, exist_ok=True)
    wanted = {_name(path): path for path in DIR.iterdir() if path.suffix in (".png", ".webp")}
    for name, path in wanted.items():
        if not (folder / name).exists():
            part = folder / f".{name}.part"
            shutil.copyfile(path, part)
            part.replace(folder / name)
    for path in folder.iterdir():
        if path.name not in wanted and not path.name.startswith("."):
            path.unlink(missing_ok=True)
