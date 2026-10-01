"""Integration tests use only generated disposable audio."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from engine import Options, convert, Cancelled, ffmpeg_path


def run_checks():
    report = []
    with tempfile.TemporaryDirectory(prefix='AudioConvert-test-') as folder:
        root = Path(folder)
        source = root / 'input' / '原始音频.wav'
        source.parent.mkdir()
        subprocess.run([ffmpeg_path(), '-nostdin', '-v', 'error', '-f', 'lavfi',
                        '-i', 'sine=frequency=440:sample_rate=44100:duration=2',
                        '-c:a', 'pcm_s16le', str(source)], check=True, creationflags=0x08000000)
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        def unchanged():
            assert source.exists() and hashlib.sha256(source.read_bytes()).hexdigest() == original
        for fmt in ['MP3','M4A','FLAC','WAV','OGG','Opus','AAC']:
            out = convert(source, None, Options(root / 'out', fmt), threading.Event())
            assert out.path.exists() and abs(out.duration-2) < .2
            unchanged()
            returned = convert(out.path, None, Options(root / 'back', 'WAV'), threading.Event())
            assert returned.path.exists() and abs(returned.duration-2) < .2
            report.append(fmt+' forward and reverse passed')
            stages = []
            copied = convert(out.path, None, Options(root/'fast', fmt, fast=True),
                             threading.Event(), lambda stage, progress: stages.append(stage))
            assert copied.path.exists() and '快速封装' in stages
            assert abs(copied.duration-out.duration) < .2
        same = convert(source, None, Options(source.parent, 'WAV'), threading.Event())
        assert same.path != source and '(1)' in same.path.name
        unchanged()
        report.append('Same-folder collision keeps original')
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(convert, source, None, Options(root/'parallel'),
                                   threading.Event()) for _ in range(4)]
            parallel = [future.result() for future in futures]
        assert len({result.path for result in parallel}) == 4
        assert all(result.path.exists() and abs(result.duration-2) < .2 for result in parallel)
        unchanged()
        report.append('Four concurrent conversions resolve output name collisions')
        nested = root / 'input' / 'album' / 'track.wav'
        nested.parent.mkdir()
        shutil.copy2(source, nested)
        result = convert(nested, root/'input', Options(root/'structure'), threading.Event())
        assert result.path.parent == root/'structure'/'album'
        report.append('Relative album structure preserved')
        cancel = threading.Event()
        def interrupt(stage, progress):
            if stage == '转换中':
                cancel.set()
        try:
            convert(source, None, Options(root/'cancel', keep=False), cancel, interrupt,
                    lambda path: (_ for _ in ()).throw(AssertionError('Must not recycle')))
            raise AssertionError('Expected cancellation')
        except Cancelled:
            pass
        unchanged()
        assert not list((root/'cancel').glob('*'))
        report.append('Cancellation preserves source and cleans temporary output')
        def unavailable(path):
            raise OSError('Recycle bin unavailable')
        result = convert(source, None, Options(root/'no-recycle', keep=False), threading.Event(), recycle=unavailable)
        assert result.warning and not result.recycled and result.path.exists()
        unchanged()
        report.append('Recycle failure retains source and validated output')
        # A source can disappear externally after output validation. Keep the
        # successful output and report a warning instead of marking it failed.
        vanished = root/'vanished.wav'
        shutil.copy2(source, vanished)
        recycle_attempts = []
        def remove_after_validation(stage, progress):
            if stage == '校验音频' and vanished.exists():
                vanished.unlink()
        result = convert(vanished, None, Options(root/'vanished-output', keep=False),
                         threading.Event(), remove_after_validation,
                         lambda path: recycle_attempts.append(path))
        assert not vanished.exists() and not recycle_attempts
        assert result.path.exists() and result.warning and not result.recycled
        report.append('External source removal retains successful output with warning')
        damaged = root/'corrupt.mp3'
        damaged.write_bytes(b'not an audio file')
        try:
            convert(damaged, None, Options(root/'invalid', keep=False), threading.Event())
            raise AssertionError('Expected invalid input failure')
        except ValueError:
            pass
        assert damaged.exists()
        report.append('Corrupt input is preserved')
        recycle_copy = root/'recycle-test.wav'
        shutil.copy2(source, recycle_copy)
        result = convert(recycle_copy, None, Options(root/'recycled', keep=False), threading.Event())
        assert result.recycled and not recycle_copy.exists(), result.warning
        assert result.path.exists()
        unchanged()
        report.append('Real Windows recycle succeeds after verified conversion')
    return report


if __name__ == '__main__':
    print(json.dumps(run_checks(), ensure_ascii=False, indent=2))
