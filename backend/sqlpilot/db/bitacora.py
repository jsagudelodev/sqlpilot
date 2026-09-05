"""Bitácora local de todo lo que SQLPilot ejecuta contra el servidor (JSONL)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _ruta() -> Path:
    base = Path(os.environ.get("SQLPILOT_HOME", Path.home() / ".sqlpilot"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "bitacora.jsonl"


def registrar(evento: str, **datos: Any) -> None:
    registro = {
        "ts": datetime.now(UTC).isoformat(),
        "usuario": os.environ.get("USERNAME") or os.environ.get("USER"),
        "evento": evento,
        **datos,
    }
    try:
        with _ruta().open("a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # la bitácora nunca debe romper el flujo principal
