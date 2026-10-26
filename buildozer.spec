[app]
# JJ Player Android build configuration
# Build from this directory with: buildozer android debug

title = JJ Player
package.name = jjplayer
package.domain = gr.jjmusic
version = 23.2
source.dir = .
source.include_exts = py,kv,png,jpg,jpeg,atlas,nml,json
source.exclude_dirs = .git,.github,bin,.buildozer

# Kivy UI and Android Java bridge for MediaPlayer
requirements = python3,kivy,pyjnius

orientation = landscape
fullscreen = 0

# Android versions and device architecture
android.api = 35
android.minapi = 23
android.ndk = 25b
android.ndk_api = 23
android.archs = arm64-v8a

# Required for audio files and local Traktor collection files
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,READ_MEDIA_AUDIO,MANAGE_EXTERNAL_STORAGE
android.allow_backup = True
android.private_storage = False

# Build metadata

[buildozer]
log_level = 2
warn_on_root = 1
