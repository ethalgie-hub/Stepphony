"""Windows MIDI piano simulator. Importing this module starts nothing.

    import MusicPlayerAI
    MusicPlayerAI.play("song.midi", mode=1, instrument="piano")

Run directly for interactive prompts, or use --help for command-line options.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
import colorsys
from collections import defaultdict, deque
from dataclasses import dataclass
import importlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

__all__ = ["play", "MusicPlayerError"]
ROOT = Path(__file__).resolve().parent
COOLDOWN = 0.5
LOOKAHEAD = 3.0
SPEED_PRESETS = (0.5, 0.75, 1.0, 1.5, 2.0)
BASE_GAIN = 0.55
GM_NAMES = (
    "Acoustic Grand Piano|Bright Acoustic Piano|Electric Grand Piano|Honky-tonk Piano|"
    "Electric Piano 1|Electric Piano 2|Harpsichord|Clavinet|Celesta|Glockenspiel|"
    "Music Box|Vibraphone|Marimba|Xylophone|Tubular Bells|Dulcimer|Drawbar Organ|"
    "Percussive Organ|Rock Organ|Church Organ|Reed Organ|Accordion|Harmonica|"
    "Tango Accordion|Acoustic Guitar Nylon|Acoustic Guitar Steel|Electric Guitar Jazz|"
    "Electric Guitar Clean|Electric Guitar Muted|Overdriven Guitar|Distortion Guitar|"
    "Guitar Harmonics|Acoustic Bass|Electric Bass Finger|Electric Bass Pick|Fretless Bass|"
    "Slap Bass 1|Slap Bass 2|Synth Bass 1|Synth Bass 2|Violin|Viola|Cello|Contrabass|"
    "Tremolo Strings|Pizzicato Strings|Orchestral Harp|Timpani|String Ensemble 1|"
    "String Ensemble 2|Synth Strings 1|Synth Strings 2|Choir Aahs|Voice Oohs|"
    "Synth Voice|Orchestra Hit|Trumpet|Trombone|Tuba|Muted Trumpet|French Horn|"
    "Brass Section|Synth Brass 1|Synth Brass 2|Soprano Sax|Alto Sax|Tenor Sax|"
    "Baritone Sax|Oboe|English Horn|Bassoon|Clarinet|Piccolo|Flute|Recorder|Pan Flute|"
    "Blown Bottle|Shakuhachi|Whistle|Ocarina|Lead 1 Square|Lead 2 Sawtooth|"
    "Lead 3 Calliope|Lead 4 Chiff|Lead 5 Charang|Lead 6 Voice|Lead 7 Fifths|"
    "Lead 8 Bass and Lead|Pad 1 New Age|Pad 2 Warm|Pad 3 Polysynth|Pad 4 Choir|"
    "Pad 5 Bowed|Pad 6 Metallic|Pad 7 Halo|Pad 8 Sweep|FX 1 Rain|FX 2 Soundtrack|"
    "FX 3 Crystal|FX 4 Atmosphere|FX 5 Brightness|FX 6 Goblins|FX 7 Echoes|"
    "FX 8 Sci-fi|Sitar|Banjo|Shamisen|Koto|Kalimba|Bag Pipe|Fiddle|Shanai|"
    "Tinkle Bell|Agogo|Steel Drums|Woodblock|Taiko Drum|Melodic Tom|Synth Drum|"
    "Reverse Cymbal|Guitar Fret Noise|Breath Noise|Seashore|Bird Tweet|Telephone Ring|"
    "Helicopter|Applause|Gunshot"
).split("|")


class MusicPlayerError(RuntimeError):
    """Invalid input, missing dependencies, or playback initialization failure."""


@dataclass(frozen=True)
class _Note:
    pitch: int
    channel: int
    velocity: int
    start_tick: int
    end_tick: int
    start: float
    end: float


@dataclass(frozen=True)
class _Event:
    tick: int
    seconds: float
    message: object


@dataclass
class _Song:
    events: list[_Event]
    notes: list[_Note]
    duration: float
    ticks_per_beat: int
    tempos: tuple[tuple[float, float], ...] = ((0.0, 120.0),)


def _dependency(name):
    try:
        return importlib.import_module(name)
    except (ImportError, OSError) as exc:
        raise MusicPlayerError(
            f"Cannot load {name}: {exc}. Run setup_music_player.ps1; "
            "see MUSIC_PLAYER_SETUP.md."
        ) from exc


def _instrument(value):
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 127:
        return value
    if isinstance(value, str):
        normalized = " ".join(value.lower().replace("_", " ").replace("-", " ").split())
        if normalized in {"original", "midi", "none"}:
            return None
        aliases = {"piano": 0, "guitar": 24, "bass": 32, "strings": 48,
                   "organ": 19, "sax": 65, "electric piano": 4}
        if normalized in aliases:
            return aliases[normalized]
        for number, name in enumerate(GM_NAMES):
            if normalized == name.lower().replace("-", " "):
                return number
        if normalized.isdecimal() and 0 <= int(normalized) <= 127:
            return int(normalized)
    raise MusicPlayerError("Instrument must be a GM name, a program number 0–127, or None.")


def _read_midi(filename):
    mido = _dependency("mido")
    source = Path(filename).expanduser()
    if not source.is_file():
        raise MusicPlayerError(f"MIDI file not found: {source}")
    try:
        midi = mido.MidiFile(source)
        if midi.type == 2:
            raise MusicPlayerError("MIDI type 2 has independent timelines; export as type 0 or 1.")
        if midi.ticks_per_beat <= 0:
            raise MusicPlayerError("SMPTE MIDI timing is not supported; export using ticks per beat.")
        events, notes = [], []
        tempos = [(0.0, 120.0)]
        pending = defaultdict(deque)
        tick, elapsed, tempo = 0, 0.0, 500000
        for msg in mido.merge_tracks(midi.tracks):
            tick += msg.time
            elapsed += mido.tick2second(msg.time, midi.ticks_per_beat, tempo)
            if msg.type == "set_tempo":
                if msg.tempo <= 0:
                    raise MusicPlayerError("MIDI tempo must be positive.")
                tempo = msg.tempo
                change = (elapsed, 60_000_000 / tempo)
                if elapsed == tempos[-1][0]:
                    tempos[-1] = change
                else:
                    tempos.append(change)
            if msg.is_meta:
                continue
            events.append(_Event(tick, elapsed, msg))
            if msg.type == "note_on" and msg.velocity > 0:
                pending[msg.channel, msg.note].append((tick, elapsed, msg.velocity))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                key = msg.channel, msg.note
                if pending[key]:
                    start_tick, start, velocity = pending[key].popleft()
                    notes.append(_Note(msg.note, msg.channel, velocity,
                                       start_tick, tick, start, elapsed))
            elif msg.type == "control_change" and msg.control in {120, 123}:
                for (channel, pitch), queue in pending.items():
                    if channel == msg.channel:
                        while queue:
                            st, start, velocity = queue.popleft()
                            notes.append(_Note(pitch, channel, velocity, st, tick, start, elapsed))
        # Incomplete note-offs must not leave keys or audio stuck after EOF.
        for (channel, pitch), queue in pending.items():
            for st, start, velocity in queue:
                notes.append(_Note(pitch, channel, velocity, st, tick, start, elapsed))
        notes.sort(key=lambda n: (n.start, n.pitch, n.channel))
        return _Song(events, notes, elapsed, midi.ticks_per_beat, tuple(tempos))
    except MusicPlayerError:
        raise
    except (OSError, ValueError, EOFError, KeyError) as exc:
        raise MusicPlayerError(f"Cannot read MIDI file {source}: {exc}") from exc


_dll_handles = []
_dll_paths = set()


def _fluid_directories():
    locations = []
    if os.environ.get("FLUIDSYNTH_PATH"):
        locations.append(Path(os.environ["FLUIDSYNTH_PATH"]).expanduser())
    for native in (ROOT / "dependencies", ROOT / "vendor" / "fluidsynth"):
        if native.exists():
            locations.extend(sorted({p.parent for p in native.rglob("*fluidsynth*.dll")}))
    directories = []
    for location in locations:
        if location.is_file():
            location = location.parent
        if location.is_dir() and location.resolve() not in directories:
            directories.append(location.resolve())
    return directories


def _load_fluidsynth():
    if sys.platform == "win32":
        for location in reversed(_fluid_directories()):
            path = str(location)
            if path not in _dll_paths:
                _dll_handles.append(os.add_dll_directory(path))
                _dll_paths.add(path)
            if path not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
    return _dependency("fluidsynth")


def _soundfont(path):
    explicit = path or os.environ.get("MUSICPLAYER_SOUNDFONT")
    if explicit:
        result = Path(explicit).expanduser()
        if result.is_file():
            return result.resolve()
        raise MusicPlayerError(f"SoundFont not found: {result}")
    candidates = []
    for folder in (ROOT / "dependencies", ROOT / "soundfonts"):
        candidates.extend(sorted(folder.rglob("*.sf2")))
        candidates.extend(sorted(folder.rglob("*.sf3")))
    # Prefer a user-supplied full sound library over the generated fallback.
    candidates.sort(key=lambda p: p.name == "BasicSynth.sf2")
    if candidates:
        return candidates[0].resolve()
    raise MusicPlayerError(
        "No SoundFont found. Place an SF2/SF3 file in dependencies/ or soundfonts/, "
        "or pass soundfont=r'C:\\path\\bank.sf2'."
    )


def _send(synth, msg, program, even_hits=False):
    if msg.type == "note_on":
        if msg.velocity:
            synth.noteon(msg.channel, msg.note, 90 if even_hits else msg.velocity)
        else:
            synth.noteoff(msg.channel, msg.note)
    elif msg.type == "note_off":
        synth.noteoff(msg.channel, msg.note)
    elif msg.type == "control_change":
        # A selected instrument must survive bank changes in the MIDI file.
        if program is None or msg.channel == 9 or msg.control not in {0, 32}:
            synth.cc(msg.channel, msg.control, msg.value)
    elif msg.type == "program_change":
        if program is None or msg.channel == 9:
            synth.program_change(msg.channel, msg.program)
    elif msg.type == "pitchwheel":
        synth.pitch_bend(msg.channel, msg.pitch)


def _pitch_color(pitch):
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb((pitch % 12) / 12, 0.68, 0.96))


def _select_programs(synth, sfid, program):
    for channel in range(16):
        bank = 128 if channel == 9 else 0
        preset = 0 if channel == 9 or program is None else program
        if synth.program_select(channel, sfid, bank, preset) < 0:
            raise MusicPlayerError(f"SoundFont has no requested preset for channel {channel + 1}.")


class _Playback:
    def __init__(self, song, synth, sfid, program, speed, volume, auto_close):
        self.song, self.synth, self.sfid, self.program = song, synth, sfid, program
        self.event_times = [event.seconds for event in song.events]
        self.tempo_times = [seconds for seconds, _ in song.tempos]
        self.position = 0.0
        self.cursor = 0
        self.last_update = time.perf_counter()
        self.speed, self.volume, self.auto_close = speed, volume, auto_close
        self.playing = True
        self.finished_at = None
        self.loop = self.muted = self.even_hits = False

    def rates(self):
        index = bisect_right(self.tempo_times, self.position) - 1
        bpm = self.song.tempos[index][1]
        return 1.0, bpm, bpm * self.song.ticks_per_beat / 60

    def set_speed(self, speed):
        self.speed = max(0.1, min(8.0, speed))

    def set_volume(self, volume):
        self.volume = max(0, min(150, volume))
        gain = 0.0 if self.muted else BASE_GAIN * self.volume / 100
        self.synth.setting("synth.gain", gain)

    def toggle_mute(self):
        self.muted = not self.muted
        self.set_volume(self.volume)

    def silence(self):
        for channel in range(16):
            self.synth.all_sounds_off(channel)

    def pause(self):
        self.playing = False
        self.silence()

    def seek(self, position, now, *, play=None):
        was_playing = self.playing if play is None else play
        self.position = max(0.0, min(self.song.duration, position))
        self.last_update = now
        self.finished_at = None
        self.playing = was_playing and self.position < self.song.duration
        self.silence()
        if self.playing:
            self._restore()

    def toggle_play(self, now):
        if self.playing:
            self.pause()
        else:
            position = 0.0 if self.position >= self.song.duration else self.position
            self.seek(position, now, play=True)

    def _restore(self):
        """Restore channel state and retrigger notes held at the seek point."""
        self.synth.system_reset()
        _select_programs(self.synth, self.sfid, self.program)
        self.cursor = bisect_right(self.event_times, self.position)
        held, sustained = {}, {}
        pedal = [False] * 16
        for event in self.song.events[:self.cursor]:
            msg = event.message
            if msg.type in ("note_on", "note_off"):
                key = (msg.channel, msg.note)
                if msg.type == "note_on" and msg.velocity:
                    held[key] = msg.velocity
                    sustained.pop(key, None)
                else:
                    velocity = held.pop(key, None)
                    if pedal[msg.channel] and velocity is not None:
                        sustained[key] = velocity
                continue
            if msg.type == "control_change":
                channel = msg.channel
                if msg.control == 64:
                    pedal[channel] = msg.value >= 64
                elif msg.control == 121:
                    pedal[channel] = False
                if msg.control in (120, 123):
                    for key in [key for key in held if key[0] == channel]:
                        velocity = held.pop(key)
                        if msg.control == 123 and pedal[channel]:
                            sustained[key] = velocity
                if msg.control == 120 or not pedal[channel]:
                    for key in [key for key in sustained if key[0] == channel]:
                        del sustained[key]
            _send(self.synth, msg, self.program)
        for (channel, pitch), velocity in sustained.items():
            self.synth.noteon(channel, pitch, 90 if self.even_hits else velocity)
            self.synth.noteoff(channel, pitch)
        for (channel, pitch), velocity in held.items():
            self.synth.noteon(channel, pitch, 90 if self.even_hits else velocity)

    def update(self, now):
        delta = now - self.last_update
        self.last_update = now
        if not self.playing:
            return
        self.position = min(self.song.duration, self.position + delta * self.speed)
        events = self.song.events
        while self.cursor < len(events) and events[self.cursor].seconds <= self.position:
            _send(self.synth, events[self.cursor].message, self.program, self.even_hits)
            self.cursor += 1
        if self.position >= self.song.duration:
            if self.loop and self.song.duration > 0:
                self.seek(0, now, play=True)
                return
            for channel in range(16):
                self.synth.cc(channel, 64, 0)
                self.synth.all_notes_off(channel)
            self.playing = False
            self.finished_at = now


class _PianoView:
    def __init__(self, pg, song, title, instrument_label):
        self.pg, self.song = pg, song
        self.title, self.instrument_label = title, instrument_label
        self.screen = pg.display.set_mode((1280, 760), pg.RESIZABLE)
        pg.display.set_caption("MusicPlayerAI — " + title)
        self.font = pg.font.SysFont("Segoe UI", 19)
        self.small = pg.font.SysFont("Segoe UI", 13)
        self.large = pg.font.SysFont("Segoe UI", 28, bold=True)
        pitches = [n.pitch for n in song.notes]
        self.low = min(21, min(pitches, default=21))
        self.high = max(108, max(pitches, default=108))
        # Range edges must be white keys so every black key has neighbors.
        if self.low % 12 in {1, 3, 6, 8, 10}:
            self.low -= 1
        if self.high % 12 in {1, 3, 6, 8, 10}:
            self.high += 1
        self.cursor, self.visible = 0, []
        self.previous_position = 0.0
        self.drag = None
        self.resume_after_drag = False
        self.edit_rate = None
        self.rate_text = ""
        self.size = None
        self._layout()

    def _layout(self):
        width, height = self.screen.get_size()
        self.size = width, height
        self.left, self.right = 24, width - 24
        self.top, self.strike = 90, height - 325
        self.bottom = height - 208
        pg = self.pg
        self.timeline = pg.Rect(self.left, height - 199, self.right - self.left, 22)
        self.volume_slider = pg.Rect(self.right - 155, height - 169, 155, 36)
        actions = ("restart", "back", "play", "forward", "stop", "loop", "stay", "even", "mute")
        self.transport = {}
        cell = (self.volume_slider.left - self.left - 8) / len(actions)
        for index, action in enumerate(actions):
            self.transport[action] = pg.Rect(round(self.left + index * cell), height - 169,
                                             max(1, round(cell) - 5), 36)
        self.rate_rows = []
        cell = (self.right - self.left - 158) / len(SPEED_PRESETS)
        for index in range(3):
            y = height - 125 + 33 * index
            label = pg.Rect(self.left, y, 150, 28)
            options = [
                pg.Rect(round(self.left + 158 + column * cell), y, max(1, round(cell) - 5), 28)
                for column in range(len(SPEED_PRESETS))
            ]
            self.rate_rows.append((label, options))
        whites = [p for p in range(self.low, self.high + 1) if p % 12 not in {1, 3, 6, 8, 10}]
        key_width = (self.right - self.left) / len(whites)
        self.keys = {}
        white_count = 0
        for pitch in range(self.low, self.high + 1):
            black = pitch % 12 in {1, 3, 6, 8, 10}
            if black:
                rect = self.pg.Rect(round(self.left + white_count * key_width - key_width * .31),
                                    self.strike, max(2, round(key_width * .62)),
                                    round((self.bottom - self.strike) * .62))
            else:
                x = round(self.left + white_count * key_width)
                rect = self.pg.Rect(x, self.strike,
                                    max(1, round(self.left + (white_count + 1) * key_width) - x),
                                    self.bottom - self.strike)
                white_count += 1
            self.keys[pitch] = rect, black

    def _button(self, rect, label, active=False):
        pg = self.pg
        pg.draw.rect(self.screen, (33, 83, 78) if active else (33, 44, 61), rect, border_radius=5)
        pg.draw.rect(self.screen, (104, 219, 194) if active else (67, 83, 105), rect, 1, border_radius=5)
        text = self.small.render(label, True, (239, 246, 252))
        self.screen.blit(text, text.get_rect(center=rect.center))

    def _controls(self, playback):
        pg, screen = self.pg, self.screen
        position, duration = playback.position, self.song.duration
        state = "Playing" if playback.playing else "Paused"
        if playback.finished_at is not None:
            state = "Finished"
        elif not playback.playing and position == 0:
            state = "Stopped"

        def timestamp(seconds):
            seconds = int(seconds)
            return f"{seconds // 60}:{seconds % 60:02d}"

        track = self.timeline.inflate(0, -16)
        fraction = position / duration if duration else 0.0
        progress = round(track.width * fraction)
        pg.draw.rect(screen, (55, 66, 86), track, border_radius=3)
        pg.draw.rect(screen, (95, 218, 190), (track.x, track.y, progress, track.height), border_radius=3)
        pg.draw.circle(screen, (212, 252, 239), (track.x + progress, track.centery), 5)

        labels = {
            "restart": "Restart", "back": "-5 s", "play": "Pause" if playback.playing else "Play",
            "forward": "+5 s", "stop": "Stop", "loop": "Loop", "stay": "Stay open",
            "even": "Even hits", "mute": "Unmute" if playback.muted else "Mute",
        }
        toggles = {"loop": playback.loop, "stay": not playback.auto_close,
                   "even": playback.even_hits, "mute": playback.muted}
        for action, rect in self.transport.items():
            self._button(rect, labels[action], toggles.get(action, action == "play"))

        slider = self.volume_slider
        self._button(slider, "")
        volume = f"Volume {round(playback.volume)}%" + (" (muted)" if playback.muted else "")
        text = self.small.render(volume, True, (239, 246, 252))
        screen.blit(text, text.get_rect(midtop=(slider.centerx, slider.y + 2)))
        rail = pg.Rect(slider.x + 8, slider.bottom - 11, slider.width - 16, 4)
        pg.draw.rect(screen, (75, 92, 113), rail, border_radius=2)
        fill = round(rail.width * playback.volume / 150)
        pg.draw.rect(screen, (95, 218, 190), (rail.x, rail.y, fill, rail.height), border_radius=2)
        pg.draw.circle(screen, (212, 252, 239), (rail.x + fill, rail.centery), 4)

        for index, ((label, options), name, base) in enumerate(zip(
            self.rate_rows, ("Speed", "BPM", "Ticks/s"), playback.rates()
        )):
            current = self.rate_text + "|" if self.edit_rate == index else self._rate_label(playback.speed * base, index)
            self._button(label, f"{name}  {current}", self.edit_rate == index)
            for rect, preset in zip(options, SPEED_PRESETS):
                self._button(rect, self._rate_label(preset * base, index), math.isclose(playback.speed, preset))

        footer = (f"{state}  {timestamp(position)} / {timestamp(duration)}"
                  "   |   Space play/pause · Arrows seek/volume · S stop · Home restart · M mute"
                  "   |   Click a rate label to type a value")
        screen.blit(self.small.render(footer, True, (162, 180, 200)), (self.left, self.size[1] - 23))

    @staticmethod
    def _rate_label(value, row):
        return f"{value:.2f}".rstrip("0").rstrip(".") + ("x" if row == 0 else "")

    def handle(self, event, playback, now):
        pg = self.pg
        if event.type == pg.VIDEORESIZE:
            if event.w < 900 or event.h < 570:
                self.screen = pg.display.set_mode((max(900, event.w), max(570, event.h)), pg.RESIZABLE)
            self._layout()
        elif event.type == pg.KEYDOWN:
            key = event.key
            if self.edit_rate is not None:
                if key in (pg.K_RETURN, pg.K_KP_ENTER):
                    try:
                        value = float(self.rate_text)
                    except ValueError:
                        return False
                    if math.isfinite(value) and value > 0:
                        playback.set_speed(value / playback.rates()[self.edit_rate])
                        self.edit_rate = None
                elif key == pg.K_ESCAPE:
                    self.edit_rate = None
                elif key == pg.K_BACKSPACE:
                    self.rate_text = self.rate_text[:-1]
                elif event.unicode and event.unicode in "0123456789." and len(self.rate_text) < 10:
                    self.rate_text += event.unicode
                return False
            if key == pg.K_ESCAPE:
                return True
            if key == pg.K_SPACE:
                playback.toggle_play(now)
            elif key in (pg.K_LEFT, pg.K_RIGHT):
                playback.seek(playback.position + (-5 if key == pg.K_LEFT else 5), now)
            elif key in (pg.K_UP, pg.K_DOWN):
                playback.set_volume(playback.volume + (5 if key == pg.K_UP else -5))
            elif key == pg.K_HOME:
                playback.seek(0, now, play=True)
            elif key == pg.K_s:
                playback.seek(0, now, play=False)
            elif key == pg.K_m:
                playback.toggle_mute()
            elif key == pg.K_l:
                playback.loop = not playback.loop
        elif event.type == pg.MOUSEBUTTONDOWN and event.button == 1:
            self.edit_rate = None
            if self.timeline.collidepoint(event.pos):
                self.drag = "seek"
                self.resume_after_drag = playback.playing
                playback.pause()
                self._drag_to(event.pos[0], playback, now)
            elif self.volume_slider.collidepoint(event.pos):
                self.drag = "volume"
                self._drag_to(event.pos[0], playback, now)
            else:
                self._click(event.pos, playback, now)
        elif event.type == pg.MOUSEMOTION and self.drag:
            self._drag_to(event.pos[0], playback, now)
        elif event.type == pg.MOUSEBUTTONUP and event.button == 1 and self.drag:
            self._drag_to(event.pos[0], playback, now)
            if self.drag == "seek" and self.resume_after_drag:
                playback.seek(playback.position, now, play=True)
            self.drag = None
        return False

    def _drag_to(self, x, playback, now):
        if self.drag == "seek":
            playback.seek((x - self.timeline.x) / self.timeline.width * self.song.duration, now, play=False)
        else:
            slider = self.volume_slider.inflate(-16, 0)
            playback.set_volume((x - slider.x) / slider.width * 150)

    def _click(self, position, playback, now):
        for index, (label, options) in enumerate(self.rate_rows):
            if label.collidepoint(position):
                self.edit_rate, self.rate_text = index, ""
                return
            for rect, speed in zip(options, SPEED_PRESETS):
                if rect.collidepoint(position):
                    playback.set_speed(speed)
                    return
        for action, rect in self.transport.items():
            if not rect.collidepoint(position):
                continue
            if action == "restart":
                playback.seek(0, now, play=True)
            elif action == "back":
                playback.seek(playback.position - 5, now)
            elif action == "play":
                playback.toggle_play(now)
            elif action == "forward":
                playback.seek(playback.position + 5, now)
            elif action == "stop":
                playback.seek(0, now, play=False)
            elif action == "loop":
                playback.loop = not playback.loop
            elif action == "stay":
                playback.auto_close = not playback.auto_close
            elif action == "even":
                playback.even_hits = not playback.even_hits
            elif action == "mute":
                playback.toggle_mute()
            return

    def draw(self, elapsed, playback=None):
        pg = self.pg
        if self.size != self.screen.get_size():
            self._layout()
        width, height = self.size
        screen = self.screen
        screen.fill((12, 17, 28))
        title = self.title if len(self.title) <= 70 else self.title[:67] + "..."
        screen.blit(self.large.render(title, True, (237, 242, 250)), (24, 18))
        subtitle = f"MIDI  /  {self.instrument_label}  /  Pitch colors  /  {self.song.ticks_per_beat} ticks per beat"
        screen.blit(self.small.render(subtitle, True, (144, 163, 188)), (25, 56))
        for pitch, (rect, black) in self.keys.items():
            if not black and pitch % 12 == 0:
                pg.draw.line(screen, (29, 38, 53), (rect.x, self.top), (rect.x, self.strike))
        # Time guides use the same seconds scale as the note rectangles.
        scale = max(1, self.strike - self.top) / LOOKAHEAD
        for second in range(math.floor(elapsed), math.ceil(elapsed + LOOKAHEAD) + 1):
            y = round(self.strike - (second - elapsed) * scale)
            if self.top <= y <= self.strike:
                pg.draw.line(screen, (27, 36, 50), (self.left, y), (self.right, y))
        if elapsed < self.previous_position:
            self.cursor, self.visible = 0, []
        self.previous_position = elapsed
        while self.cursor < len(self.song.notes) and self.song.notes[self.cursor].start <= elapsed + LOOKAHEAD:
            note = self.song.notes[self.cursor]
            if note.end > elapsed:
                self.visible.append(note)
            self.cursor += 1
        self.visible = [n for n in self.visible if n.end > elapsed]
        active = set()
        screen.set_clip(pg.Rect(self.left, self.top, self.right - self.left, self.strike - self.top))
        for note in self.visible:
            rect, black = self.keys[note.pitch]
            y1 = self.strike - (note.end - elapsed) * scale
            y2 = self.strike - (note.start - elapsed) * scale
            top, bottom = max(self.top, y1), min(self.strike, y2)
            if bottom > top:
                bar = pg.Rect(rect.x + 1, round(top), max(2, rect.width - 2),
                              max(2, round(bottom) - round(top)))
                color = _pitch_color(note.pitch)
                pg.draw.rect(screen, color, bar, border_radius=2)
                if bar.height > 2:
                    pg.draw.rect(screen, (9, 13, 24), bar, 1, border_radius=2)
                # A dark end cap separates adjacent notes even with no MIDI gap.
                if note.start >= elapsed:
                    y = bar.bottom - 1
                    pg.draw.line(screen, (9, 13, 24), (bar.left, y), (bar.right - 1, y))
            if note.start <= elapsed < note.end:
                active.add(note.pitch)
        screen.set_clip(None)
        for black_pass in (False, True):
            for pitch, (rect, black) in self.keys.items():
                if black != black_pass:
                    continue
                color = _pitch_color(pitch) if pitch in active else ((27, 32, 43) if black else (230, 235, 241))
                pg.draw.rect(screen, color, rect, border_radius=2)
                pg.draw.rect(screen, (10, 14, 23), rect, 1, border_radius=2)
                if not black and pitch % 12 == 0:
                    label = self.small.render(f"C{pitch // 12 - 1}", True, (65, 77, 93))
                    screen.blit(label, (rect.centerx - label.get_width() // 2, rect.bottom - 22))
        pg.draw.line(screen, (115, 233, 210), (self.left, self.strike), (self.right, self.strike), 2)
        if playback is not None:
            self._controls(playback)
        pg.display.flip()


def _show_sheet_music(filename):
    source = Path(filename).expanduser().resolve()
    if not source.is_file():
        raise MusicPlayerError(f"MIDI file not found: {source}")
    musescore = shutil.which("MuseScore4.exe")
    if not musescore:
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(variable):
                candidate = Path(os.environ[variable]) / "MuseScore 4" / "bin" / "MuseScore4.exe"
                if candidate.is_file():
                    musescore = str(candidate)
                    break
    if not musescore:
        raise MusicPlayerError("Mode 0 requires MuseScore Studio 4. Install it or add MuseScore4.exe to PATH.")

    try:
        with tempfile.TemporaryDirectory(dir=source.parent) as folder:
            rendered = Path(folder) / f"{source.stem}.mscz"
            result = subprocess.run(
                [musescore, "-o", str(rendered), str(source)],
                capture_output=True, text=True, errors="replace", timeout=120,
            )
            if result.returncode or not rendered.is_file() or not rendered.stat().st_size:
                detail = (result.stderr or result.stdout).strip()
                raise MusicPlayerError(f"MuseScore could not write sheet music for {source.name}. {detail}".strip())
            number = 1
            while True:
                name = source.stem if number == 1 else f"{source.stem}-{number}"
                output = source.with_name(f"{name}.mscz")
                try:
                    # On Windows rename fails if the score already exists.
                    rendered.rename(output)
                    break
                except FileExistsError:
                    number += 1
    except subprocess.TimeoutExpired as exc:
        raise MusicPlayerError("MuseScore took longer than 120 seconds to convert the MIDI file.") from exc
    except OSError as exc:
        raise MusicPlayerError(f"Could not save sheet music for {source.name}: {exc}") from exc

    try:
        subprocess.Popen([musescore, str(output)])
    except OSError as exc:
        raise MusicPlayerError(f"Score saved to {output}, but MuseScore could not open it: {exc}") from exc
    return output


def play(filename, mode=1, *, instrument="piano", soundfont=None, display_name=None,
         speed=1.0, volume=100, auto_close=True):
    """Display sheet music or play a MIDI file with a falling-note piano window.

    mode=0: save and open sheet music in MuseScore; return the saved score path.
    mode=1: standard MIDI type 0/1, all notes, tempo changes, no six-note limit.
    mode=2: reserved for future optimized lists; print a message and return.
    instrument: GM name or zero-based program 0..127; None preserves MIDI programs.
    Percussion on MIDI channel 10 remains percussion even with an override.
    soundfont: optional .sf2/.sf3; otherwise MUSICPLAYER_SOUNDFONT or soundfonts/.
    display_name: optional title for the piano window.
    speed: initial tempo multiplier (0.1–8), preserving MIDI tempo changes.
    volume: initial master volume (0–150%); MIDI velocities are used by default.
    auto_close: close after the normal cooldown, or keep the controls open.

    Relative filenames follow the caller's working directory. Mode 1 must run
    on the main thread. With auto_close it returns 0.5 seconds after playback ends.
    Closing its pygame window or pressing Ctrl+C exits early.
    """
    if isinstance(mode, bool) or mode not in (0, 1, 2):
        raise MusicPlayerError("Mode must be 0 (sheet music), 1 (MIDI), or 2 (future optimized lists).")
    if mode == 0:
        return _show_sheet_music(filename)
    if mode == 2:
        print("Code not complete: optimized-list playback is not implemented.")
        return None
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.1 <= speed <= 8:
        raise MusicPlayerError("Speed must be between 0.1 and 8.")
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 150:
        raise MusicPlayerError("Volume must be between 0 and 150 percent.")
    program = _instrument(instrument)
    song = _read_midi(filename)
    sfpath = _soundfont(soundfont)
    fs = _load_fluidsynth()
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    pg = _dependency("pygame")
    synth = None
    try:
        pg.display.init()
        pg.font.init()
        label = GM_NAMES[program] if program is not None else "Original MIDI instruments"
        title = Path(filename).name if display_name is None else str(display_name)
        view = _PianoView(pg, song, title, label)
        synth = fs.Synth(samplerate=44100, gain=BASE_GAIN * volume / 100)
        # Sustain and release tails can outlast pressed keys. Do not impose the
        # future optimizer's six-note limit on ordinary MIDI playback.
        synth.setting("synth.polyphony", max(256, _peak_polyphony(song.notes) * 4))
        sfid = synth.sfload(str(sfpath))
        if sfid < 0:
            raise MusicPlayerError(f"FluidSynth could not load SoundFont: {sfpath}")
        _select_programs(synth, sfid, program)
        # Start only audio output; Synth.start() also opens a live MIDI input.
        if sys.platform == "win32":
            synth.setting("audio.driver", "wasapi")
        synth.setting("audio.period-size", 128)
        synth.setting("audio.periods", 4)
        synth.audio_driver = fs.new_fluid_audio_driver(synth.settings, synth.synth)
        if not synth.audio_driver:
            raise MusicPlayerError("FluidSynth could not open the audio output device.")
        playback = _Playback(song, synth, sfid, program, speed, volume, auto_close)
        last_frame = -math.inf
        while True:
            now = time.perf_counter()
            playback.update(now)
            for event in pg.event.get():
                if event.type == pg.QUIT or view.handle(event, playback, now):
                    return None
            if now - last_frame >= 1 / 60:
                view.draw(playback.position, playback)
                last_frame = now
            if playback.auto_close and playback.finished_at is not None and now - playback.finished_at >= COOLDOWN:
                break
            time.sleep(0.001)
    except MusicPlayerError:
        raise
    except Exception as exc:
        raise MusicPlayerError(f"Playback failed: {exc}") from exc
    finally:
        if synth is not None:
            synth.delete()
        pg.display.quit()
        pg.font.quit()
    return None


def _peak_polyphony(notes):
    edges = sorted([(n.start, 1) for n in notes if n.end > n.start] +
                   [(n.end, -1) for n in notes if n.end > n.start])
    current, peak = 0, 0
    for _, delta in edges:
        current += delta
        peak = max(peak, current)
    return peak


def main(argv=None):
    parser = argparse.ArgumentParser(description="Display MIDI sheet music or play MIDI with a piano animation.")
    parser.add_argument("filename", nargs="?", help="MIDI filename (.mid or .midi)")
    parser.add_argument("--mode", type=int, choices=(0, 1, 2))
    parser.add_argument("--instrument", default="piano", help="GM name, 0–127, or original (default: piano)")
    parser.add_argument("--soundfont", help="Path to an SF2/SF3 sound library")
    parser.add_argument("--display-name", help="Title shown in the piano window")
    parser.add_argument("--speed", type=float, default=1.0, help="Initial playback speed multiplier")
    parser.add_argument("--volume", type=float, default=100, help="Initial volume percentage")
    parser.add_argument("--stay-open", action="store_true", help="Keep the piano window open after playback")
    parser.add_argument("--list-instruments", action="store_true")
    args = parser.parse_args(argv)
    if args.list_instruments:
        for number, name in enumerate(GM_NAMES):
            print(f"{number:3d}  {name}")
        return 0
    try:
        filename = args.filename
        if filename is None:
            filename = input("MIDI filename: ").strip().strip('"')
        mode = args.mode
        if mode is None:
            mode = int(input("Mode [0 = sheet music, 1 = MIDI, 2 = optimized lists] (default 1): ").strip() or "1")
        output = play(filename, mode, instrument=args.instrument, soundfont=args.soundfont,
                      display_name=args.display_name, speed=args.speed, volume=args.volume,
                      auto_close=not args.stay_open)
        if mode == 0:
            print(f"Sheet music saved to {output}")
        return 0
    except (MusicPlayerError, ValueError, EOFError) as exc:
        print(f"MusicPlayerAI: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nPlayback ended.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
