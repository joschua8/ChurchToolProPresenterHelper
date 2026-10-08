"""Speicherorte – im Quellcode-Ordner oder als gepacktes Programm (PyInstaller).

  * RES_DIR:  mitgelieferte, nur lesbare Dateien (web/, examples/, Standard-Formatierung)
  * DATA_DIR: Lieder, Einstellungen, Sicherungen. Beim Start aus dem Quellcode der Projektordner,
              im gepackten Programm ~/Liederverwaltung (änderbar über LIEDERVERWALTUNG_DATA).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
_SRC_DIR = Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", _SRC_DIR))

_env = os.environ.get("LIEDERVERWALTUNG_DATA")
if _env:
    DATA_DIR = Path(_env).expanduser()
elif FROZEN:
    DATA_DIR = Path.home() / "Liederverwaltung"
else:
    DATA_DIR = _SRC_DIR


def ensure_data_dir() -> None:
    """Legt den Datenordner an und übernimmt beim ersten Start die mitgelieferte Formatierung."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / "export_settings.json"
    source = RES_DIR / "export_settings.json"
    if not target.exists() and source.exists() and source != target:
        shutil.copyfile(source, target)
