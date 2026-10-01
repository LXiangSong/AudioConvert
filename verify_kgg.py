"""KGG codec vectors and generated-audio integration; never edits user files."""
import base64
from contextlib import closing
import hashlib
import os
from io import BytesIO
from pathlib import Path
import sqlite3
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util.strxor import strxor

from engine import Cancelled, Options, convert, ffmpeg_path
from kgg_core import MAGIC, KGGError, QMC2, _tea, database_keys, derive_key, find_key, header, restore


def encrypt_tea(data, key):
    padding = (8 - (len(data) + 10) % 8) % 8
    plain = bytes([padding]) + bytes(padding + 2) + data + bytes(7)
    a, b, c, d = struct.unpack('>IIII', key)
    previous_cipher = previous_input = bytes(8)
    out = bytearray()
    for offset in range(0, len(plain), 8):
        block = strxor(plain[offset:offset + 8], previous_cipher)
        left, right = struct.unpack('>II', block)
        total = 0
        for _ in range(16):
            total = (total + 0x9E3779B9) & 0xFFFFFFFF
            left = (left + (((right << 4) + a) ^ (right + total) ^ ((right >> 5) + b))) & 0xFFFFFFFF
            right = (right + (((left << 4) + c) ^ (left + total) ^ ((left >> 5) + d))) & 0xFFFFFFFF
        cipher = strxor(struct.pack('>II', left, right), previous_input)
        out.extend(cipher)
        previous_cipher, previous_input = cipher, block
    return bytes(out)


def encode_key(key, v2=False):
    simple = bytes.fromhex('695646382b20150b')
    tea_key = bytes(value for pair in zip(simple, key[:8]) for value in pair)
    encoded = base64.b64encode(key[:8] + encrypt_tea(key[8:], tea_key))
    if v2:
        inner = encrypt_tea(encoded, b'**#!(#$%&^a1cZ,T')
        outer = encrypt_tea(inner, bytes.fromhex('3338365a4a592140232a24255e262928'))
        return base64.b64encode(b'QQMusic EncV2,Key:' + outer).decode()
    return encoded.decode()


def kgg_header():
    out = bytearray(1024)
    out[:16] = MAGIC
    struct.pack_into('<II', out, 16, 1024, 5)
    struct.pack_into('<I', out, 68, 32)
    out[72:104] = b'0123456789abcdef0123456789abcdef'
    return bytes(out)


def encrypt_database(data):
    output = bytearray()
    for offset in range(0, len(data), 1024):
        number = offset // 1024 + 1
        master = bytes.fromhex('1d613145b247bf7f3d189672144fe4bf')
        key = hashlib.md5(master + struct.pack('<II', number, 0x546C4173)).digest()
        seed = number + 1
        iv = bytearray()
        for _ in range(4):
            seed = (seed * 0x9EF4 - seed // 0xCE26 * 0x7FFFFF07) & 0xFFFFFFFF
            if seed & 0x80000000:
                seed = (seed + 0x7FFFFF07) & 0xFFFFFFFF
            iv.extend(struct.pack('<I', seed))
        aes = AES.new(key, AES.MODE_CBC, hashlib.md5(iv).digest())
        page = data[offset:offset + 1024]
        if number == 1:
            cipher = aes.encrypt(page[16:])
            output.extend(bytes(8) + cipher[:8] + page[16:24] + cipher[8:])
        else:
            output.extend(aes.encrypt(page))
    return bytes(output)


class KGGChecks(unittest.TestCase):
    def test_auto_database_and_pending_writes(self):
        with tempfile.TemporaryDirectory(prefix='AudioConvert-key-test-') as folder:
            root = Path(folder)
            database = root / 'KuGou8' / 'KGMusicV3.db'
            database.parent.mkdir()
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE ShareFileItems (EncryptionKeyId TEXT, EncryptionKey TEXT)')
                db.execute('INSERT INTO ShareFileItems VALUES (?, ?)', ('song-id', 'encoded-key'))
                db.commit()
            with patch.dict(os.environ, {'APPDATA': str(root), 'LOCALAPPDATA': str(root)}):
                self.assertEqual(find_key('song-id', root / 'song.kgg', None, lambda: None), 'encoded-key')
                with self.assertRaisesRegex(KGGError, '没有这首歌曲的密钥'):
                    find_key('other-song', root / 'song.kgg', None, lambda: None)
                wal = Path(str(database) + '-wal')
                wal.write_bytes(b'pending')
                with self.assertRaisesRegex(KGGError, '尚未完成写入'):
                    find_key('song-id', root / 'song.kgg', None, lambda: None)
                wal.unlink()
                self.assertEqual(find_key('song-id', root / 'song.kgg', None, lambda: None), 'encoded-key')

    def test_specific_lookup_errors_and_fallback(self):
        with tempfile.TemporaryDirectory(prefix='AudioConvert-key-errors-') as folder:
            root = Path(folder)
            missing, broken, keys = (root / name for name in ('missing.db', 'broken.db', 'kgg.key'))
            broken.write_bytes(b'broken database')
            keys.write_text('song-id$encoded-key', encoding='utf-8')
            with patch('kgg_core.key_sources', return_value=[missing]):
                with self.assertRaisesRegex(KGGError, '未检测到本机酷狗密钥库'):
                    find_key('song-id', root / 'song.kgg', None, lambda: None)
                with self.assertRaisesRegex(KGGError, '选择的密钥库不存在'):
                    find_key('song-id', root / 'song.kgg', missing, lambda: None)
            with patch('kgg_core.key_sources', return_value=[broken]):
                with self.assertRaisesRegex(KGGError, '密钥库格式不受支持或文件不完整'):
                    find_key('song-id', root / 'song.kgg', None, lambda: None)
                with patch.object(Path, 'stat', side_effect=PermissionError()):
                    with self.assertRaisesRegex(KGGError, '没有权限读取'):
                        find_key('song-id', root / 'song.kgg', None, lambda: None)
            with patch('kgg_core.key_sources', return_value=[broken, keys]):
                self.assertEqual(find_key('song-id', root / 'song.kgg', None, lambda: None), 'encoded-key')
                with self.assertRaisesRegex(KGGError, '没有这首歌曲的密钥'):
                    find_key('other-song', root / 'song.kgg', None, lambda: None)

    def test_upstream_vectors(self):
        # Fixed answers from Kugo-Music-Converter's QMC2 regression vectors.
        key = bytes((i * 29 + 17) & 255 for i in range(256))
        self.assertEqual(QMC2(key).decrypt(bytes(1), 0), b'\x1d')
        key = bytes((i * 7 + 3) & 255 for i in range(400))
        self.assertEqual(QMC2(key).decrypt(bytes(16), 5120),
                         bytes.fromhex('c5da5561b1a57eea4705518a8161de6a'))

    def test_keys_and_padding(self):
        for size in (16, 180, 256, 300, 301, 400, 512):
            key = bytes((i * 7 + 3) & 255 for i in range(size))
            for v2 in (False, True):
                self.assertEqual(derive_key(encode_key(key, v2)), key)
        for size in range(40):
            data = bytes(range(size))
            self.assertEqual(_tea(encrypt_tea(data, bytes(16)), bytes(16)), data)
        with self.assertRaises(KGGError):
            derive_key('invalid key')
        with self.assertRaises(ValueError):
            _tea(bytes(16), bytes(16))

    def test_chunk_boundaries(self):
        for size in (180, 256, 300, 301, 400, 512, 700):
            key = bytes((i * 7 + 3) & 255 for i in range(size))
            cipher = QMC2(key)
            data = bytes(70000)
            whole = cipher.decrypt(data, 0)
            for step in (127, 5120, 32767):
                chunked = b''.join(cipher.decrypt(data[i:i + step], i) for i in range(0, len(data), step))
                self.assertEqual(chunked, whole)

    def test_database(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('PRAGMA page_size=1024')
            db.execute('CREATE TABLE ShareFileItems (EncryptionKeyId TEXT, EncryptionKey TEXT)')
            db.executemany('INSERT INTO ShareFileItems VALUES (?,?)', [(str(i), 'key' * 30) for i in range(300)])
            db.commit()
            plain = db.serialize()
        want = {str(i): 'key' * 30 for i in range(300)}
        self.assertEqual(database_keys(plain), want)
        encrypted = encrypt_database(plain)
        self.assertEqual(database_keys(encrypted), want)
        corrupted = bytearray(encrypted)
        corrupted[50] ^= 1
        with self.assertRaises(ValueError):
            database_keys(bytes(corrupted))

    def test_header_failures(self):
        for data in (b'', bytes(1024), kgg_header()):
            with self.assertRaises(KGGError):
                header(BytesIO(data), len(data))
        data = bytearray(kgg_header() + bytes(16))
        struct.pack_into('<I', data, 20, 6)
        with self.assertRaisesRegex(KGGError, '版本 6'):
            header(BytesIO(data), len(data))

    def test_full_conversion_and_missing_key(self):
        with tempfile.TemporaryDirectory(prefix='AudioConvert-kgg-test-') as folder:
            root = Path(folder)
            audio = root / 'generated.mp3'
            subprocess.run([ffmpeg_path(), '-v', 'error', '-f', 'lavfi', '-i',
                            'sine=frequency=440:duration=2', str(audio)], check=True, creationflags=0x08000000)
            for size in (256, 400):
                key = bytes((i * 7 + 3) & 255 for i in range(size))
                source = root / f'generated-{size}.kgg'
                source.write_bytes(kgg_header() + QMC2(key).decrypt(audio.read_bytes(), 0))
                original = source.read_bytes()
                keys = root / f'{size}.key'
                keys.write_text('0123456789abcdef0123456789abcdef$' + encode_key(key), encoding='utf-8')
                options = Options(root / 'out', kgg_keys=keys, fast=True)
                result = convert(source, None, options, threading.Event())
                self.assertTrue(result.path.is_file())
                self.assertAlmostEqual(result.duration, 2, delta=.2)
                self.assertEqual(source.read_bytes(), original)
                restored = root / 'cancelled.audio'
                cancel_midway = threading.Event()
                from engine import guard
                with self.assertRaises(Cancelled):
                    restore(source, restored, keys, lambda: guard(cancel_midway),
                            lambda p: cancel_midway.set())
                keys.write_text('other$' + encode_key(key), encoding='utf-8')
                with self.assertRaisesRegex(KGGError, '没有这首歌曲'):
                    convert(source, None, options, threading.Event())
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(len(list((root / 'out').glob('*.mp3'))), 1 if size == 256 else 2)
            with patch('kgg_core.key_sources', return_value=[]):
                with self.assertRaisesRegex(KGGError, '缺少酷狗密钥库'):
                    convert(source, None, Options(root / 'out', keep=False), threading.Event())
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(Cancelled):
                convert(source, None, options, cancel)
            self.assertEqual(source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
