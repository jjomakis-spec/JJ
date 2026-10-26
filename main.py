# -*- coding: utf-8 -*-
"""JJ Player v23.1 - Fixed information line and removed empty player space - Kivy / Pydroid 3
   Left: vertical playlist/folder tree. Right: tracks, BPM and musical key.
   Playlist references are matched using normalized filenames.
"""

import os
import re
import json
import unicodedata
import threading
import traceback
import time
import math
import random
import wave
import struct
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.core.window import Window
from kivy.core.audio import SoundLoader
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.progressbar import ProgressBar
from kivy.uix.widget import Widget
from kivy.graphics import Color, Rectangle, Line, Ellipse

# Android storage/audio permissions (Pydroid 3). On desktop these imports are absent.
try:
    from android.permissions import request_permissions, check_permission
except Exception:
    request_permissions = None
    check_permission = None

DEFAULT_NML = "/storage/9C33-6BBD/collection.nml"
PAGE_SIZE = 1000000  # Render the whole selected playlist; ScrollView handles browsing.

# ---------- Helpers ----------
def normalized(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch.casefold() for ch in text if ch.isalnum())


def basename(path):
    return str(path or "").replace("\\", "/").rstrip("/").split("/")[-1]


def playlist_ref_name(key):
    # A Traktor playlist KEY usually ends in the track filename.
    return basename(key)


def get_track_key(entry):
    node = entry.find("MUSICAL_KEY")
    if node is not None:
        return node.get("VALUE", "")
    info = entry.find("INFO")
    return info.get("KEY", "") if info is not None else ""


def parse_nml(path):
    if not os.path.isfile(path):
        raise FileNotFoundError("File not found:\n" + path)

    # Parse the NML and build a filename index.
    tree = ET.parse(path)
    root = tree.getroot()
    collection = root.find("./COLLECTION")
    playlists_root = root.find("./PLAYLISTS")
    if collection is None:
        raise ValueError("The file does not contain COLLECTION.")
    if playlists_root is None:
        raise ValueError("The file does not contain PLAYLISTS.")

    tracks = []
    index = defaultdict(list)
    for entry in collection.findall("ENTRY"):
        loc = entry.find("LOCATION")
        if loc is None:
            continue
        file_name = loc.get("FILE", "")
        tempo = entry.find("TEMPO")
        bpm = tempo.get("BPM", "") if tempo is not None else ""
        try:
            if bpm:
                bpm = str(round(float(bpm), 2)).rstrip("0").rstrip(".")
        except (ValueError, TypeError):
            pass
        info = entry.find("INFO")
        title = entry.get("TITLE", "") or os.path.splitext(file_name)[0]
        artist = entry.get("ARTIST", "")
        key = get_track_key(entry)
        cues = []
        for cue in entry.findall("CUE_V2"):
            try:
                cue_start = float(cue.get("START", "0") or 0)
                cue_len = float(cue.get("LEN", "0") or 0)
            except (ValueError, TypeError):
                cue_start, cue_len = 0.0, 0.0
            cues.append({
                "name": cue.get("NAME", "Cue"),
                "type": cue.get("TYPE", "0"),
                "start": cue_start,
                "length": cue_len,
                "hotcue": cue.get("HOTCUE", "-1"),
            })
        try:
            duration = float(info.get("PLAYTIME_FLOAT", info.get("PLAYTIME", "0")) or 0) if info is not None else 0.0
        except (ValueError, TypeError):
            duration = 0.0
        track = {
            "title": title,
            "artist": artist,
            "file": file_name,
            "dir": loc.get("DIR", ""),
            "volume": loc.get("VOLUME", ""),
            "bpm": bpm,
            "key": key,
            "info_key": info.get("KEY", "") if info is not None else "",
            "duration": duration,
            "cues": cues,
        }
        idx = len(tracks)
        tracks.append(track)
        # Normalized matching handles case, accents and punctuation differences.
        index[normalized(basename(file_name))].append(idx)

    # Convert playlist tree into lightweight dictionaries; do not retain XML nodes.
    node_counter = [0]

    def parse_node(node, depth=0):
        node_counter[0] += 1
        item = {
            "id": node_counter[0],
            "name": node.get("NAME", "(Unnamed)"),
            "type": node.get("TYPE", ""),
            "depth": depth,
            "children": [],
            "refs": [],
        }
        subnodes = node.find("SUBNODES")
        if subnodes is not None:
            for child in subnodes.findall("NODE"):
                item["children"].append(parse_node(child, depth + 1))
        playlist = node.find("PLAYLIST")
        if playlist is not None:
            for ref_entry in playlist.findall("ENTRY"):
                pk = ref_entry.find("PRIMARYKEY")
                if pk is not None and pk.get("TYPE", "TRACK").upper() == "TRACK":
                    item["refs"].append(pk.get("KEY", ""))
        return item

    playlist_tree = parse_node(playlists_root.find("NODE") or playlists_root)
    del tree, root, collection, playlists_root
    return {"path": path, "tracks": tracks, "index": index,
            "playlist_tree": playlist_tree, "track_count": len(tracks)}



class AndroidMediaSound:
    """Android MediaPlayer wrapper with explicit play/pause/completion states."""
    def __init__(self, path):
        from jnius import autoclass
        MediaPlayer = autoclass('android.media.MediaPlayer')
        self._player = MediaPlayer()
        self._paused = False
        self._stopped = False
        self._started = False
        self._saved_pos = 0.0
        self._player.setDataSource(path)
        self._player.prepare()

    @property
    def state(self):
        try:
            if self._player.isPlaying():
                return 'play'
            if self._paused:
                return 'pause'
            if self._stopped or not self._started:
                return 'stop'
            duration = float(self._player.getDuration() or 0) / 1000.0
            position = float(self._player.getCurrentPosition() or 0) / 1000.0
            if duration > 0 and position >= duration - 0.35:
                self._stopped = True
                return 'stop'
            # MediaPlayer may briefly report not-playing while starting.
            return 'loading'
        except Exception:
            return 'loading'

    def play(self):
        if self._stopped:
            self._player.seekTo(int(self._saved_pos * 1000))
            self._stopped = False
        self._player.start()
        self._started = True
        self._paused = False

    def pause(self):
        try:
            self._saved_pos = self.get_pos()
            if self._player.isPlaying():
                self._player.pause()
        finally:
            self._paused = True
            self._stopped = False

    def stop(self):
        try:
            self._saved_pos = 0.0
            if self._player.isPlaying():
                self._player.pause()
                self._player.seekTo(0)
        except Exception:
            pass
        self._paused = False
        self._stopped = True
        self._started = False

    def seek(self, seconds):
        seconds = max(0.0, float(seconds))
        self._player.seekTo(int(seconds * 1000))
        self._saved_pos = seconds

    def get_pos(self):
        try:
            self._saved_pos = float(self._player.getCurrentPosition()) / 1000.0
        except Exception:
            pass
        return self._saved_pos

    def unload(self):
        try:
            self._player.release()
        except Exception:
            pass

# ---------- Waveform / cue display ----------
class WaveformWidget(Widget):
    """A lightweight waveform-style overview with cue and loop markers.

    Traktor NML stores cue positions but not the decoded audio waveform itself.
    This draws a visual overview and positions real saved cue/loop markers from NML.
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.track = None
        self.position = 0.0
        self.seek_callback = None
        self.samples = None
        self.bind(pos=self.redraw, size=self.redraw)

    def set_track(self, track):
        self.track = track
        self.position = 0.0
        self.samples = None
        self.redraw()

    def set_samples(self, samples):
        self.samples = samples or None
        self.redraw()

    def set_position(self, seconds):
        self.position = max(0.0, float(seconds or 0))
        self.redraw()

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self.track and float(self.track.get("duration", 0) or 0) > 0:
            if self.seek_callback:
                fraction = max(0.0, min(1.0, (touch.x - self.x - dp(8)) / max(1.0, self.width - dp(16))))
                self.seek_callback(fraction * float(self.track.get("duration", 0) or 0))
            return True
        return super().on_touch_down(touch)

    def redraw(self, *_):
        self.canvas.clear()
        with self.canvas:
            Color(0.035, 0.045, 0.055, 1)
            Rectangle(pos=self.pos, size=self.size)
            x, y = self.x + dp(1), self.y + dp(1)
            w, h = max(dp(1), self.width - dp(2)), max(dp(1), self.height - dp(2))
            mid = y + h / 2
            track = self.track or {}
            duration = float(track.get("duration", 0) or 0)
            progress = max(0.0, min(1.0, self.position / duration)) if duration > 0 else 0.0
            # Blue played region, like the reference player skin.
            Color(0.08, 0.38, 0.72, 0.95)
            Rectangle(pos=(x, y), size=(w * progress, h))
            Color(0.12, 0.16, 0.17, 1)
            Line(points=[x, mid, x + w, mid], width=dp(0.6))
            # Dense, mirrored green waveform across the entire available width.
            bars = max(100, int(w / dp(1.2)))
            samples = self.samples or []
            bar_step = w / bars
            for i in range(bars):
                raw_amp = samples[min(len(samples) - 1, int(i * len(samples) / bars))] if samples else 0.015
                raw_amp = max(0.0, min(1.0, float(raw_amp)))
                amp = min(1.0, raw_amp ** 0.38)
                # Stretch each mirrored bar to use the waveform window height,
                # while retaining some amplitude variation like a DJ player skin.
                bar_h = max(dp(3), h * (0.58 + 0.42 * amp))
                bx = x + bar_step * i
                bar_w = max(dp(0.8), bar_step * 0.82)
                if (i / bars) < progress:
                    Color(0.15, 0.78, 1.0, 0.95)
                else:
                    Color(0.28, 0.92, 0.20, 1.0)
                Rectangle(pos=(bx, mid - bar_h / 2), size=(bar_w, bar_h))
            duration = float(track.get("duration", 0) or 0)
            for cue in track.get("cues", []):
                start = float(cue.get("start", 0) or 0)
                cue_length = float(cue.get("length", 0) or 0)
                # Some Traktor versions store cue positions in milliseconds.
                if duration > 0 and start > duration and start / 1000.0 <= duration * 1.05:
                    start /= 1000.0
                    cue_length /= 1000.0
                ctype = str(cue.get("type", "0"))
                if duration > 0:
                    px = x + w * max(0.0, min(1.0, start / duration))
                else:
                    px = x + w * (0.05 + (sum(ord(c) for c in str(cue.get("name", ""))) % 90) / 100)
                if ctype == "5" and cue_length > 0 and duration > 0:
                    Color(0.18, 0.72, 0.96, 0.24)
                    loop_w = w * min(1.0, cue_length / duration)
                    Rectangle(pos=(px, y), size=(loop_w, h))
                Color(1.0, 0.72, 0.24, 1) if ctype in ("0", "5") else Color(0.65, 0.72, 0.82, 0.85)
                Line(points=[px, y, px, y + h], width=dp(1.3))
                if cue.get("hotcue", "-1") not in ("-1", ""):
                    Color(1.0, 0.80, 0.30, 1)
                    Ellipse(pos=(px - dp(3), y + h - dp(5)), size=(dp(6), dp(6)))
            if duration > 0:
                play_x = x + w * max(0.0, min(1.0, self.position / duration))
                Color(0.20, 0.88, 0.48, 1)
                Line(points=[play_x, y - dp(2), play_x, y + h + dp(2)], width=dp(2.2))
                Color(0.20, 0.88, 0.48, 1)
                Ellipse(pos=(play_x - dp(4), mid - dp(4)), size=(dp(8), dp(8)))


class TrackTitleButton(Button):
    """Button that distinguishes a normal tap from a long press."""
    def __init__(self, on_tap=None, on_long_press=None, **kwargs):
        super().__init__(**kwargs)
        self.on_tap_callback = on_tap
        self.on_long_callback = on_long_press
        self._long_event = None
        self._long_fired = False
        self.bind(on_press=self._pressed, on_release=self._released)

    def _pressed(self, *_):
        self._long_fired = False
        if self._long_event:
            self._long_event.cancel()
        self._long_event = Clock.schedule_once(self._fire_long, 0.65)

    def _fire_long(self, *_):
        self._long_fired = True
        if self.on_long_callback:
            self.on_long_callback()

    def _released(self, *_):
        if self._long_event:
            self._long_event.cancel()
            self._long_event = None
        if not self._long_fired and self.on_tap_callback:
            self.on_tap_callback()


class LongPressButton(Button):
    """Normal release runs the button action; holding opens its detail action."""
    def __init__(self, on_long_press=None, **kwargs):
        super().__init__(**kwargs)
        self.on_long_press_callback = on_long_press
        self._long_press_event = None
        self._long_press_fired = False

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self.disabled is False:
            self._long_press_fired = False
            self._long_press_event = Clock.schedule_once(self._fire_long_press, 0.65)
        return super().on_touch_down(touch)

    def _fire_long_press(self, *_):
        self._long_press_fired = True
        if self.on_long_press_callback:
            self.on_long_press_callback()

    def on_touch_up(self, touch):
        if self._long_press_event:
            self._long_press_event.cancel()
            self._long_press_event = None
        if self.collide_point(*touch.pos) and self._long_press_fired:
            self.state = 'normal'
            return True
        return super().on_touch_up(touch)


class MarqueeLabel(Label):
    """Text ticker that scrolls continuously from right to left."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._ticker_source = str(kwargs.get("text", ""))
        self._ticker_rendered = self.text
        self._ticker_offset = 0
        self._ticker_event = Clock.schedule_interval(self._ticker_tick, 0.22)
        self.bind(text=self._ticker_text_changed)

    def _ticker_text_changed(self, *_):
        if self.text != self._ticker_rendered:
            self._ticker_source = str(self.text or "")
            self._ticker_offset = 0
            self._ticker_rendered = self.text

    def _ticker_tick(self, _dt):
        source = self._ticker_source
        if not source or self.width <= dp(20):
            return
        # Keep short text still; rotate longer text to create the old marquee effect.
        chars = max(12, int(self.width / max(dp(6), self.font_size * 0.52)))
        if len(source) <= chars:
            if self.text != source:
                self._ticker_rendered = source
                self.text = source
            return
        padded = source + "     •     "
        self._ticker_offset = (self._ticker_offset + 1) % len(padded)
        doubled = padded + padded
        shown = doubled[self._ticker_offset:self._ticker_offset + chars]
        self._ticker_rendered = shown
        self.text = shown


# ---------- Interface ----------
class TraktorBrowser(App):
    def build(self):
        self.title = "JJ1"
        Window.clearcolor = (0.055, 0.065, 0.08, 1)
        self.theme_bg = (0.055, 0.065, 0.08, 1)
        self.panel_bg = (0.09, 0.105, 0.13, 1)
        self.text_color = (0.92, 0.94, 0.97, 1)
        self.muted_color = (0.63, 0.68, 0.75, 1)
        self.accent = (0.18, 0.86, 0.43, 1)
        self.blue = (0.18, 0.72, 0.96, 1)
        self.data = None
        self.current_playlist = None
        self._playlist_rows_cache = {}
        self._playlist_select_generation = 0
        self.selected_playlist_id = None
        self.selected_track_key = None
        self.current_rows = []
        self.waveform_cache = {}
        self.filtered_rows = []
        self.page = 0
        self.expanded = set()
        self.loading_popup = None
        self.load_generation = 0
        self.current_sound = None
        self.current_track = None
        self.current_sound_path = ""
        self.paused_position = 0.0
        self.seek_override_until = 0.0
        self.player_status = None
        self.track_info = None
        self._last_track_info_update = 0.0
        self.play_btn = None
        self.last_track_tap = 0.0
        self.last_track_tap_key = None
        self.audio_path_cache = {}
        self.audio_path_cache_file = os.path.join(self.user_data_dir, "jj_audio_paths.json")
        self.analysis_running = False
        self.audio_scan_generation = 0
        self._fallback_searching = set()
        self._load_audio_path_cache()

        root = BoxLayout(orientation="vertical", spacing=dp(1), padding=(dp(6), dp(2)))
        with root.canvas.before:
            Color(*self.theme_bg)
            self._bg_rect = Rectangle(pos=root.pos, size=root.size)
        root.bind(pos=self._update_bg, size=self._update_bg)

        # Header
        header = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(48), spacing=dp(8))
        title = Label(text="[b]JJ1[/b]", markup=True, color=self.text_color,
                      size_hint_x=None, width=dp(48), font_size="18sp", halign="left", valign="middle")
        title.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        header.add_widget(title)
        # Track information ticker sits at the very top, immediately beside JJ.
        self.player_status = MarqueeLabel(text="Select a track from the list", color=(0.95, 0.72, 0.25, 1),
                                   font_size="12sp", halign="left", valign="middle")
        self.player_status.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        header.add_widget(self.player_status)
        root.add_widget(header)

        self.status = Label(text="Ready to load collection…", size_hint_y=None,
                            height=dp(14), color=self.muted_color, font_size="10sp",
                            halign="left", valign="middle")
        self.status.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        root.add_widget(self.status)

        # Dedicated live metadata line: BPM, elapsed time, remaining time and playlist.
        self.track_info = Label(text="BPM: —  •  Time: 00:00  •  Remaining: —  •  Playlist: —",
                                size_hint_y=None, height=dp(25), color=(0.35, 0.82, 0.98, 1),
                                font_size="9sp", halign="left", valign="middle",
                                shorten=True, shorten_from="right", max_lines=1)
        self.track_info.bind(size=lambda inst, val: setattr(inst, "text_size", (val[0], None)))
        root.add_widget(self.track_info)

        # Full-width player at the top, above playlists and track browser.
        player_panel = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(140),
                                 spacing=0, padding=0)
        with player_panel.canvas.before:
            Color(0.10, 0.12, 0.15, 1)
            self._player_panel_rect = Rectangle(pos=player_panel.pos, size=player_panel.size)
        player_panel.bind(pos=lambda w, p: setattr(self._player_panel_rect, "pos", p),
                          size=lambda w, s: setattr(self._player_panel_rect, "size", s))
        # Two compact rows: navigation buttons sit directly above PLAY, as requested.
        player_controls = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(76), spacing=dp(2))
        # No empty reserved row: action buttons sit directly under the status/header.
        action_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(38), spacing=dp(4))
        self.import_btn = Button(text="Import NML", size_hint_x=None, width=dp(82),
                                 background_normal="", background_color=self.accent,
                                 color=(0.08, 0.08, 0.08, 1), bold=True, font_size="8sp")
        self.import_btn.bind(on_release=self.open_import_dialog)
        action_row.add_widget(self.import_btn)
        self.analyze_btn = LongPressButton(on_long_press=self.show_analysis_details, text="Analyze", size_hint_x=None, width=dp(70),
                                  background_normal="", background_color=self.blue,
                                  color=(0.03, 0.06, 0.08, 1), bold=True, font_size="8sp")
        self.analyze_btn.bind(on_release=self.analyze_collection_paths)
        action_row.add_widget(self.analyze_btn)
        self.prev_track_btn = Button(text="PREV", size_hint_x=None, width=dp(48),
                                     background_normal="", background_color=self.blue,
                                     color=(0.03, 0.06, 0.08, 1), font_size="9sp", bold=True)
        self.prev_track_btn.bind(on_release=lambda *_: self.play_adjacent_track(-1))
        action_row.add_widget(self.prev_track_btn)
        self.play_btn = Button(text="PLAY", size_hint_x=None, width=dp(62),
                               background_normal="", background_color=self.accent,
                               color=(0.08, 0.08, 0.08, 1), bold=True, font_size="13sp")
        self.play_btn.bind(on_release=lambda *_: self.toggle_playback())
        action_row.add_widget(self.play_btn)
        self.next_track_btn = Button(text="NEXT", size_hint_x=None, width=dp(48),
                                     background_normal="", background_color=self.blue,
                                     color=(0.03, 0.06, 0.08, 1), font_size="9sp", bold=True)
        self.next_track_btn.bind(on_release=lambda *_: self.play_adjacent_track(1))
        action_row.add_widget(self.next_track_btn)
        player_controls.add_widget(action_row)

        cue_row = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(34), spacing=dp(4))
        cue_row.add_widget(Widget())
        self.cue_prev_btn = Button(text="PREV CUE", size_hint_x=None, width=dp(112), background_normal="", background_color=self.blue, color=(0.03, 0.06, 0.08, 1), font_size="12sp", bold=True)
        self.cue_prev_btn.bind(on_release=lambda *_: self.jump_saved_marker("all", -1))
        cue_row.add_widget(self.cue_prev_btn)
        self.cue_next_btn = Button(text="NEXT CUE", size_hint_x=None, width=dp(112), background_normal="", background_color=self.blue, color=(0.03, 0.06, 0.08, 1), font_size="12sp", bold=True)
        self.cue_next_btn.bind(on_release=lambda *_: self.jump_saved_marker("all", 1))
        cue_row.add_widget(self.cue_next_btn)
        cue_row.add_widget(Widget())
        player_controls.add_widget(cue_row)
        player_panel.add_widget(player_controls)
        self.waveform = WaveformWidget(size_hint_y=None, height=dp(41))
        self.waveform.seek_callback = self.seek_to
        player_panel.add_widget(self.waveform)
        Clock.schedule_interval(self.update_playback_position, 0.2)
        self.cue_summary = Label(text="CUE / LOOP markers from NML", size_hint_y=None,
                                 height=dp(20), color=self.muted_color, font_size="10sp",
                                 halign="left", valign="middle")
        self.cue_summary.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        player_panel.add_widget(self.cue_summary)
        root.add_widget(player_panel)

        # Main split: vertical playlist tree at left, track table at right.
        main = BoxLayout(orientation="horizontal", spacing=dp(2))
        self.sidebar = BoxLayout(orientation="vertical", size_hint_x=None, width=dp(62), spacing=dp(2))
        with self.sidebar.canvas.before:
            Color(*self.panel_bg)
            self._side_rect = Rectangle(pos=self.sidebar.pos, size=self.sidebar.size)
        self.sidebar.bind(pos=lambda w, p: setattr(self._side_rect, "pos", p),
                          size=lambda w, s: setattr(self._side_rect, "size", s))
        side_head = Label(text="[b]PLAYLISTS[/b]", markup=True, size_hint_y=None,
                          height=dp(27), color=self.text_color, font_size="9sp")
        self.sidebar.add_widget(side_head)
        self.tree_scroll = ScrollView(do_scroll_x=False, bar_width=dp(5))
        self.tree_box = GridLayout(cols=1, size_hint_y=None, spacing=dp(1), padding=(dp(1), dp(3)))
        self.tree_box.bind(minimum_height=self.tree_box.setter("height"))
        self.tree_scroll.add_widget(self.tree_box)
        self.sidebar.add_widget(self.tree_scroll)
        main.add_widget(self.sidebar)

        self.right = BoxLayout(orientation="vertical", spacing=dp(4))
        tools = BoxLayout(orientation="horizontal", size_hint_y=None, height=dp(42), spacing=0)
        self.playlist_title = Label(text="Select playlist", color=self.text_color,
                                    size_hint_x=None, width=dp(112),
                                    halign="left", valign="middle", font_size="11sp")
        self.playlist_title.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        tools.add_widget(self.playlist_title)
        self.search = TextInput(hint_text="Search", multiline=False,
                                size_hint_x=1, font_size="13sp",
                                background_color=(0.13, 0.15, 0.18, 1), foreground_color=self.text_color,
                                hint_text_color=self.muted_color, cursor_color=self.accent)
        self.search.bind(text=self.on_search)
        tools.add_widget(self.search)
        self.right.add_widget(tools)

        # Table header
        self.table = GridLayout(cols=1, spacing=0, size_hint=(None, None), width=dp(689))
        self.table.bind(minimum_height=self.table.setter("height"))
        self.table_scroll = ScrollView(do_scroll_x=True, do_scroll_y=True, bar_width=dp(5))
        self.table_scroll.add_widget(self.table)
        self.right.add_widget(self.table_scroll)

        # The full list is shown with scrolling — without pagination buttons.
        self.page_label = Label(text="", color=self.muted_color, halign="left", font_size="12sp")

        main.add_widget(self.right)
        root.add_widget(main)

        # Header row shown before selecting a playlist.
        self.draw_table_header()
        # Ask Android for storage/audio access before attempting to read the NML or scan audio.
        Clock.schedule_once(lambda _dt: self.request_storage_access(), 0.2)
        return root

    def request_storage_access(self):
        """Request Android runtime access needed to read music files on SD storage."""
        if request_permissions is None:
            self.load_saved_or_default()
            return
        permissions = [
            "android.permission.READ_EXTERNAL_STORAGE",
            "android.permission.WRITE_EXTERNAL_STORAGE",
            "android.permission.READ_MEDIA_AUDIO",
        ]
        try:
            request_permissions(permissions, self._storage_permission_result)
        except TypeError:
            # Compatibility with Pydroid versions exposing the one-argument API.
            try:
                request_permissions(permissions)
            except Exception as exc:
                self.status.text = "Open Android Settings and allow Pydroid to access music/SD card files."
                print("JJ permission request error:", exc)
            Clock.schedule_once(lambda _dt: self.load_saved_or_default(), 0.6)
        except Exception as exc:
            self.status.text = "Music/SD card file access permission is required in Pydroid Settings."
            print("JJ permission request error:", exc)
            Clock.schedule_once(lambda _dt: self.load_saved_or_default(), 0.6)

    def _storage_permission_result(self, permissions, grants):
        """Continue loading after Android displays the permission prompt."""
        denied = []
        try:
            for permission, granted in zip(permissions, grants):
                if not granted and permission in (
                    "android.permission.READ_EXTERNAL_STORAGE",
                    "android.permission.READ_MEDIA_AUDIO",
                ):
                    denied.append(permission)
        except Exception:
            pass
        if denied:
            self.status.text = "Music access was not granted. Android Settings → Apps → Pydroid 3 → Permissions."
        Clock.schedule_once(lambda _dt: self.load_saved_or_default(), 0.2)

    def _update_bg(self, widget, *_):
        self._bg_rect.pos = widget.pos
        self._bg_rect.size = widget.size

    def load_saved_or_default(self):
        path = DEFAULT_NML
        try:
            saved = os.path.join(self.user_data_dir, "current_nml.txt")
            if os.path.isfile(saved):
                with open(saved, "r", encoding="utf-8") as f:
                    candidate = f.read().strip()
                if candidate and os.path.isfile(candidate):
                    path = candidate
        except Exception:
            pass
        self.load_collection(path)

    def load_collection(self, path):
        self.load_generation += 1
        generation = self.load_generation
        self.status.text = "Loading collection… the file may be large."
        self.import_btn.disabled = True
        self.tree_box.clear_widgets()
        self.table.clear_widgets()
        self.loading_popup = Popup(title="Loading Traktor NML",
                                   content=Label(text="Reading collection…\nPlease wait."),
                                   size_hint=(0.8, 0.25), auto_dismiss=False)
        self.loading_popup.open()

        def worker():
            try:
                result = parse_nml(path)
                Clock.schedule_once(lambda _dt: self.finish_load(generation, result, None), 0)
            except Exception as exc:
                msg = str(exc) + "\n\n" + traceback.format_exc(limit=2)
                Clock.schedule_once(lambda _dt: self.finish_load(generation, None, msg), 0)

        threading.Thread(target=worker, daemon=True).start()

    def finish_load(self, generation, result, error):
        if generation != self.load_generation:
            return
        self.import_btn.disabled = False
        if self.loading_popup:
            self.loading_popup.dismiss()
            self.loading_popup = None
        if error:
            self.status.text = "NML loading error."
            Popup(title="File was not loaded", content=Label(text=error[:1200]),
                  size_hint=(0.92, 0.65)).open()
            return
        self.data = result
        self._playlist_rows_cache = {}
        self._playlist_select_generation = 0
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(os.path.join(self.user_data_dir, "current_nml.txt"), "w", encoding="utf-8") as f:
                f.write(result["path"])
        except Exception:
            pass
        # Start with every folder closed; the user can open folders manually.
        self.expanded = set()
        self.build_tree()
        self.status.text = ""
        self.current_playlist = None
        self.current_rows = []
        self.filtered_rows = []
        self.page = 0
        self.playlist_title.text = "Select a playlist on the left"
        self.render_page()

    def build_tree(self):
        self.tree_box.clear_widgets()
        if not self.data:
            return
        rootnode = self.data["playlist_tree"]
        # Special all-tracks row at top, without decorative icons.
        all_btn = Button(text="All Tracks", size_hint_y=None, height=dp(30),
                         halign="left", background_normal="", background_color=(0.12, 0.34, 0.23, 1) if self.selected_playlist_id == "__ALL_TRACKS__" else (0.16, 0.20, 0.25, 1),
                         color=self.text_color, font_size="13sp")
        all_btn.bind(on_release=lambda *_: self.show_all_tracks())
        self.tree_box.add_widget(all_btn)
        for child in rootnode["children"]:
            self.add_tree_node(child)

    def add_tree_node(self, node):
        kind = node["type"].upper()
        is_folder = kind == "FOLDER"
        # Playlist names are shown without icons or arrow glyphs.
        prefix = ""
        indent = "   " * max(0, node["depth"] - 1)
        name = node["name"]
        if len(name) > 18:
            name = name[:15] + "…"
        btn = Button(text=indent + prefix + name, size_hint_y=None, height=dp(27),
                     halign="left", valign="middle", padding=(dp(7), 0),
                     background_normal="", background_color=(0.12, 0.14, 0.17, 1) if is_folder else ((0.12, 0.34, 0.23, 1) if self.selected_playlist_id == node.get("id") else (0.085, 0.10, 0.125, 1)),
                     color=self.text_color, font_size="10sp")
        btn.text_size = (None, None)
        if is_folder:
            btn.bind(on_release=lambda _btn, n=node: self.toggle_folder(n))
        else:
            btn._jj_playlist_id = node.get("id")
            btn.bind(on_release=lambda _btn, n=node: self.show_playlist(n))
        self.tree_box.add_widget(btn)
        if is_folder and node["id"] in self.expanded:
            for child in node["children"]:
                self.add_tree_node(child)

    def toggle_folder(self, node):
        if node["id"] in self.expanded:
            self.expanded.remove(node["id"])
        else:
            self.expanded.add(node["id"])
        self.build_tree()

    def show_all_tracks(self):
        if not self.data:
            return
        self.current_playlist = None
        self.selected_playlist_id = "__ALL_TRACKS__"
        self.current_rows = list(self.data["tracks"])
        self.page = 0
        self.playlist_title.text = "All tracks in collection"
        self.build_tree()
        self.apply_search()

    def show_playlist(self, node):
        if not self.data:
            return
        playlist_id = node.get("id")
        self.current_playlist = node
        self.selected_playlist_id = playlist_id
        self._playlist_select_generation += 1
        generation = self._playlist_select_generation
        self.playlist_title.text = f"{node['name']}  •  Loading tracks…"
        # Update the selected color immediately; do not rebuild the playlist tree.
        for widget in self.tree_box.children:
            if isinstance(widget, Button):
                widget_id = getattr(widget, "_jj_playlist_id", None)
                if widget_id is not None:
                    widget.background_color = (0.12, 0.34, 0.23, 1) if widget_id == playlist_id else (0.085, 0.10, 0.125, 1)
        cached = self._playlist_rows_cache.get(playlist_id)
        if cached is not None:
            self._finish_playlist_selection(node, cached[0], cached[1], generation)
            return

        # Resolve large playlists off the UI thread so taps remain responsive.
        data = self.data
        def resolve():
            rows = []
            missing = 0
            tracks = data["tracks"]
            index = data["index"]
            for ref in node["refs"]:
                norm = normalized(playlist_ref_name(ref))
                candidates = index.get(norm, [])
                if not candidates:
                    missing += 1
                    continue
                chosen = candidates[0]
                if len(candidates) > 1:
                    ref_norm = normalized(ref)
                    best_score = -1
                    for idx in candidates:
                        candidate = tracks[idx]
                        score = 1 if normalized(candidate.get("file", "")) in ref_norm else 0
                        folder_bits = [normalized(x) for x in str(candidate.get("dir", "")).split("/") if normalized(x)]
                        score += sum(1 for bit in folder_bits if bit and bit in ref_norm) * 2
                        if score > best_score:
                            best_score = score
                            chosen = idx
                rows.append(tracks[chosen])
            self._playlist_rows_cache[playlist_id] = (rows, missing)
            Clock.schedule_once(lambda _dt: self._finish_playlist_selection(node, rows, missing, generation), 0)
        threading.Thread(target=resolve, daemon=True).start()

    def _finish_playlist_selection(self, node, rows, missing, generation):
        if generation != self._playlist_select_generation:
            return
        self.current_playlist = node
        self.selected_playlist_id = node.get("id")
        self.current_rows = rows
        self.page = 0
        self.playlist_title.text = f"{node['name']}  •  {len(rows):,} tracks" + (f"  •  {missing} unmatched" if missing else "")
        self.apply_search()

    def on_search(self, *_):
        if self.data is not None:
            self.apply_search()

    def apply_search(self):
        query = normalized(self.search.text)
        if not query:
            self.filtered_rows = self.current_rows[:]
        else:
            self.filtered_rows = [t for t in self.current_rows
                                  if query in normalized(t.get("title", "") + t.get("artist", "") + t.get("file", ""))]
        self.page = 0
        self.render_page()

    def draw_table_header(self):
        row = BoxLayout(orientation="horizontal", size_hint=(None, None), width=dp(470), height=dp(28), spacing=dp(1))
        for text, width in [("#", 24), ("ARTIST", 120), ("TITLE", 190), ("BPM", 48), ("KEY", 48)]:
            lab = Label(text=f"[b]{text}[/b]", markup=True, size_hint_x=None, width=dp(width),
                        color=self.accent, font_size="11sp", halign="left", valign="middle")
            lab.bind(size=lambda inst, val: setattr(inst, "text_size", val))
            row.add_widget(lab)
        self.table.add_widget(row)

    def render_page(self):
        """Show a larger initial chunk and load the next chunk before reaching the bottom."""
        self.table.clear_widgets()
        self.draw_table_header()
        self.rendered_count = 0
        self._append_track_batch()
        if hasattr(self, "table_scroll"):
            if not getattr(self, "_lazy_scroll_bound", False):
                self.table_scroll.bind(scroll_y=self._on_track_scroll)
                self._lazy_scroll_bound = True

    def _append_track_batch(self, *_):
        total = len(self.filtered_rows)
        start = getattr(self, "rendered_count", 0)
        # Render a small first batch immediately, then fill the list in short
        # UI-thread batches so selecting a playlist shows tracks quickly.
        end = min(total, start + 80)
        for i in range(start, end):
            track = self.filtered_rows[i]
            row = BoxLayout(orientation="horizontal", size_hint=(None, None), width=dp(430), height=dp(25), spacing=0)
            track_key = (normalized(track.get("file", "")), normalized(track.get("artist", "")), normalized(track.get("title", "")))
            bg = (0.10, 0.34, 0.23, 1) if track_key == self.selected_track_key else ((0.105, 0.12, 0.145, 1) if (i % 2 == 0) else (0.075, 0.09, 0.11, 1))
            with row.canvas.before:
                Color(*bg)
                rect = Rectangle(pos=row.pos, size=row.size)
            row.bind(pos=lambda w, p, r=rect: setattr(r, "pos", p),
                     size=lambda w, s, r=rect: setattr(r, "size", s))
            vals = [str(i + 1), track.get("artist", ""),
                    track.get("title", "") or os.path.splitext(track.get("file", ""))[0],
                    track.get("bpm", ""), track.get("key", "")]
            widths = [24, 120, 190, 48, 48]
            for j, (val, width) in enumerate(zip(vals, widths)):
                if j in (1, 2):
                    cell = TrackTitleButton(
                        text=str(val), size_hint_x=None, width=dp(width),
                        color=self.text_color, font_size="10sp", halign="left", valign="middle",
                        shorten=True, shorten_from="right", background_normal="",
                        background_color=(0, 0, 0, 0), padding=(dp(2), 0),
                        on_tap=lambda t=track: self.on_track_tap(t),
                        on_long_press=lambda t=track: self.show_track_details(t))
                    cell.bind(size=lambda inst, size: setattr(inst, "text_size", size))
                else:
                    cell = Label(text=str(val), size_hint_x=None, width=dp(width), color=self.text_color,
                                 font_size="11sp", halign="left", valign="middle", shorten=True,
                                 shorten_from="right")
                    cell.bind(size=lambda inst, size: setattr(inst, "text_size", size))
                row.add_widget(cell)
            self.table.add_widget(row)
        self.rendered_count = end
        if total:
            self.page_label.text = f"1–{end:,} of {total:,}" if end < total else f"{total:,} tracks"
            # Continue rendering in small slices without blocking the first view.
            if end < total and not getattr(self, "_track_batch_scheduled", False):
                self._track_batch_scheduled = True
                Clock.schedule_once(self._append_track_batch_safe, 0.01)
        else:
            self.page_label.text = "No tracks found"

    def _on_track_scroll(self, _scroll, scroll_y):
        # Prefetch the next batch before the user reaches the end of the visible list.
        if (scroll_y < 0.25
                and getattr(self, "rendered_count", 0) < len(self.filtered_rows)
                and not getattr(self, "_track_batch_scheduled", False)):
            self._track_batch_scheduled = True
            Clock.schedule_once(self._append_track_batch_safe, 0)

    def _append_track_batch_safe(self, *_):
        try:
            self._append_track_batch()
        finally:
            self._track_batch_scheduled = False

    def change_page(self, amount):
        total_pages = max(1, (len(self.filtered_rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page = max(0, min(total_pages - 1, self.page + amount))
        self.render_page()

    def _load_audio_path_cache(self):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.audio_path_cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # Load the index without checking every SD-card path on the UI thread.
            # A path is validated only when its track is selected, avoiding a slow startup.
            self.audio_path_cache = {
                key: [path for path in paths if isinstance(path, str)]
                for key, paths in raw.items() if isinstance(paths, list)
            }
            self.audio_path_cache = {k: v for k, v in self.audio_path_cache.items() if v}
        except Exception:
            self.audio_path_cache = {}

    def _save_audio_path_cache(self):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            temp = self.audio_path_cache_file + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.audio_path_cache, f, ensure_ascii=False)
            os.replace(temp, self.audio_path_cache_file)
        except Exception as exc:
            print("JJ path cache save error:", exc)

    def _audio_scan_roots(self):
        roots = []
        # Discover mounted Android volumes instead of assuming one SD-card UUID.
        for base in ("/storage", "/mnt/media_rw"):
            try:
                for name in os.listdir(base):
                    candidate = os.path.join(base, name)
                    if os.path.isdir(candidate) and name not in ("self", "emulated", "enc_emulated"):
                        roots.append(candidate)
            except Exception:
                pass
        # Keep the known SD-card mount used by the earlier working v4-v7 builds,
        # while also discovering other UUIDs dynamically.
        roots.extend(("/storage/9C33-6BBD", "/storage/emulated/0", "/sdcard"))
        result = []
        seen = set()
        for root in roots:
            try:
                real = os.path.realpath(root)
                if os.path.isdir(root) and real not in seen:
                    seen.add(real)
                    result.append(root)
            except Exception:
                pass
        return result

    def show_analysis_details(self):
        """Show scan/index details on a long press of the Analyze button."""
        tracks = (self.data or {}).get("tracks", [])
        total = len(tracks)
        matched = sum(1 for t in tracks if self.audio_path_cache.get(normalized(basename(t.get("file", "")))))
        missing = [str(t.get("title") or t.get("file") or "Unknown")
                   for t in tracks if not self.audio_path_cache.get(normalized(basename(t.get("file", ""))))]
        roots = self._audio_scan_roots()
        lines = ["Collection Analysis Details", "",
                 f"Status: {'In progress' if self.analysis_running else 'Completed / available'}",
                 f"Tracks in NML: {total}",
                 f"Files found: {matched}",
                 f"Files not found: {max(0, total - matched)}",
                 f"Cached entries: {len(self.audio_path_cache)}",
                 "Folders being checked:"]
        lines.extend(["• " + root for root in roots])
        if missing:
            lines.extend(["", "Examples of tracks not found:"])
            lines.extend(["• " + name for name in missing[:25]])
            if len(missing) > 25:
                lines.append(f"…and {len(missing) - 25}")
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(10))
        details = Label(text="\n".join(lines), color=self.text_color, halign="left", valign="top",
                        size_hint_y=None, font_size="12sp")
        details.bind(width=lambda inst, w: setattr(inst, "text_size", (w, None)),
                     texture_size=lambda inst, size: setattr(inst, "height", size[1]))
        scroll = ScrollView(do_scroll_x=False)
        scroll.add_widget(details)
        content.add_widget(scroll)
        close_btn = Button(text="Close", size_hint_y=None, height=dp(42))
        content.add_widget(close_btn)
        popup = Popup(title="Collection Analysis", content=content, size_hint=(0.92, 0.82))
        close_btn.bind(on_release=popup.dismiss)
        popup.open()

    def analyze_collection_paths(self, *_):
        if self.analysis_running:
            return
        if not self.data or not self.data.get("tracks"):
            self.status.text = "First tap “Import New NML”."
            return
        self.analysis_running = True
        self.audio_scan_generation += 1
        generation = self.audio_scan_generation
        self.analyze_btn.disabled = True
        self.status.text = "Analyzing collection…"
        wanted = {normalized(basename(t.get("file", ""))) for t in self.data["tracks"] if t.get("file")}
        roots = self._audio_scan_roots()
        if not roots:
            self.analysis_running = False
            self.analyze_btn.disabled = False
            self.status.text = "Storage access unavailable. Allow Pydroid 3 to access music/SD card files."
            return

        def worker():
            found = {}
            scanned = 0
            last_report = time.monotonic()
            audio_exts = {".mp3", ".m4a", ".aac", ".wav", ".aif", ".aiff", ".flac", ".ogg", ".opus", ".wma", ".mp4"}
            for root in roots:
                for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
                    dirs[:] = [d for d in dirs if d not in ("Android", ".thumbnails", "LOST.DIR", "$RECYCLE.BIN", ".Trash-1000", "System Volume Information")]
                    for name in files:
                        ext = os.path.splitext(name)[1].lower()
                        if ext not in audio_exts:
                            continue
                        key = normalized(name)
                        if key in wanted:
                            found.setdefault(key, []).append(os.path.join(current, name))
                    scanned += len(files)
                    if time.monotonic() - last_report >= 0.6:
                        Clock.schedule_once(lambda _dt, n=scanned, g=generation: self._collection_scan_progress(g, n), 0)
                        last_report = time.monotonic()
            Clock.schedule_once(lambda _dt, f=found, n=scanned, g=generation: self._finish_collection_analysis(g, f, n), 0)

        threading.Thread(target=worker, daemon=True).start()

    def _collection_scan_progress(self, generation, scanned):
        if generation == self.audio_scan_generation and self.analysis_running:
            self.status.text = "Analyzing collection…"

    def _finish_collection_analysis(self, generation, found, scanned):
        if generation != self.audio_scan_generation:
            return
        # Preserve still-valid prior matches if a storage root was temporarily inaccessible.
        merged = dict(self.audio_path_cache)
        for key, paths in found.items():
            merged[key] = list(dict.fromkeys(paths))
        self.audio_path_cache = {k: v for k, v in merged.items() if v}
        self._save_audio_path_cache()
        self.analysis_running = False
        self.analyze_btn.disabled = False
        matched = sum(1 for t in self.data["tracks"] if self.audio_path_cache.get(normalized(basename(t.get("file", "")))))
        self.status.text = "Collection ready"

    def resolve_audio_path(self, track):
        """Resolve via cached index first; only then try likely paths from NML metadata."""
        file_name = str(track.get("file", "") or "").strip()
        if not file_name:
            return ""
        saved = str(track.get("resolved_path", "") or "")
        if saved and os.path.isfile(saved):
            return saved
        audio_filename = basename(file_name)
        cache_key = normalized(audio_filename)
        cached = self.audio_path_cache.get(cache_key, [])
        if cached:
            directory = str(track.get("dir", "") or "")
            parts = [normalized(part) for part in re.split(r"[:/]+", directory.replace("\\", "/").strip().lstrip("/")) if part not in (".", "..")]
            # Prefer a cached file whose parent path matches the NML directory suffix.
            def score(path):
                parent_parts = [normalized(x) for x in os.path.dirname(path).split(os.sep) if x]
                common = 0
                for a, b in zip(reversed(parts), reversed(parent_parts)):
                    if a != b:
                        break
                    common += 1
                return common
            # Most tracks have one cached path. Test it first instead of sorting
            # and checking every duplicate path on every selection.
            cached_order = list(cached)
            if len(cached_order) > 1:
                cached_order.sort(key=score, reverse=True)
            for candidate in cached_order:
                if os.path.isfile(candidate):
                    track["resolved_path"] = candidate
                    return candidate
        volume = str(track.get("volume", "") or "").strip()
        directory = str(track.get("dir", "") or "")
        # Traktor NML stores folders as components like ":Music:House:".
        parts = [part for part in re.split(r"[:/]+", directory.replace("\\", "/").strip().lstrip("/")) if part not in (".", "..")]
        candidates = []
        volume_clean = volume.strip("/\\")
        if volume_clean.startswith("storage/"):
            candidates.append(os.path.join("/", volume_clean, *parts, audio_filename))
        elif volume_clean and volume_clean not in (".", ".."):
            candidates.append(os.path.join("/storage", volume_clean, *parts, audio_filename))

        # Do not assume a fixed SD-card UUID: Android assigns a different one per card/device.
        roots = self._audio_scan_roots()
        # The v4-v7 versions checked this SD mount explicitly; retain that route.
        roots = list(dict.fromkeys(["/storage/9C33-6BBD"] + roots))
        for root in roots:
            candidates.append(os.path.join(root, *parts, audio_filename))
            candidates.append(os.path.join(root, audio_filename))
        directory_clean = directory.replace("/:", "/").replace("\\", "/").strip()
        if directory_clean.startswith("/"):
            candidates.append(os.path.join(directory_clean, audio_filename))

        for candidate in dict.fromkeys(os.path.normpath(c) for c in candidates if c):
            if os.path.isfile(candidate):
                track["resolved_path"] = candidate
                paths = self.audio_path_cache.setdefault(cache_key, [])
                if candidate not in paths:
                    paths.append(candidate)
                    self._save_audio_path_cache()
                return candidate
        return ""

    def on_track_tap(self, track):
        """A single tap selects the track, loads it into the player, and starts playback."""
        self.last_track_tap = time.monotonic()
        self.last_track_tap_key = track.get("file", "")
        self.select_track(track, autoplay=True)

    def analyze_audio_waveform(self, path, buckets=180):
        """Return real per-segment peak levels using WAV or an installed ffmpeg decoder."""
        if not path or not os.path.isfile(path):
            return None
        try:
            if path.lower().endswith('.wav'):
                with wave.open(path, 'rb') as wf:
                    channels, width, rate, frames = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
                    if frames <= 0 or width not in (1, 2, 3, 4):
                        return None
                    per_bucket = max(1, frames // buckets)
                    values = []
                    for _ in range(buckets):
                        raw = wf.readframes(per_bucket)
                        if not raw:
                            break
                        step = width * channels
                        peak = 0.0
                        for off in range(0, len(raw) - step + 1, step):
                            chunk = raw[off:off + width]
                            if width == 1:
                                sample = (chunk[0] - 128) / 128.0
                            else:
                                sample = int.from_bytes(chunk, 'little', signed=True) / float(1 << (8 * width - 1))
                            peak = max(peak, abs(sample))
                        values.append(peak)
                    return values or None
            ffmpeg = shutil.which('ffmpeg')
            if ffmpeg:
                proc = subprocess.run([ffmpeg, '-v', 'error', '-i', path, '-f', 'f32le', '-ac', '1', '-ar', '4000', 'pipe:1'],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
                raw = proc.stdout
                if proc.returncode != 0 or len(raw) < 4:
                    return None
                count = len(raw) // 4
                values = []
                for b in range(buckets):
                    start = b * count // buckets
                    end = max(start + 1, (b + 1) * count // buckets)
                    peak = 0.0
                    for (sample,) in struct.iter_unpack('<f', raw[start * 4:min(end, count) * 4]):
                        peak = max(peak, abs(sample))
                    values.append(min(1.0, peak))
                return values
        except Exception:
            return None
        return None

    def request_waveform_analysis(self, track):
        path = self.resolve_audio_path(track)
        if not path:
            return
        if path in self.waveform_cache:
            self.waveform.set_samples(self.waveform_cache[path])
            return
        def worker():
            samples = self.analyze_audio_waveform(path)
            def finish(_dt):
                self.waveform_cache[path] = samples
                if self.current_track is track and hasattr(self, 'waveform'):
                    self.waveform.set_samples(samples)
                    if samples is None:
                        self.player_status.text = 'Waveform analysis failed: a WAV file or available FFmpeg decoder is required.'
            Clock.schedule_once(finish, 0)
        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _format_clock(seconds):
        seconds = max(0, int(seconds or 0))
        minutes, secs = divmod(seconds, 60)
        return f"{minutes:02d}:{secs:02d}"

    def _update_track_info(self, position=None):
        """Refresh the compact live metadata line without changing the title ticker."""
        if not self.track_info:
            return
        track = self.current_track
        if not track:
            self.track_info.text = "BPM: —  •  Time: 00:00  •  Remaining: —  •  Playlist: —"
            return
        duration = float(track.get("duration", 0) or 0)
        if position is None:
            position = self.paused_position
            if self.current_sound and getattr(self.current_sound, "state", "") == "play":
                try:
                    position = float(self.current_sound.get_pos() or position)
                except Exception:
                    pass
        position = max(0.0, min(float(position or 0), duration if duration > 0 else float(position or 0)))
        remaining = max(0.0, duration - position) if duration > 0 else 0
        bpm = track.get("bpm") or "—"
        playlists = self._playlist_names_for_track(track)
        selected_name = (self.current_playlist or {}).get("name", "") if isinstance(self.current_playlist, dict) else ""
        if selected_name and selected_name in playlists:
            playlist_text = selected_name
        elif playlists:
            playlist_text = ", ".join(playlists[:3])
        else:
            playlist_text = "Not found in NML"
        self.track_info.text = (f"BPM: {bpm}  •  Time: {self._format_clock(position)}"
                                f"  •  Remaining: -{self._format_clock(remaining)}"
                                f"  •  Playlist: {playlist_text}")

    def select_track(self, track, autoplay=False):
        self.current_track = track
        self.selected_track_key = (normalized(track.get("file", "")), normalized(track.get("artist", "")), normalized(track.get("title", "")))
        # Repaint rows immediately so the tapped track remains highlighted.
        if hasattr(self, "table"):
            self.render_page()
        title = track.get("title") or track.get("file") or "Unknown track"
        artist = track.get("artist", "")
        self.player_status.text = ((artist + " — ") if artist else "") + title[:120]
        self._update_track_info(0.0)
        if hasattr(self, "waveform"):
            self.waveform.set_track(track)
            self.request_waveform_analysis(track)
        cues = track.get("cues", [])
        cue_count = sum(1 for c in cues if str(c.get("type", "0")) == "0" and c.get("hotcue", "-1") not in ("-1", ""))
        loop_count = sum(1 for c in cues if str(c.get("type", "0")) == "5")
        duration = float(track.get("duration", 0) or 0)
        mins, secs = divmod(int(duration), 60)
        self.cue_summary.text = f"{mins:02d}:{secs:02d}   •   Cues: {cue_count}   •   Loops: {loop_count}   •   BPM: {track.get('bpm') or '—'}   •   Key: {track.get('key') or '—'}"
        if autoplay:
            self.start_track(track)

    def seek_to(self, seconds):
        """Seek to a waveform position and prevent stale backend position from snapping back."""
        if not self.current_track:
            self.player_status.text = "Select a track first."
            return
        duration = float((self.current_track or {}).get("duration", 0) or 0)
        target = max(0.0, min(float(seconds), duration if duration > 0 else float(seconds)))
        if not self.current_sound or not self.current_sound_path:
            self.start_track(self.current_track)
            Clock.schedule_once(lambda _dt: self._finish_seek(target), 0.8)
            self.waveform.set_position(target)
            return
        try:
            # Kivy backends can briefly report the old position after a seek.
            self.current_sound.seek(target)
            self.paused_position = target
            self.seek_override_until = time.monotonic() + 1.0
            self.waveform.set_position(target)
            self._update_track_info(target)
        except Exception:
            # Fallback: restart the sound and try seeking once playback is active.
            try:
                self.current_sound.stop()
                self.current_sound.play()
                self.paused_position = target
                self.seek_override_until = time.monotonic() + 1.0
                Clock.schedule_once(lambda _dt: self._finish_seek(target), 0.25)
                self.waveform.set_position(target)
                self._update_track_info(target)
            except Exception:
                self.player_status.text = "Seeking is not supported by this audio backend."

    def _finish_seek(self, target):
        try:
            if self.current_sound:
                self.current_sound.seek(max(0.0, float(target)))
                self.paused_position = float(target)
                self.waveform.set_position(target)
        except Exception:
            pass

    def update_playback_position(self, _dt):
        sound = self.current_sound
        if not sound or not self.current_track:
            return
        try:
            now = time.monotonic()
            if now - self._last_track_info_update >= 1.0:
                self._last_track_info_update = now
                self._update_track_info()
            if sound.state == "play":
                if time.monotonic() >= self.seek_override_until:
                    pos = float(sound.get_pos() or 0.0)
                    self.paused_position = pos
                    self.waveform.set_position(pos)
            elif sound.state == "stop" and self.play_btn.text == "PAUSE":
                # Only a real stop/completion advances; a paused/loading player does not.
                Clock.schedule_once(lambda _d: self.play_adjacent_track(1), 0.15)
                self.play_btn.text = "PLAY"
        except Exception:
            pass

    def show_track_details(self, track):
        """Long-press a track to see its details without cluttering the main screen."""
        title = track.get("title") or os.path.splitext(track.get("file", ""))[0] or "—"
        artist = track.get("artist") or "—"
        duration = float(track.get("duration", 0) or 0)
        mins, secs = divmod(int(duration), 60)
        cues = track.get("cues", [])
        cue_count = sum(1 for c in cues if str(c.get("type", "0")) == "0" and c.get("hotcue", "-1") not in ("-1", ""))
        loop_count = sum(1 for c in cues if str(c.get("type", "0")) == "5")
        path = self.resolve_audio_path(track) or "Not located yet"
        playlists = self._playlist_names_for_track(track)
        playlist_text = ", ".join(playlists) if playlists else "Not found in NML"
        message = (f"Playlist: {playlist_text}\nArtist: {artist}\nTitle: {title}\nBPM: {track.get('bpm') or '—'}\n"
                   f"Key: {track.get('key') or '—'}\nDuration: {mins:02d}:{secs:02d}\n"
                   f"CUE: {cue_count}  •  LOOP: {loop_count}\nFile: {path}")
        content = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))
        info = Label(text=message, color=self.text_color, halign="left", valign="top", font_size="12sp")
        info.bind(size=lambda inst, val: setattr(inst, "text_size", val))
        content.add_widget(info)
        close_btn = Button(text="Close", size_hint_y=None, height=dp(42))
        content.add_widget(close_btn)
        popup = Popup(title="Track Details", content=content, size_hint=(0.92, 0.72))
        close_btn.bind(on_release=popup.dismiss)
        popup.open()

    def _playlist_names_for_track(self, track):
        """Return playlist names containing this track, in NML tree order."""
        if not self.data:
            return []
        target = normalized(track.get("file", ""))
        matches = []
        def walk(node):
            if node.get("refs"):
                for ref in node["refs"]:
                    if normalized(playlist_ref_name(ref)) == target:
                        matches.append(node.get("name", "Playlist"))
                        break
            for child in node.get("children", []):
                walk(child)
        walk(self.data.get("playlist_tree", {}))
        return list(dict.fromkeys(matches))

    def jump_saved_marker(self, marker_type, direction):
        """Move one saved CUE or LOOP at a time in chronological order."""
        track = self.current_track
        if not track:
            self.player_status.text = "Select a track first."
            return

        markers = []
        for cue in track.get("cues", []):
            ctype = str(cue.get("type", "0"))
            is_cue = ctype == "0" and cue.get("hotcue", "-1") not in ("-1", "")
            is_loop = ctype == "5"
            if marker_type == "all":
                matches = is_cue or is_loop
            elif marker_type == "cue":
                matches = is_cue
            else:
                matches = is_loop
            if not matches:
                continue
            try:
                pos = float(cue.get("start", 0) or 0)
                duration = float(track.get("duration", 0) or 0)
                # Traktor NML usually stores cue positions in milliseconds; normalize to seconds.
                if duration > 0 and pos > duration * 10:
                    pos /= 1000.0
                markers.append((max(0.0, pos), cue, "LOOP" if is_loop else "CUE"))
            except (TypeError, ValueError):
                continue

        # Stable chronological order; if two markers share a position, preserve NML order.
        markers.sort(key=lambda item: item[0])
        if not markers:
            # PREV CUE with no saved markers returns to the beginning of the track.
            if direction < 0 and marker_type in ("all", "cue"):
                cursor = getattr(self, "_saved_marker_cursor", None)
                if isinstance(cursor, dict):
                    cursor.pop(("all" if marker_type == "all" else marker_type, str(track.get("file", ""))), None)
                self.player_status.text = "Track start"
                self.seek_to(0.0)
                return
            if marker_type == "all":
                self.player_status.text = "No saved CUE or LOOP markers."
            else:
                self.player_status.text = "No saved " + ("CUE" if marker_type == "cue" else "LOOP") + " markers."
            return

        # Use a shared cursor for CUE and LOOP so navigation never skips between types.
        track_key = str(track.get("file", ""))
        cursor_key = ("all" if marker_type == "all" else marker_type, track_key)
        cursor = getattr(self, "_saved_marker_cursor", None)
        if not isinstance(cursor, dict):
            cursor = {}
            self._saved_marker_cursor = cursor
        current_index = cursor.get(cursor_key)

        if current_index is None or current_index < 0 or current_index >= len(markers):
            if time.monotonic() < getattr(self, "seek_override_until", 0):
                current_pos = float(getattr(self, "paused_position", 0.0) or 0.0)
            else:
                try:
                    current_pos = float(self.current_sound.get_pos() or 0.0) if self.current_sound else float(getattr(self, "paused_position", 0.0) or 0.0)
                except Exception:
                    current_pos = float(getattr(self, "paused_position", 0.0) or 0.0)
            positions = [item[0] for item in markers]
            # If already on a marker, NEXT/PREV moves to its immediate neighbor.
            nearest_index = min(range(len(positions)), key=lambda i: abs(positions[i] - current_pos))
            if abs(positions[nearest_index] - current_pos) <= 1.5:
                current_index = nearest_index
            else:
                current_index = None

        if current_index is None:
            positions = [item[0] for item in markers]
            if direction > 0:
                target_index = next((i for i, pos in enumerate(positions) if pos > current_pos + 0.08), None)
            else:
                target_index = next((i for i in range(len(positions)-1, -1, -1) if positions[i] < current_pos - 0.08), None)
        else:
            target_index = current_index + (1 if direction > 0 else -1)
            if target_index < 0 or target_index >= len(markers):
                target_index = None

        if target_index is None:
            self.player_status.text = "No " + ("next" if direction > 0 else "previous") + (" CUE or LOOP." if marker_type == "all" else (" CUE." if marker_type == "cue" else " LOOP."))
            return

        cursor[cursor_key] = target_index
        target_pos, _target_marker, target_type = markers[target_index]
        self.player_status.text = f"{target_type} • {target_pos:.2f}s"
        self.seek_to(target_pos)

    def show_track_playlists(self, track):
        """List every playlist containing a track, matching its filename."""
        if not self.data:
            return
        target = normalized(track.get("file", ""))
        matches = []
        def walk(node):
            if node.get("refs"):
                for ref in node["refs"]:
                    if normalized(playlist_ref_name(ref)) == target:
                        matches.append(node.get("name", "Playlist"))
                        break
            for child in node.get("children", []):
                walk(child)
        walk(self.data.get("playlist_tree", {}))
        unique = list(dict.fromkeys(matches))
        message = "Found in playlists:\n\n" + "\n".join("• " + name for name in unique) if unique else "No matching playlist found in NML."
        Popup(title="Where is this track?", content=Label(text=message, color=self.text_color),
              size_hint=(0.88, 0.7)).open()

    def _find_audio_path_fallback(self, track):
        """Last-resort filename search. Runs in a worker only when normal path resolution fails."""
        file_name = str(track.get("file", "") or "").strip()
        if not file_name:
            return ""
        target = normalized(basename(file_name))
        # Try paths reconstructed from Traktor's original VOLUME and DIR first.
        volume = str(track.get("volume", "") or "").strip().strip("/\\")
        parts = [part for part in re.split(r"[:/]+", str(track.get("dir", "") or "").replace("\\", "/").strip().lstrip("/")) if part not in (".", "..")]
        direct = []
        if volume:
            if volume.startswith("storage/"):
                direct.append(os.path.join("/", volume, *parts, basename(file_name)))
            else:
                direct.append(os.path.join("/storage", volume, *parts, basename(file_name)))
        for root in self._audio_scan_roots():
            direct.extend((os.path.join(root, *parts, basename(file_name)), os.path.join(root, basename(file_name))))
        for candidate in dict.fromkeys(os.path.normpath(c) for c in direct):
            if os.path.isfile(candidate):
                return candidate
        # Search mounted storage for the exact normalized filename, pruning large system folders.
        excluded = {"android", ".thumbnails", "lost.dir", "$recycle.bin", ".trash-1000", "system volume information"}
        for root in self._audio_scan_roots():
            for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
                dirs[:] = [d for d in dirs if d.casefold() not in excluded]
                for name in files:
                    if normalized(name) == target:
                        candidate = os.path.join(current, name)
                        if os.path.isfile(candidate):
                            return candidate
        return ""

    def _finish_audio_path_fallback(self, track, cache_key, path):
        self._fallback_searching.discard(cache_key)
        if path:
            track["resolved_path"] = path
            self.audio_path_cache[cache_key] = [path]
            self._save_audio_path_cache()
            # Start the same selected track now that its actual path is known.
            if self.current_track is track:
                self.start_track(track)
        else:
            title = track.get("title") or track.get("file") or "Unknown track"
            if self.current_track is track:
                self.play_btn.text = "PLAY"
                self.player_status.text = str(title)[:55] + " | Not found. Check SD card access permissions."

    def start_track(self, track=None):
        if track is not None:
            self.current_track = track
        if not self.current_track:
            self.player_status.text = "Select a track first."
            return
        title = self.current_track.get("title") or os.path.splitext(self.current_track.get("file", ""))[0] or "Unknown track"
        artist = self.current_track.get("artist", "")
        display_title = ((artist + " — ") if artist else "") + title
        path = self.resolve_audio_path(self.current_track)
        if not path:
            cache_key = normalized(basename(self.current_track.get("file", "")))
            if cache_key not in self._fallback_searching:
                self._fallback_searching.add(cache_key)
                self.player_status.text = display_title[:55] + " | Searching for file…"
                self.play_btn.text = "…"
                track_to_find = self.current_track
                def search_worker():
                    try:
                        found_path = self._find_audio_path_fallback(track_to_find)
                    except Exception:
                        found_path = ""
                    Clock.schedule_once(lambda _dt, t=track_to_find, k=cache_key, p=found_path: self._finish_audio_path_fallback(t, k, p), 0)
                threading.Thread(target=search_worker, daemon=True).start()
            else:
                self.player_status.text = display_title[:55] + " | Searching for file…"
            return
        # Invalidate older preparation jobs if the user quickly selects another track.
        self._play_generation = getattr(self, "_play_generation", 0) + 1
        generation = self._play_generation
        self.player_status.text = "Loading audio… " + display_title[:80]
        self.play_btn.text = "…"
        old_sound = self.current_sound
        self.current_sound = None
        if old_sound:
            try:
                old_sound.stop()
                if hasattr(old_sound, "unload"):
                    old_sound.unload()
            except Exception:
                pass

        def worker():
            sound = None
            errors = []
            try:
                sound = AndroidMediaSound(path)  # MediaPlayer.prepare() runs off the UI thread.
            except Exception as exc:
                errors.append(str(exc))
                try:
                    sound = SoundLoader.load(path)
                except Exception as fallback_exc:
                    errors.append(str(fallback_exc))
            Clock.schedule_once(lambda _dt, g=generation, snd=sound, p=path, label=display_title, err="; ".join(errors):
                                self._finish_track_prepare(g, snd, p, label, err), 0)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_track_prepare(self, generation, sound, path, display_title, error):
        if generation != getattr(self, "_play_generation", 0):
            if sound and hasattr(sound, "unload"):
                try: sound.unload()
                except Exception: pass
            return
        if sound is None:
            self.play_btn.text = "PLAY"
            self.player_status.text = display_title[:55] + " | Could not open audio: " + (error[:65] or "unknown error")
            return
        try:
            self.current_sound = sound
            self.current_sound_path = path
            self.paused_position = 0.0
            sound.play()
            self.play_btn.text = "PAUSE"
            self.player_status.text = display_title[:110]
        except Exception as exc:
            try:
                sound.unload()
            except Exception:
                pass
            self.current_sound = None
            self.play_btn.text = "PLAY"
            self.player_status.text = display_title[:55] + " | Error: " + str(exc)[:65]

    def toggle_playback(self):
        if self.current_sound and self.current_sound.state == "play":
            try:
                self.paused_position = float(self.current_sound.get_pos() or 0.0)
                if hasattr(self.current_sound, "pause"):
                    self.current_sound.pause()
                else:
                    self.current_sound.stop()
                self.play_btn.text = "PLAY"
                title = (self.current_track or {}).get("title", "")
                self.player_status.text = title[:95] if title else "Paused"
            except Exception:
                self.stop_playback()
            return
        if self.current_sound and self.current_sound_path and os.path.isfile(self.current_sound_path):
            try:
                if self.paused_position > 0 and not hasattr(self.current_sound, "pause"):
                    self.current_sound.seek(self.paused_position)
                self.current_sound.play()
                self.play_btn.text = "PAUSE"
                title = (self.current_track or {}).get("title", "")
                self.player_status.text = title[:100] if title else "Playing"
                return
            except Exception:
                pass
        self.start_track()

    def stop_playback(self):
        if self.current_sound:
            try:
                self.current_sound.stop()
            except Exception:
                pass
        self.paused_position = 0.0
        self.play_btn.text = "PLAY"
        if self.current_track:
            title = self.current_track.get("title") or self.current_track.get("file", "")
            self.player_status.text = "Stopped: " + title[:85]
        else:
            self.player_status.text = "Select a track and press Play"

    def play_adjacent_track(self, direction):
        if not self.filtered_rows:
            self.player_status.text = "There are no tracks in the list."
            return
        try:
            current_index = next(i for i, t in enumerate(self.filtered_rows) if t is self.current_track)
        except StopIteration:
            current_file = normalized(basename((self.current_track or {}).get("file", "")))
            current_index = next((i for i, t in enumerate(self.filtered_rows)
                                  if normalized(basename(t.get("file", ""))) == current_file and current_file),
                                 -1 if direction > 0 else 0)
        new_index = current_index + direction
        if new_index < 0 or new_index >= len(self.filtered_rows):
            self.player_status.text = "There is no more track in this direction."
            return
        self.select_track(self.filtered_rows[new_index], autoplay=True)

    def open_import_dialog(self, *_):
        start_path = "/storage"
        try:
            if self.data and os.path.isdir(os.path.dirname(self.data.get("path", ""))):
                start_path = os.path.dirname(self.data["path"])
        except Exception:
            pass
        chooser = FileChooserListView(path=start_path, filters=["*.nml", "*.NML"], dirselect=False,
                                     multiselect=False)
        layout = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(6))
        layout.add_widget(Label(text="Select the new collection.nml from your device or SD card.",
                                size_hint_y=None, height=dp(42), color=self.text_color))
        layout.add_widget(chooser)
        buttons = BoxLayout(size_hint_y=None, height=dp(42), spacing=dp(6))
        cancel = Button(text="Cancel")
        use_file = Button(text="Load Selected", background_normal="",
                          background_color=self.accent, color=(0.08, 0.08, 0.08, 1), bold=True)
        buttons.add_widget(cancel)
        buttons.add_widget(use_file)
        layout.add_widget(buttons)
        popup = Popup(title="Import New NML", content=layout, size_hint=(0.95, 0.9))
        cancel.bind(on_release=popup.dismiss)

        def choose_file(*_):
            if not chooser.selection:
                self.status.text = "No file selected."
                return
            path = chooser.selection[0]
            if not path.lower().endswith(".nml"):
                self.status.text = "Select a file with the .nml extension."
                return
            popup.dismiss()
            self.load_collection(path)

        use_file.bind(on_release=choose_file)
        popup.open()


if __name__ == "__main__":
    TraktorBrowser().run()
