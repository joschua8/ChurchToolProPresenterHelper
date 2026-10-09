#!/usr/bin/env python3
"""ChurchTools-Ablaufplan -> ProPresenter-7-Playlist (.proplaylist).

  * Ablaufpunkte (Überschriften und normale Punkte wie „Begrüßungsfolien“) werden Kopfzeilen.
  * Lieder werden NICHT neu aus ChurchTools erzeugt, sondern auf die Präsentationen verwiesen,
    die schon in der lokalen ProPresenter-Bibliothek liegen. Zuordnung über den Liednamen
    (exakt -> normalisiert -> ähnlich), bei mehreren Treffern nach Bibliotheks-Reihenfolge.

Format (aus echten Exporten von ProPresenter auf macOS und Windows abgeleitet): ZIP (unkomprimiert)
mit der Datei `data` = rv.data.PlaylistDocument und – wie beim Export aus ProPresenter – den
verwendeten .pro-Dateien direkt daneben. Lieder verweisen per `document_path` auf die .pro-Datei
(`local {root: ROOT_SHOW, path: "Libraries/<Bibliothek>/<Name>.pro"}` plus absoluter Pfad) und
nennen das ausgewählte Arrangement der Präsentation.
"""

from __future__ import annotations

import copy
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
    add_lords_prayer: bool = True        # Vaterunser unter dem Ablaufpunkt „Vaterunser“ einfügen
    add_silence: bool = True             # Stille-Bilder unter dem Ablaufpunkt „Stille“ einfügen
    silence_dir: str = ""                # Ordner mit den Stille-Bildern (.jpg/.png)

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


# ------------------------------------------------------------------ Automatische Einträge

# Vaterunser: CCLI 7113062 laut Joschua; die Präsentation „Sample/Vater Unser.pro“ trägt 7116302.
LORDS_PRAYER_CCLI = {7113062, 7116302}
LORDS_PRAYER_NAMES = ("Vater Unser", "Vaterunser")

IMAGE_EXT = {".jpg", ".jpeg", ".png"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac"}
SLIDES_EXT = {".pptx", ".ppt", ".key", ".pdf"}
KEYNOTE_IDS = ("com.apple.Keynote", "com.apple.iWork.Keynote")


def media_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    for kind, exts in (("image", IMAGE_EXT), ("video", VIDEO_EXT), ("audio", AUDIO_EXT), ("slides", SLIDES_EXT)):
        if ext in exts:
            return kind
    return ""


def _key(text: str) -> str:
    """„Vater Unser“, „Vaterunser“, „Vater-unser“ -> „vaterunser“."""
    return norm_name(text).replace(" ", "")


_ccli_cache: dict[str, tuple[float, int]] = {}


def doc_ccli(root: Path | None, doc: Doc) -> int:
    """CCLI-Nummer einer Präsentation (gecacht nach Änderungszeit)."""
    if root is None:
        return 0
    path = root / doc.rel
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0
    hit = _ccli_cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    from presentation_pb2 import Presentation
    pres = Presentation()
    try:
        pres.ParseFromString(path.read_bytes())
        number = pres.ccli.song_number
    except Exception:  # defekte/fremde Datei
        number = 0
    _ccli_cache[str(path)] = (mtime, number)
    return number


def find_lords_prayer(matcher: Matcher, root: Path | None) -> Match:
    """Vaterunser über die CCLI-Nummer, sonst über den Namen."""
    hits = matcher._sort([d for d in matcher.docs if doc_ccli(root, d) in LORDS_PRAYER_CCLI])
    if hits:
        return Match("exact", hits[0], hits[1:])
    for name in LORDS_PRAYER_NAMES:
        m = matcher.match(name, fuzzy=False)
        if m.doc:
            return m
    return Match("missing")


def silence_images(settings: PlaylistSettings) -> list[Path]:
    folder = Path(settings.silence_dir).expanduser() if settings.silence_dir else None
    if not folder or not folder.is_dir():
        return []
    return sorted((f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXT
                   and not f.name.startswith(".")), key=lambda f: f.name.casefold())


def _auto_row(row_id: str, kind: str, auto: str, title: str, before: bool = False, **extra) -> dict:
    return {"id": row_id, "type": kind, "auto": auto, "title": title, "note": "", "duration": 0,
            "start": "", "before": before, "responsible": "", **extra}


def _file_target(rows: list[dict], name: str) -> int | None:
    """Ablaufpunkt, zu dem eine Datei gehört („Predigt Gottes Geschenk.pptx“ -> „Predigt“)."""
    fk = re.sub(r"\d+", "", _key(Path(name).stem))
    if len(fk) < 4:
        return None
    for i, row in enumerate(rows):
        if row["type"] == "song" or row.get("auto"):
            continue
        rk = re.sub(r"\d+", "", _key(row["title"]))
        if len(rk) >= 4 and (rk in fk or fk in rk):
            return i
    return None


def add_auto_rows(rows: list[dict], matcher: Matcher, root: Path | None, settings: PlaylistSettings,
                  event_files: list[dict]) -> list[dict]:
    """Ergänzt den Ablauf um Vaterunser, Stille-Bilder und die Dateien am Termin.

    Die Zeilen werden immer erzeugt; ob sie in die Playlist kommen, entscheidet die Oberfläche
    (Häkchen, Standard aus add_lords_prayer / add_silence).
    """
    images = silence_images(settings)
    silence_error = "" if images else (
        "Ordner für Stille-Bilder unter Einstellungen festlegen." if not settings.silence_dir
        else f"Keine Bilder (.jpg/.png) in {settings.silence_dir}.")
    prayer = None

    files_after: dict[int, list[dict]] = {}
    skipped: list[str] = []  # .docx & Co. kommen nicht in die Playlist, nur als Hinweis-Kopfzeile
    for f in event_files:
        kind = media_kind(f["name"])
        if not kind:
            skipped.append(f["name"])
            continue
        file_row = _auto_row(f"file-{f['id']}", "media", "file", f["name"],
                             media={"source": "ct", "fileId": f["id"], "name": f["name"], "kind": kind})
        files_after.setdefault(_file_target(rows, f["name"]), []).append(file_row)

    out: list[dict] = []
    if skipped:
        out.append(_auto_row("auto-skipped", "header", "skipped", "Nicht übernommene Dateien: " + ", ".join(skipped)))
    for i, row in enumerate(rows):
        out.append(row)
        out.extend(files_after.get(i, []))
        if row["type"] == "song":
            continue
        key = _key(row["title"])
        if "vaterunser" in key:
            if prayer is None:
                prayer = find_lords_prayer(matcher, root)
            out.append(_auto_row(f"auto-vaterunser-{row['id']}", "song", "vaterunser", "Vaterunser", row["before"],
                                 song={"songId": None, "arrangementId": None, "title": "Vaterunser",
                                       "arrangement": "", "key": ""},
                                 match=prayer.to_json()))
        if key == "stille" or key.startswith("stille"):
            out.append(_auto_row(f"auto-stille-{row['id']}", "media", "stille", "Stille-Bilder", row["before"],
                                 media={"source": "silence", "kind": "image", "count": len(images)},
                                 error=silence_error))
    rest = files_after.get(None, [])
    if rest:
        out.append(_auto_row("auto-files", "header", "files", "Dateien aus ChurchTools"))
        out.extend(rest)
    return out


# ------------------------------------------------------------------ Medien-Präsentationen

BUNDLE_LIBRARY = "Ablaufplan"   # Bibliothek, unter der mitgelieferte Präsentationen verwiesen werden


def image_size(path: Path) -> tuple[int, int]:
    """Pixelgröße aus dem JPEG-/PNG-Kopf; Standard 1920×1080."""
    import struct
    try:
        with open(path, "rb") as fh:
            head = fh.read(26)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
            if head[:2] == b"\xff\xd8":
                fh.seek(2)
                while True:
                    marker, size = struct.unpack(">HH", fh.read(4))
                    if 0xFFC0 <= marker <= 0xFFCF and marker not in (0xFFC4, 0xFFC8, 0xFFCC):
                        h, w = struct.unpack(">xHH", fh.read(5))
                        return w, h
                    fh.seek(size - 2, 1)
    except (OSError, struct.error):
        pass
    return 1920, 1080


def find_soffice() -> str | None:
    """LibreOffice (für PowerPoint -> PDF ohne Oberfläche)."""
    import shutil
    for cand in (shutil.which("soffice"), "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                 r"C:\Program Files\LibreOffice\program\soffice.exe"):
        if cand and Path(cand).exists():
            return cand
    return None


def pdf_to_images(pdf: Path, out_dir: Path, stem: str, width: int = 1920) -> list[Path]:
    """Jede PDF-Seite als JPEG mit `width` Pixel Breite (PyMuPDF)."""
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError("PyMuPDF fehlt (pip install pymupdf)") from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    images = []
    with pymupdf.open(pdf) as doc:
        for n, page in enumerate(doc, 1):
            zoom = width / page.rect.width
            target = out_dir / f"{stem} {n}.jpg"
            page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(target, jpg_quality=90)
            images.append(target)
    if not images:
        raise RuntimeError("Die Datei hat keine Seiten.")
    return images


def _keynote_to_pdf(path: Path, out_dir: Path) -> Path:
    """Rückfall ohne LibreOffice: Keynote per AppleScript (kann an Keynote-Dialogen hängen bleiben)."""
    import plistlib
    import subprocess
    installed = set()
    for app in Path("/Applications").glob("Keynote*.app"):
        try:
            installed.add(plistlib.loads((app / "Contents/Info.plist").read_bytes()).get("CFBundleIdentifier"))
        except (OSError, ValueError):
            pass
    app_id = next((i for i in KEYNOTE_IDS if i in installed), None)
    if sys.platform != "darwin" or app_id is None:
        raise RuntimeError("Weder LibreOffice noch Keynote gefunden (brew install --cask libreoffice).")
    target = out_dir / f"{path.stem}.pdf"
    script = f'''on run argv
  with timeout of 240 seconds
    tell application id "{app_id}"
      set doc to open (POSIX file (item 1 of argv))
      export doc to (POSIX file (item 2 of argv)) as PDF
      close doc saving no
    end tell
  end timeout
end run'''
    try:
        res = subprocess.run(["osascript", "-", str(path), str(target)], input=script, text=True,
                             capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Keynote nicht erreichbar: {exc}") from exc
    if res.returncode != 0 or not target.exists():
        hint = " (Keynote zeigt evtl. einen Dialog)" if "-1712" in res.stderr else ""
        raise RuntimeError(f"Keynote konnte die Datei nicht umwandeln{hint}: {res.stderr.strip()[:200]}")
    return target


def _powerpoint_to_pdf(path: Path, out_dir: Path) -> Path:
    """Windows ohne LibreOffice: installiertes PowerPoint per COM (PowerShell) nach PDF."""
    import subprocess
    target = (out_dir / f"{path.stem}.pdf").resolve()
    script = ("$ErrorActionPreference='Stop'; $pp = New-Object -ComObject PowerPoint.Application; "
              "try { $p = $pp.Presentations.Open($env:LV_SRC, $true, $false, $false); "
              "$p.SaveAs($env:LV_DST, 32); $p.Close() } finally { $pp.Quit() }")
    env = {**os.environ, "LV_SRC": str(path.resolve()), "LV_DST": str(target)}
    try:
        res = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=300, env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"PowerPoint nicht erreichbar: {exc}") from exc
    if res.returncode != 0 or not target.exists():
        raise RuntimeError("Weder LibreOffice noch PowerPoint konnten die Datei umwandeln: "
                           f"{(res.stderr or res.stdout).strip()[:200]}")
    return target


def slides_to_images(path: Path, out_dir: Path) -> list[Path]:
    """PowerPoint/Keynote/PDF -> ein JPEG (1920 px breit) je Folie, benannt „<Datei> 1.jpg“ …

    PowerPoint geht über LibreOffice headless nach PDF (keine Dialoge), sonst über PowerPoint (Windows)
    bzw. Keynote (macOS).
    Wirft RuntimeError, wenn das nicht geht.
    """
    import subprocess
    out_dir.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".pdf":
        return pdf_to_images(path, out_dir, path.stem)
    soffice = find_soffice() if path.suffix.lower() != ".key" else None
    if soffice:
        pdf_dir = out_dir / "pdf"
        # eigenes Profil: läuft auch, wenn LibreOffice gerade geöffnet ist
        profile = (out_dir / "lo-profile").resolve().as_uri()
        try:
            res = subprocess.run([soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                                  "--convert-to", "pdf", "--outdir", str(pdf_dir), str(path)],
                                 capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"LibreOffice nicht erreichbar: {exc}") from exc
        pdf = pdf_dir / f"{path.stem}.pdf"
        if res.returncode != 0 or not pdf.exists():
            raise RuntimeError(f"LibreOffice konnte die Datei nicht umwandeln: {(res.stderr or res.stdout).strip()[:200]}")
    elif sys.platform.startswith("win"):
        pdf = _powerpoint_to_pdf(path, out_dir)
    else:
        pdf = _keynote_to_pdf(path, out_dir)
    return pdf_to_images(pdf, out_dir, path.stem)


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "\x1f".join(("liederverwaltung-media",) + parts))).upper()


def media_presentation(name: str, media: list[tuple[Path, str]], root: Path | None):
    """Präsentation mit einer Folie je Medium (Bild / Video / Audio), wie ProPresenter Bilder importiert.

    media: [(Datei, Name im Bundle unter Media/)]. Verweise: Media/Assets/<Name> im Arbeitsordner.
    """
    import action_pb2
    from pro_export import get_template

    pres = copy.deepcopy(get_template().base)
    pres.uuid.string = _uid(name)
    pres.name = name
    group = pres.cue_groups.add()
    group.group.uuid.string = _uid(name, "group")
    group.group.name = name
    for n, (path, bundle_name) in enumerate(media):
        kind = media_kind(path.name)
        cue = pres.cues.add()
        cue.uuid.string = _uid(name, "cue", str(n))
        cue.isEnabled = True
        cue.completion_action_type = cue.COMPLETION_ACTION_TYPE_LAST
        slide = cue.actions.add()
        slide.uuid.string = _uid(name, "slide-action", str(n))
        slide.isEnabled = True
        slide.type = action_pb2.Action.ACTION_TYPE_PRESENTATION_SLIDE
        slide.label.text = Path(bundle_name).stem
        base = slide.slide.presentation.base_slide
        base.uuid.string = _uid(name, "slide", str(n))
        base.size.width, base.size.height = 1920, 1080
        base.background_color.alpha = 1

        act = cue.actions.add()
        act.uuid.string = _uid(name, "media-action", str(n))
        act.name = Path(bundle_name).stem
        act.isEnabled = True
        act.type = action_pb2.Action.ACTION_TYPE_MEDIA
        el = act.media.element
        el.uuid.string = _uid(name, "media", str(n))
        _document_url(el.url, root, f"Media/Assets/{bundle_name}")
        el.metadata.format = path.suffix.lstrip(".").upper()
        if kind == "audio":
            el.audio.SetInParent()
            act.media.audio.SetInParent()
        else:
            w, h = image_size(path) if kind == "image" else (1920, 1080)
            props = el.video if kind == "video" else el.image
            props.drawing.natural_size.width, props.drawing.natural_size.height = w, h
            (act.media.video if kind == "video" else act.media.image).SetInParent()
            act.media.layer_type = act.media.LAYER_TYPE_FOREGROUND
        group.cue_identifiers.add().string = cue.uuid.string

    arr = pres.arrangements.add()
    arr.uuid.string = _uid(name, "arrangement")
    arr.name = "Standard"
    arr.group_identifiers.add().string = group.group.uuid.string
    pres.selected_arrangement.string = arr.uuid.string
    return pres


def _bundle_name(path: Path, taken: set[str]) -> str:
    stem = re.sub(r'[\\/:*?"<>|]+', "_", nfc(path.stem)).strip() or "Datei"
    name, n = f"{stem}{path.suffix.lower()}", 2
    while name.casefold() in taken:
        name, n = f"{stem} ({n}){path.suffix.lower()}", n + 1
    taken.add(name.casefold())
    return name


# ------------------------------------------------------------------ .proplaylist


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


def _set_color(color, hex_color: str) -> None:
    hex_color = hex_color.lstrip("#")
    color.red, color.green, color.blue = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    color.alpha = 1


def _document_url(url, root: Path | None, rel: str) -> None:
    """Verweis auf eine Präsentation, wie ProPresenter ihn selbst schreibt."""
    url.local.root = basicTypes_pb2.URL.LocalRelativePath.ROOT_SHOW
    url.local.path = rel
    if root is None:
        return
    full = root / rel
    if sys.platform.startswith("win"):
        # So schreibt es ProPresenter unter Windows selbst: normaler Pfad mit „\“, keine file://-URL.
        url.platform = basicTypes_pb2.URL.PLATFORM_WIN32
        url.absolute_string = str(PureWindowsPath(full))
    else:
        url.platform = basicTypes_pb2.URL.PLATFORM_MACOS
        url.absolute_string = "file://" + quote(str(full), safe="/")


def _arrangement_uuid(path: Path) -> str:
    """Ausgewähltes (sonst erstes) Arrangement der Präsentation – ProPresenter nennt es im Playlist-Eintrag."""
    import presentation_pb2
    try:
        pres = presentation_pb2.Presentation()
        pres.ParseFromString(path.read_bytes())
    except Exception:
        return ""
    if pres.selected_arrangement.string:
        return pres.selected_arrangement.string
    return pres.arrangements[0].uuid.string if pres.arrangements else ""


def build_playlist(name: str, entries: list[dict], root: Path | None,
                   bundle: dict[str, bytes | Path] | None = None) -> propresenter_pb2.PlaylistDocument:
    """entries: {"kind": "header", "name", "color"} | {"kind": "presentation", "name", "rel"}
    | {"kind": "media", "name", "files": [Path]} -> neue Präsentation, die samt Medien in `bundle` landet
    (Zip-Pfad -> Inhalt, wie bei ProPresenters eigenem Playlist-Export: <Name>.pro und Media/<Datei>).
    Lieder werden NICHT mitgepackt: ProPresenter würde sie beim Öffnen als neue Bibliothek
    importieren (Duplikate). Sie werden nur über den Pfad in der vorhandenen Bibliothek verwiesen.
    """
    from pro_export import get_template, file_stem, unique_stem  # App-Version wie bei den exportierten Liedern

    bundle = {} if bundle is None else bundle
    taken_pro: set[str] = set()
    taken_media: set[str] = set()

    doc = propresenter_pb2.PlaylistDocument()
    doc.application_info.CopyFrom(get_template().base.application_info)
    if sys.platform.startswith("win"):
        # Mit Mac-Kennung verwirft ProPresenter unter Windows die Verweise auf die Lieder.
        info = doc.application_info
        info.platform = type(info).PLATFORM_WINDOWS
        info.platform_version.Clear()
        info.platform_version.major_version = 10
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
            arrangement = _arrangement_uuid(root / e["rel"]) if root else ""
            if arrangement:
                item.presentation.arrangement.string = arrangement
        elif e["kind"] == "media":
            media = [(f, _bundle_name(f, taken_media)) for f in e["files"]]
            stem = unique_stem(file_stem(e["name"]), taken_pro)
            pres = media_presentation(e["name"], media, root)
            bundle[f"{stem}.pro"] = pres.SerializeToString()
            for f, bundle_name in media:
                bundle[f"Media/{bundle_name}"] = f
            _document_url(item.presentation.document_path, root, f"Libraries/{BUNDLE_LIBRARY}/{stem}.pro")
        else:
            _set_color(item.header.color, e.get("color") or DEFAULT_ITEM_COLOR)
    return doc


def playlist_entries(rows: list[dict], settings: PlaylistSettings, docs_by_rel: dict[str, Doc],
                     resolve_media=None) -> tuple[list[dict], list[str]]:
    """Zeilen aus der Oberfläche (mit include/choice) -> Playlist-Einträge + Hinweise.

    resolve_media(row) -> [Path]: Dateien einer Medien-Zeile (lädt z. B. aus ChurchTools); wirft RuntimeError.
    """
    entries, notes = [], []
    for row in rows:
        if not row.get("include", True):
            continue
        kind = row.get("type")
        title = (row.get("title") or "").strip()
        if kind == "media":
            try:
                files = resolve_media(row) if resolve_media else []
            except RuntimeError as exc:
                notes.append(f"„{title}“: {exc} – übersprungen.")
                continue
            if files:
                name = Path(title).stem if (row.get("media") or {}).get("source") == "ct" else title
                entries.append({"kind": "media", "name": name, "files": files})
            else:
                notes.append(f"„{title}“: keine Dateien – übersprungen.")
        elif kind == "song":
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
            if row.get("auto") == "skipped":
                color = MISSING_COLOR
            entries.append({"kind": "header", "name": title, "color": color})
    return entries, notes


def to_proplaylist(doc: propresenter_pb2.PlaylistDocument, bundle: dict[str, bytes | Path] | None = None) -> bytes:
    """ZIP (unkomprimiert) mit `data`; plus mitgelieferte Präsentationen und Medien (bundle)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        zf.writestr("data", doc.SerializeToString())
        for name, content in (bundle or {}).items():
            if isinstance(content, Path):
                zf.write(content, name)
            else:
                zf.writestr(name, content)
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
