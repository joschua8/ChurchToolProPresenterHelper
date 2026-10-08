#!/usr/bin/env python3
"""Lokale Weboberfläche für die Liederdatenbank.

Start:  .venv/bin/python app.py   ->  http://127.0.0.1:5005

  * Lieder: alle .sng-Lieder im Ordner songs/ durchsuchen, ohne .sng per Freitext erfassen
  * ChurchTools: Ablaufpläne ansehen und als ProPresenter-Playlist (.proplaylist) exportieren;
    Lieder-Import mit Abgleich (nur neue/geänderte Lieder werden geladen, download_songs.py)
  * ProPresenter: Abgleich Liederdatenbank <-> Lieder-Bibliothek (pp_sync.py), Export als .pro
  * Einstellungen: ChurchTools-Anmeldung, Ordner der ProPresenter-Lieder-Bibliothek
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
import paths
import pp_sync
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

BASE_DIR = paths.RES_DIR
SONGS_DIR = Path(os.environ.get("SONGS_DIR", paths.DATA_DIR / "songs")).resolve()
REPORT_NAME = "fehlende_sng.csv"
APP_SETTINGS_PATH = Path(os.environ.get("APP_SETTINGS", paths.DATA_DIR / "app_settings.json"))
BACKUP_DIR = Path(os.environ.get("PP_BACKUP_DIR", paths.DATA_DIR / "backup"))

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


# ------------------------------------------------------------------ Einstellungen


def load_app_settings() -> dict:
    """ChurchTools-Adresse, Benutzername und (nur wenn gewünscht) Login-Token."""
    data = {"ct_url": dl.DEFAULT_URL, "ct_user": "", "ct_token": ""}
    try:
        saved = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            data.update({k: str(saved[k]).strip() for k in data if isinstance(saved.get(k), str)})
    except (OSError, ValueError):
        pass
    return data


def save_app_settings(**changes) -> dict:
    data = {**load_app_settings(), **{k: (v or "").strip() for k, v in changes.items()}}
    APP_SETTINGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        APP_SETTINGS_PATH.chmod(0o600)  # enthält ggf. den Login-Token
    except OSError:
        pass
    return data


# --------------------------------------------------------- ChurchTools-Anmeldung


class CTSession:
    """Angemeldeter ChurchTools-Client im Speicher dieses (nur lokal erreichbaren) Servers.

    Das Passwort wird nie gespeichert. Ist in den Einstellungen ein Login-Token hinterlegt,
    meldet sich der Server damit bei Bedarf selbst an.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.client: dl.ChurchToolsClient | None = None
        self.user = ""
        self.auto_error = ""
        self._auto_tried = ""

    def set(self, client: dl.ChurchToolsClient, user: str) -> None:
        with self.lock:
            self.client, self.user, self.auto_error = client, user, ""

    def clear(self, manual: bool = False) -> None:
        """manual: vom Benutzer abgemeldet -> nicht gleich wieder automatisch mit dem Token anmelden."""
        cfg = load_app_settings()
        with self.lock:
            self.client, self.user = None, ""
            self._auto_tried = cfg["ct_token"] + cfg["ct_url"] if manual else ""

    def get(self) -> dl.ChurchToolsClient | None:
        """Aktueller Client; meldet sich mit gespeichertem Token an, falls nötig (einmal pro Token)."""
        with self.lock:
            if self.client is not None:
                return self.client
        cfg = load_app_settings()
        token = cfg["ct_token"]
        if not token or self._auto_tried == token + cfg["ct_url"]:
            return None
        self._auto_tried = token + cfg["ct_url"]
        try:
            client = dl.ChurchToolsClient(cfg["ct_url"] or dl.DEFAULT_URL)
            self.set(client, client.login_with_token(token))
            return client
        except (dl.ChurchToolsError, requests.RequestException) as exc:
            self.auto_error = f"Anmeldung mit gespeichertem Token fehlgeschlagen: {exc}"
            return None

    def status(self) -> dict:
        self.get()
        with self.lock:
            return {"loggedIn": self.client is not None, "user": self.user,
                    "url": self.client.base_url if self.client else "", "autoError": self.auto_error}


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
        return jsonify({"error": "Die ChurchTools-Anmeldung ist abgelaufen. Bitte unter Einstellungen neu anmelden.",
                        "needsLogin": True}), 401
    return jsonify({"error": msg, "needsTotp": "Zwei-Faktor" in msg}), 400


def _need_client():
    client = ct.get()
    if client is None:
        return None, (jsonify({"error": "Bitte zuerst unter Einstellungen bei ChurchTools anmelden.",
                               "needsLogin": True}), 401)
    return client, None


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
    token = (data.get("token") or "").strip()
    save_app_settings(ct_url=client.base_url, ct_user=(data.get("user") or "").strip() or load_app_settings()["ct_user"],
                      **({"ct_token": token} if token and data.get("remember") else {}))
    return jsonify(ct.status())


@app.post("/api/ct/logout")
def ct_logout():
    ct.clear(manual=True)
    if (request.get_json(silent=True) or {}).get("forget"):
        save_app_settings(ct_token="")
    return jsonify(ct.status())


@app.get("/api/settings")
def get_settings():
    cfg = load_app_settings()
    return jsonify({"ctUrl": cfg["ct_url"], "ctUser": cfg["ct_user"], "ctTokenSaved": bool(cfg["ct_token"]),
                    "songsDir": str(SONGS_DIR), "defaultUrl": dl.DEFAULT_URL, "ct": ct.status()})


@app.put("/api/settings")
def put_settings():
    data = request.get_json(silent=True) or {}
    changes = {}
    if isinstance(data.get("ctUrl"), str):
        changes["ct_url"] = data["ctUrl"]
    if isinstance(data.get("ctUser"), str):
        changes["ct_user"] = data["ctUser"]
    if data.get("forgetToken"):
        changes["ct_token"] = ""
    save_app_settings(**changes)
    return get_settings()


# ------------------------------------------------------------------- Ablaufpläne


@app.get("/api/ct/events")
def ct_events():
    """Termine im Zeitraum (Standard: 7 Tage zurück bis 8 Wochen voraus)."""
    client, err = _need_client()
    if err:
        return err
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
    client, err = _need_client()
    if err:
        return err
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
    song_dir = settings.song_dir(docs)
    libs_dir = root / "Libraries" if root else None
    return jsonify({
        "root": str(root) if root else "",
        "detected": playlist.detect_show_root(),
        "libraries": playlist.ordered_libraries(docs, settings.library_order),
        "libraryDirs": [{"name": playlist.nfc(d.name), "path": str(d)}
                        for d in sorted(libs_dir.iterdir(), key=lambda p: p.name.casefold())
                        if d.is_dir()] if libs_dir and libs_dir.is_dir() else [],
        "songDir": str(song_dir) if song_dir else "",
        "songDirOk": bool(song_dir and song_dir.is_dir()),
        "songDirCount": len(pp_sync.pp_files(song_dir)),
        "docs": [d.to_json() for d in docs],
        "settings": settings.to_dict(),
    })


@app.put("/api/playlist/settings")
def put_playlist_settings():
    settings = playlist.PlaylistSettings.from_dict(request.get_json(silent=True))
    if settings.show_root and not (Path(settings.show_root).expanduser() / "Libraries").is_dir():
        return error(f"Im Ordner „{settings.show_root}“ gibt es keinen Unterordner „Libraries“.")
    if settings.song_library and not Path(settings.song_library).expanduser().is_dir():
        return error(f"Den Ordner „{settings.song_library}“ gibt es nicht.")
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
        self.plan: dl.SyncPlan | None = None   # letzter Abgleich (Grundlage für den Download)
        self.plan_time: float | None = None

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


@app.post("/api/import/diff")
def import_diff():
    """Fragt nur die Liederliste ab und vergleicht sie mit dem lokalen Stand (lädt nichts herunter)."""
    client, err = _need_client()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    with job.lock:
        if job.running:
            return error("Ein Import läuft gerade.", 409)
    try:
        songs = list(client.iter_songs())
    except (dl.ChurchToolsError, requests.RequestException) as exc:
        return _ct_error(exc, client.base_url)
    plan = dl.plan_sync(songs, SONGS_DIR, bool(data.get("byCategory")), client.base_url)
    with job.lock:
        job.plan, job.plan_time = plan, time.time()
    return jsonify({**plan.to_json(), "checked": job.plan_time})


def _run_import(client: dl.ChurchToolsClient, plan: dl.SyncPlan, only: set[str] | None,
                force: bool, delete: set[str]) -> None:
    summary = {"ok": False}
    try:
        job.write(f"Angemeldet als {ct.user} bei {client.base_url}")
        result = dl.sync(client, SONGS_DIR, plan, only=only, force=force, delete=delete, log=job.write)
        report = dl.write_report(SONGS_DIR, result)
        dl.write_index(SONGS_DIR, result)
        summary = {
            "ok": not result.failed,
            "downloaded": len(result.downloaded),
            "skipped": result.skipped,
            "deleted": len(result.deleted),
            "missing": len(result.missing),
            "failed": [{"title": t, "error": e} for t, e in result.failed],
            "report": report.name,
        }
        job.write("")
        job.write(f"Fertig: {len(result.downloaded)} heruntergeladen, {result.skipped} unverändert, "
                  f"{len(result.missing)} ohne .sng, {len(result.failed)} Fehler"
                  + (f", {len(result.deleted)} gelöscht." if result.deleted else "."))
    except requests.ConnectionError:
        msg = f"ChurchTools unter {client.base_url} nicht erreichbar – Adresse und Internetverbindung prüfen."
        job.write(f"Abbruch: {msg}")
        summary = {"ok": False, "error": msg}
    except requests.Timeout:
        msg = "ChurchTools antwortet nicht (Zeitüberschreitung). Bitte später erneut versuchen."
        job.write(f"Abbruch: {msg}")
        summary = {"ok": False, "error": msg}
    except (dl.ChurchToolsError, requests.RequestException) as exc:
        job.write(f"Abbruch: {exc}")
        summary = {"ok": False, "error": str(exc)}
    except Exception as exc:
        job.write(f"Unerwarteter Fehler: {exc!r}")
        summary = {"ok": False, "error": repr(exc)}
    finally:
        with job.lock:
            job.summary = summary
            job.running = False
            job.plan = None  # Stand hat sich geändert -> vor dem nächsten Import neu abgleichen


@app.post("/api/import")
def start_import():
    """Lädt aus dem letzten Abgleich: body.only (Pfade), sonst neue+geänderte; force = alle; delete = Pfade."""
    client, err = _need_client()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    with job.lock:
        if job.running:
            return error("Ein Import läuft bereits.", 409)
        plan = job.plan
        if plan is None:
            return error("Bitte zuerst den Abgleich mit ChurchTools durchführen.", 409)
        only = {str(x) for x in data["only"]} if isinstance(data.get("only"), list) else None
        delete = {str(x) for x in data.get("delete") or []}
        job.running, job.log, job.summary, job.started = True, [], None, time.time()
    threading.Thread(target=_run_import, args=(client, plan, only, bool(data.get("force")), delete),
                     daemon=True).start()
    return jsonify({"ok": True}), 202


@app.get("/api/import")
def import_status():
    return jsonify(job.snapshot(request.args.get("since", 0, type=int)))


# ------------------------------------------------------------- ProPresenter-Abgleich


def _pp_dir() -> Path | None:
    settings, _, docs = _library()
    return settings.song_dir(docs)


@app.get("/api/pp/diff")
def pp_diff():
    pp_dir = _pp_dir()
    if not pp_dir or not pp_dir.is_dir():
        return jsonify({"error": "Keine ProPresenter-Lieder-Bibliothek gefunden – bitte unter Einstellungen den Ordner angeben.",
                        "needsSettings": True}), 400
    rows = pp_sync.diff(SONGS_DIR, pp_dir, dl.load_index(SONGS_DIR))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return jsonify({"dir": str(pp_dir), "rows": [r.to_json() for r in rows], "counts": counts})


@app.post("/api/pp/write")
def pp_write():
    """Ausgewählte Lieder als .pro direkt in die Lieder-Bibliothek schreiben.

    body: {ids: [...], overwrite: bool}. Fehlende Lieder werden neu angelegt; abweichende nur mit
    overwrite (die alte Datei wird vorher nach backup/ gesichert).
    """
    data = request.get_json(silent=True) or {}
    ids = {str(i) for i in data.get("ids") or []}
    if not ids:
        return error("Keine Lieder ausgewählt.")
    pp_dir = _pp_dir()
    if not pp_dir or not pp_dir.is_dir():
        return error("Keine ProPresenter-Lieder-Bibliothek gefunden – bitte unter Einstellungen den Ordner angeben.")
    index = dl.load_index(SONGS_DIR)
    rows = {r.db.id: r for r in pp_sync.diff(SONGS_DIR, pp_dir, index) if r.db and r.db.id in ids}
    targets, chosen, skipped = {}, [], []
    for sid, r in rows.items():
        if r.status == "db_only":
            target = pp_dir / f"{r.db.stem}.pro"
            if target.exists():
                skipped.append(f"{r.title}: {target.name} existiert bereits")
                continue
        elif r.status == "different" and data.get("overwrite"):
            target = r.pp
        else:
            continue
        targets[sid] = target
        chosen.append(r.db)
    if not chosen:
        return error("Nichts zu schreiben – die Auswahl ist schon aktuell." if not skipped else "; ".join(skipped))
    written, failed, backup = pp_sync.write_to_library(chosen, pp_dir, pro_export.load_style(), index,
                                                       SONGS_DIR, targets, BACKUP_DIR)
    return jsonify({"written": written, "failed": failed + skipped, "dir": str(pp_dir),
                    "backup": str(backup) if backup else ""})


@app.get("/api/config")
def config():
    return jsonify({"defaultUrl": dl.DEFAULT_URL, "songsDir": str(SONGS_DIR)})


# ------------------------------------------------------------------------ Seite


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "web", "index.html")


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Lokale Weboberfläche für die Liederdatenbank")
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    paths.ensure_data_dir()
    if _port_in_use(args.port):  # läuft schon (z. B. zweiter Doppelklick) -> nur Browser öffnen
        print(f"Liederverwaltung läuft bereits auf {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    print(f"Liederverwaltung läuft auf {url}  (Beenden: dieses Fenster schließen oder Strg+C)")
    print(f"Datenordner: {paths.DATA_DIR}")
    print(f"Liederordner: {SONGS_DIR}")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    # Ohne Flask-Startbanner/Anfrageprotokoll: das Fenster zeigt nur, was für Benutzer wichtig ist.
    import logging
    import flask.cli
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask.cli.show_server_banner = lambda *a, **k: None
    # Nur lokal erreichbar – Zugangsdaten gehen über diesen Server.
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
