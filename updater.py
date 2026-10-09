"""Selbst-Aktualisierung des gepackten Programms über GitHub-Releases.

Ablauf: Beim Start (vor dem Server) und danach alle paar Stunden wird das neueste Release von
github.com/joschua8/churchtools-propresenter-bridge abgefragt. Ist es neuer als die eigene Version,
wird die ZIP für diese Plattform geladen, das Programm daneben ausgepackt und die laufende Datei
ersetzt; danach startet das Programm neu.

  * Version: version.py (vom Build aus Datum + Laufnummer geschrieben, z. B. 2026.10.09.42).
    Aus dem Quellcode gestartet („dev“) wird nie aktualisiert.
  * Windows: eine laufende .exe kann nicht überschrieben, aber umbenannt werden ->
    alte Datei wird zu „….exe.old“ (beim nächsten Start gelöscht), neue kommt an ihren Platz.
  * PyInstaller (eine Datei): Neustart mit PYINSTALLER_RESET_ENVIRONMENT=1, sonst hält sich das neue
    Programm für den Kindprozess des alten und nutzt dessen (gleich gelöschten) Entpack-Ordner.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import requests

import paths

try:
    from version import __version__ as VERSION
except ImportError:  # Quellcode ohne Build
    VERSION = "dev"

REPO = "joschua8/churchtools-propresenter-bridge"
LATEST_URL = os.environ.get("LIEDBRUECKE_UPDATE_URL", f"https://api.github.com/repos/{REPO}/releases/latest")  # Env: zum Testen
CHECK_INTERVAL = 6 * 3600
ENV_DISABLE = "LIEDBRUECKE_NO_UPDATE"


class UpdateError(RuntimeError):
    pass


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", v or ""))


def asset_name() -> str:
    """Name der Release-ZIP für dieses Programm (wie im Build-Workflow)."""
    if sys.platform.startswith("win"):
        return "Liedbruecke-Windows.zip"
    if sys.platform == "darwin":
        arm = platform.machine().lower() in ("arm64", "aarch64")
        return f"Liedbruecke-macOS-{'AppleSilicon' if arm else 'Intel'}.zip"
    return ""


def can_update() -> tuple[bool, str]:
    if not paths.FROZEN:
        return False, "Aus dem Quellcode gestartet – Updates über git."
    if os.environ.get(ENV_DISABLE):
        return False, f"Abgeschaltet ({ENV_DISABLE})."
    if VERSION == "dev":
        return False, "Programm ohne Versionsnummer gebaut."
    if not asset_name():
        return False, "Für dieses Betriebssystem gibt es keine Updates."
    return True, ""


class Updater:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest: dict | None = None   # {version, url, size, notes, page}
        self.checked_at = 0.0
        self.error = ""
        self.installing = False
        self.newest = ""                  # neueste veröffentlichte Version (auch wenn nicht neuer)

    # ------------------------------------------------------------------ Abfrage

    def check(self, timeout: float = 15) -> dict | None:
        """Neuestes Release abfragen; Ergebnis nur, wenn es neuer ist als diese Version."""
        try:
            resp = requests.get(LATEST_URL, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
            if resp.status_code == 404:
                raise UpdateError("Noch kein Release veröffentlicht.")
            resp.raise_for_status()
            rel = resp.json()
        except (requests.RequestException, ValueError) as exc:
            with self.lock:
                self.error, self.checked_at = f"Update-Prüfung fehlgeschlagen: {exc}", time.time()
            return None
        except UpdateError as exc:
            with self.lock:
                self.error, self.checked_at = str(exc), time.time()
            return None
        version = (rel.get("tag_name") or "").lstrip("v")
        asset = next((a for a in rel.get("assets") or [] if a.get("name") == asset_name()), None)
        latest = None
        if asset and version_tuple(version) > version_tuple(VERSION):
            latest = {"version": version, "url": asset.get("browser_download_url"), "size": asset.get("size") or 0,
                      "notes": (rel.get("body") or "").strip()[:2000], "page": rel.get("html_url") or ""}
        with self.lock:
            self.latest, self.newest, self.error, self.checked_at = latest, version, "", time.time()
        return latest

    def status(self) -> dict:
        ok, why = can_update()
        with self.lock:
            return {
                "version": VERSION,
                "enabled": ok,
                "reason": why,
                "available": self.latest,
                "newest": self.newest,
                "checkedAt": self.checked_at,
                "error": self.error,
                "installing": self.installing,
            }

    # ------------------------------------------------------------------ Installation

    def install(self, latest: dict, log=print) -> None:
        """Lädt die neue Fassung und ersetzt die laufende Programmdatei (Neustart macht restart())."""
        ok, why = can_update()
        if not ok:
            raise UpdateError(why)
        exe = Path(sys.executable).resolve()
        with tempfile.TemporaryDirectory(prefix="lv-update-") as tmp:
            archive = Path(tmp) / "update.zip"
            log(f"Lade Version {latest['version']} …")
            try:
                with requests.get(latest["url"], stream=True, timeout=60) as resp:
                    resp.raise_for_status()
                    with open(archive, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=1 << 16):
                            fh.write(chunk)
            except requests.RequestException as exc:
                raise UpdateError(f"Download fehlgeschlagen: {exc}") from exc
            if latest.get("size") and archive.stat().st_size != latest["size"]:
                raise UpdateError("Download unvollständig.")
            try:
                with zipfile.ZipFile(archive) as zf:
                    member = next((n for n in zf.namelist()
                                   if Path(n).name.startswith("Liedbruecke") and not n.endswith((".md", "/"))), None)
                    if member is None:
                        raise UpdateError("Programmdatei fehlt in der ZIP.")
                    new = exe.with_name(exe.name + ".new")  # neben dem Programm: rename geht nicht über Laufwerke
                    with zf.open(member) as src, open(new, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            except zipfile.BadZipFile as exc:
                raise UpdateError("Download ist keine gültige ZIP.") from exc
        new.chmod(0o755)
        try:
            if sys.platform.startswith("win"):
                old = exe.with_name(exe.name + ".old")
                if old.exists():
                    old.unlink()
                exe.rename(old)  # laufende .exe darf umbenannt, aber nicht überschrieben werden
                new.rename(exe)
            else:
                os.replace(new, exe)  # laufendes Programm behält die alte Datei bis zum Ende
                subprocess.run(["xattr", "-d", "com.apple.quarantine", str(exe)], capture_output=True)
        except OSError as exc:
            new.unlink(missing_ok=True)
            raise UpdateError(f"Programmdatei nicht ersetzbar ({exc}). Liegt sie in einem schreibgeschützten Ordner?") from exc
        log(f"Version {latest['version']} installiert.")


def cleanup_old() -> None:
    """Reste eines früheren Updates entfernen (Windows: alte .exe)."""
    if not paths.FROZEN:
        return
    exe = Path(sys.executable).resolve()
    for suffix in (".old", ".new"):
        leftover = exe.with_name(exe.name + suffix)
        for _ in range(10):  # die alte .exe ist evtl. noch kurz gesperrt
            try:
                leftover.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.5)


def restart(args: list[str]) -> None:
    """Startet das (neue) Programm mit `args` und beendet dieses."""
    exe = str(Path(sys.executable).resolve())
    # Werkzeug macht den Server-Socket vererbbar und merkt ihn sich in WERKZEUG_SERVER_FD –
    # ohne Aufräumen hielte das neue Programm den Port selbst belegt.
    env = {k: v for k, v in os.environ.items() if not k.startswith("WERKZEUG_")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    sys.stdout.flush()
    if sys.platform.startswith("win"):
        subprocess.Popen([exe, *args], env=env, creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
        os._exit(0)
    os.closerange(3, 4096)
    os.execve(exe, [exe, *args], env)  # gleiches Terminal-Fenster


updater = Updater()
