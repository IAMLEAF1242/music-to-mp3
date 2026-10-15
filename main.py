"""Music to MP3 - Android (Kivy) version of music_to_mp3_v3.py."""

import os
import threading
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.utils import escape_markup, platform

import core
import storage

KV = """
BoxLayout:
    orientation: "vertical"
    padding: dp(12)
    spacing: dp(8)

    BoxLayout:
        size_hint_y: None
        height: dp(44)
        spacing: dp(8)
        ToggleButton:
            text: "Song"
            group: "mode"
            state: "down"
            allow_no_selection: False
            on_state: if self.state == "down": app.set_mode("song")
        ToggleButton:
            text: "Playlist"
            group: "mode"
            allow_no_selection: False
            on_state: if self.state == "down": app.set_mode("playlist")

    Label:
        id: src_label
        text: "Song name"
        size_hint_y: None
        height: dp(22)
        halign: "left"
        text_size: self.size
    TextInput:
        id: src
        multiline: False
        write_tab: False
        size_hint_y: None
        height: dp(44)

    BoxLayout:
        id: artist_box
        orientation: "vertical"
        size_hint_y: None
        height: dp(68)
        Label:
            text: "Artist (optional)"
            size_hint_y: None
            height: dp(22)
            halign: "left"
            text_size: self.size
        TextInput:
            id: artist
            multiline: False
            write_tab: False

    Label:
        text: "Save to"
        size_hint_y: None
        height: dp(22)
        halign: "left"
        text_size: self.size
    BoxLayout:
        size_hint_y: None
        height: dp(44)
        spacing: dp(8)
        TextInput:
            id: dst
            readonly: True
            multiline: False
            hint_text: "Choose a folder"
        Button:
            text: "Browse..."
            size_hint_x: None
            width: dp(110)
            on_release: app.browse()

    BoxLayout:
        id: sub_row
        size_hint_y: None
        height: dp(34)
        CheckBox:
            id: sub
            active: True
            size_hint_x: None
            width: dp(40)
        Label:
            text: "Playlist in its own subfolder"
            halign: "left"
            text_size: self.size
            valign: "middle"
    BoxLayout:
        id: redo_row
        size_hint_y: None
        height: dp(34)
        CheckBox:
            id: redo
            size_hint_x: None
            width: dp(40)
        Label:
            text: "Re-download finished tracks"
            halign: "left"
            text_size: self.size
            valign: "middle"
    BoxLayout:
        size_hint_y: None
        height: dp(34)
        CheckBox:
            id: strict
            size_hint_x: None
            width: dp(40)
        Label:
            text: "Strict audio (no music-video fallback)"
            halign: "left"
            text_size: self.size
            valign: "middle"

    BoxLayout:
        id: conc_row
        orientation: "vertical"
        size_hint_y: None
        height: dp(58)
        Label:
            id: conc_label
            text: "Concurrent downloads: 3"
            halign: "left"
            text_size: self.size
        Slider:
            id: conc
            min: 1
            max: 8
            step: 1
            value: 3
            on_value: app.on_conc(self.value)

    BoxLayout:
        size_hint_y: None
        height: dp(48)
        spacing: dp(8)
        Button:
            id: go
            text: "Download"
            on_release: app.start(False)
        Button:
            id: lst
            text: "List only"
            on_release: app.start(True)
        Button:
            id: halt
            text: "Stop"
            disabled: True
            on_release: app.stop_run()

    ProgressBar:
        id: bar
        max: 1
        value: 0
        size_hint_y: None
        height: dp(8)
    Label:
        id: status
        text: "Ready"
        size_hint_y: None
        height: dp(22)
        halign: "left"
        text_size: self.size

    ScrollView:
        size_hint_y: 0.55
        GridLayout:
            id: tracks
            cols: 1
            size_hint_y: None
            height: self.minimum_height
            spacing: dp(2)
    ScrollView:
        id: log_scroll
        size_hint_y: 0.45
        Label:
            id: log
            markup: True
            text: ""
            size_hint_y: None
            height: self.texture_size[1]
            text_size: self.width, None
            halign: "left"
            valign: "top"
            font_size: "12sp"
"""

COLORS = {"done": "1a7f37", "failed": "cf222e", "skipped": "8a9199"}


class MusicApp(App):
    title = "Music to MP3"

    def build(self):
        Window.softinput_mode = "below_target"
        self.mode = "song"
        self.location = ""
        self.running = False
        self.limiter = None
        self.rows = {}
        self.log_lines = []
        self.counts = ""
        self.root_widget = Builder.load_string(KV)
        self.ids_ = self.root_widget.ids
        if platform == "android":
            os.environ["ANDROID_NATIVE_LIB_DIR"] = storage.native_lib_dir()
        core.SINK = lambda m, err=False: Clock.schedule_once(lambda dt: self.add_log(m, err))
        core.STATUS = lambda n, s, k="": Clock.schedule_once(lambda dt: self.set_status(n, s, k))
        core.PROGRESS = lambda r, t: Clock.schedule_once(lambda dt: self.set_progress(dict(r), t))
        return self.root_widget

    def on_start(self):
        self.set_mode("song")

    def on_stop(self):
        core.stop()

    # ---- mode / layout ----------------------------------------------------
    def set_mode(self, mode):
        if not hasattr(self, "ids_"):
            return
        self.mode = mode
        ids, playlist = self.ids_, mode == "playlist"
        ids.src_label.text = "Playlist link" if playlist else "Song name"
        ids.src.hint_text = ("Apple Music or Spotify link" if playlist
                             else "e.g. Feel Good Inc.")
        self._show(ids.artist_box, not playlist, dp(68))
        self._show(ids.sub_row, playlist, dp(34))
        self._show(ids.redo_row, playlist, dp(34))
        self._show(ids.conc_row, playlist, dp(58))
        ids.lst.disabled = self.running or not playlist
        ids.lst.opacity = 1 if playlist else 0

    @staticmethod
    def _show(widget, visible, height):
        widget.height = height if visible else 0
        widget.opacity = 1 if visible else 0
        widget.disabled = not visible

    def on_conc(self, value):
        n = int(value)
        self.ids_.conc_label.text = f"Concurrent downloads: {n}"
        if self.limiter:
            self.limiter.set(n)

    # ---- save-to folder -----------------------------------------------------
    def browse(self):
        if platform == "android":
            storage.pick_folder(lambda loc: Clock.schedule_once(lambda dt: self.set_location(loc)))
        else:
            self._desktop_picker()

    def set_location(self, loc):
        if loc:
            self.location = loc
            self.ids_.dst.text = storage.describe(loc)

    def _desktop_picker(self):
        chooser = FileChooserListView(dirselect=True, path=str(Path.home()))
        box = BoxLayout(orientation="vertical", spacing=dp(6))
        box.add_widget(chooser)
        pick = Button(text="Use this folder", size_hint_y=None, height=dp(44))
        box.add_widget(pick)
        popup = Popup(title="Choose a folder", content=box, size_hint=(0.9, 0.9))

        def choose(*_):
            target = chooser.selection[0] if chooser.selection else chooser.path
            if os.path.isfile(target):
                target = os.path.dirname(target)
            self.set_location(target)
            popup.dismiss()

        pick.bind(on_release=choose)
        popup.open()

    # ---- running ------------------------------------------------------------
    def start(self, listing):
        if self.running:
            return
        ids = self.ids_
        raw = ids.src.text.strip().strip('"')
        playlist = self.mode == "playlist"
        if not raw:
            self.alert("Enter a playlist link first." if playlist else "Enter a song name first.")
            return
        if not listing and not self.location:
            self.alert("Choose a 'Save to' folder first.")
            return
        args = dict(raw=raw, mode=self.mode, listing=listing,
                    artist="" if playlist else ids.artist.text.strip(),
                    location=self.location, sub=ids.sub.active, redo=ids.redo.active,
                    strict=ids.strict.active)
        self.running = True
        self.limiter = core.Limiter(int(ids.conc.value))
        core.CANCEL.clear()
        ids.go.disabled = ids.lst.disabled = True
        ids.halt.disabled = False
        ids.bar.value = 0
        ids.status.text = "Working..."
        self.counts = ""
        threading.Thread(target=self._job, args=(args,), daemon=True).start()

    def stop_run(self):
        if self.running:
            self.ids_.status.text = "Stopping..."
            core.stop()

    def _job(self, a):
        code = 1
        try:
            workdir = Path(self.user_data_dir) / "work"
            state = Path(self.user_data_dir) / "state"
            got = core.read_source(a["raw"])
            if got is None and a["mode"] == "playlist":
                raise core.PlaylistError("Enter an Apple Music or Spotify playlist link.")
            if got is None:
                Clock.schedule_once(lambda dt: self.show_tracks("song", [(a["raw"], a["artist"])]))
                target = storage.Target(a["location"], "", state)
                code = core.download_single(a["raw"], a["artist"], target, workdir, a["strict"])
            else:
                name, tracks = got
                Clock.schedule_once(lambda dt: self.show_tracks(name, tracks))
                core.emit(f"Playlist: {name} ({len(tracks)} tracks)")
                if a["listing"]:
                    code = 0
                else:
                    sub = core.safe_name(name) if a["sub"] else ""
                    target = storage.Target(a["location"], sub, state)
                    code = core.download_all(tracks, target, workdir, a["strict"],
                                             a["redo"], self.limiter)
        except core.Cancelled:
            code = 130
            core.emit("Cancelled.")
        except (core.PlaylistError, OSError) as exc:
            core.emit("Error: " + str(exc), True)
            Clock.schedule_once(lambda dt: self.alert(str(exc)))
        except Exception:
            import traceback
            core.emit(traceback.format_exc(), True)
        finally:
            Clock.schedule_once(lambda dt: self.finished(code))

    def finished(self, code):
        self.running = False
        self.limiter = None
        ids = self.ids_
        ids.go.disabled = False
        ids.halt.disabled = True
        self.set_mode(self.mode)
        word = {0: "Finished", 130: "Cancelled"}.get(code, "Finished with errors")
        ids.status.text = word + ("   " + self.counts if self.counts else "")

    # ---- display ------------------------------------------------------------
    def alert(self, text):
        label = Label(text=text, text_size=(dp(280), None), halign="center")
        Popup(title="Music to MP3", content=label, size_hint=(0.9, 0.4)).open()

    def show_tracks(self, name, tracks):
        grid = self.ids_.tracks
        grid.clear_widgets()
        self.rows = {}
        for i, (title, artist) in enumerate(tracks, 1):
            base = escape_markup(f"{i}. {artist + ' - ' if artist else ''}{title}")
            label = Label(markup=True, size_hint_y=None, height=dp(40), halign="left",
                          valign="middle", font_size="13sp")
            label.bind(width=lambda w, v: setattr(w, "text_size", (v, None)))
            label.text = base + "\n[color=8a9199]Waiting[/color]"
            grid.add_widget(label)
            self.rows[i] = (label, base)
        self.ids_.bar.max = max(1, len(tracks))
        self.ids_.bar.value = 0

    def set_status(self, n, text, kind):
        row = self.rows.get(n)
        if row:
            label, base = row
            color = COLORS.get(kind, "dddddd")
            label.text = f"{base}\n[color={color}]{escape_markup(text)}[/color]"

    def set_progress(self, r, total):
        done = r["saved"] + r["skipped"] + r["failed"]
        self.ids_.bar.max = max(1, total)
        self.ids_.bar.value = done
        self.counts = f"{done}/{total}  saved {r['saved']}  skipped {r['skipped']}  failed {r['failed']}"
        self.ids_.status.text = self.counts

    def add_log(self, msg, err):
        line = escape_markup(msg)
        self.log_lines.append(f"[color=cf222e]{line}[/color]" if err else line)
        del self.log_lines[:-300]
        self.ids_.log.text = "\n".join(self.log_lines)
        Clock.schedule_once(lambda dt: setattr(self.ids_.log_scroll, "scroll_y", 0), 0.05)


if __name__ == "__main__":
    MusicApp().run()
