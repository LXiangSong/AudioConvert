# AudioConvert

中文使用、打包和目录说明：[项目说明.md](项目说明.md)。

Native Windows 10/11 x64 audio converter. Run dist/AudioConvert.exe.
No browser, installed Python, or separately installed FFmpeg required.

## Usage

1. Select a source folder or add individual audio files. The queue starts empty.
2. Select an output folder. MP3 is the default.
3. Keep original files is checked by default. Unchecking requests Recycle Bin handling only after conversion and full output decoding succeed.
4. Start conversion. Cancel, retry failures, open output folder, or double-click a completed file to play.

Follows Windows light/dark theme. Right-click a file for task details or removal from the queue.

## Formats

Outputs: MP3, M4A (AAC), FLAC, WAV (16-bit PCM), OGG (Vorbis), Opus, AAC.
Inputs: common FFmpeg audio formats, XM restoration, and KuGou KGG v5.
KGG requires the matching local KuGou key database; the audio file alone is insufficient.
Auto-discovery checks %APPDATA%/KuGou8, common KuGou data folders, the audio folder,
and the executable folder (including its keys subfolder). Advanced settings can select
KGMusicV3.db or an exported kgg.key (id$ekey per line); clear the field to resume auto-discovery.
Use the database from the computer that downloaded the songs. Fully exit KuGou before copying
the database or converting to ensure pending database writes are complete.
Database decoding stays in memory; no keys are uploaded or logged.
Missing keys, invalid databases, and unsupported KGG versions show explicit errors.
New proprietary encryption variants may not be supported.
MP3/OGG/Opus default to stereo; explicit mono/stereo is available. Opus uses 48 kHz when a sample rate is selected. OGG presets map to Vorbis quality, so bitrate is approximate. Artwork is not copied; basic metadata is copied where supported. Increasing bitrate or using a lossless format cannot restore lost quality.

## Source protection

- Never overwrites existing files; conflicts get a numeric suffix.
- Writes and fully decodes temporary output, then renames into place.
- Failure or cancellation retains source; already finished items stay finished.
- Recycle Bin unavailable: keep source, retain valid output, show warning.
- Never falls back to permanent deletion. Source hash/stat are rechecked before recycling.
- Nested output directories are excluded from source scanning. Scans do not follow junctions or symbolic links.
- Uses the first audio stream from the input.

Logs: %LOCALAPPDATA%/AudioConvert/logs/AudioConvert.log.

## Development

Python 3.13 x64: install requirements.txt; run python main.py.
Integration checks: python verify.py. Creates test audio and recycles one disposable test copy, never user audio.
KGG codec and generated-audio checks: python verify_kgg.py.
Build: double-click build.bat. It prepares the virtual environment, installs missing
dependencies, builds dist/AudioConvert.exe, and checks packaged window startup.
Requires Python 3.13 x64; the first dependency installation needs internet.
Later builds reuse installed dependencies and build caches.
Build candidates are checked before replacing the previous release.
Successful build metadata and SHA256: output/build-report.json.
The console stays open on completion or failure. Full log: output/build.log.
Close any running AudioConvert.exe before rebuilding.
Command-line build: .venv/Scripts/python.exe build.py.
Use build.py --clean only when a full clean rebuild is needed.
Third-party provenance: THIRD_PARTY.md.
