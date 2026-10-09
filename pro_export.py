#!/usr/bin/env python3
"""Export von SongBeamer-Liedern (.sng) nach ProPresenter 7 (.pro).

Ablauf:  .sng  ->  Song  ->  .json (Protobuf-Textformat, wie examples/BeispielZielFormatierung.json)
               ->  propresenterFormatter.json_to_pro  ->  .pro

Rahmen (App-Version, Foliengröße, Textbox-Grundaufbau) kommt aus der Beispieldatei als Vorlage.
Alles, was man in der Weboberfläche einstellen kann (Schrift, Farben, Ausrichtung, Ränder …),
steht in ExportStyle; die Standardwerte entsprechen exakt der Beispieldatei.
Die Einstellungen der Weboberfläche liegen in export_settings.json und gelten auch für die CLI.

CLI:
    python pro_export.py                 # alle Lieder aus songs/ -> export/*.pro
    python pro_export.py --json          # zusätzlich die .json-Zwischendateien schreiben
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from propresenterFormatter import formatter
from propresenterFormatter.formatter import parse_presentation, presentation_to_json
from sng import Section, Song, read_sng

import graphicsData_pb2  # noqa: E402  (über propresenterFormatter im Suchpfad)
import hotKey_pb2  # noqa: E402

from paths import DATA_DIR, RES_DIR  # noqa: E402

BASE_DIR = DATA_DIR
TEMPLATE_PATH = RES_DIR / "examples" / "BeispielZielFormatierung.json"
SETTINGS_PATH = Path(os.environ.get("EXPORT_SETTINGS", DATA_DIR / "export_settings.json"))
SLIDE_W, SLIDE_H = 1920, 1080

# Deterministische UUIDs: gleiches Lied -> gleiche IDs bei jedem Export.
_NS = uuid.UUID("6f1c7a52-3f0e-4b8e-9a51-7d1e2c0b9a10")

# Schriftfamilie -> PostScript-Namen (normal, fett, kursiv, fett+kursiv), RTF-Familie, CSS für die Vorschau.
# Nur Schriften, die auf macOS vorinstalliert sind (ProPresenter muss sie finden).
FONTS = {
    "Helvetica": (("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"), "swiss", "Helvetica"),
    "Helvetica Neue": (("HelveticaNeue", "HelveticaNeue-Bold", "HelveticaNeue-Italic", "HelveticaNeue-BoldItalic"), "swiss", "'Helvetica Neue'"),
    "Arial": (("ArialMT", "Arial-BoldMT", "Arial-ItalicMT", "Arial-BoldItalicMT"), "swiss", "Arial"),
    "Avenir Next": (("AvenirNext-Regular", "AvenirNext-Bold", "AvenirNext-Italic", "AvenirNext-BoldItalic"), "swiss", "'Avenir Next'"),
    "Verdana": (("Verdana", "Verdana-Bold", "Verdana-Italic", "Verdana-BoldItalic"), "swiss", "Verdana"),
    "Gill Sans": (("GillSans", "GillSans-Bold", "GillSans-Italic", "GillSans-BoldItalic"), "swiss", "'Gill Sans'"),
    "Futura": (("Futura-Medium", "Futura-Bold", "Futura-MediumItalic", "Futura-Bold"), "swiss", "Futura"),
    "Georgia": (("Georgia", "Georgia-Bold", "Georgia-Italic", "Georgia-BoldItalic"), "roman", "Georgia"),
    "Times New Roman": (("TimesNewRomanPSMT", "TimesNewRomanPS-BoldMT", "TimesNewRomanPS-ItalicMT", "TimesNewRomanPS-BoldItalicMT"), "roman", "'Times New Roman'"),
}

# Gruppenfarben (RGB 0..1) je Abschnittstyp, angelehnt an das Beispiel.
GROUP_COLORS = {
    "Verse": (1, 1, 0),
    "Chorus": (1, 0, 0),
    "Pre-Chorus": (1, 0.5, 0),
    "Bridge": (0, 0, 1),
    "Intro": (0, 1, 0),
    "Outro": (0, 1, 0),
    "Interlude": (0, 0, 0),
    "Instrumental": (0, 0, 0),
    "Coda": (0, 1, 0),
    "Misc": (0.5, 0, 1),
    "Tag": (0.5, 0, 1),
}
# Leere Folie vor dem Liedtext (eigene Gruppe am Anfang des Arrangements).
BLANK_LABEL, BLANK_NAME, BLANK_COLOR = "__blank__", "Blank", (0.2, 0.2, 0.2)
NOTE_ABBR = {
    "Verse": "V", "Chorus": "CHO", "Pre-Chorus": "PRE", "Bridge": "BRI", "Intro": "INT",
    "Outro": "OUT", "Interlude": "INS", "Instrumental": "INS", "Coda": "CODA", "Misc": "MISC", "Tag": "TAG",
}
# SongBeamer-Bezeichnungen, die auch vorkommen können -> Typ
_LABEL_ALIASES = {
    "Vers": "Verse", "Strophe": "Verse", "Refrain": "Chorus", "Pre-Refrain": "Pre-Chorus",
    "Zwischenspiel": "Interlude", "Ending": "Outro", "Schluss": "Outro", "Unbekannt": "Misc", "Unknown": "Misc",
    "Teil": "Misc", "Part": "Misc",
}

# Tastenkürzel („Song-Navigation“) auf der ersten Folie jeder Gruppe – Zuordnung von Joschua (2026-10-08).
# In der Oberfläche änderbar.
DEFAULT_HOTKEYS = {
    "Verse 1": "A", "Verse 2": "S", "Verse 3": "D", "Verse 4": "F", "Verse 5": "G", "Verse 6": "H",
    "Chorus": "C",
    "Bridge": "B",
    "Pre-Chorus": "P",
    "Interlude": "I",
    "Tag": "T",
    "Outro": "O",
    "Intro": "N",
    "Misc": "B",  # SongBeamer „Unbekannt“/„Teil“ – meist eine Bridge
}
# Gruppentypen, die unter anderem Namen in der Tabelle stehen (Refrain/Zwischenspiel sind schon beim Einlesen Chorus/Interlude).
HOTKEY_ALIASES = {"Instrumental": "Interlude", "Ending": "Outro"}
_HOTKEY_RE = re.compile(r"^[A-Z0-9]$")

_ALIGN = {"left": ("\\ql", "ALIGNMENT_LEFT"), "center": ("\\qc", "ALIGNMENT_CENTER"), "right": ("\\qr", "ALIGNMENT_RIGHT")}
_VALIGN = {"top": "VERTICAL_ALIGNMENT_TOP", "middle": "VERTICAL_ALIGNMENT_MIDDLE", "bottom": "VERTICAL_ALIGNMENT_BOTTOM"}


# ---------------------------------------------------------------------- Einstellungen


@dataclass
class TextStyle:
    font: str = "Helvetica"
    size: float = 85          # Punkt (= Pixel auf der 1920×1080-Folie)
    bold: bool = True
    italic: bool = False
    color: str = "#ffffff"
    uppercase: bool = False

    def postscript(self) -> str:
        names = FONTS.get(self.font, FONTS["Helvetica"])[0]
        return names[(1 if self.bold else 0) + (2 if self.italic else 0)]


@dataclass
class ExportStyle:
    """Alle einstellbaren Formatierungen. Standard = examples/BeispielZielFormatierung.json."""
    lines_per_slide: int = 4      # einsprachig
    pairs_per_slide: int = 2      # zweisprachig (Original + Übersetzung)
    main: TextStyle = field(default_factory=TextStyle)
    translation: TextStyle = field(default_factory=lambda: TextStyle(font="Arial", size=70, bold=False, italic=True))
    pair_spacing: float = 10      # Leerzeile zwischen Zeilenpaaren (pt), 0 = keine
    align: str = "center"         # left / center / right
    valign: str = "bottom"        # top / middle / bottom
    margin_top: int = 0           # Textbox-Ränder in Pixel
    margin_bottom: int = 190
    margin_left: int = 0
    margin_right: int = 0
    background: str = "#000000"   # Folienhintergrund
    box_fill: bool = True         # Textbox-Hintergrund (im Beispiel an, schwarz)
    box_color: str = "#000000"
    box_opacity: float = 1.0
    shadow: bool = False          # Textschatten (im Beispiel konfiguriert, aber aus)
    shrink_to_fit: bool = False   # ProPresenter verkleinert Schrift, wenn der Text nicht passt
    blank_first: bool = True      # leere Folie vor dem Liedtext
    hotkeys_enabled: bool = True  # Tastenkürzel wie im Beispiel setzen
    hotkeys: dict = field(default_factory=lambda: dict(DEFAULT_HOTKEYS))  # Gruppenname -> Taste

    @classmethod
    def from_dict(cls, data: dict | None) -> "ExportStyle":
        """Tolerant: unbekannte Felder ignorieren, ungültige Werte durch Standardwerte ersetzen."""
        style = cls()
        if not isinstance(data, dict):
            return style
        for f in fields(cls):
            if f.name not in data:
                continue
            default = getattr(style, f.name)
            value = data[f.name]
            if isinstance(default, TextStyle):
                setattr(style, f.name, _text_style(value, default))
            elif isinstance(default, dict):
                setattr(style, f.name, _hotkeys(value, default))
            else:
                setattr(style, f.name, _coerce(value, default))
        style.lines_per_slide = _clamp(style.lines_per_slide, 1, 12)
        style.pairs_per_slide = _clamp(style.pairs_per_slide, 1, 6)
        style.pair_spacing = _clamp(style.pair_spacing, 0, 200)
        style.box_opacity = _clamp(style.box_opacity, 0, 1)
        # Textbox frei verschiebbar, aber mindestens 50 px groß und innerhalb der Folie.
        style.margin_top = _clamp(style.margin_top, 0, SLIDE_H - 50)
        style.margin_bottom = _clamp(style.margin_bottom, 0, SLIDE_H - 50 - style.margin_top)
        style.margin_left = _clamp(style.margin_left, 0, SLIDE_W - 50)
        style.margin_right = _clamp(style.margin_right, 0, SLIDE_W - 50 - style.margin_left)
        if style.align not in _ALIGN:
            style.align = "center"
        if style.valign not in _VALIGN:
            style.valign = "bottom"
        return style

    def to_dict(self) -> dict:
        return asdict(self)


def _coerce(value, default):
    try:
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, int):
            return int(round(float(value)))
        if isinstance(default, float):
            return float(value)
        if isinstance(default, str):
            value = str(value)
            if default.startswith("#"):
                return value.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else default
            return value
    except (TypeError, ValueError):
        pass
    return default


def _clamp(value, lo, hi):
    return type(value)(min(max(value, lo), hi))


def _text_style(data, default: TextStyle) -> TextStyle:
    ts = copy.deepcopy(default)
    if isinstance(data, dict):
        for f in fields(TextStyle):
            if f.name in data:
                setattr(ts, f.name, _coerce(data[f.name], getattr(ts, f.name)))
    if ts.font not in FONTS:
        ts.font = default.font
    ts.size = _clamp(float(ts.size), 8, 300)
    return ts


def _hotkeys(data, default: dict) -> dict:
    if not isinstance(data, dict):
        return dict(default)
    out = {}
    for name, key in data.items():
        name, key = " ".join(str(name).split()), str(key).strip().upper()
        if name and (key == "" or _HOTKEY_RE.match(key)):
            out[name] = key
    return out


def hotkey_for(group_name: str, style: ExportStyle) -> str:
    """Taste für eine Gruppe; „Chorus“ und „Chorus 1“ gelten als gleich."""
    if not style.hotkeys_enabled:
        return ""
    keys = style.hotkeys
    m = re.match(r"^(.*?)(?: (\d+))?$", group_name)
    base, num = m.group(1), m.group(2)
    for b in (base, HOTKEY_ALIASES.get(base)):
        if not b:
            continue
        for candidate in ([f"{b} {num}"] if num else []) + ([b] if num in (None, "1") else []) + ([f"{b} 1"] if not num else []):
            if candidate in keys:
                return keys[candidate]
    return ""


def load_style(path: Path | None = None) -> ExportStyle:
    path = path or SETTINGS_PATH
    try:
        return ExportStyle.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ExportStyle()


def save_style(style: ExportStyle, path: Path | None = None) -> None:
    path = path or SETTINGS_PATH
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(style.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ------------------------------------------------------------------------------ RTF

_RTF_EMPTY = (
    "{\\rtf1\\ansi\\ansicpg1252\\cocoartf2907\n"
    "\\cocoatextscaling0\\cocoaplatform0{\\fonttbl}\n"
    "{\\colortbl;\\red255\\green255\\blue255;}\n"
    "{\\*\\expandedcolortbl;;}\n}"
)


def rtf_escape(text: str) -> str:
    out = []
    for ch in text:
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ord(ch) < 128:
            out.append(ch)
        else:
            try:
                out.append("\\'%02x" % ch.encode("cp1252")[0])
            except UnicodeEncodeError:
                out.append("\\uc0\\u%d " % ord(ch))
    return "".join(out)


def _fs(size: float) -> int:
    return int(round(size * 2))  # RTF: halbe Punkt


def build_rtf(paragraphs: list[tuple[str, str]], style: ExportStyle | None = None) -> str:
    """paragraphs: Liste aus (Art, Text) mit Art in main / trans / spacer.

    Aufbau wie von ProPresenter/Cocoa erzeugt: f0 = Hauptschrift, f1 = Helvetica (Rücksetzen),
    f2 = Übersetzungsschrift; cf1/cf2 = Textfarben.
    """
    style = style or ExportStyle()
    if not paragraphs:
        return _RTF_EMPTY
    m, t = style.main, style.translation
    fam = lambda ts: FONTS.get(ts.font, FONTS["Helvetica"])[1]
    colors = [m.color] + ([t.color] if t.color != m.color else [])
    cf_main, cf_trans = 1, (1 if t.color == m.color else 2)

    header = (
        "{\\rtf1\\ansi\\ansicpg1252\\cocoartf2907\n"
        "\\cocoatextscaling0\\cocoaplatform0{\\fonttbl"
        f"\\f0\\f{fam(m)}\\fcharset0 {m.postscript()};"
        "\\f1\\fswiss\\fcharset0 Helvetica;"
        f"\\f2\\f{fam(t)}\\fcharset0 {t.postscript()};\n}}\n"
        "{\\colortbl;" + "".join("\\red%d\\green%d\\blue%d;" % _rgb(c) for c in colors) + "}\n"
        "{\\*\\expandedcolortbl;" + ";" * len(colors) + "}\n"
    )

    def start_reset(ts: TextStyle, font: str, cf: int) -> tuple[str, str]:
        on = font + ("\\b" if ts.bold else "") + ("\\i" if ts.italic else "")
        off = "\\f1" + ("\\b0" if ts.bold else "") + ("\\i0" if ts.italic else "")
        return (f"{on}\\fs{_fs(ts.size)} \\cf{cf} \\expnd0\\expndtw0\\kerning0\n",
                f"{off}\\fs24 \\cf0 \\kerning1\\expnd0\\expndtw0 \\\n")

    styles = {
        "main": start_reset(m, "\\f0", cf_main),
        "trans": start_reset(t, "\\f2", cf_trans),
        "spacer": (f"\\fs{_fs(style.pair_spacing)} \\cf1 \\expnd0\\expndtw0\\kerning0\n",
                   "\\fs24 \\cf0 \\kerning1\\expnd0\\expndtw0 \\\n"),
    }
    para = f"\\pard{_ALIGN[style.align][0]}\\partightenfactor0\n\n"
    parts = [header]
    for i, (kind, text) in enumerate(paragraphs):
        start, reset = styles[kind]
        ts = m if kind == "main" else t
        body = "\\'a0" if kind == "spacer" else rtf_escape(text.upper() if ts.uppercase else text)
        parts.append(para + start + body)
        parts.append("}" if i == len(paragraphs) - 1 else "\n" + reset)
    return "".join(parts)


# ---------------------------------------------------------------------- Folien


def _chunks(items: list, size: int) -> list[list]:
    """Teilt gleichmäßig auf (5 Zeilen bei 4 pro Folie -> 3 + 2 statt 4 + 1)."""
    if not items:
        return [[]]
    n = math.ceil(len(items) / size)
    base, extra = divmod(len(items), n)
    out, pos = [], 0
    for i in range(n):
        k = base + (1 if i < extra else 0)
        out.append(items[pos:pos + k])
        pos += k
    return out


def section_slides(section: Section, lang_count: int, style: ExportStyle) -> list[list[tuple[str, str]]]:
    lines = [ln for ln in section.lines if ln.strip()]
    if lang_count >= 2:
        pairs = [lines[i:i + lang_count] for i in range(0, len(lines), lang_count)]
        slides = []
        for chunk in _chunks(pairs, style.pairs_per_slide):
            paras: list[tuple[str, str]] = []
            for j, pair in enumerate(chunk):
                if j and style.pair_spacing > 0:
                    paras.append(("spacer", ""))
                paras.append(("main", pair[0]))
                paras.extend(("trans", t) for t in pair[1:2])
            slides.append(paras)
        return slides
    return [[("main", ln) for ln in chunk] for chunk in _chunks(lines, style.lines_per_slide)]


def section_type(label: str) -> str:
    word = re.sub(r"\s*\d+[a-z]?$", "", label).strip()
    return _LABEL_ALIASES.get(word, word)


def group_names(labels: list[str]) -> dict[str, str]:
    """„Chorus 1“ -> „Chorus“, „Bridge 2“ bleibt (wie im Beispiel); Strophen behalten die Nummer."""
    names = {}
    for lbl in labels:
        t = section_type(lbl)
        num = re.search(r"(\d+[a-z]?)$", lbl)
        if t != "Verse" and (not num or num.group(1) == "1"):
            names[lbl] = t  # wie im Beispiel: „Bridge“, „Bridge 2“ – nur Strophen tragen immer eine Nummer
        else:
            names[lbl] = f"{t} {num.group(1) if num else 1}"
    return names


def arrangement_notes(order: list[str]) -> str:
    """Kurzform der Reihenfolge wie im Beispiel: „V1 - V2 - CHO - V3 - CHO 2x“."""
    parts: list[list] = []
    for lbl in order:
        if lbl == BLANK_LABEL:
            continue
        t = section_type(lbl)
        num = re.search(r"(\d+)$", lbl)
        abbr = NOTE_ABBR.get(t, t.upper())
        if num and (t == "Verse" or num.group(1) != "1"):
            abbr += num.group(1)
        if parts and parts[-1][0] == abbr:
            parts[-1][1] += 1
        else:
            parts.append([abbr, 1])
    return " - ".join(a + (f" {n}x" if n > 1 else "") for a, n in parts)


@dataclass
class Group:
    label: str                         # Abschnitt in der .sng
    name: str                          # Gruppenname in ProPresenter
    color: tuple[float, float, float]
    slides: list[list[tuple[str, str]]]


def song_groups(song: Song, style: ExportStyle) -> tuple[list[Group], list[str]]:
    """Gruppen (in Reihenfolge des ersten Auftretens) + Arrangement-Reihenfolge (Labels).

    Gemeinsame Grundlage für Export und Vorschau.
    """
    labels = [s.label for s in song.sections]
    order = [l for l in song.order if l in labels] or labels
    names = group_names(labels)
    seen: list[str] = []
    for lbl in order + labels:  # nicht verwendete Abschnitte hinten an
        if lbl not in seen:
            seen.append(lbl)
    by_label = {s.label: s for s in song.sections}
    groups = [
        Group(lbl, names[lbl], GROUP_COLORS.get(section_type(lbl), (0.5, 0.5, 0.5)),
              section_slides(by_label[lbl], song.lang_count, style))
        for lbl in seen
    ]
    if style.blank_first:
        groups.insert(0, Group(BLANK_LABEL, BLANK_NAME, BLANK_COLOR, [[]]))
        order = [BLANK_LABEL] + order
    return groups, order


# ------------------------------------------------------------------- Vorlage


class Template:
    """Vorlage aus der Beispieldatei: Präsentationsrahmen + eine Folie (Cue)."""

    def __init__(self, path: Path = TEMPLATE_PATH):
        pres = parse_presentation(path.read_text(encoding="utf-8"))
        cue = next(
            c for c in pres.cues
            if c.actions and c.actions[0].slide.presentation.base_slide.elements
        )
        self.cue = copy.deepcopy(cue)
        self.cue.hot_key.Clear()
        self.cue.name = ""
        self.group = copy.deepcopy(pres.cue_groups[0])
        self.group.group.hotKey.Clear()
        del self.group.cue_identifiers[:]

        base = copy.deepcopy(pres)
        for name in ("cues", "cue_groups", "arrangements"):
            del getattr(base, name)[:]
        for name in ("uuid", "selected_arrangement", "ccli"):
            base.ClearField(name)
        base.name = base.notes = ""
        self.base = base

    def styled_cue(self, style: ExportStyle):
        """Folienvorlage mit den Formatierungen aus style (Text kommt später ins RTF)."""
        cue = copy.deepcopy(self.cue)
        slide = cue.actions[0].slide.presentation.base_slide
        r, g, b = _rgb(style.background)
        bg = slide.background_color
        bg.red, bg.green, bg.blue, bg.alpha = r / 255, g / 255, b / 255, 1
        for el in slide.elements:
            e = el.element
            e.bounds.origin.x = style.margin_left
            e.bounds.origin.y = style.margin_top
            e.bounds.size.width = SLIDE_W - style.margin_left - style.margin_right
            e.bounds.size.height = SLIDE_H - style.margin_top - style.margin_bottom
            e.fill.enable = style.box_fill
            r, g, b = _rgb(style.box_color)
            c = e.fill.color
            c.red, c.green, c.blue, c.alpha = r / 255, g / 255, b / 255, style.box_opacity
            text = e.text
            text.vertical_alignment = graphicsData_pb2.Graphics.Text.VerticalAlignment.Value(_VALIGN[style.valign])
            text.attributes.paragraph_style.alignment = (
                graphicsData_pb2.Graphics.Text.Attributes.Paragraph.Alignment.Value(_ALIGN[style.align][1]))
            text.shadow.enable = style.shadow
            text.scale_behavior = graphicsData_pb2.Graphics.Text.ScaleBehavior.Value(
                "SCALE_BEHAVIOR_SCALE_FONT_DOWN" if style.shrink_to_fit else "SCALE_BEHAVIOR_NONE")
        return cue


_template: Template | None = None


def get_template() -> Template:
    global _template
    if _template is None:
        _template = Template()
    return _template


# ------------------------------------------------------------- Song -> Presentation


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "\x1f".join(parts))).upper()


def song_to_presentation(song: Song, style: ExportStyle | None = None, key: str = "", name: str = ""):
    """key: eindeutige Kennung (z. B. Dateiname) für stabile UUIDs; name: Präsentationsname (Standard: Titel)."""
    style = style or ExportStyle()
    tpl = get_template()
    cue_tpl = tpl.styled_cue(style)
    pres = copy.deepcopy(tpl.base)
    key = key or song.title or "Lied"
    pres.uuid.string = _uid(key)
    pres.name = name or song.title

    groups, order = song_groups(song, style)
    pres.notes = arrangement_notes(order)

    group_ids = {}
    for grp in groups:
        gid = _uid(key, "group", grp.label)
        group_ids[grp.label] = gid
        cg = pres.cue_groups.add()
        cg.CopyFrom(tpl.group)
        cg.group.uuid.string = gid
        cg.group.name = grp.name
        c = cg.group.color
        c.red, c.green, c.blue, c.alpha = (*grp.color, 1)

        for n, paras in enumerate(grp.slides):
            cue = pres.cues.add()
            cue.CopyFrom(cue_tpl)
            hk = hotkey_for(grp.name, style) if n == 0 else ""
            if hk:  # wie im Beispiel: nur die erste Folie einer Gruppe
                cue.hot_key.code = hotKey_pb2.HotKey.KeyCode.Value(f"KEY_CODE_ANSI_{hk}")
            cid = _uid(key, "cue", grp.label, str(n))
            cue.uuid.string = cid
            action = cue.actions[0]
            action.uuid.string = _uid(key, "action", grp.label, str(n))
            slide = action.slide.presentation.base_slide
            slide.uuid.string = _uid(key, "slide", grp.label, str(n))
            for k, el in enumerate(slide.elements):
                el.element.uuid.string = _uid(key, "element", grp.label, str(n), str(k))
                el.element.text.rtf_data = build_rtf(paras if k == 0 else [], style).encode("latin-1")
            cg.cue_identifiers.add().string = cid

    arr = pres.arrangements.add()
    arr.uuid.string = _uid(key, "arrangement")
    arr.name = "Standard"
    for lbl in order:
        arr.group_identifiers.add().string = group_ids[lbl]
    pres.selected_arrangement.string = arr.uuid.string

    ccli = pres.ccli
    ccli.song_title = song.title
    ccli.author = song.author
    m = re.match(r"\s*(\d{4})\b[\s,.-]*(.*)", song.copyright)
    if m:
        ccli.copyright_year = int(m.group(1))
        ccli.publisher = m.group(2).strip()
    elif song.copyright:
        ccli.publisher = song.copyright.strip()
    if song.ccli.strip().isdigit():
        ccli.song_number = int(song.ccli.strip())
    return pres


def song_to_json(song: Song, style: ExportStyle | None = None, key: str = "", name: str = "") -> str:
    """Song -> Zwischenformat (.json, Protobuf-Textformat wie im Beispiel)."""
    return presentation_to_json(song_to_presentation(song, style, key, name))


def song_to_pro(song: Song, style: ExportStyle | None = None, key: str = "", name: str = "") -> tuple[str, bytes]:
    """Song -> (Zwischenformat, .pro-Bytes). Der zweite Schritt läuft über den propresenterFormatter."""
    text = song_to_json(song, style, key, name)
    return text, formatter.json_to_pro(text)


def preview(song: Song, style: ExportStyle) -> dict:
    """Folien eines Liedes in Arrangement-Reihenfolge, für die Vorschau in der Weboberfläche."""
    groups, order = song_groups(song, style)
    by_label = {g.label: g for g in groups}
    return {
        "title": song.title,
        "notes": arrangement_notes(order),
        "groups": [
            {"name": g.name, "hotkey": hotkey_for(g.name, style),
             "color": "#%02x%02x%02x" % tuple(int(v * 255) for v in g.color),
             "slides": [[{"kind": k, "text": t} for k, t in s] for s in g.slides]}
            for g in (by_label[lbl] for lbl in order)
        ],
    }


# ------------------------------------------------------------------- Dateinamen

_STANDARD_SUFFIX_RE = re.compile(r" - Standard(?:-Arrangement)?(?: \(\d+\))?$", re.IGNORECASE)


def export_name(path: Path, songs_dir: Path, song: Song, index: dict | None = None) -> str:
    """Name des Liedes wie in ChurchTools hinterlegt (ohne „ - Standard-Arrangement“).

    Quelle: churchtools_index.json (beim Import geschrieben). Weitere Arrangements eines Liedes
    behalten ihren Zusatz („Ruft zu dem Herrn - In G“), damit nichts überschrieben wird.
    Ohne Index (ältere Importe): Zusatz „ - Standard-Arrangement“ wird vom Dateinamen entfernt.
    """
    entry = (index or {}).get(path.relative_to(songs_dir).as_posix())
    if entry and entry.get("name"):
        name = entry["name"]
        if entry.get("multiple") and not entry.get("default") and entry.get("arrangement"):
            name += f" - {entry['arrangement']}"
        return name
    if song.manual and song.title:
        return song.title
    return _STANDARD_SUFFIX_RE.sub("", path.stem)


def file_stem(name: str) -> str:
    """Name -> zulässiger Dateiname (wie beim Download bereinigt)."""
    from download_songs import safe_filename
    return safe_filename(name)


def unique_stem(stem: str, taken: set[str]) -> str:
    candidate, n = stem, 2
    while candidate.casefold() in taken:
        candidate = f"{stem} ({n})"
        n += 1
    taken.add(candidate.casefold())
    return candidate


def font_choices() -> list[dict]:
    return [{"name": name, "css": css} for name, (_, _, css) in FONTS.items()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Lieder (.sng) als ProPresenter-Dateien (.pro) exportieren")
    parser.add_argument("--songs", type=Path, default=BASE_DIR / "songs")
    parser.add_argument("--out", type=Path, default=BASE_DIR / "export")
    parser.add_argument("--json", action="store_true", help="Zwischendateien (.json) mit ablegen")
    parser.add_argument("--lines", type=int, help="Zeilen pro Folie (einsprachig), überschreibt export_settings.json")
    args = parser.parse_args()

    from download_songs import load_index

    style = load_style()
    if args.lines:
        style.lines_per_slide = args.lines
    args.out.mkdir(parents=True, exist_ok=True)
    index = load_index(args.songs)
    taken: set[str] = set()
    n = 0
    for path in sorted(args.songs.rglob("*.sng")):
        song = read_sng(path)
        name = export_name(path, args.songs, song, index)
        stem = unique_stem(file_stem(name), taken)
        key = path.relative_to(args.songs).as_posix()  # stabile UUIDs je Datei
        text, data = song_to_pro(song, style, key=key, name=name)
        (args.out / f"{stem}.pro").write_bytes(data)
        if args.json:
            (args.out / f"{stem}.json").write_text(text, encoding="utf-8")
        n += 1
    print(f"{n} Lieder nach {args.out} exportiert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
