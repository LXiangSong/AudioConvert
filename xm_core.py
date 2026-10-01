# Adapted from DemaciaForge/Ximalaya-Decrypt-Enhance main.py.
# Original algorithm: sld272/Ximalaya-XM-Decrypt; see THIRD_PARTY.md.
import base64
import io
import os
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from mutagen.easyid3 import ID3
import wasmtime

class XMInfo:
    def __init__(self):
        self.title = ""
        self.artist = ""
        self.album = ""
        self.tracknumber = 0
        self.size = 0
        self.header_size = 0
        self.ISRC = ""
        self.encodedby = ""
        self.encoding_technology = ""

    def iv(self):
        if self.ISRC != "":
            return bytes.fromhex(self.ISRC)
        return bytes.fromhex(self.encodedby)

def get_xm_info(data: bytes):
    id3 = ID3(io.BytesIO(data), v2_version=3)
    id3value = XMInfo()
    try:
        id3value.title = str(id3["TIT2"])
        id3value.album = str(id3["TALB"])
        id3value.artist = str(id3["TPE1"])
        id3value.size = int(str(id3["TSIZ"]))
        id3value.encoding_technology = str(id3["TSSE"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("XM 文件缺少必要的 ID3 元数据") from exc
    # 提取官方文件头里自带的真实集数序号
    try:
        id3value.tracknumber = int(str(id3["TRCK"]))
    except:
        id3value.tracknumber = 0
        
    id3value.ISRC = "" if id3.get("TSRC") is None else str(id3["TSRC"])
    id3value.encodedby = "" if id3.get("TENC") is None else str(id3["TENC"])
    id3value.header_size = id3.size
    return id3value

def get_printable_count(x: bytes):
    for i, c in enumerate(x):
        if c < 0x20 or c > 0x7e:
            return i
    return len(x)

def get_printable_bytes(x: bytes):
    return x[:get_printable_count(x)]

def xm_decrypt(raw_data):
    wasm_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xm_encryptor.wasm")
    store = wasmtime.Store()
    module = wasmtime.Module.from_file(store.engine, wasm_path)
    xm_encryptor = wasmtime.Instance(store, module, [])
    exports = xm_encryptor.exports(store)
    
    xm_info = get_xm_info(raw_data)
    encrypted_data = raw_data[xm_info.header_size:xm_info.header_size + xm_info.size:]

    # Stage 1 aes-256-cbc
    xm_key = b"ximalayaximalayaximalayaximalaya"
    cipher = AES.new(xm_key, AES.MODE_CBC, xm_info.iv())
    if not encrypted_data:
        raise ValueError("XM 加密数据长度无效，文件可能不完整")
    # Some XM variants store a partial final block; pad only that case.
    decrypt_data = encrypted_data if len(encrypted_data) % 16 == 0 else pad(encrypted_data, 16)
    de_data = cipher.decrypt(decrypt_data)
    
    # Stage 2 xmDecrypt
    de_data = get_printable_bytes(de_data)
    track_id = str(xm_info.tracknumber).encode()
    stack_pointer = exports["a"](store, -16)
    assert isinstance(stack_pointer, int)
    de_data_offset = exports["c"](store, len(de_data))
    assert isinstance(de_data_offset, int)
    track_id_offset = exports["c"](store, len(track_id))
    assert isinstance(track_id_offset, int)
    memory_i = exports["i"]
    memory_i.write(store, de_data, de_data_offset)
    memory_i.write(store, track_id, track_id_offset)

    exports["g"](store, stack_pointer, de_data_offset, len(de_data), track_id_offset, len(track_id))
    result_header = memory_i.read(store, stack_pointer, stack_pointer + 8)
    result_pointer = int.from_bytes(result_header[0:4], "little", signed=True)
    result_length = int.from_bytes(result_header[4:8], "little", signed=True)
    
    result_data = bytes(memory_i.read(store, result_pointer, result_pointer + result_length)).decode()
    
    # Stage 3 combine
    decrypted_data = base64.b64decode(xm_info.encoding_technology + result_data)
    final_data = decrypted_data + raw_data[xm_info.header_size + xm_info.size::]
    return xm_info, final_data
