# Packaging MNT with PyInstaller

This is the spike doc — a working build on Linux with the spec in
`MNT.spec`. The Windows build is expected to mostly Just Work but
needs end-to-end verification on a real Windows machine, plus
follow-up work for the installer (Inno Setup or NSIS).

## Quick start

```bash
# From the project root with the dev environment active:
pip install pyinstaller
pyinstaller --clean --noconfirm MNT.spec
```

Output:
- `dist/MNT/MNT` (or `MNT.exe` on Windows) — entry point
- `dist/MNT/_internal/` — bundled Python runtime, Qt plugins, CUDA libs, etc.

To launch:
```bash
./dist/MNT/MNT          # Linux
.\dist\MNT\MNT.exe      # Windows
```

## Bundle size

Around **3.8 GB** on Linux, dominated by GPU runtime libs (mandatory
for the CUDA inference path):

| Component | Size | Why it's there |
|---|---|---|
| `nvidia/cudnn/` | ~1 GB | cuDNN — onnxruntime-gpu dlopens this |
| `PySide6/` | ~640 MB | Qt 6.x runtime + plugins |
| `libcublasLt.so.12` | ~485 MB | onnxruntime CUDA EP runtime |
| `onnxruntime/` | ~430 MB | Python + native session libs |
| `libcufft.so.11` | ~185 MB | onnxruntime CUDA EP runtime |
| `opencv_contrib_python.libs/` | ~115 MB | OpenCV codec deps |
| Everything else | ~900 MB | Python stdlib, numpy, scipy, etc. |

The Windows bundle will be roughly the same — most of the bulk is
cross-platform pip-installed packages.

## What the spec does (and doesn't)

The spec was tuned during the spike to avoid common bloat pitfalls:

### Aggressive excludes
We DON'T use torch — but several transitive deps (supervision,
trackers) would pull in `torch` + `torchaudio` + `triton` + numba +
polars unless explicitly excluded. The spec lists these explicitly
in `excludes=[]`. Bundle went from 8.4 GB → 3.8 GB after this pass.

### Targeted `collect_all`
PyInstaller's `collect_all('nvidia')` would grab every CUDA package
including the ones torch depends on (cublas, cufft, cusparse, nccl,
…). We only need `nvidia.cudnn`. The spec calls `collect_all('nvidia.cudnn')`
specifically.

### Hidden imports instead of `collect_all` for supervision/trackers
We listed `supervision` and `trackers` as hidden imports rather than
running `collect_all` on them, because `collect_all` would re-pull
their torch deps even with `torch` excluded.

### `matplotlib` is NOT excluded
Even though MNT doesn't plot anything, `supervision/draw/color.py`
imports `matplotlib.colors` at module level for color-name → RGB
conversion. Excluding matplotlib breaks all supervision imports.

## Known launch behavior

When you run the bundle:
1. **Faulthandler installs first** (~instant) — see `main.py`.
2. **PyInstaller bootloader extracts/links libs** (~1-2 s in
   one-folder mode; faster on subsequent runs because OS file cache).
3. **Python imports** (~2-3 s) — biggest CPU chunk; PySide6, cv2,
   onnxruntime, our app code.
4. **License manager loads cache** (instant) — `~/.local/share/MNT/license.dat`
   on Linux, `%APPDATA%/Baksters/MNT/license.dat` on Windows.
5. If unlicensed → **Activation dialog** appears, app waits for input.
6. If licensed → **MainWindow** opens, auto-update check fires in
   background (~5 s timeout if offline).

## Frozen-mode path handling

`core/paths.py::app_dir()` returns:
- **Dev**: `<repo>/` (resolved from `core/paths.py`'s location)
- **Frozen**: `<install>/` (resolved from `sys.executable`)

So `default_models_dir()` returns `<install>/models/` in the bundle —
customers can drop `.onnx` files there and the file picker will open
to that location.

The license cache (`secure_storage.py`) uses `platformdirs.user_data_dir`
which is OS user-data, NOT the install dir, so the license survives
uninstall/reinstall.

`core/worker_process.py` and `main.py` both guard their `sys.path`
hacks with `if not getattr(sys, "frozen", False)` — those are dev-only
fixes for `python main.py` and have no effect in the bundle.

## Verifying the bundle on Linux

```bash
# Headless smoke test — proves imports + license init work.
# App will sit waiting on the activation dialog in offscreen mode;
# timeout after 15 s to confirm "still running" rather than "crashed".
timeout 15 env QT_QPA_PLATFORM=offscreen ./dist/MNT/MNT
echo "Exit code: $?"  # 124 = timed out (process killed) = good
```

Full UI test:
```bash
./dist/MNT/MNT
# → Activation dialog appears. Paste a Keygen license key. Should
#   reach MainWindow within ~3 seconds.
```

## Windows-specific TODOs (not done in spike)

These are deliberate gaps left for the next packaging pass:

1. **App icon + version metadata** — set `icon='build_assets/icon.ico'`
   in the EXE() block + Windows version info (CompanyName, ProductName,
   etc.). Need an `.ico` file first.
2. **Installer** — wrap `dist/MNT/` into an Inno Setup or NSIS installer
   so customers double-click an `.exe` instead of unzipping a folder.
   Should also create a Start Menu shortcut + Desktop shortcut + uninstaller.
3. **Code signing** — Windows SmartScreen warns on unsigned binaries.
   An OV/EV code-signing certificate (~$100-400/yr) makes the warning
   go away. Not required for v1 but reduces "is this safe?" support
   tickets.
4. **headless_main.py** — currently NOT included as a separate
   executable. If customers need to run headless mode on the install,
   add a second EXE() block in the spec or pass `--add-data` to bundle
   the headless entry alongside.
5. **Auto-update integration** — the update notifier opens the
   browser to the GitHub release page. The customer downloads the new
   installer and runs it manually. Windows can lock files in use —
   the installer needs to detect a running MNT process and prompt to
   close it before overwriting.

## Troubleshooting

### "ModuleNotFoundError: No module named X"
Add `X` to the `hidden=[]` list in the spec. If it's a package with
submodules, use `collect_submodules('X')` or `collect_all('X')`.

### Bundle is suddenly larger
Run `du -sh dist/MNT/_internal/* | sort -hr | head -10` to find the
new big folder. Common culprit: a new dep introduced a transitive
dependency on torch / numba / polars. Add those to the spec's
`excludes` list.

### App launches but no GUI shows
On Linux, check `DISPLAY` is set and X11 is available. The bundle
respects `QT_QPA_PLATFORM=offscreen` if the env var is set — useful
for headless smoke tests.

### Build is slow
The first build is ~2 min. Subsequent builds reuse the `build/`
cache and are ~30 s. Use `pyinstaller --clean` when you've changed
the spec or deps.

### CUDA inference falls back to CPU silently
Check `dist/MNT/_internal/nvidia/cudnn/lib/libcudnn.so.9` exists. If
missing, `collect_all('nvidia.cudnn')` didn't run properly — likely a
naming change in the nvidia-cudnn-cu12 package; check the pip package's
actual layout.
