#!/usr/bin/env python3
"""Lokale Weboberfläche für die Liederdatenbank.

Start:  .venv/bin/python app.py   ->  http://127.0.0.1:5005

  * ChurchTools-Import starten (nutzt download_songs.py)
  * alle .sng-Lieder im Ordner songs/ durchsuchen und anzeigen
  * Lieder ohne .sng-Datei per Freitext erfassen, bearbeiten, löschen
  * Ablaufpläne aus ChurchTools ansehen und als ProPresenter-Playlist (.proplaylist) exportieren
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import threading
import time
import webbrowser
import zipfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

import download_songs as dl
import playlist
import pro_export
from sng import (
    Song,
    build_sng,
    is_manual_file,
    norm_title,
    parse_freetext,
    read_sng,
    song_to_freetext,
)

BASE_DIR = Path(__file__).resolve().parent
SONGS_DIR = Path(os.environ.get("SONGS_DIR", BASE_DIR / "songs")).resolve()
REPORT_NAME = "fehlende_sng.csv"

app = Flask(__name__, static_folder=None)


# ----------------------------------------------------------------------- Hilfen


def song_path(song_id: str) -> Path:
    """Löst eine Lied-ID (relativer Pfad) sicher innerhalb von SONGS_DIR auf."""
    path = (SONGS_DIR / song_id).resolve()
    if not path.is_relative_to(SONGS_DIR) or path.suffix.lower() != ".sng":
        abort(400, "Ungültige Lied-ID")
    return path


def song_json(path: Path, song: Song, with_text: bool = True, index: dict | None = None) -> dict:
    data = {
        "id": path.relative_to(SONGS_DIR).as_posix(),
        "title": song.title or path.stem,
        "exportName": pro_export.export_name(path, SONGS_DIR, song, index),
        "author": song.author,
        "ccli": song.ccli,
        "copyright": song.copyright,
        "manual": song.manual,
        "langCount": song.lang_count,
        "folder": path.parent.relative_to(SONGS_DIR).as_posix() if path.parent != SONGS_DIR else "",
        "modified": path.stat().st_mtime,
    }
    if with_text:
        data["sections"] = [{"label": s.label, "lines": s.lines} for s in song.ordered_sections()]
    return data


def load_missing() -> list[dict]:
    report = SONGS_DIR / REPORT_NAME
    if not report.exists():
        return []
    with open(report, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def error(message: str, status: int = 400):
    return jsonify({"error": message}), status


# ------------------------------------------------------------------------ Lieder


@app.get("/api/songs")
def list_songs():
    songs, broken = [], []
    index = dl.load_index(SONGS_DIR)
    if SONGS_DIR.exists():
        for path in sorted(SONGS_DIR.rglob("*.sng"), key=lambda p: p.name.casefold()):
            try:
                songs.append(song_json(path, read_sng(path), index=index))
            except Exception as exc:  # defekte Datei soll die Liste nicht blockieren
                broken.append({"id": path.relative_to(SONGS_DIR).as_posix(), "error": str(exc)})

    have = {norm_title(s["title"]) for s in songs}
    missing = []
    for row in load_missing():
        title = row.get("titel", "")
        missing.append(
            {
                "ctId": row.get("id", ""),
                "title": title,
                "category": row.get("kategorie", ""),
                "author": row.get("autor", ""),
                "link": row.get("link", ""),
                "covered": norm_title(title) in have,
            }
        )
    return jsonify({"songs": songs, "missing": missing, "broken": broken, "dir": str(SONGS_DIR)})


@app.get("/api/songs/<path:song_id>")
def get_song(song_id: str):
    path = song_path(song_id)
    if not path.exists():
        return error("Lied nicht gefunden", 404)
    song = read_sng(path)
    data = song_json(path, song)
    if song.manual:
        data["freetext"] = song_to_freetext(song)
    return jsonify(data)


@app.post("/api/preview")
def preview():
    sections, order, warnings = parse_freetext((request.json or {}).get("text", ""))
    by_label = {s.label: s for s in sections}
    return jsonify(
        {
            "sections": [{"label": l, "lines": by_label[l].lines} for l in order],
            "order": order,
            "warnings": warnings,
        }
    )


def _song_from_request(data: dict) -> tuple[Song, list[str]] | tuple[None, str]:
    title = " ".join((data.get("title") or "").split())
    if not title:
        return None, "Bitte einen Titel angeben."
    sections, order, warnings = parse_freetext(data.get("text") or "")
    if not sections:
        return None, "Bitte den Liedtext eingeben."
    song = Song(
        title=title,
        author=(data.get("author") or "").strip(),
        ccli=(data.get("ccli") or "").strip(),
        copyright=(data.get("copyright") or "").strip(),
        sections=sections,
        order=order,
    )
    return song, warnings


def _write_song(song: Song, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".sng.part")
    tmp.write_text(build_sng(song), encoding="utf-8")
    tmp.replace(target)


@app.post("/api/songs")
def create_song():
    song, result = _song_from_request(request.json or {})
    if song is None:
        return error(result)
    target = SONGS_DIR / f"{dl.safe_filename(song.title)}.sng"
    if target.exists():
        return error(f"Es gibt bereits eine Datei „{target.name}“. Bitte den Titel anpassen.", 409)
    _write_song(song, target)
    return jsonify({"id": target.relative_to(SONGS_DIR).as_posix(), "warnings": result}), 201


@app.put("/api/songs/<path:song_id>")
def update_song(song_id: str):
    path = song_path(song_id)
    if not path.exists():
        return error("Lied nicht gefunden", 404)
    if not is_manual_file(path):
        return error("Nur von Hand erfasste Lieder können hier bearbeitet werden.", 403)
    song, result = _song_from_request(request.json or {})
    if song is None:
        return error(result)
    target = path.parent / f"{dl.safe_filename(song.title)}.sng"
    if target != path and target.exists():
        return error(f"Es gibt bereits eine Datei „{target.name}“. Bitte den Titel anpassen.", 409)
    _write_song(song, target)
    if target != path:
        path.unlink()
    return jsonify({"id": target.relative_to(SONGS_DIR).as_posix(), "warnings": result})


@app.delete("/api/songs/<path:song_id>")
def delete_song(song_id: str):
    path = song_path(song_id)
    if not path.exists():
        return error("Lied nicht gefunden", 404)
    if not is_manual_file(path):
        return error("Nur von Hand erfasste Lieder können gelöscht werden.", 403)
    path.unlink()
    return jsonify({"ok": True})


# ------------------------------------------------------------------------ Export


def _export(path: Path, style, index: dict, taken: set[str] | None = None) -> tuple[str, str, bytes]:
    """-> (Dateiname ohne Endung, Zwischenformat, .pro). Name wie in ChurchTools, siehe pro_export.export_name."""
    song = read_sng(path)
    name = pro_export.export_name(path, SONGS_DIR, song, index)
    stem = pro_export.file_stem(name)
    if taken is not None:
        stem = pro_export.unique_stem(stem, taken)
    key = path.relative_to(SONGS_DIR).as_posix()  # stabile UUIDs je Datei
    text, data = pro_export.song_to_pro(song, style, key=key, name=name)
    return stem, text, data


def _style() -> pro_export.ExportStyle:
    """Formatierung aus dem Request (Vorschau mit ungespeicherten Änderungen), sonst die gespeicherte."""
    body = request.get_json(silent=True) if request.is_json else None
    if isinstance(body, dict) and isinstance(body.get("style"), dict):
        return pro_export.ExportStyle.from_dict(body["style"])
    return pro_export.load_style()


@app.get("/api/export/settings")
def get_export_settings():
    return jsonify({
        "style": pro_export.load_style().to_dict(),
        "defaults": pro_export.ExportStyle().to_dict(),
        "fonts": pro_export.font_choices(),
    })


@app.put("/api/export/settings")
def put_export_settings():
    style = pro_export.ExportStyle.from_dict(request.get_json(silent=True))
    pro_export.save_style(style)
    return jsonify({"style": style.to_dict()})


@app.post("/api/export/preview")
def export_preview():
    data = request.get_json(silent=True) or {}
    path = song_path(str(data.get("id", "")))
    if not path.exists():
        return error("Lied nicht gefunden", 404)
    style = _style()
    song = read_sng(path)
    return jsonify({**pro_export.preview(song, style), "style": style.to_dict(),
                    "exportName": pro_export.export_name(path, SONGS_DIR, song, dl.load_index(SONGS_DIR))})


@app.get("/api/export/<fmt>/<path:song_id>")
def export_song(fmt: str, song_id: str):
    """Einzelnes Lied als .pro (Download) oder als .json-Zwischenformat (Ansicht)."""
    path = song_path(song_id)
    if not path.exists():
        return error("Lied nicht gefunden", 404)
    stem, text, data = _export(path, pro_export.load_style(), dl.load_index(SONGS_DIR))
    if fmt == "pro":
        return send_file(io.BytesIO(data), mimetype="application/octet-stream",
                         as_attachment=True, download_name=f"{stem}.pro")
    if fmt == "json":
        return app.response_class(text, mimetype="text/plain; charset=utf-8")
    return error("Format muss pro oder json sein")


@app.post("/api/export")
def export_zip():
    """Mehrere Lieder (ids) oder alle (ids fehlt) als ZIP mit .pro-Dateien, optional plus .json.

    Formatierung: body.style, sonst export_settings.json.
    """
    data = request.json or {}
    if data.get("ids"):
        paths = [song_path(i) for i in data["ids"]]
    else:
        paths = sorted(SONGS_DIR.rglob("*.sng"), key=lambda p: p.name.casefold())
    include_json = bool(data.get("includeJson"))
    style = _style()

    index = dl.load_index(SONGS_DIR)
    taken: set[str] = set()
    buf = io.BytesIO()
    failed = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in paths:
            if not path.exists():
                continue
            try:
                stem, text, pro = _export(path, style, index, taken)
            except Exception as exc:  # ein defektes Lied soll den Export nicht abbrechen
                failed.append(f"{path.name}: {exc}")
                continue
            zf.writestr(f"{stem}.pro", pro)
            if include_json:
                zf.writestr(f"json/{stem}.json", text)
        if failed:
            zf.writestr("FEHLER.txt", "\n".join(failed))
    buf.seek(0)
    name = "ProPresenter-Lieder.zip" if not data.get("ids") else f"ProPresenter-Lieder ({len(paths)}).zip"
    resp = send_file(buf, mimetype="application/zip", as_attachment=True, download_name=name)
    resp.headers["X-Export-Failed"] = str(len(failed))
    return resp


# --------------------------------------------------------- ChurchTools-Anmeldung


class CTSession:
    """Angemeldeter ChurchTools-Client im Speicher dieses (nur lokal erreichbaren) Servers.

    Nötig für Ablaufpläne; Passwort wird nicht gespeichert, nur das Sitzungs-Cookie bzw. der Token
    im requests-Client, solange der Server läuft.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.client: dl.ChurchToolsClient | None = None
        self.user = ""

    def set(self, client: dl.ChurchToolsClient, user: str) -> None:
        with self.lock:
            self.client, self.user = client, user

    def clear(self) -> None:
        with self.lock:
            self.client, self.user = None, ""

    def status(self) -> dict:
        with self.lock:
            return {"loggedIn": self.client is not None, "user": self.user,
                    "url": self.client.base_url if self.client else ""}


ct = CTSession()


def _login(data: dict) -> tuple[dl.ChurchToolsClient, str]:
    """Anmeldung aus Formulardaten (url, user, password, totp | token)."""
    url = (data.get("url") or dl.DEFAULT_URL).strip()
    token = (data.get("token") or "").strip()
    user = (data.get("user") or "").strip()
    password = data.get("password") or ""
    if not token and not (user and password):
        raise dl.ChurchToolsError("Bitte Benutzername und Passwort oder einen Login-Token angeben.")
    client = dl.ChurchToolsClient(url)
    if token:
        name = client.login_with_token(token)
    else:
        totp = (data.get("totp") or "").strip()
        name = client.login_with_password(user, password, get_totp=lambda: totp)
    return client, name


def _ct_error(exc: Exception, url: str = ""):
    if isinstance(exc, requests.ConnectionError):
        return error(f"ChurchTools {url} nicht erreichbar – Adresse und Internetverbindung prüfen.", 502)
    if isinstance(exc, requests.Timeout):
        return error("ChurchTools antwortet nicht (Zeitüberschreitung).", 504)
    msg = str(exc)
    if "angemeldet" in msg:
        ct.clear()
        return jsonify({"error": "Die ChurchTools-Anmeldung ist abgelaufen. Bitte neu anmelden.", "needsLogin": True}), 401
    return jsonify({"error": msg, "needsTotp": "Zwei-Faktor" in msg}), 400


@app.get("/api/ct/status")
def ct_status():
    return jsonify(ct.status())


@app.post("/api/ct/login")
def ct_login():
    data = request.get_json(silent=True) or {}
    try:
        client, name = _login(data)
    except (dl.ChurchToolsError, requests.RequestException) as exc:
        return _ct_error(exc, data.get("url") or "")
    ct.set(client, name)
    return jsonify(ct.status())


@app.post("/api/ct/logout")
def ct_logout():
    ct.clear()
    return jsonify(ct.status())


# ------------------------------------------------------------------- Ablaufpläne


@app.get("/api/ct/events")
def ct_events():
    """Termine im Zeitraum (Standard: 7 Tage zurück bis 8 Wochen voraus)."""
    with ct.lock:
        client = ct.client
    if client is None:
        return jsonify({"error": "Bitte zuerst bei ChurchTools anmelden.", "needsLogin": True}), 401
    today = date.today()
    date_from = request.args.get("from") or (today - timedelta(days=7)).isoformat()
    date_to = request.args.get("to") or (today + timedelta(weeks=8)).isoformat()
    try:
        events = client.events(date_from, date_to)
    except (dl.ChurchToolsError, requests.RequestException) as exc:
        return _ct_error(exc, client.base_url)
    rows = sorted((playlist.event_json(e) for e in events), key=lambda e: e["start"] or "")
    return jsonify({"events": rows, "from": date_from, "to": date_to})


def _library():
    settings = playlist.load_settings()
    root = settings.root()
    docs = playlist.scan_library(root)
    return settings, root, docs


@app.get("/api/ct/events/<int:event_id>/plan")
def ct_plan(event_id: int):
    """Ablaufplan + Zuordnung der Lieder zu Präsentationen in der lokalen ProPresenter-Bibliothek."""
    with ct.lock:
        client = ct.client
    if client is None:
        return jsonify({"error": "Bitte zuerst bei ChurchTools anmelden.", "needsLogin": True}), 401
    try:
        agenda = client.agenda(event_id)
    except (dl.ChurchToolsError, requests.RequestException) as exc:
        return _ct_error(exc, client.base_url)
    if agenda is None:
        return error("Für diesen Termin gibt es keinen Ablaufplan.", 404)
    settings, root, docs = _library()
    matcher = playlist.Matcher(docs, settings.library_order)
    # Name/Beginn kommen aus der Terminliste der Oberfläche (spart eine zweite Abfrage).
    ev = {"name": request.args.get("name") or agenda.get("name") or "", "startDate": request.args.get("start") or ""}
    return jsonify({
        "agenda": {"id": agenda.get("id"), "name": agenda.get("name") or "", "isFinal": bool(agenda.get("isFinal"))},
        "items": playlist.plan_items(agenda, matcher, settings.fuzzy),
        "playlistName": playlist.default_playlist_name(ev, agenda),
        "libraryFound": bool(docs),
    })


@app.get("/api/playlist/library")
def playlist_library():
    settings, root, docs = _library()
    return jsonify({
        "root": str(root) if root else "",
        "detected": playlist.detect_show_root(),
        "libraries": playlist.ordered_libraries(docs, settings.library_order),
        "docs": [d.to_json() for d in docs],
        "settings": settings.to_dict(),
    })


@app.put("/api/playlist/settings")
def put_playlist_settings():
    settings = playlist.PlaylistSettings.from_dict(request.get_json(silent=True))
    if settings.show_root and not (Path(settings.show_root).expanduser() / "Libraries").is_dir():
        return error(f"Im Ordner „{settings.show_root}“ gibt es keinen Unterordner „Libraries“.")
    playlist.save_settings(settings)
    return playlist_library()


@app.post("/api/playlist")
def make_playlist():
    """Zeilen aus der Oberfläche -> .proplaylist (Download). body: {name, items: [{type, title, include, rel}]}"""
    data = request.get_json(silent=True) or {}
    name = " ".join((data.get("name") or "").split()) or "Ablaufplan"
    rows = data.get("items") or []
    if not isinstance(rows, list) or not rows:
        return error("Der Ablaufplan ist leer.")
    settings, root, docs = _library()
    if isinstance(data.get("settings"), dict):
        settings = playlist.PlaylistSettings.from_dict({**settings.to_dict(), **data["settings"]})
    entries, notes = playlist.playlist_entries(rows, settings, {d.rel: d for d in docs})
    if not entries:
        return error("Keine Einträge ausgewählt.")
    body = playlist.to_proplaylist(playlist.build_playlist(name, entries, root))
    resp = send_file(io.BytesIO(body), mimetype="application/octet-stream", as_attachment=True,
                     download_name=f"{dl.safe_filename(name)}.proplaylist")
    resp.headers["X-Playlist-Items"] = str(len(entries))
    resp.headers["X-Playlist-Notes"] = quote(json.dumps(notes, ensure_ascii=False))
    return resp


# ------------------------------------------------------------------------ Import


class ImportJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log: list[str] = []
        self.summary: dict | None = None
        self.started: float | None = None

    def write(self, line: str) -> None:
        with self.lock:
            self.log.append(line.rstrip("\n"))

    def snapshot(self, since: int) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "log": self.log[since:],
                "next": len(self.log),
                "summary": self.summary,
                "started": self.started,
            }


job = ImportJob()


def _run_import(creds: dict, by_category: bool) -> None:
    summary = {"ok": False}
    url = (creds.get("url") or dl.DEFAULT_URL).strip()
    try:
        session = ct.status()
        if not (creds.get("token") or creds.get("password")) and session["loggedIn"]:
            client, name = ct.client, session["user"]
            job.write(f"Bestehende Anmeldung bei {client.base_url} wird verwendet.")
        else:
            job.write(f"Verbinde mit {url.rstrip('/')} …")
            client, name = _login(creds)
            ct.set(client, name)  # auch für Ablaufpläne nutzbar
        job.write(f"Angemeldet als {name}")
        result = dl.run(client, SONGS_DIR, by_category, log=job.write)
        report = dl.write_report(SONGS_DIR, result)
        dl.write_index(SONGS_DIR, result)
        summary = {
            "ok": not result.failed,
            "downloaded": len(result.downloaded),
            "missing": len(result.missing),
            "failed": [{"title": t, "error": e} for t, e in result.failed],
            "report": report.name,
        }
        job.write("")
        job.write(f"Fertig: {len(result.downloaded)} heruntergeladen, {len(result.missing)} ohne .sng, "
                  f"{len(result.failed)} Fehler.")
    except requests.ConnectionError:
        msg = f"ChurchTools unter {url} nicht erreichbar – Adresse und Internetverbindung prüfen."
        job.write(f"Abbruch: {msg}")
        summary = {"ok": False, "error": msg}
    except requests.Timeout:
        msg = "ChurchTools antwortet nicht (Zeitüberschreitung). Bitte später erneut versuchen."
        job.write(f"Abbruch: {msg}")
        summary = {"ok": False, "error": msg}
    except (dl.ChurchToolsError, requests.RequestException) as exc:
        job.write(f"Abbruch: {exc}")
        summary = {"ok": False, "error": str(exc), "needsTotp": "Zwei-Faktor" in str(exc)}
    except Exception as exc:
        job.write(f"Unerwarteter Fehler: {exc!r}")
        summary = {"ok": False, "error": repr(exc)}
    finally:
        with job.lock:
            job.summary = summary
            job.running = False


@app.post("/api/import")
def start_import():
    data = request.json or {}
    token = (data.get("token") or "").strip()
    user = (data.get("user") or "").strip()
    password = data.get("password") or ""
    if not token and not (user and password) and not ct.status()["loggedIn"]:
        return error("Bitte Benutzername und Passwort oder einen Login-Token angeben.")
    with job.lock:
        if job.running:
            return error("Ein Import läuft bereits.", 409)
        job.running, job.log, job.summary, job.started = True, [], None, time.time()
    threading.Thread(
        target=_run_import,
        args=({k: data.get(k) for k in ("url", "user", "password", "token", "totp")}, bool(data.get("byCategory"))),
        daemon=True,
    ).start()
    return jsonify({"ok": True}), 202


@app.get("/api/import")
def import_status():
    return jsonify(job.snapshot(request.args.get("since", 0, type=int)))


@app.get("/api/config")
def config():
    return jsonify({"defaultUrl": dl.DEFAULT_URL, "songsDir": str(SONGS_DIR)})


# ------------------------------------------------------------------------ Seite


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "web", "index.html")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lokale Weboberfläche für die Liederdatenbank")
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    print(f"Liederverwaltung läuft auf {url}  (Beenden mit Strg+C)")
    print(f"Liederordner: {SONGS_DIR}")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    # Nur lokal erreichbar – Zugangsdaten gehen über diesen Server.
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
