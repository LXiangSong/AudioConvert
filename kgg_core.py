"""KuGou v5 restoration using local keys; no network requests.

QMC2/SQLite codec follows AudioDecrypt and unlock-music implementations.
Python adaptation, 2026-10-01. See THIRD_PARTY.md for provenance.
"""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import struct
import sys
import threading

from Crypto.Cipher import AES
from Crypto.Util.strxor import strxor

MAGIC = bytes.fromhex('7cd532eb86027f4ba8afa68e0fff9914')
SQLITE_HEADER = b'SQLite format 3\0'
_cache: dict[Path, tuple[tuple[int, int], dict[str, str]]] = {}
_cache_lock = threading.Lock()


class KGGError(ValueError):
    pass


def header(stream, size: int) -> tuple[int, str]:
    fixed = stream.read(72)
    if len(fixed) != 72 or fixed[:16] != MAGIC:
        raise KGGError('KGG 文件头无效或已损坏。')
    offset, version = struct.unpack_from('<II', fixed, 16)
    if version != 5:
        raise KGGError(f'暂不支持 KGG 加密版本 {version}，当前支持第 5 版。')
    length, = struct.unpack_from('<I', fixed, 68)
    if not 0 < length <= 256 or not 72 + length <= offset < size:
        raise KGGError('KGG 文件头长度异常或音频内容为空。')
    try:
        key_id = stream.read(length).decode('ascii')
    except UnicodeError as error:
        raise KGGError('KGG 歌曲密钥标识无效。') from error
    return offset, key_id


def _tea_block(data: bytes, key: bytes) -> bytes:
    left, right = struct.unpack('>II', data)
    a, b, c, d = struct.unpack('>IIII', key)
    total = 0x9E3779B9 * 16 & 0xFFFFFFFF
    for _ in range(16):
        right = (right - (((left << 4) + c) ^ (left + total) ^ ((left >> 5) + d))) & 0xFFFFFFFF
        left = (left - (((right << 4) + a) ^ (right + total) ^ ((right >> 5) + b))) & 0xFFFFFFFF
        total = (total - 0x9E3779B9) & 0xFFFFFFFF
    return struct.pack('>II', left, right)


def _tea(data: bytes, key: bytes) -> bytes:
    if len(data) < 16 or len(data) % 8:
        raise ValueError('invalid TEA length')
    first = _tea_block(data[:8], key)
    pad = 1 + (first[0] & 7)
    plain = bytearray(first)
    previous_cipher = data[:8]
    position = 8
    while position < len(data):
        block = data[position:position + 8]
        decoded = _tea_block(strxor(first, block), key)
        plain.extend(strxor(decoded, previous_cipher))
        first, previous_cipher = decoded, block
        position += 8
    start = pad + 2
    if start > len(plain) - 7 or plain[-7:] != bytes(7):
        raise ValueError('invalid TEA padding')
    return bytes(plain[start:-7])


def derive_key(encoded: str) -> bytes:
    try:
        raw = base64.b64decode(encoded, validate=True)
        prefix = b'QQMusic EncV2,Key:'
        if raw.startswith(prefix):
            raw = _tea(raw[len(prefix):], bytes.fromhex('3338365a4a592140232a24255e262928'))
            raw = base64.b64decode(_tea(raw, b'**#!(#$%&^a1cZ,T'), validate=True)
        if len(raw) < 16:
            raise ValueError('short key')
        simple = bytes.fromhex('695646382b20150b')
        tea_key = bytes(value for pair in zip(simple, raw[:8]) for value in pair)
        key = raw[:8] + _tea(raw[8:], tea_key)
        if not key:
            raise ValueError('empty key')
        return key
    except (ValueError, struct.error) as error:
        raise KGGError('KGG 歌曲密钥无效或已过期，请使用原下载设备的密钥库。') from error


class QMC2:
    def __init__(self, key: bytes):
        if not key:
            raise ValueError('empty QMC2 key')
        self.key = key
        self.n = len(key)
        self.hash = 1
        if self.n <= 300:
            masks = bytearray(0x8000)
            for offset in range(len(masks)):
                index = (offset * offset + 71214) % self.n
                shift = (index + 4) % 8
                value = key[index]
                masks[offset] = ((value << shift) | (value >> shift)) & 255
            self.masks = bytes(masks)
        else:
            self.box = [i & 255 for i in range(self.n)]
            j = 0
            for i in range(self.n):
                j = (j + self.box[i] + key[i]) % self.n
                self.box[i], self.box[j] = self.box[j], self.box[i]
            for value in key:
                if value:
                    next_hash = (self.hash * value) & 0xFFFFFFFF
                    if next_hash <= self.hash:
                        break
                    self.hash = next_hash

    def skip(self, segment: int) -> int:
        seed = self.key[segment % self.n]
        return int(self.hash / ((segment + 1) * seed) * 100) % self.n if seed else 0

    def decrypt(self, data: bytes, offset: int) -> bytes:
        out = bytearray(data)
        cursor = 0
        while cursor < len(out):
            if self.n <= 300:
                start = offset if offset <= 0x7FFF else offset % 0x7FFF
                count = min(len(out) - cursor, 0x8000 - start if offset <= 0x7FFF else 0x7FFF - start)
                out[cursor:cursor + count] = strxor(bytes(out[cursor:cursor + count]), self.masks[start:start + count])
            elif offset < 128:
                count = min(len(out) - cursor, 128 - offset)
                for i in range(count):
                    out[cursor + i] ^= self.key[self.skip(offset + i)]
            else:
                count = min(len(out) - cursor, 5120 - offset % 5120)
                skip = offset % 5120 + self.skip(offset // 5120)
                box = self.box.copy()
                j = k = 0
                for i in range(-skip, count):
                    j = (j + 1) % self.n
                    k = (k + box[j]) % self.n
                    box[j], box[k] = box[k], box[j]
                    if i >= 0:
                        out[cursor + i] ^= box[(box[j] + box[k]) % self.n]
            cursor += count
            offset += count
        return bytes(out)


def _page(data: bytes, number: int) -> bytes:
    master = bytes.fromhex('1d613145b247bf7f3d189672144fe4bf')
    key = hashlib.md5(master + struct.pack('<II', number, 0x546C4173)).digest()
    seed = number + 1
    iv = bytearray()
    for _ in range(4):
        seed = (seed * 0x9EF4 - (seed // 0xCE26) * 0x7FFFFF07) & 0xFFFFFFFF
        if seed & 0x80000000:
            seed = (seed + 0x7FFFFF07) & 0xFFFFFFFF
        iv.extend(struct.pack('<I', seed))
    return AES.new(key, AES.MODE_CBC, hashlib.md5(iv).digest()).decrypt(data)


def database_keys(data: bytes, check=lambda: None) -> dict[str, str]:
    if not data.startswith(SQLITE_HEADER):
        if not data or len(data) % 1024 or data[20:24] != b'\0\x40\x20\x20':
            raise ValueError('密钥库格式不受支持或文件不完整')
        expected = data[16:24]
        first = _page(data[8:16] + data[24:1024], 1)
        if first[:8] != expected:
            raise ValueError('密钥库解密校验失败，可能是新版酷狗数据库')
        decoded = bytearray(SQLITE_HEADER + first)
        for offset in range(1024, len(data), 1024):
            check()
            decoded.extend(_page(data[offset:offset + 1024], offset // 1024 + 1))
        data = bytes(decoded)
    try:
        with closing(sqlite3.connect(':memory:')) as db:
            db.deserialize(data)
            return dict(db.execute("SELECT EncryptionKeyId, EncryptionKey FROM ShareFileItems "
                                   "WHERE EncryptionKeyId IS NOT NULL AND EncryptionKeyId != '' "
                                   "AND EncryptionKey IS NOT NULL AND EncryptionKey != ''"))
    except sqlite3.Error as error:
        raise ValueError(f'密钥库数据库损坏：{error}') from error


def key_sources(source: Path, selected: Path | None) -> list[Path]:
    if selected:
        return [selected]
    app = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    bases = [source.parent, app, app / 'keys']
    for name in ('APPDATA', 'LOCALAPPDATA'):
        if location := os.environ.get(name):
            bases.extend(Path(location) / folder for folder in ('KuGou8', 'KuGou', 'KuGou8/Database'))
    return list(dict.fromkeys(base / name for base in bases for name in ('KGMusicV3.db', 'kgg.key')))


def find_key(key_id: str, source: Path, selected: Path | None, check) -> str:
    found = False
    loaded = False
    failures = []
    for path in key_sources(source, selected):
        check()
        try:
            st = path.stat()
            found = True
            if not path.is_file():
                raise ValueError('选择的位置不是数据库文件')
            # Check pending writes even when a previously loaded DB is cached.
            if path.suffix.lower() != '.key':
                wal = Path(str(path) + '-wal')
                try:
                    pending = wal.stat().st_size > 0
                except FileNotFoundError:
                    pending = False
                if pending:
                    raise ValueError('酷狗密钥库尚未完成写入，请完全退出酷狗后重试')
            signature = st.st_size, st.st_mtime_ns
            # One load per database signature even with parallel conversions.
            with _cache_lock:
                check()
                cached = _cache.get(path)
                if cached is None or cached[0] != signature:
                    if path.suffix.lower() == '.key':
                        entries = (line.partition('$') for line in path.read_text(encoding='utf-8-sig').splitlines())
                        mapping = {k: v for k, sep, v in entries if sep and k and v}
                    else:
                        mapping = database_keys(path.read_bytes(), check)
                    _cache[path] = signature, mapping
                else:
                    mapping = cached[1]
            loaded = True
            if key := mapping.get(key_id):
                return key
        except FileNotFoundError:
            continue
        except PermissionError:
            found = True
            failures.append(f'没有权限读取酷狗密钥库，请检查文件权限。\n数据库：{path}')
        except (OSError, ValueError, sqlite3.Error) as error:
            failures.append(f'密钥库读取失败：{error}。\n数据库：{path}')
    if failures and not loaded:
        raise KGGError('KGG ' + '\n'.join(failures))
    if selected and not found:
        raise KGGError('KGG 选择的密钥库不存在，请重新选择。')
    if not found:
        raise KGGError('KGG 未检测到本机酷狗密钥库，缺少酷狗密钥库无法还原。'
                       '请在原下载电脑取得 %APPDATA%/KuGou8/KGMusicV3.db，'
                       '放在音频文件夹或在高级设置中选择后重试。')
    raise KGGError('KGG 密钥库中没有这首歌曲的密钥。请在原下载电脑登录酷狗并重新下载歌曲，'
                   '完全退出酷狗后使用对应的 KGMusicV3.db 重试。'
                   + ('\n另外检测到：' + '\n'.join(failures) if failures else ''))


def restore(source: Path, destination: Path, selected: Path | None, check, progress):
    check()
    with source.open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        offset, key_id = header(stream, size)
        cipher = QMC2(derive_key(find_key(key_id, source, selected, check)))
        stream.seek(offset)
        audio_offset = 0
        with destination.open('wb') as output:
            while block := stream.read(256 * 1024):
                check()
                output.write(cipher.decrypt(block, audio_offset))
                audio_offset += len(block)
                progress(audio_offset / (size - offset))
    check()
