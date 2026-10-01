"""Build the native executable and verify packaged startup."""
from pathlib import Path
import subprocess
import sys
import hashlib
import json
from datetime import datetime, timezone
import imageio_ffmpeg
import PySide6


def main():
    root = Path(__file__).resolve().parent
    if not (root/'assets'/'AudioConvert.ico').is_file():
        raise FileNotFoundError('Missing assets/AudioConvert.ico')
    (root/'build-bin').mkdir(exist_ok=True)
    import shutil
    ffmpeg_source = Path(imageio_ffmpeg.get_ffmpeg_exe())
    ffmpeg_copy = root/'build-bin'/'ffmpeg.exe'
    if not ffmpeg_copy.exists() or ffmpeg_copy.stat().st_size != ffmpeg_source.stat().st_size or ffmpeg_copy.stat().st_mtime_ns != ffmpeg_source.stat().st_mtime_ns:
        shutil.copy2(ffmpeg_source, ffmpeg_copy)
    args = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
            '--name', 'AudioConvert', '--icon', str(root/'assets'/'AudioConvert.ico'),
            '--distpath', str(root/'build'/'release-candidate'), '--workpath', str(root/'build'),
            '--add-binary', str(root/'build-bin'/'ffmpeg.exe')+';bin',
            '--add-data', str(root/'xm_encryptor.wasm')+';.',
            '--add-data', str(root/'assets')+';assets',
            '--add-data', str(root/'THIRD_PARTY.md')+';.',
            '--collect-all', 'wasmtime', '--hidden-import', 'win32com.shell.shell',
            '--hidden-import', 'win32timezone', '--hidden-import', 'pythoncom',
            '--hidden-import', 'pywintypes', '--exclude-module', 'tkinter']
    # Keep the root MSVC runtime consistent with the Qt wheel.
    qt_dir = Path(PySide6.__file__).resolve().parent
    for pattern in ('vcruntime140*.dll', 'msvcp140*.dll', 'concrt140.dll'):
        for dll in sorted(qt_dir.glob(pattern)):
            args.extend(['--add-binary', str(dll)+';.'])
    if '--clean' in sys.argv:
        args.append('--clean')
    args.append(str(root/'main.py'))
    subprocess.run(args, cwd=root, check=True)
    print('Checking packaged application startup...', flush=True)
    (root/'output').mkdir(exist_ok=True)
    screenshot = root/'output'/'packaged-startup.png'
    screenshot.unlink(missing_ok=True)
    exe = root/'build'/'release-candidate'/'AudioConvert.exe'
    process = subprocess.Popen([str(exe), '--screenshot', str(screenshot)], cwd=root)
    try:
        code = process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, check=False)
        raise RuntimeError('Packaged startup timed out. See output/build.log.') from None
    if code != 0 or not screenshot.exists() or screenshot.stat().st_size == 0:
        raise RuntimeError(f'Packaged startup failed (exit {code}). See output/build.log.')
    # Publish only after the candidate passes startup; retain the previous EXE on failure.
    release = root/'dist'/'AudioConvert.exe'
    release.parent.mkdir(exist_ok=True)
    exe.replace(release)
    report = {'built_at': datetime.now(timezone.utc).isoformat(), 'startup_check': True,
              'sha256': hashlib.sha256(release.read_bytes()).hexdigest(),
              'bytes': release.stat().st_size}
    (root/'output'/'build-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(f'BUILD AND STARTUP CHECK PASSED\n{release}', flush=True)


if __name__ == "__main__":
    main()
