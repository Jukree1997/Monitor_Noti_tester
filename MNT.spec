# PyInstaller spec for Monitor_Noti_tester (GUI).
#
# Build:
#     pyinstaller --clean --noconfirm MNT.spec
#
# Output:
#     dist/MNT/MNT(.exe)   ← entry point
#     dist/MNT/_internal/  ← bundled Python, Qt plugins, CUDA libs, etc.
#
# One-folder mode. Faster startup, easier to debug, customers can drop
# user data (models/, config/) next to the exe.
#
# Bundle size discipline:
#   - We DON'T collect_all() on packages whose transitive closures
#     include torch (supervision, trackers). Those pull in torchaudio,
#     triton, numba/llvmlite, polars/pyarrow — gigabytes of bloat we
#     never use at runtime (we run inference via onnxruntime, not torch).
#   - We DO collect_all('nvidia.cudnn') specifically — onnxruntime-gpu
#     dlopens libcudnn.so.9 lazily and we preload it in core/onnx_runtime.py.
#   - Aggressive excludes below cut every torch/numba/polars transitive
#     dep so we land around ~1-2 GB instead of 8+ GB.

from PyInstaller.utils.hooks import collect_all, collect_submodules

# ─────────────────────────────────────────────────────────────────────
# Targeted dependency collection
# ─────────────────────────────────────────────────────────────────────

# onnxruntime — ships its own bundled CUDA runtime in its native libs,
# plus Python wrappers for the C++ session. collect_all is correct here.
ort_datas, ort_binaries, ort_hidden = collect_all('onnxruntime')

# nvidia.cudnn ONLY — not the rest of the nvidia.* packages (which are
# torch's CUDA deps, gigabytes we don't need). See core/onnx_runtime.py
# `_preload_cudnn` for the runtime side that depends on these libs.
try:
    cudnn_datas, cudnn_binaries, cudnn_hidden = collect_all('nvidia.cudnn')
except Exception:
    cudnn_datas, cudnn_binaries, cudnn_hidden = [], [], []

# PySide6 — auto-collected by the official hook in most cases but
# explicit is safer (Qt plugins, platform integration libs).
pyside_datas, pyside_binaries, pyside_hidden = collect_all('PySide6')

# Filter out Qt modules we don't import — saves ~288 MB on disk,
# critical for keeping the AppImage under GitHub Releases' 2 GiB
# per-file limit. Our code only uses QtCore / QtGui / QtWidgets.
_QT_EXCLUDE_PATTERNS = (
    # Qt WebEngine (Chromium engine, 194 MB) — we don't render web pages
    'libQt6WebEngine', 'QtWebEngine', 'qtwebengine',
    # Qt Quick / QML stack — we use Widgets, not QML
    'libQt6Quick', 'libQt6Qml', 'QtQuick', 'QtQml',
    'libQt6Labs', 'qmlls', 'qmlplugindump',
    # Other unused Qt modules
    'libQt6Designer', 'libQt6Pdf', 'libQt6PdfQuick', 'libQt6PdfWidgets',
    'libQt6ShaderTools',
    'libQt6Charts', 'libQt6DataVisualization',
    'libQt6Bluetooth', 'libQt6Nfc', 'libQt6Positioning',
    'libQt6SerialPort', 'libQt6SerialBus',
    'libQt6Multimedia',  # we use opencv for video, not QtMultimedia
    'libQt6Test',        # unit-test framework, not needed at runtime
    'libQt6Help',        # online help system
    'libQt6Sql',         # we don't use a Qt-managed DB
)

def _keep_pyside_item(item):
    """Return True if a (dest, src, type) tuple should stay in the
    bundle. Drops anything whose destination path contains one of
    the excluded module names above."""
    dest = (item[0] if item else '') or ''
    return not any(p in dest for p in _QT_EXCLUDE_PATTERNS)

pyside_binaries = [b for b in pyside_binaries if _keep_pyside_item(b)]
pyside_datas = [d for d in pyside_datas if _keep_pyside_item(d)]
pyside_hidden = [h for h in pyside_hidden
                 if not any(p.replace('lib', '').replace('Qt6', 'Qt')
                            in h for p in _QT_EXCLUDE_PATTERNS)]


# ─────────────────────────────────────────────────────────────────────
# Hidden imports
# ─────────────────────────────────────────────────────────────────────
#
# These need to be listed because PyInstaller's static analysis misses
# them (dynamic imports, namespace packages, platform-specific shims).

hidden = [
    # py-machineid imports machineid.linux / .windows / .darwin at runtime
    'machineid',
    # cryptography Fernet uses the openssl backend
    'cryptography.hazmat.backends.openssl',
    # supervision + trackers — listed as hidden imports rather than
    # collect_all to avoid dragging in torch.
    'supervision',
    'trackers',
] + ort_hidden + cudnn_hidden + pyside_hidden


# ─────────────────────────────────────────────────────────────────────
# Analysis (GUI entry)
# ─────────────────────────────────────────────────────────────────────

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=ort_binaries + cudnn_binaries + pyside_binaries,
    datas=ort_datas + cudnn_datas + pyside_datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # We use onnxruntime, NOT torch. Cut the entire torch family.
        'torch',
        'torchvision',
        'torchaudio',
        'triton',         # torch GPU compiler — huge, unused
        # Numba/LLVM — pulled in by some sklearn/supervision paths;
        # we don't actually JIT anything at runtime.
        'numba',
        'llvmlite',
        # Dataframe libs — none of our code uses them.
        'pandas',
        'polars',
        'pyarrow',
        # NOTE: matplotlib intentionally NOT excluded — supervision
        # imports matplotlib.colors at top of supervision/draw/color.py
        # for color-name → RGB conversion. Removing matplotlib breaks
        # all of supervision's annotator imports.
        # Interactive shells.
        'tkinter',
        'IPython',
        'jupyter',
        'jupyter_client',
        'jupyter_core',
        'notebook',
        'ipykernel',
        # Avoid pulling in alternate Qt bindings via PIL.
        'PIL.ImageQt',
        # Misc heavyweights that occasionally sneak in via tests / docs.
        'sphinx',
        'pytest',
        'setuptools.command.test',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MNT',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,   # UPX can trigger antivirus false-positives on Windows
    console=False,   # GUI — no console window on Windows. Set True to debug.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='build_assets/icon.ico',   # uncomment once an icon exists
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MNT',
)
