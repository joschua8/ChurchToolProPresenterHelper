#!/usr/bin/env python3
"""Lädt SongBeamer-Dateien (.sng) aus der ChurchTools-Liederdatenbank herunter.

Es werden nur neue und geänderte Lieder geladen (Abgleich über churchtools_index.json),
mit --all alle.

Lieder ohne hinterlegte .sng-Datei werden in einem Bericht (fehlende_sng.csv)
aufgeführt und am Ende auf der Konsole ausgegeben.

Zugangsdaten (in dieser Reihenfolge ausgewertet):
  1. Kommandozeile:   --user / --password  oder  --token
  2. Umgebungsvariablen: CT_USER / CT_PASSWORD  oder  CT_TOKEN
  3. Interaktive Abfrage
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from sng import is_manual_file

DEFAULT_URL = "https://kirche-am-fahlt.church.tools"
PAGE_SIZE = 100
# Datei -> exakter ChurchTools-Name + Arrangement (Dateinamen sind bereinigt und tragen ggf. „ - Arrangement“).
INDEX_NAME = "churchtools_index.json"


class ChurchToolsError(RuntimeError):
    pass


def _api_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
        return body.get("translatedMessage") or body.get("message") or resp.text[:300]
    except ValueError:
        return resp.text[:300]


class ChurchToolsClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/{path.lstrip('/')}"

    def login_with_password(
        self,
        username: str,
        password: str,
        get_totp: Callable[[], str] = lambda: input("Zwei-Faktor-Code (TOTP): ").strip(),
    ) -> str:
        resp = self.session.post(
            self._url("login"),
            json={"username": username, "password": password, "rememberMe": False},
            timeout=30,
        )
        if resp.status_code != 200:
            raise ChurchToolsError(f"Login fehlgeschlagen (HTTP {resp.status_code}): {_api_message(resp)}")
        data = resp.json().get("data", {})
        if data.get("status") == "totp":
            code = get_totp()
            if not code:
                raise ChurchToolsError("Zwei-Faktor-Code erforderlich")
            resp = self.session.post(
                self._url("login/totp"),
                json={"code": code, "personId": data.get("personId")},
                timeout=30,
            )
            if resp.status_code != 200:
                raise ChurchToolsError(f"2FA fehlgeschlagen (HTTP {resp.status_code}): {resp.text[:300]}")
        return self._check_whoami()

    def login_with_token(self, token: str) -> str:
        self.session.headers["Authorization"] = f"Login {token}"
        return self._check_whoami()

    def _check_whoami(self) -> str:
        """Prüft die Anmeldung und gibt den Namen des Benutzers zurück."""
        resp = self.session.get(self._url("whoami"), timeout=30)
        if resp.status_code != 200:
            raise ChurchToolsError(f"Anmeldung nicht gültig (HTTP {resp.status_code})")
        person = resp.json().get("data", {})
        if not person.get("id"):
            raise ChurchToolsError("Anmeldung nicht gültig (anonymer Benutzer)")
        return f"{person.get('firstName', '')} {person.get('lastName', '')}".strip()

    def iter_songs(self):
        page = 1
        while True:
            resp = self.session.get(
                self._url("songs"), params={"page": page, "limit": PAGE_SIZE}, timeout=60
            )
            if resp.status_code != 200:
                raise ChurchToolsError(f"Liederliste nicht abrufbar (HTTP {resp.status_code}): {resp.text[:300]}")
            body = resp.json()
            yield from body.get("data", [])
            pagination = body.get("meta", {}).get("pagination", {})
            if page >= pagination.get("lastPage", page):
                break
            page += 1

    def _get_data(self, path: str, what: str, **params):
        resp = self.session.get(self._url(path), params=params or None, timeout=60)
        if resp.status_code == 401:
            raise ChurchToolsError(f"{what}: nicht (mehr) angemeldet")
        if resp.status_code != 200:
            raise ChurchToolsError(f"{what} nicht abrufbar (HTTP {resp.status_code}): {_api_message(resp)}")
        return resp.json()

    def events(self, date_from: str, date_to: str) -> list[dict]:
        """Termine mit Ablaufplan-Möglichkeit (Gottesdienste usw.), Datum als JJJJ-MM-TT."""
        events, page = [], 1
        while True:
            body = self._get_data("events", "Termine", **{"from": date_from, "to": date_to, "page": page})
            events.extend(body.get("data") or [])
            pagination = (body.get("meta") or {}).get("pagination") or {}
            if page >= pagination.get("lastPage", page):
                return events
            page += 1

    def agenda(self, event_id: int | str) -> dict | None:
        """Ablaufplan eines Termins (None, wenn es keinen gibt)."""
        resp = self.session.get(self._url(f"events/{event_id}/agenda"), timeout=60)
        if resp.status_code == 404:
            return None
        if resp.status_code == 401:
            raise ChurchToolsError("Ablaufplan: nicht (mehr) angemeldet")
        if resp.status_code != 200:
            raise ChurchToolsError(f"Ablaufplan nicht abrufbar (HTTP {resp.status_code}): {_api_message(resp)}")
        return resp.json().get("data") or None

    def event_files(self, event_id: int | str) -> list[dict]:
        """Dateien am Termin (Reiter „Dateien“ im Ablauf): [{id, name}]. Links ohne Datei fallen weg."""
        data = self._get_data(f"events/{event_id}", "Termin").get("data") or {}
        files = []
        for f in data.get("eventFiles") or []:
            if (f.get("domainType") or "file") != "file" or not f.get("domainIdentifier"):
                continue
            files.append({"id": str(f["domainIdentifier"]), "name": (f.get("title") or "").strip(),
                          "url": f.get("frontendUrl") or ""})
        return files

    def file_url(self, file_id: str) -> str:
        """Download-Adresse einer Datei (GET /files/{id}/meta -> fileUrl)."""
        data = self._get_data(f"files/{file_id}/meta", "Datei").get("data") or {}
        url = data.get("fileUrl") or ""
        if not url:
            raise ChurchToolsError(f"Datei {file_id}: keine Download-Adresse")
        return url

    def download(self, file_url: str, target: Path) -> None:
        if not file_url.startswith("http"):
            file_url = f"{self.base_url}/{file_url.lstrip('/')}"
        with self.session.get(file_url, stream=True, timeout=120) as resp:
            if resp.status_code != 200:
                raise ChurchToolsError(f"HTTP {resp.status_code}")
            tmp = target.with_suffix(target.suffix + ".part")
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    fh.write(chunk)
            tmp.replace(target)


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".")
    return name or "unbenannt"


def unique_path(path: Path, taken: set[Path]) -> Path:
    candidate, n = path, 2
    # Von Hand erfasste Lieder (Weboberfläche) werden nie überschrieben.
    while candidate in taken or is_manual_file(candidate):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    taken.add(candidate)
    return candidate


@dataclass
class Result:
    downloaded: list[str] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    index: dict[str, dict] = field(default_factory=dict)
    skipped: int = 0
    deleted: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ Abgleich (Diff)
#
# Statt bei jedem Import alles neu zu laden, wird zuerst nur die Liederliste abgefragt und mit
# churchtools_index.json verglichen. Jede heruntergeladene .sng-Datei bekommt dort einen
# „Fingerabdruck“ der ChurchTools-Datei (Datei-ID, Speichername, Größe, Änderungsdatum).
# Geändert hat sich ein Lied, wenn sich der Fingerabdruck oder der Name unterscheidet.

_FP_KEYS = ("id", "filename", "size", "fileSize", "modifiedDate", "updatedAt")


def fingerprint(arr: dict, f: dict) -> str:
    """Stabiler Fingerabdruck einer .sng-Datei in ChurchTools."""
    fp = {k: f.get(k) for k in _FP_KEYS if f.get(k) not in (None, "")}
    meta = f.get("meta") or {}
    if isinstance(meta, dict) and meta.get("modifiedDate"):
        fp["meta"] = meta["modifiedDate"]
    if not fp.get("id") and not fp.get("filename"):
        fp["url"] = f.get("fileUrl") or ""  # Notlösung, falls die API nichts anderes liefert
    fp["name"] = f.get("name") or ""
    return json.dumps(fp, sort_keys=True, ensure_ascii=False)


def _match_key(song_id, arr: dict, f: dict) -> tuple:
    return (str(song_id), str(arr.get("id") or arr.get("name") or ""), (f.get("name") or "").lower())


def _legacy_key(song_id, arr_name: str) -> tuple:
    return (str(song_id), arr_name or "")


@dataclass
class PlanItem:
    """Eine .sng-Datei aus ChurchTools und was mit ihr passieren soll."""
    status: str          # new | changed | unchanged | unknown (älterer Import ohne Fingerabdruck)
    rel: str             # Zielpfad relativ zum Liederordner
    title: str
    category: str
    arrangement: str
    file_url: str
    entry: dict          # künftiger Indexeintrag
    reason: str = ""

    def to_json(self) -> dict:
        return {"status": self.status, "rel": self.rel, "title": self.title, "category": self.category,
                "arrangement": self.arrangement if self.entry.get("multiple") else "", "reason": self.reason,
                "songId": self.entry.get("id")}


@dataclass
class SyncPlan:
    items: list[PlanItem] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)      # Lieder ohne .sng in ChurchTools
    removed: list[dict] = field(default_factory=list)      # lokal vorhanden, in ChurchTools nicht mehr
    total_songs: int = 0

    def counts(self) -> dict:
        c = {"new": 0, "changed": 0, "unchanged": 0, "unknown": 0}
        for it in self.items:
            c[it.status] += 1
        return {**c, "missing": len(self.missing), "removed": len(self.removed), "songs": self.total_songs}

    def to_json(self) -> dict:
        return {"counts": self.counts(), "items": [i.to_json() for i in self.items],
                "missing": self.missing, "removed": self.removed}


def plan_sync(songs: list[dict], out_dir: Path, by_category: bool, base_url: str = "") -> SyncPlan:
    """Vergleicht die ChurchTools-Liederliste mit dem lokalen Stand, ohne etwas herunterzuladen."""
    index = load_index(out_dir)
    existing = {rel: e for rel, e in index.items() if (out_dir / rel).exists()}
    by_key: dict[tuple, str] = {}
    by_legacy: dict[tuple, list[str]] = {}
    for rel, e in existing.items():
        if e.get("key"):
            by_key[tuple(e["key"])] = rel
        by_legacy.setdefault(_legacy_key(e.get("id"), e.get("arrangement", "")), []).append(rel)

    plan = SyncPlan(total_songs=len(songs))
    taken: set[Path] = {out_dir / rel for rel in existing}
    used: set[str] = set()

    for song in songs:
        title = song.get("name") or f"Lied {song.get('id')}"
        category = (song.get("category") or {}).get("name") or "Ohne Kategorie"
        sng_files = [
            (arr, f)
            for arr in song.get("arrangements") or []
            for f in arr.get("files") or []
            if (f.get("name") or "").lower().endswith(".sng")
        ]
        if not sng_files:
            plan.missing.append({
                "id": song.get("id"),
                "titel": title,
                "kategorie": category,
                "autor": song.get("author") or "",
                "arrangements": ", ".join(a.get("name") or "" for a in song.get("arrangements") or []),
                "link": f"{base_url}/?q=churchservice#SongView/filterSongId:{song.get('id')}",
            })
            continue

        multiple = len(sng_files) > 1
        for arr, f in sng_files:
            arr_name = arr.get("name") or ""
            key = _match_key(song.get("id"), arr, f)
            fp = fingerprint(arr, f)
            entry = {
                "id": song.get("id"),
                "name": title,
                "arrangement": arr_name,
                "default": bool(arr["isDefault"]) if "isDefault" in arr else arr_name.lower().startswith("standard"),
                "multiple": multiple,
                "category": category,
                "key": list(key),
                "fingerprint": fp,
            }
            rel = by_key.get(key)
            if rel is None:  # älterer Index ohne Schlüssel: über Lied-ID + Arrangement zuordnen
                rel = next((r for r in by_legacy.get(_legacy_key(song.get("id"), arr_name), []) if r not in used), None)
            if rel is not None and rel not in used:
                used.add(rel)
                old = existing[rel]
                if not old.get("fingerprint"):
                    status, reason = "unknown", "älterer Import – Stand unbekannt"
                elif old["fingerprint"] != fp:
                    status, reason = "changed", "Datei in ChurchTools geändert"
                elif old.get("name") != title or bool(old.get("multiple")) != multiple:
                    status, reason = "changed", "Name/Arrangement geändert"
                else:
                    status, reason = "unchanged", ""
            else:
                target_dir = out_dir / safe_filename(category) if by_category else out_dir
                stem = safe_filename(title)
                if multiple:
                    stem = f"{stem} - {safe_filename(arr_name or (f.get('name') or '')[:-4])}"
                target = unique_path(target_dir / f"{stem}.sng", taken)
                rel = target.relative_to(out_dir).as_posix()
                status, reason = "new", ""
            plan.items.append(PlanItem(status, rel, title, category, arr_name, f.get("fileUrl") or "", entry, reason))

    for rel, e in sorted(existing.items()):
        if rel not in used:
            plan.removed.append({"rel": rel, "title": e.get("name") or Path(rel).stem,
                                 "arrangement": e.get("arrangement", "") if e.get("multiple") else ""})
    return plan


def sync(
    client: ChurchToolsClient,
    out_dir: Path,
    plan: SyncPlan,
    only: set[str] | None = None,
    force: bool = False,
    delete: set[str] | None = None,
    log: Callable[[str], None] = print,
) -> Result:
    """Lädt neue/geänderte Dateien (oder nur `only`, oder mit force alle) und pflegt den Index."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = Result(missing=plan.missing)
    if only is not None:
        todo = [it for it in plan.items if it.rel in only]
    elif force:
        todo = list(plan.items)
    else:
        todo = [it for it in plan.items if it.status in ("new", "changed")]
    todo_rels = {it.rel for it in todo}

    # Unveränderte (bzw. ältere ohne Fingerabdruck) behalten ihre Datei, bekommen aber den aktuellen Indexeintrag.
    for it in plan.items:
        if it.rel not in todo_rels and it.status in ("unchanged", "unknown"):
            result.index[it.rel] = it.entry
            result.skipped += 1

    log(f"{len(todo)} von {len(plan.items)} Dateien werden heruntergeladen, {result.skipped} sind aktuell.\n")
    for i, it in enumerate(todo, 1):
        target = out_dir / it.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        label = {"new": "NEU ", "changed": "AKT ", "unknown": "AKT ", "unchanged": "AKT "}[it.status]
        try:
            if is_manual_file(target):
                raise ChurchToolsError("Zieldatei ist ein von Hand erfasstes Lied – wird nicht überschrieben")
            client.download(it.file_url, target)
            result.downloaded.append(it.rel)
            result.index[it.rel] = it.entry
            log(f"[{i}/{len(todo)}] {label} {it.rel}")
        except (ChurchToolsError, requests.RequestException, KeyError) as exc:
            result.failed.append((it.title, str(exc)))
            log(f"[{i}/{len(todo)}] FEHLER {it.title}: {exc}")
        time.sleep(0.05)  # Server schonen

    removable = {r["rel"] for r in plan.removed}
    for rel in sorted((delete or set()) & removable):
        path = out_dir / rel
        if path.exists() and not is_manual_file(path):
            path.unlink()
            result.deleted.append(rel)
            log(f"GELÖSCHT {rel}")
    return result


def run(client: ChurchToolsClient, out_dir: Path, by_category: bool, log: Callable[[str], None] = print,
        force: bool = False) -> Result:
    """Abgleich + Download in einem Schritt (CLI): nur neue und geänderte Lieder, mit force alle."""
    out_dir.mkdir(parents=True, exist_ok=True)
    songs = list(client.iter_songs())
    log(f"{len(songs)} Lieder in ChurchTools gefunden.")
    plan = plan_sync(songs, out_dir, by_category, client.base_url)
    c = plan.counts()
    log(f"Abgleich: {c['new']} neu, {c['changed']} geändert, {c['unchanged'] + c['unknown']} unverändert, "
        f"{c['missing']} ohne .sng, {c['removed']} nicht mehr in ChurchTools.")
    return sync(client, out_dir, plan, force=force, log=log)


def load_index(out_dir: Path) -> dict[str, dict]:
    try:
        data = json.loads((out_dir / INDEX_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_index(out_dir: Path, result: Result) -> Path:
    """Ergänzt den Index; Einträge zu nicht mehr vorhandenen Dateien fallen weg."""
    index = {k: v for k, v in load_index(out_dir).items() if (out_dir / k).exists()}
    index.update(result.index)
    path = out_dir / INDEX_NAME
    path.write_text(json.dumps(index, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def write_report(out_dir: Path, result: Result) -> Path:
    report = out_dir / "fehlende_sng.csv"
    with open(report, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["id", "titel", "kategorie", "autor", "arrangements", "link"], delimiter=";"
        )
        writer.writeheader()
        writer.writerows(result.missing)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Alle .sng-Lieder aus ChurchTools herunterladen.")
    parser.add_argument("--url", default=os.environ.get("CT_URL", DEFAULT_URL), help="ChurchTools-Adresse")
    parser.add_argument("--user", default=os.environ.get("CT_USER"), help="Benutzername / E-Mail")
    parser.add_argument("--password", default=os.environ.get("CT_PASSWORD"), help="Passwort")
    parser.add_argument("--token", default=os.environ.get("CT_TOKEN"), help="Login-Token (statt Passwort)")
    parser.add_argument("--out", default="songs", type=Path, help="Zielordner (Standard: ./songs)")
    parser.add_argument("--by-category", action="store_true", help="Unterordner pro Liedkategorie anlegen")
    parser.add_argument("--all", action="store_true", help="Alle Lieder neu laden statt nur neue/geänderte")
    args = parser.parse_args()

    client = ChurchToolsClient(args.url)
    try:
        if args.token:
            name = client.login_with_token(args.token)
        else:
            user = args.user or input("Benutzername / E-Mail: ").strip()
            password = args.password or getpass.getpass("Passwort: ")
            name = client.login_with_password(user, password)
        print(f"Angemeldet als {name}")

        result = run(client, args.out, args.by_category, force=args.all)
    except (ChurchToolsError, requests.RequestException) as exc:
        print(f"Abbruch: {exc}", file=sys.stderr)
        return 1

    report = write_report(args.out, result)
    write_index(args.out, result)
    print("\n=== Zusammenfassung ===")
    print(f"Heruntergeladen:  {len(result.downloaded)} .sng-Dateien ({result.skipped} waren aktuell)")
    print(f"Ohne .sng:        {len(result.missing)} Lieder  -> {report}")
    for m in result.missing:
        print(f"  - {m['titel']}")
    if result.failed:
        print(f"Fehlgeschlagen:   {len(result.failed)}")
        for title, err in result.failed:
            print(f"  - {title}: {err}")
    return 0 if not result.failed else 2


if __name__ == "__main__":
    sys.exit(main())
