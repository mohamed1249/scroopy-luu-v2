"""Owned per-request files. Never change the process working directory."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import uuid
from urllib.parse import unquote


def safe_component(value):
    # Decode before sanitizing; generated prefixes avoid Windows device names.
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", unquote(str(value))).strip(". _")
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"item-{label[:16] or 'untitled'}-{digest}"


class RunWorkspace:
    def __init__(self, base="runs"):
        self.base = Path(base).resolve()
        if os.name == "nt" and not str(self.base).startswith("\\\\?\\"):
            absolute = str(self.base)
            self.base = Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\")
                             else "\\\\?\\" + absolute)
        self.base.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex
        self.root = self.base / self.run_id
        self.root.mkdir(exist_ok=False)

    def page(self, index, url, parent=None):
        base = self.root / "pages" if parent is None else Path(parent) / "subpages"
        folder = base / (f"{index:04d}-{safe_component(url)}" if parent is None else f"{index:04d}")
        if len(str(folder)) > 220:
            folder = self.root / "deep-pages" / f"{index:04d}-{safe_component(url)}"
        folder.mkdir(parents=True, exist_ok=False)
        return folder

    def relative(self, path):
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    def cleanup(self):
        # Only remove this instance's direct child, never an input-derived path.
        if self.root.is_symlink() or self.root.resolve().parent != self.base:
            raise ValueError("Refusing cleanup outside owned run directory")
        if self.root.name != self.run_id:
            raise ValueError("Run directory identity changed")
        shutil.rmtree(self.root)
