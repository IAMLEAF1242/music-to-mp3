# Music to MP3 – Android

Kivy port of `music_to_mp3_v3.py`. Same song matching (Topic recordings first, then
audio/lyric uploads, music video only as a last resort), same Apple Music / Spotify
link reading, same Song / Playlist toggle and optional Artist field.

**This project was written but never built or run on a phone** (the environment it
was written in cannot reach the Android SDK). Expect to fix small things on the
first build.

## Build (Linux or Windows WSL2)

```bash
sudo apt install -y git zip unzip openjdk-17-jdk python3-pip autoconf libtool pkg-config \
    zlib1g-dev libncurses5-dev libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev
pip install --user buildozer cython
cd music_to_mp3_android
buildozer android debug          # first run downloads the SDK/NDK; takes a while
```

The APK appears in `bin/`. Install it with `adb install bin/*.apk` or copy it to the
phone and open it (allow "install unknown apps").

## "Save to" works like this

Android does not let apps write to arbitrary folders. **Browse...** opens the system
folder picker; the app gets permission for the folder you choose (and keeps it after
restarts). Songs are downloaded to the app's private space, then copied into that
folder. Playlists go in a subfolder named after the playlist when the checkbox is on.
The field starts empty and Download asks for a folder if you skip it.

## MP3 conversion needs ffmpeg

A phone has no ffmpeg. To get real MP3s:

1. Get a static **arm64 Android** ffmpeg executable.
2. Save it as `libs/arm64-v8a/libffmpeg.so` in this folder.
3. Uncomment `android.add_libs_arm64_v8a` in `buildozer.spec` and rebuild.

(It is named `.so` because Android only lets apps run programs from the native
library folder.) Without it, the app saves the original audio as M4A and says so
in the log.

## Known risks

- **YouTube JavaScript runtime.** Recent yt-dlp often wants Deno/Node to read YouTube.
  Phones have neither, so some videos may fail or only offer fewer formats. If every
  song fails, this is the first suspect.
- **yt-dlp ages.** It is frozen into the APK. Rebuild with the latest version when
  downloads break.
- **Background limits.** Android may pause downloads when the screen is off or the
  app is in the background. Keep the app open for big playlists.
- **Desktop testing.** `pip install kivy yt-dlp certifi` then `python main.py` runs the
  same app on a computer (folder picker is a plain file dialog there).
- Not ported: loading a `.txt` list file, `--preview`, `--channel`, and the
  `failed_tracks.txt` files (failures show in the track list and log).
