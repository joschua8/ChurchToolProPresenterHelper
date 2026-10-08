#!/usr/bin/env python3
"""Lädt alle SongBeamer-Dateien (.sng) aus der ChurchTools-Liederdatenbank herunter.

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


def run(client: ChurchToolsClient, out_dir: Path, by_category: bool, log: Callable[[str], None] = print) -> Result:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = Result()
    taken: set[Path] = set()

    songs = list(client.iter_songs())
    log(f"{len(songs)} Lieder gefunden.\n")

    for i, song in enumerate(songs, 1):
        title = song.get("name") or f"Lied {song.get('id')}"
        category = (song.get("category") or {}).get("name") or "Ohne Kategorie"
        target_dir = out_dir / safe_filename(category) if by_category else out_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        sng_files = [
            (arr, f)
            for arr in song.get("arrangements") or []
            for f in arr.get("files") or []
            if (f.get("name") or "").lower().endswith(".sng")
        ]

        if not sng_files:
            result.missing.append(
                {
                    "id": song.get("id"),
                    "titel": title,
                    "kategorie": category,
                    "autor": song.get("author") or "",
                    "arrangements": ", ".join(a.get("name") or "" for a in song.get("arrangements") or []),
                    "link": f"{client.base_url}/?q=churchservice#SongView/filterSongId:{song.get('id')}",
                }
            )
            log(f"[{i}/{len(songs)}] HINWEIS: keine .sng – {title}")
            continue

        multiple = len(sng_files) > 1
        for arr, f in sng_files:
            stem = safe_filename(title)
            if multiple:
                stem = f"{stem} - {safe_filename(arr.get('name') or f.get('name')[:-4])}"
            target = unique_path(target_dir / f"{stem}.sng", taken)
            try:
                client.download(f["fileUrl"], target)
                result.downloaded.append(str(target.relative_to(out_dir)))
                arr_name = arr.get("name") or ""
                result.index[target.relative_to(out_dir).as_posix()] = {
                    "id": song.get("id"),
                    "name": title,
                    "arrangement": arr_name,
                    "default": bool(arr["isDefault"]) if "isDefault" in arr else arr_name.lower().startswith("standard"),
                    "multiple": multiple,
                    "category": category,
                }
                log(f"[{i}/{len(songs)}] OK   {target.relative_to(out_dir)}")
            except (ChurchToolsError, requests.RequestException, KeyError) as exc:
                result.failed.append((title, str(exc)))
                log(f"[{i}/{len(songs)}] FEHLER {title}: {exc}")
            time.sleep(0.05)  # Server schonen

    return result


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

        result = run(client, args.out, args.by_category)
    except (ChurchToolsError, requests.RequestException) as exc:
        print(f"Abbruch: {exc}", file=sys.stderr)
        return 1

    report = write_report(args.out, result)
    write_index(args.out, result)
    print("\n=== Zusammenfassung ===")
    print(f"Heruntergeladen:  {len(result.downloaded)} .sng-Dateien")
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
