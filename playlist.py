#!/usr/bin/env python3
"""ChurchTools-Ablaufplan -> ProPresenter-7-Playlist (.proplaylist).

  * Ablaufpunkte (Überschriften und normale Punkte wie „Begrüßungsfolien“) werden Kopfzeilen.
  * Lieder werden NICHT neu aus ChurchTools erzeugt, sondern auf die Präsentationen verwiesen,
    die schon in der lokalen ProPresenter-Bibliothek liegen. Zuordnung über den Liednamen
    (exakt -> normalisiert -> ähnlich), bei mehreren Treffern nach Bibliotheks-Reihenfolge.

Format (aus einer echten .proplaylist abgeleitet): ZIP (unkomprimiert) mit einer Datei `data`
= rv.data.PlaylistDocument. Lieder verweisen per `document_path` auf die .pro-Datei;
ProPresenter löst sie über `local {root: ROOT_SHOW, path: "Libraries/<Bibliothek>/<Name>.pro"}` auf.
"""

from __future__ import annotations

import difflib
import io
import json
import os
import re
import sys
import unicodedata
import uuid
import zipfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

import propresenterFormatter  # noqa: F401  (pb2-Module in den Suchpfad)
import propresenter_pb2  # noqa: E402
import basicTypes_pb2  # noqa: E402

from paths import DATA_DIR  # noqa: E402

SETTINGS_PATH = Path(os.environ.get("PLAYLIST_SETTINGS", DATA_DIR / "playlist_settings.json"))

# Farben wie im ChurchTools-Export: Überschriften rot, normale Ablaufpunkte rosa.
DEFAULT_HEADER_COLOR = "#ff0000"
DEFAULT_ITEM_COLOR = "#ff00f0"
MISSING_COLOR = "#ff9500"


def show_root_candidates() -> list[Path]:
    """Mögliche ProPresenter-Arbeitsordner (enthalten Libraries/)."""
    home = Path.home()
    cands = [
        home / "Library/Application Support/RenewedVision/ProPresenter/UserWorkspaces/ProPresenter",
        home / "Documents/ProPresenter",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        base = Path(appdata) / "RenewedVision" / "ProPresenter"
        cands[:0] = [base / "LocalWorkspaces" / "ProPresenter", base / "UserWorkspaces" / "ProPresenter"]
    return cands


def detect_show_root() -> str:
    env = os.environ.get("PP_SHOW_ROOT")
    if env:
        return env
    for cand in show_root_candidates():
        if (cand / "Libraries").is_dir():
            return str(cand)
    return ""


# ------------------------------------------------------------------ Einstellungen


@dataclass
class PlaylistSettings:
    show_root: str = ""                  # leer = automatisch erkennen (bzw. aus song_library ableiten)
    song_library: str = ""               # Ordner der Lieder-Bibliothek (…/Libraries/Songs); leer = automatisch
    library_order: list[str] = field(default_factory=list)  # bevorzugte Bibliotheken zuerst
    header_color: str = DEFAULT_HEADER_COLOR
    item_color: str = DEFAULT_ITEM_COLOR
    include_normal: bool = True          # normale Ablaufpunkte als Kopfzeile
    include_before: bool = True          # Punkte „vor dem Gottesdienst“
    missing_as_header: bool = True       # nicht gefundenes Lied: Kopfzeile mit Hinweis statt weglassen
    fuzzy: bool = True                   # ähnliche Namen automatisch zuordnen

    @classmethod
    def from_dict(cls, data: dict | None) -> "PlaylistSettings":
        s = cls()
        for f in fields(cls):
            if not isinstance(data, dict) or f.name not in data:
                continue
            value, default = data[f.name], getattr(s, f.name)
            if isinstance(default, bool):
                setattr(s, f.name, bool(value))
            elif isinstance(default, list):
                if isinstance(value, list):
                    setattr(s, f.name, [str(v) for v in value if isinstance(v, str)])
            elif f.name.endswith("_color"):
                if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                    setattr(s, f.name, value.lower())
            elif isinstance(value, str):
                setattr(s, f.name, value.strip())
        return s

    def to_dict(self) -> dict:
        return asdict(self)

    def root(self) -> Path | None:
        if self.show_root:
            return Path(self.show_root).expanduser()
        lib = Path(self.song_library).expanduser() if self.song_library else None
        if lib and lib.parent.name == "Libraries":
            return lib.parent.parent
        root = detect_show_root()
        return Path(root) if root else None

    def song_dir(self, docs: list["Doc"] | None = None) -> Path | None:
        """Ordner der ProPresenter-Lieder-Bibliothek: Einstellung, sonst bevorzugte Bibliothek im Arbeitsordner."""
        if self.song_library:
            return Path(self.song_library).expanduser()
        root = self.root()
        if root is None:
            return None
        libs = root / "Libraries"
        if docs is None:
            docs = scan_library(root)
        names = ordered_libraries(docs, self.library_order)
        if names:
            for d in libs.iterdir() if libs.is_dir() else []:
                if nfc(d.name) == names[0]:
                    return d
        songs = libs / "Songs"
        return songs if songs.is_dir() else None


def load_settings(path: Path | None = None) -> PlaylistSettings:
    try:
        return PlaylistSettings.from_dict(json.loads((path or SETTINGS_PATH).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return PlaylistSettings()


def save_settings(settings: PlaylistSettings, path: Path | None = None) -> None:
    (path or SETTINGS_PATH).write_text(json.dumps(settings.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ Bibliothek


@dataclass
class Doc:
    library: str
    name: str        # Dateiname ohne .pro, so wie er auf der Platte steht (macOS: oft NFD)
    rel: str         # "Libraries/<Bibliothek>/<Datei>.pro" relativ zum Arbeitsordner

    @property
    def label(self) -> str:
        return f"{nfc(self.library)}/{nfc(self.name)}"

    def to_json(self) -> dict:
        return {"library": nfc(self.library), "name": nfc(self.name), "rel": self.rel}


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def scan_library(root: Path | None) -> list[Doc]:
    docs: list[Doc] = []
    libs = root / "Libraries" if root else None
    if not libs or not libs.is_dir():
        return docs
    for lib in sorted(libs.iterdir(), key=lambda p: p.name.casefold()):
        if not lib.is_dir():
            continue
        for f in sorted(lib.iterdir(), key=lambda p: p.name.casefold()):
            if f.suffix.lower() == ".pro" and f.is_file():
                docs.append(Doc(lib.name, f.stem, f"Libraries/{lib.name}/{f.name}"))
    return docs


def library_names(docs: list[Doc]) -> list[str]:
    return sorted({nfc(d.library) for d in docs}, key=str.casefold)


def ordered_libraries(docs: list[Doc], preferred: list[str]) -> list[str]:
    """Bibliotheken in Prioritätsreihenfolge: Einstellung, dann „Songs“, dann Namen mit „song“, dann Rest."""
    names = library_names(docs)
    pref = [nfc(p) for p in preferred if nfc(p) in names]
    rest = sorted((n for n in names if n not in pref),
                  key=lambda n: (n.casefold() != "songs", "song" not in n.casefold(), n.casefold()))
    return pref + rest


# ------------------------------------------------------------------ Zuordnung

_ARR_SUFFIX_RE = re.compile(r"\s+-\s+(standard(?:-arrangement)?)\s*$", re.IGNORECASE)
_CCLI_RE = re.compile(r"(?<!\w)\d{5,8}(?!\w)")


def norm_name(name: str) -> str:
    """Vergleichsschlüssel: Unicode, Groß/klein, Satzzeichen, CCLI-Nummern, „ - Standard-Arrangement“ egal."""
    s = nfc(name).casefold().replace("ß", "ss")
    s = _ARR_SUFFIX_RE.sub("", s)
    s = _CCLI_RE.sub(" ", s)
    return re.sub(r"[^\w]+", " ", s).strip()


@dataclass
class Match:
    status: str                       # exact | normalized | similar | missing
    doc: Doc | None = None
    candidates: list[Doc] = field(default_factory=list)  # weitere passende Präsentationen

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "doc": self.doc.to_json() if self.doc else None,
            "candidates": [d.to_json() for d in self.candidates],
        }


_PARTS_RE = re.compile(r"\s+[_/|]\s+")


def name_parts(name: str) -> list[str]:
    """Zweisprachige Namen („Heilig für immer _ Holy forever“, „King Of Kings / König aller Könige“)."""
    parts = [norm_name(p) for p in _PARTS_RE.split(nfc(name))]
    return [p for p in parts if p] if len(parts) > 1 else []


class Matcher:
    def __init__(self, docs: list[Doc], library_order: list[str]):
        self.docs = docs
        self.rank = {name: i for i, name in enumerate(ordered_libraries(docs, library_order))}
        self.by_norm: dict[str, list[Doc]] = {}
        self.by_part: dict[str, list[Doc]] = {}
        for d in docs:
            self.by_norm.setdefault(norm_name(d.name), []).append(d)
            for part in name_parts(d.name):
                self.by_part.setdefault(part, []).append(d)
        self.norm_keys = [k for k in self.by_norm if k]

    def _sort(self, docs: list[Doc]) -> list[Doc]:
        return sorted(docs, key=lambda d: (self.rank.get(nfc(d.library), 99), nfc(d.name).casefold()))

    def _lookup(self, name: str) -> dict[int, int]:
        """-> {id(doc): Stufe}; 0 = exakt, 1 = normalisiert gleich, 2 = ein Sprachteil gleich."""
        found: dict[int, int] = {}

        def add(docs, level):
            for d in docs:
                found[id(d)] = min(found.get(id(d), level), level)

        wanted = nfc(name).casefold()
        add((d for d in self.docs if nfc(d.name).casefold() == wanted), 0)
        key = norm_name(name)
        if key:
            add(self.by_norm.get(key, []), 1)
            add(self.by_part.get(key, []), 2)
        for part in name_parts(name):
            add(self.by_norm.get(part, []), 2)
            add(self.by_part.get(part, []), 2)
        return found

    def match(self, title: str, arrangement: str = "", fuzzy: bool = True) -> Match:
        """title/arrangement wie in ChurchTools. Weitere Arrangements („In G“) bevorzugt mit Zusatz."""
        wanted = []
        if arrangement and not _ARR_SUFFIX_RE.search(f" - {arrangement}"):
            wanted.append(f"{title} - {arrangement}")
        wanted.append(title)

        by_id = {id(d): d for d in self.docs}
        best, level, others = None, 0, []
        for name in wanted:
            found = self._lookup(name)
            # Bibliotheks-Priorität vor Trefferstufe: eine gepflegte Fassung in „Songs“ schlägt den Import.
            ranked = sorted(found, key=lambda i: (self.rank.get(nfc(by_id[i].library), 99), found[i],
                                                  nfc(by_id[i].name).casefold()))
            for i in ranked:
                if best is None:
                    best, level = by_id[i], found[i]
                elif by_id[i] is not best and by_id[i] not in others:
                    others.append(by_id[i])
        if best is not None:
            return Match("exact" if level == 0 else "normalized", best, others)

        if not fuzzy:
            return Match("missing")
        key = norm_name(title)
        if len(key) < 4:
            return Match("missing")
        scored: list[tuple[float, str]] = []
        for k in self.norm_keys:
            ratio = difflib.SequenceMatcher(None, key, k).ratio()
            # abgeschnittene Dateinamen („Da wohnt ein Sehnen tief in un“)
            if len(k) >= 12 and (key.startswith(k) or k.startswith(key)):
                ratio = max(ratio, 0.9)
            if ratio >= 0.86:
                scored.append((ratio, k))
        if not scored:
            return Match("missing")
        scored.sort(key=lambda t: -t[0])
        top = scored[0][0]
        best_docs = self._sort([d for r, k in scored if r == top for d in self.by_norm[k]])
        others = self._sort([d for r, k in scored if r != top for d in self.by_norm[k]])
        return Match("similar", best_docs[0], best_docs[1:] + others)


# ------------------------------------------------------------------ Ablaufplan

ITEM_TYPES = {"header", "normal", "song"}


def _local_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def event_json(ev: dict) -> dict:
    start = _local_time(ev.get("startDate"))
    cal = ev.get("calendar") or {}
    return {
        "id": ev.get("id"),
        "name": ev.get("name") or ev.get("caption") or "Ohne Namen",
        "start": start.isoformat() if start else ev.get("startDate"),
        "calendar": cal.get("title") or cal.get("name") or "",
        "canceled": bool(ev.get("isCanceled")),
    }


def default_playlist_name(event: dict, agenda: dict | None = None) -> str:
    """Wie der ChurchTools-Export: „2026-05-24 1000 - Gottesdienst (Entwurf)“."""
    start = _local_time(event.get("startDate")) if "startDate" in event else _local_time(event.get("start"))
    name = event.get("name") or "Ablaufplan"
    stamp = start.strftime("%Y-%m-%d %H%M") if start else ""
    title = f"{stamp} - {name}" if stamp else name
    if agenda is not None and not agenda.get("isFinal", True):
        title += " (Entwurf)"
    return title


def plan_items(agenda: dict, matcher: Matcher, fuzzy: bool = True) -> list[dict]:
    """ChurchTools-Ablaufplan -> Zeilen für die Oberfläche (inkl. Lied-Zuordnung)."""
    rows = []
    items = sorted(agenda.get("items") or [], key=lambda i: (i.get("position") is None, i.get("position") or 0))
    for it in items:
        kind = it.get("type") if it.get("type") in ITEM_TYPES else "normal"
        song = it.get("song") or None
        if kind != "song" and song and song.get("songId"):
            kind = "song"
        responsible = it.get("responsible") or {}
        start = _local_time(it.get("start"))
        row = {
            "id": it.get("id"),
            "type": kind,
            "title": (it.get("title") or "").strip(),
            "note": it.get("note") or "",
            "duration": it.get("duration") or 0,
            "start": start.strftime("%H:%M") if start else "",
            "before": bool(it.get("isBeforeEvent")),
            "responsible": responsible.get("text") if isinstance(responsible, dict) else str(responsible or ""),
        }
        if kind == "song":
            song = song or {}
            title = (song.get("title") or row["title"]).strip()
            arrangement = (song.get("arrangement") or "").strip()
            row["song"] = {
                "songId": song.get("songId"),
                "arrangementId": song.get("arrangementId"),
                "title": title,
                "arrangement": arrangement,
                "key": song.get("key") or "",
            }
            row["title"] = title
            row["match"] = matcher.match(title, arrangement, fuzzy).to_json()
        rows.append(row)
    return rows


# ------------------------------------------------------------------ .proplaylist


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def _set_color(color, hex_color: str) -> None:
    hex_color = hex_color.lstrip("#")
    color.red, color.green, color.blue = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    color.alpha = 1


def _win_file_url(path) -> str:
    p = PureWindowsPath(path)
    return "file:///" + quote(p.as_posix(), safe="/:")


def _document_url(url, root: Path | None, rel: str) -> None:
    """Verweis auf eine Präsentation, wie ProPresenter ihn selbst schreibt."""
    url.local.root = basicTypes_pb2.URL.LocalRelativePath.ROOT_SHOW
    url.local.path = rel
    if root is None:
        return
    full = root / rel
    if sys.platform.startswith("win"):
        # ProPresenter erwartet auch unter Windows eine Datei-URL (file:///C:/…), keinen Pfad mit „\“.
        url.platform = basicTypes_pb2.URL.PLATFORM_WIN32
        url.absolute_string = _win_file_url(full)
    else:
        url.platform = basicTypes_pb2.URL.PLATFORM_MACOS
        url.absolute_string = "file://" + quote(str(full), safe="/")


def build_playlist(name: str, entries: list[dict], root: Path | None) -> propresenter_pb2.PlaylistDocument:
    """entries: {"kind": "header", "name", "color"} | {"kind": "presentation", "name", "rel"}."""
    from pro_export import get_template  # App-Version wie bei den exportierten Liedern

    doc = propresenter_pb2.PlaylistDocument()
    doc.application_info.CopyFrom(get_template().base.application_info)
    doc.type = propresenter_pb2.PlaylistDocument.TYPE_PRESENTATION
    node = doc.root_node
    node.uuid.string = _uuid()
    node.name = "PLAYLIST"
    node.expanded = True
    pl = node.playlists.playlists.add()
    pl.uuid.string = _uuid()
    pl.name = name
    pl.expanded = True
    pl.items.SetInParent()  # leere Playlist bleibt eine Playlist (keine Ordner)
    for e in entries:
        item = pl.items.items.add()
        item.uuid.string = _uuid()
        item.name = e["name"]
        if e["kind"] == "presentation":
            _document_url(item.presentation.document_path, root, e["rel"])
        else:
            _set_color(item.header.color, e.get("color") or DEFAULT_ITEM_COLOR)
    return doc


def playlist_entries(rows: list[dict], settings: PlaylistSettings, docs_by_rel: dict[str, Doc]) -> tuple[list[dict], list[str]]:
    """Zeilen aus der Oberfläche (mit include/choice) -> Playlist-Einträge + Hinweise."""
    entries, notes = [], []
    for row in rows:
        if not row.get("include", True):
            continue
        kind = row.get("type")
        title = (row.get("title") or "").strip()
        if kind == "song":
            rel = row.get("rel") or ""
            d = docs_by_rel.get(rel)
            if d:
                entries.append({"kind": "presentation", "name": nfc(d.name), "rel": d.rel})
            elif rel:
                notes.append(f"„{title}“: Präsentation {rel} nicht (mehr) in der Bibliothek – übersprungen.")
            elif settings.missing_as_header:
                entries.append({"kind": "header", "name": f"{title} (fehlt in ProPresenter)", "color": MISSING_COLOR})
                notes.append(f"„{title}“ nicht in der ProPresenter-Bibliothek gefunden – als Kopfzeile eingefügt.")
            else:
                notes.append(f"„{title}“ nicht in der ProPresenter-Bibliothek gefunden – weggelassen.")
        elif title:
            color = settings.header_color if kind == "header" else settings.item_color
            entries.append({"kind": "header", "name": title, "color": color})
    return entries, notes


def to_proplaylist(doc: propresenter_pb2.PlaylistDocument) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("data", doc.SerializeToString())
    return buf.getvalue()


def read_proplaylist(data: bytes) -> propresenter_pb2.PlaylistDocument:
    """Liest auch ProPresenters eigene Dateien (deren ZIP64-Ende ist defekt -> Fallback über lokalen Header)."""
    try:
        raw = zipfile.ZipFile(io.BytesIO(data)).read("data")
    except zipfile.BadZipFile:
        import struct
        i = data.find(b"PK\x03\x04")
        _, _, _, method, _, _, _, csize, _, nlen, xlen = struct.unpack("<IHHHHHIIIHH", data[i:i + 30])
        if method != 0:
            raise
        start = i + 30 + nlen + xlen
        extra = data[i + 30 + nlen:start]
        if csize == 0xFFFFFFFF and extra[:2] == b"\x01\x00":  # ZIP64-Größe im Extrafeld
            csize = struct.unpack("<Q", extra[4:12])[0]
        raw = data[start:start + csize]
    doc = propresenter_pb2.PlaylistDocument()
    doc.ParseFromString(raw)
    return doc
