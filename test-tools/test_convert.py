import hashlib
import json
from pathlib import Path
import subprocess
import sys

import imageio_ffmpeg
import mutagen

sys.path.insert(0, str(Path(__file__).parent / 'enhance'))
from main import decrypt_xm_file

source = Path(sys.argv[1])
root = Path(__file__).resolve().parents[1]
out = root / 'output'
before = hashlib.sha256(source.read_bytes()).hexdigest()
decrypt_xm_file(str(source), str(out), False)
audio_files = list(out.rglob('*.m4a')) + list(out.rglob('*.mp3')) + list(out.rglob('*.flac')) + list(out.rglob('*.wav'))
if len(audio_files) != 1:
    raise RuntimeError(f'Expected one restored audio file, found {len(audio_files)}')
restored = audio_files[0]
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
target = out / (source.stem + '.mp3')
subprocess.run([ffmpeg, '-hide_banner', '-nostdin', '-n', '-i', str(restored), '-map', '0:a:0', '-map_metadata', '0', '-c:a', 'libmp3lame', '-b:a', '128k', '-id3v2_version', '3', str(target)], check=True)
check = subprocess.run([ffmpeg, '-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-i', str(target), '-f', 'null', '-'], capture_output=True, text=True)
if check.returncode or check.stderr.strip():
    raise RuntimeError(f'Full decode verification failed: {check.stderr}')
original_info = mutagen.File(restored).info
final_info = mutagen.File(target).info
assert abs(original_info.length - final_info.length) < 1
assert before == hashlib.sha256(source.read_bytes()).hexdigest()
report = {'project': 'https://github.com/DemaciaForge/Ximalaya-Decrypt-Enhance', 'source_sha256': before, 'restored': str(restored), 'restored_format': type(original_info).__name__, 'output': str(target), 'duration_seconds': final_info.length, 'bitrate': final_info.bitrate, 'sample_rate': final_info.sample_rate, 'channels': final_info.channels, 'bytes': target.stat().st_size, 'full_decode_verified': True, 'source_unchanged': True}
(out / 'test-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
