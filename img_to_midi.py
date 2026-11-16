r"""Recognize printed sheet music and save MIDI (Python 3.10+).

Setup:
    python -m pip install Pillow music21
    Install Audiveris: https://github.com/Audiveris/audiveris/releases

From main.py in this folder:
    from sheet_music_to_midi import image_to_midi
    midi_filename = image_to_midi(["img_1.jpeg", "img_2.jpeg"], "file.midi")

If Audiveris is not detected, pass audiveris_path=r"C:\...\Audiveris.exe"
or set the AUDIVERIS_PATH environment variable. A runnable .jar is also
supported when its required Java version is installed.

Recognition accuracy depends on scan quality. Printed staff notation works
best; handwriting and unusual notation may need correction in Audiveris.
Importing this module performs no conversions, installs, or downloads.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
import copy
from fractions import Fraction
import io
import os
from pathlib import Path
import shutil
import re
import subprocess
import tempfile
import sys
import time
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

__all__ = ["image_to_midi", "SheetMusicConversionError"]


class SheetMusicConversionError(RuntimeError):
    """Recognition or MIDI generation failed; no successful output is returned."""


class _MilestoneProgress:
    """One dash per completed milestone; percentage counts steps, not time."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.completed = 0
        self.output = sys.stdout
        if enabled:
            try:
                from colorama import just_fix_windows_console
                just_fix_windows_console()
                self.output = sys.stdout
            except ImportError:
                # Modern Windows Terminal and ANSI terminals already support color.
                pass

    def update(self, completed: int, label: str):
        self.completed = completed
        if self.enabled:
            color = "\033[32m" if completed == 10 else "\033[33m"
            bar = "-" * completed
            print(f"\r{color}[{bar:<10}] {completed * 10:3d}% {label:<40}\033[0m",
                  end="", flush=True, file=self.output)

    def stop(self):
        self.update(self.completed, "Stopped")
        self.newline()

    def newline(self):
        if self.enabled:
            print(file=self.output, flush=True)


def _audiveris_command(location: str | os.PathLike | None) -> list[str]:
    explicit = location or os.environ.get("AUDIVERIS_PATH")
    if explicit:
        executable = Path(explicit).expanduser()
        if not executable.is_file():
            found = shutil.which(str(explicit))
            if not found:
                raise FileNotFoundError(f"Audiveris not found: {explicit}")
            executable = Path(found)
    else:
        found = shutil.which("Audiveris.exe") or shutil.which("audiveris")
        candidates = []
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if root:
                candidates.extend([
                    Path(root) / "Audiveris" / "Audiveris.exe",
                    Path(root) / "Audiveris" / "bin" / "Audiveris.exe",
                    Path(root) / "Programs" / "Audiveris" / "Audiveris.exe",
                ])
        executable = Path(found) if found else next(
            (candidate for candidate in candidates if candidate.is_file()), None
        )
        if executable is None:
            raise SheetMusicConversionError(
                "Install Audiveris from https://github.com/Audiveris/audiveris/releases "
                "and pass audiveris_path= to image_to_midi(), or set AUDIVERIS_PATH."
            )
    if executable.suffix.lower() == ".jar":
        java = shutil.which("java")
        if not java:
            raise SheetMusicConversionError("Java is required to run an Audiveris JAR.")
        return [java, "-jar", str(executable.resolve())]
    if executable.suffix.lower() in {".bat", ".cmd"}:
        raise SheetMusicConversionError(
            "Pass the installed Audiveris.exe or a runnable Audiveris .jar, "
            "rather than a Windows batch launcher."
        )
    return [str(executable.resolve())]


def _normalize_image(source: Path, folder: Path) -> Path:
    """Normalize a single image, preserving all of its frames."""
    return _normalize_images([source], folder)


def _has_staff_linework(page) -> bool:
    """Keep even a single plausible staff line, including percussion staves."""
    import numpy as np
    from PIL import Image

    preview = page.convert("L")
    preview.thumbnail((1400, 1900), Image.Resampling.LANCZOS)
    width = preview.width
    strip = max(24, width // 12)
    if width < strip:
        return True
    for angle in (0, -2, 2, -4, 4):
        view = preview if not angle else preview.rotate(
            angle, resample=Image.Resampling.BILINEAR, fillcolor=255
        )
        ink = np.asarray(view) < 195
        if not ink.any():
            return False
        for x in range(0, width - strip + 1, strip // 2):
            rows = np.count_nonzero(ink[:, x:x + strip], axis=1)
            if np.any(rows >= strip * 0.76):
                return True
    return False


def _normalize_images(sources: list[Path], folder: Path, *, skip_nonmusic=False) -> Path:
    """Combine images and their frames in input order into one score document."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    pages = []
    try:
        for source in sources:
            try:
                with Image.open(source) as image:
                    for index in range(getattr(image, "n_frames", 1)):
                        image.seek(index)
                        oriented = ImageOps.exif_transpose(image)
                        if oriented.mode == "RGB" and "transparency" not in oriented.info:
                            page = oriented
                        else:
                            rgba = oriented.convert("RGBA")
                            background = Image.new("RGBA", rgba.size, "white")
                            background.alpha_composite(rgba)
                            page = background.convert("RGB")
                        if not skip_nonmusic or _has_staff_linework(page):
                            pages.append(page)
                        else:
                            page.close()
            except (UnidentifiedImageError, OSError, EOFError, ValueError) as exc:
                raise SheetMusicConversionError(
                    f"Cannot decode image {source.name!r}. Any image format supported by "
                    "Pillow is accepted, regardless of its filename extension."
                ) from exc
        if not pages:
            raise SheetMusicConversionError("No staff linework was found in the supplied pages.")
        destination = folder / ("score.tiff" if len(pages) > 1 else "score.png")
        if len(pages) > 1:
            pages[0].save(destination, save_all=True, append_images=pages[1:])
        else:
            pages[0].save(destination)
        return destination
    finally:
        for page in pages:
            page.close()


def _audiveris_parallel_args(image_path: Path) -> list[str]:
    """Use Audiveris's own page scheduling for bounded Windows workloads."""
    if sys.platform != "win32" or (os.cpu_count() or 1) < 8 or image_path.suffix != ".tiff":
        return []

    import ctypes
    from ctypes import wintypes
    from PIL import Image

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("load", wintypes.DWORD),
            ("total_physical", ctypes.c_uint64),
            ("available_physical", ctypes.c_uint64),
            ("total_page_file", ctypes.c_uint64),
            ("available_page_file", ctypes.c_uint64),
            ("total_virtual", ctypes.c_uint64),
            ("available_virtual", ctypes.c_uint64),
            ("available_extended_virtual", ctypes.c_uint64),
        ]

    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
        return []
    if memory.available_physical < 4 * 1024 ** 3:
        return []

    with Image.open(image_path) as pages:
        if not 2 <= pages.n_frames <= 4:
            return []
        total_pixels = 0
        for index in range(pages.n_frames):
            pages.seek(index)
            total_pixels += pages.width * pages.height
        if total_pixels > 36_000_000:
            return []
    return ["-constant", "org.audiveris.omr.sheet.Book.processAllStubsInParallel=true"]


def _export_order(path: Path) -> list:
    """Keep Audiveris movement 2 before movement 10."""
    return [int(piece) if piece.isdigit() else piece.lower()
            for piece in re.split(r"(\d+)", str(path))]


def _exported_sheets(log: str) -> dict[str, list[int]]:
    """Map an Audiveris MusicXML filename to the source pages it contains."""
    result = {}
    pages = []
    for line in log.splitlines():
        if "Exporting sheet(s):" in line:
            pages = [int(n) for n in re.findall(r"#(\d+)", line.split("Exporting sheet(s):", 1)[1])]
        elif " exported to " in line and pages:
            result[Path(line.rsplit(" exported to ", 1)[1].strip()).name] = pages
            pages = []
    return result


def _pitch_key(note):
    return (
        note.findtext("pitch/step"),
        int(note.findtext("pitch/octave")),
        float(note.findtext("pitch/alter", "0")),
    )


def _pitch_position(pitch):
    step, octave, _ = pitch
    return 7 * octave + "CDEFGAB".index(step)


def _beamed_chords(measure):
    """Collect only ordinary filled-head chords on the same staff and voice."""
    groups = []
    group = []
    for note in measure.findall("note"):
        if note.find("chord") is None:
            group = []
            groups.append(group)
        group.append(note)
    for group in groups:
        if len(group) < 2:
            continue
        first = group[0]
        kind = (first.findtext("staff", "1"), first.findtext("voice", "1"),
                first.findtext("type"), first.findtext("stem"))
        if kind[2] not in {"eighth", "16th", "32nd", "64th", "128th", "256th"}:
            continue
        if kind[3] not in {"up", "down"} or first.find("beam") is None:
            continue
        if any(
            note.find("pitch") is None or note.find("grace") is not None
            or note.find("cue") is not None or note.find("notehead") is not None
            or note.get("default-x") is None
            or (note.findtext("staff", "1"), note.findtext("voice", "1"),
                note.findtext("type"), note.findtext("stem")) != kind
            for note in group
        ):
            continue
        pitches = {_pitch_key(note): note for note in group}
        if len(pitches) == len(group):
            yield kind, pitches, min(float(note.get("default-x")) for note in group)


def _staff_patch(ink, staff, clef, left, pitch, interline):
    """Sample ink at a staff-relative pitch using Audiveris's line geometry."""
    import numpy as np

    lines = staff.findall("./lines/line")
    sign, line, octave_change = clef
    anchors = {"G": 32, "F": 24, "C": 28}  # G4, F3, C4 in diatonic units
    if len(lines) != 5 or sign not in anchors or not 1 <= line <= 5:
        return None
    x = left + interline * 0.63
    ys = []
    for staff_line in (lines[0], lines[-1]):
        points = [(float(pt.get("x")), float(pt.get("y"))) for pt in staff_line.findall("point")]
        if len(points) < 2 or not points[0][0] <= x <= points[-1][0]:
            return None
        xs, values = zip(*points)
        ys.append(float(np.interp(x, xs, values)))
    spacing = (ys[1] - ys[0]) / 4
    if not interline * 0.7 < spacing < interline * 1.25:
        return None
    top_pitch = anchors[sign] + 7 * octave_change + 2 * (5 - line)
    y = ys[0] + (top_pitch - _pitch_position(pitch)) * spacing / 2
    # Sample the center/right of the head. This excludes the common stem on
    # the left for down-stems and on the far right for up-stems.
    x0 = round(x - interline * 0.45)
    y0 = round(y - interline * 0.8)
    width = max(5, round(interline * 0.9))
    height = max(7, round(interline * 1.45))
    margin = max(1, round(interline * 0.08))
    if x0 - margin < 0 or y0 - margin < 0:
        return None
    patch = ink[y0 - margin:y0 + height + margin, x0 - margin:x0 + width + margin]
    if patch.shape != (height + 2 * margin, width + 2 * margin):
        return None
    return patch, margin, height, width


def _patch_similarity(template, target):
    """Compare two filled notehead patches allowing a small registration error."""
    import numpy as np

    if template is None or target is None:
        return 0.0
    reference, margin, height, width = template
    candidate, _, _, _ = target
    reference = reference[margin:margin + height, margin:margin + width]
    count = np.count_nonzero(reference)
    if count < height * width * 0.36:
        return 0.0
    best = 0.0
    for dy in range(2 * margin + 1):
        for dx in range(2 * margin + 1):
            view = candidate[dy:dy + height, dx:dx + width]
            total = count + np.count_nonzero(view)
            best = max(best, 2 * np.count_nonzero(reference & view) / total)
    return best


def _repair_measure_pitches(measure, part, stack, clefs, ink, interline):
    """Fix a one-step pitch outlier only when two repeated chords and ink agree."""
    groups = list(_beamed_chords(measure))
    changes = []
    if len(groups) < 3:
        return changes
    left = float(stack.get("left"))
    width = float(stack.get("right")) - left
    if abs(float(measure.get("width", "0")) * interline / 10 - width) > interline * 0.6:
        return changes
    accidentals = {(n.findtext("pitch/step"), n.findtext("pitch/octave"))
                   for n in measure.findall("note") if n.find("accidental") is not None}
    staves = part.findall("staff")
    for index, (kind, notes, x) in enumerate(groups):
        staff_number = int(kind[0])
        clef = clefs.get(kind[0])
        if not clef or not 1 <= staff_number <= len(staves):
            continue
        staff = staves[staff_number - 1]
        if staff.find("lines") is None:
            continue
        before = {}
        after = {}
        for peer_index in range(max(0, index - 5), min(len(groups), index + 6)):
            if peer_index == index:
                continue
            peer_kind, peer, peer_x = groups[peer_index]
            if peer_kind != kind or len(peer) != len(notes) or abs(peer_x - x) > 140:
                continue
            old, new = notes.keys() - peer.keys(), peer.keys() - notes.keys()
            if len(old) != 1 or len(new) != 1:
                continue
            old_pitch, new_pitch = next(iter(old)), next(iter(new))
            if abs(_pitch_position(old_pitch) - _pitch_position(new_pitch)) != 1:
                continue
            target = notes[old_pitch]
            if (target.find("accidental") is not None or target.find("tie") is not None
                    or (new_pitch[0], str(new_pitch[1])) in accidentals):
                continue
            if peer_x < x:
                before[(old_pitch, new_pitch)] = peer[new_pitch]
            elif peer_x > x:
                after.setdefault((old_pitch, new_pitch), peer[new_pitch])
        agreed = before.keys() & after.keys()
        if len(agreed) != 1:
            continue
        old_pitch, new_pitch = next(iter(agreed))
        target = notes[old_pitch]
        target_left = left + float(target.get("default-x")) * interline / 10
        corrected = _staff_patch(ink, staff, clef, target_left, new_pitch, interline)
        current = _staff_patch(ink, staff, clef, target_left, old_pitch, interline)
        donors = (before[(old_pitch, new_pitch)], after[(old_pitch, new_pitch)])
        templates = [_staff_patch(ink, staff, clef,
                                  left + float(donor.get("default-x")) * interline / 10,
                                  new_pitch, interline) for donor in donors]
        visual_matches = [(_patch_similarity(template, corrected),
                           _patch_similarity(template, current)) for template in templates]
        if all(
            (proposed >= 0.87 and proposed - original >= 0.16)
            or (proposed >= 0.92 and proposed - original >= 0.10)
            for proposed, original in visual_matches
        ):
            changes.append((target, donors[0].find("pitch")))
    return changes


def _read_musicxml(path: Path):
    if path.suffix.lower() != ".mxl":
        return ET.parse(path).getroot()
    with ZipFile(path) as export:
        manifest = ET.fromstring(export.read("META-INF/container.xml"))
        rootfile = next(node for node in manifest.iter() if node.tag.split("}")[-1] == "rootfile")
        return ET.fromstring(export.read(rootfile.attrib["full-path"]))


def _correct_export_pitches(score, book: Path, page_numbers: list[int]) -> bool:
    """Correct pitches in place when the scan supports a change."""
    import numpy as np
    from PIL import Image

    if not page_numbers or not book.is_file():
        return False
    try:
        changes = []
        with ZipFile(book) as archive:
            for part in score.findall("part"):
                clefs = {}
                active_octave_shifts = set()
                page_index, system_index, stack_index = 0, 0, 0
                loaded_page = None
                for measure in part.findall("measure"):
                    printing = measure.find("print")
                    if printing is not None and printing.get("new-page") == "yes":
                        page_index += 1
                        system_index, stack_index = 0, 0
                    elif printing is not None and printing.get("new-system") == "yes":
                        system_index += 1
                        stack_index = 0
                    ambiguous_layout = bool(active_octave_shifts)
                    seen_note = False
                    for element in measure:
                        if element.tag == "note":
                            seen_note = True
                        elif element.tag == "attributes":
                            for clef in element.findall("clef"):
                                ambiguous_layout |= seen_note
                                clefs[clef.get("number", "1")] = (
                                    clef.findtext("sign"), int(clef.findtext("line", "0")),
                                    int(clef.findtext("clef-octave-change", "0")),
                                )
                        elif element.tag == "direction":
                            for shift in element.findall("direction-type/octave-shift"):
                                ambiguous_layout = True
                                shift_id = (element.findtext("staff", "1"), shift.get("number", "1"))
                                if shift.get("type") == "stop":
                                    active_octave_shifts.discard(shift_id)
                                else:
                                    active_octave_shifts.add(shift_id)
                    if not ambiguous_layout and page_index < len(page_numbers) and len(list(_beamed_chords(measure))) >= 3:
                        if loaded_page != page_index:
                            page = page_numbers[page_index]
                            sheet_dir = f"sheet#{page}"
                            sheet = ET.fromstring(archive.read(f"{sheet_dir}/{sheet_dir}.xml"))
                            interline = float(sheet.find("./scale/interline").get("main"))
                            systems = sheet.findall(".//system")
                            with Image.open(io.BytesIO(archive.read(f"{sheet_dir}/BINARY.png"))) as raster:
                                ink = np.asarray(raster.convert("L")) < 128
                            loaded_page = page_index
                        if system_index < len(systems):
                            system = systems[system_index]
                            stacks = [s for s in system.findall("stack") if s.get("special") != "CAUTIONARY"]
                            part_id = part.get("id", "").removeprefix("P")
                            staff_part = next((p for p in system.findall("part") if p.get("id") == part_id), None)
                            if staff_part is not None and stack_index < len(stacks):
                                changes.extend(_repair_measure_pitches(
                                    measure, staff_part, stacks[stack_index], clefs, ink, interline
                                ))
                    stack_index += 1
        for note, pitch in changes:
            original = note.find("pitch")
            note[list(note).index(original)] = copy.deepcopy(pitch)
        return bool(changes)
    except (OSError, BadZipFile, ET.ParseError, KeyError, StopIteration, ValueError, TypeError, AttributeError):
        # Geometry varies across Audiveris releases. Keep the original export
        # whenever the optional verification data is missing or incompatible.
        return False


_SUSTAINED_DYNAMICS = {"pppppp", "ppppp", "pppp", "ppp", "pp", "p", "mp", "mf",
                       "f", "ff", "fff", "ffff", "fffff", "ffffff"}


def _share_keyboard_dynamics(score) -> bool:
    """Apply marks printed between a keyboard's two staves to both hands."""
    keyboard_parts = set()
    for definition in score.findall("part-list/score-part"):
        names = [definition.findtext("part-name", "")]
        names.extend(node.text or "" for node in definition.findall("score-instrument/instrument-name"))
        programs = {node.text for node in definition.findall("midi-instrument/midi-program")}
        keyboard_name = any(re.search(r"\b(piano|pianoforte|harpsichord|clavichord|organ|keyboard|celesta)\b",
                                      name, re.IGNORECASE) for name in names)
        if keyboard_name or programs & {str(number) for number in (*range(1, 9), *range(17, 25))}:
            keyboard_parts.add(definition.get("id"))

    default_distance = score.findtext("defaults/staff-layout[@number='2']/staff-distance")
    changed = False
    for part in score.findall("part"):
        if part.get("id") not in keyboard_parts:
            continue
        divisions = Fraction(1)
        staff_count = "1"
        distance = default_distance
        for measure in part.findall("measure"):
            printing = measure.find("print")
            if printing is not None:
                if printing.get("new-system") == "yes" or printing.get("new-page") == "yes":
                    distance = default_distance
                distance = printing.findtext("staff-layout[@number='2']/staff-distance", distance)
            position = Fraction(0)
            markings = {}
            for index, element in enumerate(measure):
                if element.tag == "attributes":
                    staff_count = element.findtext("staves", staff_count)
                    new_divisions = element.findtext("divisions")
                    if new_divisions is not None:
                        divisions = Fraction(new_divisions)
                elif element.tag in {"note", "backup", "forward"}:
                    if element.tag == "note" and (element.find("chord") is not None or
                                                  element.find("grace") is not None):
                        continue
                    duration = Fraction(element.findtext("duration", "0")) / divisions
                    position += -duration if element.tag == "backup" else duration
                elif element.tag == "direction" and staff_count == "2":
                    dynamics = element.findall("direction-type/dynamics")
                    if dynamics:
                        offset = Fraction(element.findtext("offset", "0")) / divisions
                        staff = element.findtext("staff", "1")
                        markings.setdefault(position + offset, []).append(
                            (index, element, staff, dynamics)
                        )
            if distance is None:
                continue
            gap = float(distance)
            if not math.isfinite(gap) or gap <= 0:
                continue
            additions = []
            for events in markings.values():
                # Separately marked staves or voices already have an explicit balance.
                if len(events) != 1:
                    continue
                index, direction, staff, dynamics = events[0]
                if staff not in {"1", "2"} or direction.find("voice") is not None:
                    continue
                if len(dynamics) != 1 or len(dynamics[0]) != 1:
                    continue
                dynamic = dynamics[0]
                if dynamic[0].tag not in _SUSTAINED_DYNAMICS or dynamic.get("default-y") is None:
                    continue
                separation = 40 + gap  # A five-line staff is 40 MusicXML tenths tall.
                y = float(dynamic.get("default-y")) + float(dynamic.get("relative-y", "0"))
                if not (-separation < y < -40 if staff == "1" else 0 < y < gap):
                    continue

                other_staff = "2" if staff == "1" else "1"
                placement = "above" if other_staff == "2" else "below"
                shared = ET.Element("direction", {"placement": placement})
                duplicate = copy.deepcopy(dynamic)
                duplicate.set("default-y", str(y + separation if staff == "1" else y - separation))
                duplicate.attrib.pop("relative-y", None)
                ET.SubElement(shared, "direction-type").append(duplicate)
                offset = direction.find("offset")
                if offset is not None:
                    shared.append(copy.deepcopy(offset))
                ET.SubElement(shared, "staff").text = other_staff
                sound = direction.find("sound")
                if sound is not None and sound.get("dynamics") is not None:
                    ET.SubElement(shared, "sound", {"dynamics": sound.get("dynamics")})
                additions.append((index, shared))
            for index, shared in sorted(additions, key=lambda item: item[0], reverse=True):
                measure.insert(index + 1, shared)
                changed = True
    return changed


def _prepare_musicxml_export(path: Path, book: Path, page_numbers: list[int]):
    try:
        score = _read_musicxml(path)
    except (OSError, BadZipFile, ET.ParseError, KeyError, StopIteration):
        return None
    corrected = _correct_export_pitches(score, book, page_numbers)
    shared = _share_keyboard_dynamics(score)
    if corrected or shared:
        return ET.tostring(score, encoding="unicode")
    return None


def _combine_scores(scores: list):
    """Sequence exports, preserving simultaneous parts within each export.

    Parts correspond by staff order. Every segment starts after the longest
    part of the preceding segment, so shorter voices do not overlap pages.
    """
    from music21 import note, stream

    if len(scores) == 1:
        return scores[0]
    combined = stream.Score()
    parts = []
    offset = 0
    for score in scores:
        segment_parts = list(score.parts) or [score]
        duration = score.highestTime
        while len(parts) < len(segment_parts):
            part = stream.Part(id=f"part_{len(parts) + 1}")
            if offset:
                part.insert(0, note.Rest(quarterLength=offset))
            parts.append(part)
            combined.insert(0, part)
        for index, target in enumerate(parts):
            end = 0
            if index < len(segment_parts):
                original = segment_parts[index]
                start = original.getOffsetInHierarchy(score) if original is not score else 0
                if start:
                    target.insert(offset, note.Rest(quarterLength=start))
                for element in original.flatten():
                    target.insert(offset + start + element.offset, copy.deepcopy(element))
                end = start + original.highestTime
            if end < duration:
                target.insert(offset + end, note.Rest(quarterLength=duration - end))
        # Preserve score-level tempo/direction events as well as part contents.
        for element in score:
            if not isinstance(element, stream.Stream) and element.classes[0] != "Metadata":
                combined.insert(offset + element.offset, copy.deepcopy(element))
        offset += duration
    return combined


def image_to_midi(
    image_path: str | os.PathLike | Sequence[str | os.PathLike],
    output_path: str | os.PathLike | None = None,
    *,
    audiveris_path: str | os.PathLike | None = None,
    timeout: float = 600,
    overwrite: bool = False,
    show_progress: bool = True,
) -> str:
    """Read one image or an ordered list of pages and return one MIDI filename.

    Relative paths are relative to the caller's current working directory.
    A list/tuple is processed in the exact order given, without sorting, as one
    multi-page score. For a list, the default output is file.midi beside the
    first input. A single filename retains its default of input_stem.mid.
    If Audiveris splits the book into movements, exports are joined in numeric
    movement order. Parts correspond by staff order across these exports.
    output_path may specify another .mid/.midi file. Parent folders must exist.
    JPG, JPEG, PNG, GIF, BMP, TIFF, WEBP and other Pillow-supported image
    formats are decoded by content. Multi-frame files become multi-page scores.
    PDF is not an image format; convert PDF pages to images before calling.
    Pages with no detectable staff linework are skipped. When the scan and two
    nearby matching chords agree, visually verified one-step pitch corrections
    may be applied to the recognized score before MIDI export.

    Requires Pillow, music21, and a separate Audiveris installation. Recognition
    runs locally and may take several minutes. timeout limits Audiveris only.
    Small batches may use Audiveris's parallel page recognition on Windows.
    If that run fails, the sequential retry uses the remaining timeout.
    Existing output files are protected unless overwrite=True. Raises
    FileNotFoundError, FileExistsError, ValueError, or SheetMusicConversionError
    on failure; never returns a filename for an unsuccessful conversion.

    By default, a yellow bar adds one dash per completed conversion milestone.
    The percentage counts 10 milestones, not elapsed time or remaining work.
    Recognition is one long milestone, so the bar may pause there. It turns
    green at 100% only after saving successfully. Pass show_progress=False
    to disable console output.
    """
    progress = _MilestoneProgress(show_progress)
    progress.update(0, "Checking input files")
    try:
        result = _convert_images_to_midi(
            image_path, output_path, audiveris_path=audiveris_path,
            timeout=timeout, overwrite=overwrite, _progress=progress.update,
        )
    except BaseException:
        progress.stop()
        raise
    progress.update(10, "Done - MIDI saved")
    progress.newline()
    return result


def _convert_images_to_midi(
    image_path: str | os.PathLike | Sequence[str | os.PathLike],
    output_path: str | os.PathLike | None = None,
    *,
    audiveris_path: str | os.PathLike | None = None,
    timeout: float = 600,
    overwrite: bool = False,
    _progress=None,
) -> str:
    """Perform conversion; image_to_midi owns the activity indicator lifecycle."""
    report = _progress if _progress is not None else lambda completed, label: None
    single_image = isinstance(image_path, (str, os.PathLike))
    if single_image:
        inputs = [image_path]
    elif isinstance(image_path, Sequence):
        inputs = list(image_path)
    else:
        raise TypeError("image_path must be a filename or an ordered list of filenames.")
    if not inputs:
        raise ValueError("Provide at least one image filename.")
    sources = []
    for item in inputs:
        if not isinstance(item, (str, os.PathLike)):
            raise TypeError("Each image filename must be a string or Path object.")
        source = Path(item).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Image does not exist: {source}")
        sources.append(source)
    destination = (
        Path(output_path).expanduser().resolve()
        if output_path is not None else (
            sources[0].with_suffix(".mid") if single_image
            else sources[0].parent / "file.midi"
        )
    )
    if destination in sources:
        raise ValueError("The MIDI output must not replace the source image.")
    if destination.suffix.lower() not in {".mid", ".midi"}:
        raise ValueError("output_path must end in .mid or .midi.")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"Output folder does not exist: {destination.parent}")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {destination}")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number of seconds.")
    report(1, "Checking dependencies")
    try:
        import PIL  # noqa: F401
        from music21 import converter, stream
    except ImportError as exc:
        raise SheetMusicConversionError(
            "Install Python dependencies using: python -m pip install Pillow music21"
        ) from exc

    command = _audiveris_command(audiveris_path)
    report(2, "Preparing image pages")
    with tempfile.TemporaryDirectory(prefix="sheet_music_") as temporary:
        folder = Path(temporary)
        normalized = _normalize_images(sources, folder, skip_nonmusic=True)
        parallel_args = _audiveris_parallel_args(normalized)
        attempts = [parallel_args, []] if parallel_args else [[]]
        report(3, "Recognizing sheet music")
        deadline = time.monotonic() + timeout
        try:
            for index, options in enumerate(attempts):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                exports = folder / ("recognized" if index == 0 else "recognized_sequential")
                exports.mkdir()
                result = subprocess.run(
                    command + ["-batch", "-transcribe", "-export", "-output",
                               str(exports), *options, "--", str(normalized)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=remaining, check=False,
                )
                if result.returncode == 0:
                    break
                if options and time.monotonic() < deadline:
                    report(3, "Retrying recognition sequentially")
        except subprocess.TimeoutExpired as exc:
            raise SheetMusicConversionError(
                f"Audiveris exceeded {timeout} seconds. Try a clearer scan or increase timeout."
            ) from exc
        except OSError as exc:
            raise SheetMusicConversionError(f"Could not launch Audiveris: {exc}") from exc
        log_tail = (result.stdout or "")[-6000:]
        if result.returncode:
            raise SheetMusicConversionError(
                f"Audiveris failed with exit code {result.returncode}.\n{log_tail}"
            )
        report(4, "Locating recognized scores")
        scores = sorted((
            path for path in exports.rglob("*")
            if path.suffix.lower() in {".mxl", ".musicxml", ".xml"}
            and path.is_file()
        ), key=_export_order)
        # Prefer compressed score exports over equivalent plain XML copies.
        compressed = [path for path in scores if path.suffix.lower() == ".mxl"]
        if compressed:
            scores = compressed
        if not scores:
            raise SheetMusicConversionError(
                "Audiveris did not export any recognized scores. "
                "Use consecutive pages of one musical piece and check the scan in Audiveris.\n"
                + log_tail
            )
        report(5, "Reading recognized scores")
        try:
            recognized = []
            sheet_order = _exported_sheets(result.stdout or "")
            book = exports / normalized.with_suffix(".omr").name
            for path in scores:
                revised = _prepare_musicxml_export(path, book, sheet_order.get(path.name, []))
                score = (converter.parseData(revised, format="musicxml") if revised
                         else converter.parse(str(path)))
                if isinstance(score, stream.Opus):
                    raise ValueError(f"Unexpected nested collection of scores in {path.name}.")
                if not list(score.recurse().notes):
                    raise ValueError(f"No playable notes were recognized in {path.name}.")
                recognized.append(score)
            report(6, "Combining scores")
            score = _combine_scores(recognized)
            report(7, "Writing MIDI")
            rendered = folder / "result.mid"
            score.write("midi", fp=str(rendered))
            report(8, "Validating MIDI")
            with rendered.open("rb") as midi:
                if midi.read(4) != b"MThd":
                    raise ValueError("The MIDI exporter did not produce a valid MIDI header.")
        except Exception as exc:
            raise SheetMusicConversionError(f"Could not convert recognized music to MIDI: {exc}") from exc

        # Render first, then publish. Exclusive creation protects existing files
        # even if another caller created one during recognition.
        report(9, "Saving MIDI file")
        if overwrite:
            handle, staged_name = tempfile.mkstemp(suffix=".mid", dir=destination.parent)
            staged = Path(staged_name)
            try:
                with os.fdopen(handle, "wb") as output, rendered.open("rb") as midi:
                    shutil.copyfileobj(midi, output)
                os.replace(staged, destination)
            finally:
                staged.unlink(missing_ok=True)
        else:
            with destination.open("xb") as output:
                try:
                    with rendered.open("rb") as midi:
                        shutil.copyfileobj(midi, output)
                except BaseException:
                    output.close()
                    destination.unlink(missing_ok=True)
                    raise
    return str(destination)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="+", help="Sheet-music images in page order")
    parser.add_argument("-o", "--output", help="Output .mid filename")
    parser.add_argument("--audiveris", help="Audiveris executable or runnable JAR")
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-progress", action="store_true", help="Hide the activity bar")
    args = parser.parse_args()
    try:
        print(image_to_midi(args.image, args.output, audiveris_path=args.audiveris,
                            timeout=args.timeout, overwrite=args.overwrite,
                            show_progress=not args.no_progress))
    except (OSError, ValueError, SheetMusicConversionError) as error:
        parser.exit(1, f"Error: {error}\n")
