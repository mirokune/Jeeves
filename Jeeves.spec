# -*- mode: python ; coding: utf-8 -*-
"""
Jeeves.spec — PyInstaller build specification for JeevesBot.

Build with:   pyinstaller Jeeves.spec
Or use:       build.bat

Output:       dist/Jeeves/Jeeves.exe
"""

import os
import sys

block_cipher = None

spec_dir = os.path.dirname(os.path.abspath(SPEC))

# Cog modules are loaded dynamically via bot.load_extension(), so PyInstaller
# can't auto-detect them. Discover every top-level module next to Jeeves.py
# instead of maintaining a hand-written list — a forgotten entry here shows up
# at runtime as "Extension '<name>' could not be loaded or found".
hidden_imports = sorted(
    os.path.splitext(f)[0]
    for f in os.listdir(spec_dir)
    if f.endswith('.py') and f != 'Jeeves.py'
)
print('[Jeeves.spec] hidden imports:', ', '.join(hidden_imports))

# Conda environments keep the native DLLs behind several stdlib extension
# modules in <env>/Library/bin, which PyInstaller only finds if that folder is
# on PATH (it does not scan pathex for binary dependencies). Ship them
# explicitly — without libcrypto/libssl the bundled _ssl.pyd has nothing to
# load, `import ssl` fails, and the bot never reaches Discord. Non-conda
# Pythons have no such folder and skip this entirely.
CONDA_RUNTIME_DLLS = [
    'libcrypto-3-x64.dll',   # _ssl, _hashlib
    'libssl-3-x64.dll',      # _ssl
    'ffi.dll', 'ffi-8.dll',  # _ctypes
    'sqlite3.dll',           # _sqlite3
    'liblzma.dll',           # _lzma
    'libbz2.dll',            # _bz2
    'libexpat.dll',          # pyexpat
    'zlib.dll',
]

conda_binaries = []
conda_dll_dir = os.path.join(sys.prefix, 'Library', 'bin')
if os.path.isdir(conda_dll_dir):
    for dll in CONDA_RUNTIME_DLLS:
        path = os.path.join(conda_dll_dir, dll)
        if os.path.isfile(path):
            conda_binaries.append((path, '.'))
    print('[Jeeves.spec] conda DLLs bundled:',
          ', '.join(os.path.basename(b[0]) for b in conda_binaries) or 'none')

a = Analysis(
    [os.path.join(spec_dir, 'Jeeves.py')],
    pathex=[spec_dir],
    binaries=conda_binaries,
    datas=[
        (os.path.join(spec_dir, 'config.env.example'), '.'),
    ],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Jeeves',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Jeeves',
)
