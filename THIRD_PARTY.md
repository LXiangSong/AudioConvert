# AudioConvert third-party components

Native interface and conversion orchestration are local project code.

- KGG v5 / KuGou SQLite codec and QMC2: Python adaptation added 2026-10-01, referencing https://github.com/0x77fe/AudioDecrypt (GPL-3.0; KGG.cpp, KGG.h, QMC2.cpp, Standard/TEA.h). QMC2 boundary behavior, EncV2 key derivation and regression vectors were cross-checked against https://github.com/skxxxkx666/Kugo-Music-Converter (backend/internal/algo/kgg, based on unlock-music.dev/cli). The latter repository did not expose a root LICENSE during inspection. Do not assume these components are available under a permissive license. Modifications include streaming restoration, complete TEA padding validation, in-memory SQLite loading, signature caching, cancellation and Chinese diagnostics. GPL terms: https://www.gnu.org/licenses/gpl-3.0.html. Bundled application distribution must preserve applicable licensing and corresponding-source obligations.

- XM restoration and xm_encryptor.wasm: adapted from https://github.com/DemaciaForge/Ximalaya-Decrypt-Enhance (main.py fetched 2026-09-30). Upstream https://github.com/sld272/Ximalaya-XM-Decrypt and Aynakeya's XM analysis. The upstream README does not grant a unified license for every component. These notices do not grant additional redistribution rights.
- PySide6 Essentials / Qt: LGPLv3 / GPLv3 / commercial licenses as supplied by Qt. https://www.qt.io/licensing/
- FFmpeg: binary supplied by imageio-ffmpeg 0.6.0. Actual build flags/license: ffmpeg -buildconf and ffmpeg -L. https://ffmpeg.org/legal.html
- imageio-ffmpeg: BSD-2-Clause. https://github.com/imageio/imageio-ffmpeg
- Wasmtime Python: Apache-2.0 with LLVM exception. https://github.com/bytecodealliance/wasmtime-py
- mutagen: GPL-2.0-or-later. https://github.com/quodlibet/mutagen
- PyCryptodome: BSD / public-domain components. https://www.pycryptodome.org/
- Send2Trash: BSD-3-Clause. Its Windows progress sink is reused with a strict failure exception to prohibit permanent deletion. https://github.com/arsenetar/send2trash
- pywin32: Python Software Foundation license. https://github.com/mhammond/pywin32
- PyInstaller: GPL with an exception for produced applications. https://pyinstaller.org/

This local build does not declare all third-party components redistributable under one new license. Preserve applicable notices and satisfy respective licenses before distributing further.
