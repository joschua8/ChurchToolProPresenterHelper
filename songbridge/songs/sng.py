"""Lesen und Schreiben von SongBeamer-Dateien (.sng) sowie Umwandlung von Freitext.

Aufbau einer .sng-Datei (UTF-8 mit BOM):
    #Title=...            Kopfzeilen "#Schlüssel=Wert"
    #VerseOrder=Verse 1,Chorus 1,...
    ---                   Trenner vor jedem Abschnitt („--“ = neue Folie, wird genauso gelesen)
    Verse 1               Abschnittsname (erste Zeile nach dem Trenner)
    Liedzeile ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Kennzeichnet von Hand erfasste Lieder; der ChurchTools-Import überschreibt sie nie.
MANUAL_EDITOR = "Liederverwaltung (manuell)"  # früherer Programmname – Kennung in vorhandenen Dateien, nicht ändern

# Freitext-Schlüsselwort -> SongBeamer-Abschnittstyp
LABEL_TYPES = {
    "strophe": "Verse",
    "vers": "Verse",
    "verse": "Verse",
    "refrain": "Chorus",
    "chorus": "Chorus",
    "kehrvers": "Chorus",
    "pre-chorus": "Pre-Chorus",
    "prechorus": "Pre-Chorus",
    "pre chorus": "Pre-Chorus",
    "pre-refrain": "Pre-Chorus",
    "prerefrain": "Pre-Chorus",
    "bridge": "Bridge",
    "brücke": "Bridge",
    "intro": "Intro",
    "interlude": "Interlude",
    "instrumental": "Instrumental",
    "zwischenspiel": "Interlude",
    "zwischenteil": "Interlude",
    "outro": "Ending",
    "ending": "Ending",
    "ende": "Ending",
    "schluss": "Ending",
    "coda": "Coda",
    "tag": "Tag",
    "misc": "Misc",
}

_LABEL_RE = re.compile(
    r"^\s*[\[(]?\s*(" + "|".join(sorted(map(re.escape, LABEL_TYPES), key=len, reverse=True)) + r")"
    r"\s*(\d+)?\s*[\])]?\s*:?\s*$",
    re.IGNORECASE,
)
# Abschnittsname in einer .sng (erste Zeile nach „---“): bekannte Bezeichnung, optional mit Nummer („Verse 1a“).
# Alles andere ist schon Liedtext.
_SNG_LABEL_RE = re.compile(
    r"^\s*(" + "|".join(sorted(map(re.escape, [*LABEL_TYPES, "unbekannt", "unknown", "teil", "part"]),
                              key=len, reverse=True)) + r")(?:\s*\d+[a-z]?)?\s*$",
    re.IGNORECASE,
)
# Abschnitte ohne Text (leere Folie), z. B. "Instrumental" allein in einem Block
TEXTLESS_TYPES = {"Instrumental", "Interlude", "Intro", "Ending"}
# "1. Text..." oder "1) Text..." am Blockanfang (Gesangbuch-Stil)
_NUMBERED_RE = re.compile(r"^\s*(\d{1,2})[.)]\s+(\S.*)$")


@dataclass
class Section:
    label: str
    lines: list[str]


@dataclass
class Song:
    title: str = ""
    author: str = ""
    ccli: str = ""
    copyright: str = ""
    sections: list[Section] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    lang_count: int = 1
    editor: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def manual(self) -> bool:
        return self.editor == MANUAL_EDITOR

    def ordered_sections(self) -> list[Section]:
        by_label = {s.label: s for s in self.sections}
        if self.order and all(lbl in by_label for lbl in self.order):
            return [by_label[lbl] for lbl in self.order]
        return list(self.sections)


# --------------------------------------------------------------------------- lesen


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def parse_sng(text: str) -> Song:
    song = Song()
    # SongBeamer: „---“ trennt Abschnitte, „--“ Folien. Ein Block ohne Abschnittsnamen hängt am vorigen Abschnitt.
    blocks = re.split(r"^-{2,}[ \t]*$", text.replace("\r\n", "\n").replace("\r", "\n"), flags=re.MULTILINE)
    for line in blocks[0].splitlines():
        if line.startswith("#") and "=" in line:
            key, value = line[1:].split("=", 1)
            song.headers[key.strip()] = value.strip()

    h = song.headers
    song.title = h.get("Title", "")
    song.author = h.get("Author", "")
    song.ccli = h.get("CCLI", "")
    song.copyright = h.get("(c)", "")
    song.editor = h.get("Editor", "")
    song.order = [s.strip() for s in h.get("VerseOrder", "").split(",") if s.strip()]
    try:
        song.lang_count = max(1, int(h.get("LangCount", "1")))
    except ValueError:
        song.lang_count = 1

    parsed = []  # (Abschnittsname oder None, Zeilen)
    for block in blocks[1:]:
        lines = [ln.rstrip() for ln in block.strip("\n").split("\n")]
        while lines and not lines[-1]:
            lines.pop()
        if not lines:
            continue
        if is_sng_label(lines[0]) or lines[0].strip() in song.order:
            parsed.append((lines[0].strip(), lines[1:]))
        else:  # erste Zeile ist schon Liedtext (z. B. alte SongBeamer-Dateien ohne Abschnittsnamen)
            parsed.append((None, lines))
    if all(label for label, _ in parsed):
        song.sections = [Section(label, lines) for label, lines in parsed]
    else:
        _add_unlabeled(song, parsed)
    return song


def is_sng_label(line: str) -> bool:
    """Ist die erste Zeile eines Blocks ein Abschnittsname („Verse 1“, „Chorus“, „Unbekannt 2a“ …)?"""
    return bool(_SNG_LABEL_RE.match(line))


def _add_unlabeled(song: Song, parsed: list[tuple[str | None, list[str]]]) -> None:
    """Blöcke ohne Abschnittsnamen einordnen (wie SongBeamer):

      * nach einem benannten Abschnitt -> weitere Folie dieses Abschnitts;
      * sonst (Lied ganz ohne Namen) -> eigener Abschnitt: Text, der mehrfach vorkommt, wird „Chorus“,
        der Rest „Verse 1, 2, …“; Wiederholungen landen in der Reihenfolge.
    """
    taken = {label for label, _ in parsed if label}
    counters: dict[str, int] = {}

    def next_label(kind: str) -> str:
        n = counters.get(kind, 0) + 1
        while f"{kind} {n}" in taken:
            n += 1
        counters[kind] = n
        taken.add(f"{kind} {n}")
        return f"{kind} {n}"

    texts = [_norm_lines(lines) for label, lines in parsed if label is None]
    sequence: list[str] = []  # Abschnitte in Dateireihenfolge, mit Wiederholungen
    generated: dict[str, Section] = {}  # Text -> erzeugter Abschnitt
    named: Section | None = None  # zuletzt begonnener benannter Abschnitt
    for label, lines in parsed:
        if label:
            named = Section(label, list(lines))
            song.sections.append(named)
            sequence.append(label)
        elif named is not None:
            named.lines.extend(lines)
        elif (key := _norm_lines(lines)) in generated:
            sequence.append(generated[key].label)
        else:
            sec = Section(next_label("Chorus" if texts.count(key) > 1 else "Verse"), list(lines))
            generated[key] = sec
            song.sections.append(sec)
            sequence.append(sec.label)
    # Die Kopfzeile VerseOrder kennt die erzeugten Abschnitte nicht -> Reihenfolge aus der Datei.
    if generated:
        song.order = sequence


def read_sng(path: Path) -> Song:
    return parse_sng(read_text(path))


def is_manual_file(path: Path) -> bool:
    try:
        head = read_text(path)[:2000]
    except OSError:
        return False
    return f"#Editor={MANUAL_EDITOR}" in head


# ------------------------------------------------------------------------ schreiben


def build_sng(song: Song) -> str:
    order = song.order or [s.label for s in song.sections]
    header = [
        ("LangCount", "1"),
        ("Title", song.title),
        ("Editor", MANUAL_EDITOR),
        ("Version", "3"),
        ("Author", song.author),
        ("CCLI", song.ccli),
        ("(c)", song.copyright),
        ("VerseOrder", ",".join(order)),
    ]
    out = [f"#{k}={_one_line(v)}" for k, v in header if v or k in ("Title", "LangCount")]
    for sec in song.sections:
        out.append("---")
        out.append(sec.label)
        out.extend(sec.lines)
    return "﻿" + "\n".join(out) + "\n"


def _one_line(value: str) -> str:
    return " ".join(str(value).split())


# ------------------------------------------------------------------ Freitext -> Lied


def parse_freetext(text: str) -> tuple[list[Section], list[str], list[str]]:
    """Wandelt eingefügten Liedtext in Abschnitte + Reihenfolge um.

    Regeln:
      * Leerzeilen trennen Abschnitte.
      * Erste Zeile wie "Strophe 1", "[Refrain]", "Chorus:", "Bridge" -> Abschnittsname.
      * "1. Text..." am Blockanfang -> Strophe 1.
      * Nur ein Name ohne Text (z. B. "Refrain") -> Wiederholung eines vorhandenen Abschnitts;
        bei Instrumental/Intro/Zwischenspiel/Schluss ohne Vorgänger -> leerer Abschnitt (leere Folie).
      * Ein Block ohne Namen, der wörtlich einem vorhandenen Abschnitt entspricht, gilt als Wiederholung.
      * Sonst ohne Namen -> nächste Strophe.
    Rückgabe: (Abschnitte, Reihenfolge, Hinweise)
    """
    sections: list[Section] = []
    order: list[str] = []
    warnings: list[str] = []
    counters: dict[str, int] = {}

    def by_label(label: str) -> Section | None:
        return next((s for s in sections if s.label == label), None)

    def by_lines(lines: list[str]) -> Section | None:
        key = _norm_lines(lines)
        return next((s for s in sections if _norm_lines(s.lines) == key), None)

    def next_label(kind: str) -> str:
        n = counters.get(kind, 0) + 1
        while by_label(f"{kind} {n}"):
            n += 1
        counters[kind] = n
        return f"{kind} {n}"

    def add(label: str, lines: list[str]) -> None:
        sections.append(Section(label, lines))
        order.append(label)

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b for b in re.split(r"\n\s*\n", normalized) if b.strip()]

    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        kind, number = None, None

        m = _LABEL_RE.match(lines[0])
        if m:
            kind = LABEL_TYPES[m.group(1).lower()]
            number = int(m.group(2)) if m.group(2) else None
            lines = lines[1:]
        else:
            m = _NUMBERED_RE.match(lines[0])
            if m:
                kind, number = "Verse", int(m.group(1))
                lines = [m.group(2)] + lines[1:]

        if kind is None:
            same = by_lines(lines)
            if same:
                order.append(same.label)
            else:
                add(next_label("Verse"), lines)
            continue

        if not lines:  # nur Name -> Wiederholung
            if number is not None:
                target = by_label(f"{kind} {number}")
            else:
                target = next((s for s in reversed(sections) if s.label.startswith(kind + " ")), None)
            if target:
                order.append(target.label)
            elif kind in TEXTLESS_TYPES:
                add(f"{kind} {number}" if number is not None else next_label(kind), [])
            else:
                warnings.append(f"„{kind}{' ' + str(number) if number else ''}“ wird wiederholt, kommt aber vorher nicht vor – ignoriert.")
            continue

        if number is None:
            same = next(
                (s for s in sections if s.label.startswith(kind + " ") and _norm_lines(s.lines) == _norm_lines(lines)),
                None,
            )
            if same:
                order.append(same.label)
            else:
                add(next_label(kind), lines)
            continue

        label = f"{kind} {number}"
        existing = by_label(label)
        if existing is None:
            counters[kind] = max(counters.get(kind, 0), number)
            add(label, lines)
        elif _norm_lines(existing.lines) == _norm_lines(lines):
            order.append(label)
        else:
            new_label = next_label(kind)
            warnings.append(f"„{label}“ kommt zweimal mit unterschiedlichem Text vor – zweiter als „{new_label}“ gespeichert.")
            add(new_label, lines)

    return sections, order, warnings


def song_to_freetext(song: Song) -> str:
    """Gegenstück zu parse_freetext – zum Bearbeiten eines gespeicherten Liedes."""
    by_label = {s.label: s for s in song.sections}
    order = song.order if song.order and all(l in by_label for l in song.order) else [s.label for s in song.sections]
    seen: set[str] = set()
    parts = []
    for label in order:
        if label in seen:
            parts.append(label)
        else:
            seen.add(label)
            parts.append("\n".join([label] + by_label[label].lines))
    unused = [s for s in song.sections if s.label not in seen]
    parts.extend("\n".join([s.label] + s.lines) for s in unused)
    return "\n\n".join(parts)


def _norm_lines(lines: list[str]) -> str:
    return "\n".join(" ".join(ln.split()).casefold() for ln in lines)


def norm_title(title: str) -> str:
    return re.sub(r"[^\w]+", " ", title.casefold()).strip()
