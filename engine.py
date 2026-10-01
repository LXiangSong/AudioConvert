"""Local conversion engine. Source files are never overwritten or unlinked."""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("AudioConvert")
INPUTS = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.opus', '.wma',
          '.ape', '.aif', '.aiff', '.alac', '.m4b', '.mp2', '.ac3', '.amr',
          '.aifc', '.au', '.caf', '.wv', '.xm', '.kgg'}
OUTPUTS = ['MP3', 'M4A', 'FLAC', 'WAV', 'OGG', 'Opus', 'AAC']
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Options:
    output: Path
    format: str = 'MP3'
    bitrate: int = 192
    sample_rate: int = 0
    channels: int = 0
    keep: bool = True
    preserve_structure: bool = True
    fast: bool = False
    kgg_keys: Path | None = None


@dataclass
class Result:
    path: Path
    duration: float
    recycled: bool = False
    warning: str = ''


def ffmpeg_path() -> str:
    bundled = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'bin' / 'ffmpeg.exe'
    if bundled.is_file():
        return str(bundled)
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def guard(cancel: threading.Event):
    if cancel.is_set():
        raise Cancelled('已取消，原文件保留')


def digest(path: Path, cancel: threading.Event) -> str:
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            guard(cancel)
            hasher.update(block)
    return hasher.hexdigest()


def probe(path: Path, cancel: threading.Event) -> tuple[float, str]:
    guard(cancel)
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen([ffmpeg_path(), '-hide_banner', '-nostdin', '-i', str(path)],
                             stdout=subprocess.DEVNULL, stderr=err, creationflags=CREATE_FLAGS)
        try:
            while p.poll() is None:
                if cancel.wait(.05):
                    raise Cancelled()
            err.seek(0)
            description = err.read().decode('utf-8', errors='replace')
        finally:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=10)
    if not re.search(r'Stream.*Audio:', description):
        raise ValueError('无法识别音频流，文件可能损坏或格式不受支持。\n' + description[-1200:])
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', description)
    duration = sum(float(x) * weight for x, weight in zip(match.groups(), [3600, 60, 1])) if match else 0
    if duration <= 0:
        raise ValueError('无法确认音频时长，未转换，也未改动源文件。')
    return duration, description


def run_ffmpeg(args: list[str], cancel: threading.Event, duration: float,
               progress: Callable[[float], None]):
    guard(cancel)
    command = [ffmpeg_path(), '-hide_banner', '-nostdin', '-v', 'error',
               '-progress', 'pipe:1', '-nostats', *args]
    with tempfile.TemporaryFile() as err:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=err,
                                   creationflags=CREATE_FLAGS)
        lines: queue.Queue = queue.Queue()
        def reader():
            for line in iter(process.stdout.readline, b''):
                lines.put(line.decode('utf-8', errors='replace').strip())
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            while process.poll() is None or not lines.empty():
                guard(cancel)
                try:
                    line = lines.get(timeout=.1)
                    if line.startswith('out_time_us='):
                        value = line.split('=', 1)[1]
                        if value.lstrip('-').isdigit():
                            progress(min(1, max(0, int(value) / 1e6 / duration)))
                except queue.Empty:
                    pass
            thread.join(timeout=2)
            err.seek(0)
            errors = err.read().decode('utf-8', errors='replace').strip()
            if process.returncode != 0 or errors:
                raise ValueError(errors[-2200:] or '音频处理失败')
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            thread.join(timeout=2)
            process.stdout.close()


def strict_recycle(path: Path):
    """Use modern Windows Recycle-on-delete ONLY, with no legacy fallback."""
    if os.name != 'nt' or sys.getwindowsversion().major < 10:
        raise OSError('当前系统不支持安全回收，源文件已保留')
    import pythoncom
    from win32com.shell import shell, shellcon
    from win32com.server.exception import COMException
    from send2trash.win.IFileOperationProgressSink import FileOperationProgressSink

    class RecycleOnlySink(FileOperationProgressSink):
        def PreDeleteItem(self, flags, item):
            # Returning an HRESULT from a Python COM callback is insufficient;
            # throw to abort if Windows would permanently delete instead.
            if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
                raise COMException(desc='拒绝永久删除，源文件保留', scode=-2147467259)

        def PostDeleteItem(self, flags, item, hr_delete, newly_created):
            if hr_delete < 0:
                raise COMException(desc='移入回收站失败', scode=hr_delete)
            super().PostDeleteItem(flags, item, hr_delete, newly_created)

    pythoncom.CoInitialize()
    try:
        operation = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None,
                                              pythoncom.CLSCTX_ALL, shell.IID_IFileOperation)
        operation.SetOperationFlags(shellcon.FOF_NOCONFIRMATION | shellcon.FOF_NOERRORUI |
                                    shellcon.FOF_SILENT | shellcon.FOFX_EARLYFAILURE |
                                    0x20000000 | 0x00080000)
        sink = RecycleOnlySink()
        wrapped = pythoncom.WrapObject(sink, shell.IID_IFileOperationProgressSink)
        item = shell.SHCreateItemFromParsingName(str(path), None, shell.IID_IShellItem)
        operation.DeleteItem(item, wrapped)
        result = operation.PerformOperations()
        if result or operation.GetAnyOperationsAborted():
            raise OSError('移入回收站失败或已取消')
    finally:
        pythoncom.CoUninitialize()
    if path.exists():
        raise OSError('回收站未接收文件，源文件已保留')


def convert(source: Path, root: Path | None, options: Options,
            cancel: threading.Event, notify: Callable[[str, int], None] = lambda *_: None,
            recycle: Callable[[Path], None] = strict_recycle) -> Result:
    source = source.resolve(strict=True)
    if options.format not in OUTPUTS:
        raise ValueError('不支持的输出格式')
    if source.suffix.lower() not in INPUTS:
        raise ValueError('不支持的输入格式')
    if source.stat().st_size == 0:
        raise ValueError('源文件为空')
    output_root = options.output.resolve()
    folder = output_root
    if root and options.preserve_structure:
        try:
            relative = source.parent.relative_to(root.resolve())
            folder = output_root / relative
        except ValueError:
            pass
    folder.mkdir(parents=True, exist_ok=True)
    # Resolve again after creation: never follow output subfolders outside output root.
    if not folder.resolve().is_relative_to(output_root.resolve()):
        raise ValueError('输出子目录指向输出文件夹之外，已停止处理')
    guard(cancel)
    original_hash = digest(source, cancel) if not options.keep else None
    original_stat = source.stat()
    with tempfile.TemporaryDirectory(prefix='AudioConvert-') as scratch:
        input_path = source
        metadata = {}
        if source.suffix.lower() == '.xm':
            notify('还原 XM', 1)
            from xm_core import xm_decrypt
            info, audio = xm_decrypt(source.read_bytes())
            guard(cancel)
            input_path = Path(scratch) / 'restored.audio'
            input_path.write_bytes(audio)
            metadata = {'title': info.title, 'artist': info.artist, 'album': info.album}
        elif source.suffix.lower() == '.kgg':
            from kgg_core import restore
            notify('还原 KGG', 1)
            input_path = Path(scratch) / 'restored.audio'
            restore(source, input_path, options.kgg_keys, lambda: guard(cancel),
                    lambda p: notify('还原 KGG', 1 + int(p * 9)))
        try:
            duration, description = probe(input_path, cancel)
        except ValueError as error:
            if source.suffix.lower() == '.kgg':
                from kgg_core import KGGError
                raise KGGError('KGG 还原后的音频无法读取，歌曲密钥可能不匹配或文件已损坏。'
                               '请使用原下载设备的密钥库重试。\n' + str(error)) from error
            raise
        ext = options.format.lower()
        audio = re.search(r'Stream[^\n]*Audio: ([a-zA-Z0-9_]+)', description)
        compatible = {'mp3': 'mp3', 'm4a': 'aac', 'aac': 'aac', 'flac': 'flac',
                      'wav': 'pcm_s16le', 'ogg': 'vorbis', 'opus': 'opus'}
        copy_audio = (options.fast and not options.channels and not options.sample_rate
                      and audio is not None and audio.group(1) == compatible[ext])
        fd, temporary = tempfile.mkstemp(prefix='.audioconvert-', suffix='.' + ext, dir=folder)
        os.close(fd)
        temp_path = Path(temporary)
        try:
            codecs = {'mp3': ['-c:a', 'libmp3lame', '-b:a', f'{options.bitrate}k', '-id3v2_version', '3'],
                      'm4a': ['-c:a', 'aac', '-b:a', f'{options.bitrate}k', '-movflags', '+faststart'],
                      'aac': ['-c:a', 'aac', '-b:a', f'{options.bitrate}k'],
                      'flac': ['-c:a', 'flac'], 'wav': ['-c:a', 'pcm_s16le'],
                      'ogg': ['-c:a', 'libvorbis', '-q:a', '5' if options.bitrate >= 192 else '3'],
                      'opus': ['-c:a', 'libopus', '-b:a', f'{min(options.bitrate, 256)}k']}
            args = ['-y', '-xerror', '-err_detect', 'explode', '-i', str(input_path),
                    '-map', '0:a:0', '-vn', '-map_metadata', '0', '-threads', '2', *codecs[ext]]
            if copy_audio:
                args = ['-y', '-xerror', '-i', str(input_path), '-map', '0:a:0',
                        '-vn', '-map_metadata', '0', '-c:a', 'copy']
                if ext == 'm4a':
                    args += ['-movflags', '+faststart']
            if options.channels:
                args += ['-ac', str(options.channels)]
            elif not copy_audio and ext in ('mp3', 'ogg', 'opus'):
                args += ['-ac', '2']
            if options.sample_rate:
                args += ['-ar', str(48000 if ext == 'opus' else options.sample_rate)]
            for name, value in metadata.items():
                args += ['-metadata', f'{name}={value}']
            args.append(str(temp_path))
            stage = '快速封装' if copy_audio else '转换中'
            start_progress = 10 if source.suffix.lower() == '.kgg' else 2
            notify(stage, start_progress)
            run_ffmpeg(args, cancel, duration,
                       lambda p: notify(stage, start_progress + int(p * (86 - start_progress))))
            guard(cancel)
            notify('校验音频', 87)
            converted_duration, _ = probe(temp_path, cancel)
            if abs(converted_duration - duration) > max(1.0, duration * .005):
                raise ValueError('输出时长与源音频不符，源文件保留')
            run_ffmpeg(['-xerror', '-err_detect', 'explode', '-i', str(temp_path),
                        '-map', '0:a:0', '-f', 'null', '-'], cancel, converted_duration,
                       lambda p: notify('校验音频', 87 + int(p * 11)))
            guard(cancel)
            if temp_path.stat().st_size <= 0:
                raise ValueError('输出为空，源文件保留')
            # Windows rename refuses to overwrite. Retry suffixes in case another process wins.
            index = 0
            while True:
                suffix = '' if index == 0 else f' ({index})'
                target = folder / f'{source.stem}{suffix}.{ext}'
                if target.exists():
                    index += 1
                    continue
                try:
                    os.rename(temp_path, target)
                    break
                except FileExistsError:
                    index += 1
            result = Result(target, converted_duration)
            if not options.keep:
                # Cancellation or an input change after conversion MUST keep the source.
                if cancel.is_set():
                    result.warning = '转换完成，取消请求已阻止源文件回收'
                else:
                    try:
                        current_stat = source.stat()
                        if (current_stat.st_size != original_stat.st_size or
                                current_stat.st_mtime_ns != original_stat.st_mtime_ns or
                                digest(source, cancel) != original_hash):
                            result.warning = '源文件内容发生变化，已保留'
                        else:
                            guard(cancel)
                            recycle(source)
                            result.recycled = True
                    except Exception as error:
                        result.warning = '输出已保存，未能确认源文件回收：' + (str(error) or '回收操作已取消')
                        log.warning('Recycle failed: %s', error)
            notify('完成', 100)
            log.info('Converted %s -> %s; recycled=%s; warning=%s', source, target, result.recycled, result.warning)
            return result
        finally:
            # Only this operation's temporary output is removed. Never delete a source.
            if temp_path.exists():
                temp_path.unlink()
