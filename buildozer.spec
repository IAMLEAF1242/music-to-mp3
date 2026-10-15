[app]
title = Music to MP3
package.name = musictomp3
package.domain = org.example
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 1.0

# yt-dlp is frozen into the APK at build time. YouTube changes often, so rebuild
# with the newest yt-dlp (drop the pin below) when downloads start failing.
requirements = python3,kivy==2.3.1,pyjnius,android,openssl,certifi,yt-dlp

orientation = portrait
fullscreen = 0

# Only internet access is needed. Saving uses the folder picker, so there are
# no storage permissions.
android.permissions = INTERNET,WAKE_LOCK

android.api = 34
android.minapi = 26
android.archs = arm64-v8a
android.accept_sdk_license = True

# MP3 conversion: put a static arm64 ffmpeg binary at libs/arm64-v8a/libffmpeg.so
# (see README), then uncomment the next line. Without it the app still works but
# saves the original audio (M4A) instead of MP3.
# android.add_libs_arm64_v8a = libs/arm64-v8a/libffmpeg.so

[buildozer]
log_level = 2
warn_on_root = 1
