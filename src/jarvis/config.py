from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    home: Path
    defaults: dict = field(default_factory=dict)

    @classmethod
    def load(cls, home: str | Path | None = None) -> Settings:
        source_root = Path(__file__).resolve().parents[2]
        fallback = (
            source_root if (source_root / "pyproject.toml").exists() else Path.home() / ".jarvis"
        )
        root = Path(home or os.environ.get("JARVIS_HOME", fallback)).expanduser().resolve()
        defaults = {}
        for name in ("settings.toml", "local.toml"):
            path = root / "config" / name
            if path.exists():
                defaults.update(tomllib.loads(path.read_text(encoding="utf-8")))
        for directory in ("data", "outputs"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        return cls(root, defaults)

    @property
    def db(self) -> Path:
        return self.home / "data" / "jarvis.sqlite3"

    @property
    def cdp_url(self) -> str:
        return self.defaults.get("cdp_url", "http://127.0.0.1:9222")
