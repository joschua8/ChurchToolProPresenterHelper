"""Speicherorte – im Quellcode-Ordner oder als gepacktes Programm (PyInstaller).

  * RES_DIR:  mitgelieferte, nur lesbare Dateien (web/, examples/, Standard-Formatierung)
  * DATA_DIR: Lieder, Einstellungen, Sicherungen. Beim Start aus dem Quellcode der Projektordner,
              im gepackten Programm ~/SongBridge (änderbar über SONGBRIDGE_DATA;
              ein vorhandenes ~/Liedbruecke bzw. ~/Liederverwaltung von früheren Namen wird weiter benutzt).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
_SRC_DIR = Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", _SRC_DIR))

_env = next((os.environ[k] for k in ("SONGBRIDGE_DATA", "LIEDBRUECKE_DATA", "LIEDERVERWALTUNG_DATA")
             if os.environ.get(k)), "")
if _env:
    DATA_DIR = Path(_env).expanduser()
elif FROZEN:
    # Frühere Programmnamen „Liedbruecke“/„Liederverwaltung“: vorhandenen Datenordner weiter benutzen.
    _dirs = [Path.home() / n for n in ("SongBridge", "Liedbruecke", "Liederverwaltung")]
    DATA_DIR = next((d for d in _dirs if d.is_dir()), _dirs[0])
else:
    DATA_DIR = _SRC_DIR


def ensure_data_dir() -> None:
    """Legt den Datenordner an und übernimmt beim ersten Start die mitgelieferte Formatierung."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / "export_settings.json"
    source = RES_DIR / "export_settings.json"
    if not target.exists() and source.exists() and source != target:
        shutil.copyfile(source, target)
