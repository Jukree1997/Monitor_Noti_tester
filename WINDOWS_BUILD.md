# Building MNT on Windows

Step-by-step for producing `MNT-Setup-1.0.0.exe` on a Windows machine.
This is what your customers will download and double-click. The Linux
build (AppImage) is separate — see `build_appimage.sh`.

You only need to do this once per release version. After the first
build the cycle is just: bump `__version__` → re-run pyinstaller → re-run
Inno Setup → upload the new `Setup-*.exe` to GitHub Releases.

---

## 1. Prerequisites (one-time setup)

### 1a. Install Python 3.12

Download from https://www.python.org/downloads/windows/ — pick the
"Windows installer (64-bit)". **Check the "Add Python to PATH" box**
during install.

Verify in PowerShell:
```powershell
python --version
# Should print: Python 3.12.x
```

### 1b. Install Git

https://git-scm.com/download/win — accept defaults.

### 1c. Install Inno Setup 6

https://jrsoftware.org/isdl.php → "Stable Release" → run the
installer. Accept defaults. Inno Setup is free.

After install, verify the compiler is reachable:
```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /?
# Should print Inno Setup Command-Line Compiler help
```

### 1d. NVIDIA driver (if testing GPU inference)

If the Windows machine has an NVIDIA GPU, install the latest NVIDIA
GeForce/Studio driver from https://www.nvidia.com/Download/index.aspx.
You do NOT need the CUDA Toolkit — our bundle ships its own cuDNN.

---

## 2. Clone + install deps (one-time per machine)

In PowerShell:

```powershell
cd $HOME\Documents
git clone https://github.com/Jukree1997/Monitor_Noti_tester.git
cd Monitor_Noti_tester
git checkout main

# Use a venv so deps don't pollute the system Python
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If the activate command is blocked by PowerShell's execution policy:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller
```

Total install size: ~6 GB (mostly CUDA libs that come with
onnxruntime-gpu + nvidia-cudnn-cu12).

---

## 3. Build the PyInstaller bundle (every release)

```powershell
# From the repo root, with .venv active:
pyinstaller --clean --noconfirm MNT.spec
```

Takes ~3 minutes on first build, ~1 minute on rebuilds. Output:
`dist\MNT\MNT.exe` plus `dist\MNT\_internal\` with bundled libs.

Smoke-test the bundle directly before packaging:
```powershell
.\dist\MNT\MNT.exe
```
If the activation dialog appears (or MainWindow opens if your
license cache survives), the bundle is good. Close it.

---

## 4. Compile the Inno Setup installer (every release)

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" MNT.iss
```

Takes ~30-60 seconds (LZMA2/ultra64 compression on ~3.8 GB of files).
Output: `Output\MNT-Setup-1.0.0.exe` (~1.5-2 GB single file).

---

## 5. Verify the installer

On the build machine itself, or even better on a **clean test PC** with
no Python and no dev tools:

```powershell
# Run the installer — it walks you through the wizard
.\Output\MNT-Setup-1.0.0.exe
```

You should see:
1. UAC prompt (admin install)
2. License agreement screen (LICENSE.txt content)
3. Install path picker (defaults to `C:\Program Files\MNT\`)
4. "Create desktop icon" optional checkbox
5. File copy progress (will take ~30-60s for the ~3.8 GB)
6. "Launch MNT" optional checkbox at end

After install, confirm:
- Start Menu shows "Baksters Notification Runner"
- (Optional) Desktop has the MNT icon
- `C:\Program Files\MNT\MNT.exe` exists
- Apps & Features lists "Baksters Notification Runner" with an uninstaller

Launch MNT. Activate with a Keygen license key. Verify the app works.

---

## 6. Test the upgrade flow

Bump the version + rebuild + repeat install:

```powershell
# Edit core\version.py — change __version__ = "1.0.1"
pyinstaller --clean --noconfirm MNT.spec
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" MNT.iss
# Produces Output\MNT-Setup-1.0.1.exe
.\Output\MNT-Setup-1.0.1.exe
```

Expected:
- Installer detects existing install (via the AppId GUID in MNT.iss)
- If MNT is currently running, prompts you to close it
- Overwrites files in place — does NOT prompt for a new install path
- **License cache survives** the upgrade (it lives in `%APPDATA%\Baksters\MNT\`,
  not the install dir). So you don't need to re-activate.
- Launch the new install — Help → About should show v1.0.1.

---

## 7. Cut a GitHub release with the installer

After verifying the installer on a clean machine:

```powershell
git tag v1.0.0
git push origin v1.0.0
gh release create v1.0.0 .\Output\MNT-Setup-1.0.0.exe `
    --title "v1.0.0" `
    --notes "Initial release."
```

Customers (and the in-app update notifier) will now see this release
on https://github.com/Jukree1997/Monitor_Noti_tester/releases.

---

## Common gotchas

### "ISCC.exe is not recognized"
Use the full path: `& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"`. Or
add Inno Setup to PATH.

### Installer compilation fails with "file not found"
You probably didn't run pyinstaller first, or you ran it from a
different directory. The `.iss` script references `dist\MNT\*` —
that must exist relative to the .iss file's location.

### Bundle launches on the build machine but crashes on a clean PC
Most likely: a missing system library. PyInstaller bundles a lot
but not literally everything. Test on a clean Windows 10 VM. If you
see `api-ms-win-*` errors, the customer needs the Visual C++ 2015-2022
Redistributable: https://aka.ms/vs/17/release/vc_redist.x64.exe

### "This app can't run on your PC" SmartScreen warning
Windows SmartScreen flags unsigned executables. You can either:
- Click "More info" → "Run anyway" (acceptable for early testing)
- Buy a code-signing certificate (~$100-400/yr) — eliminates the
  warning entirely. Use a Standard Code Signing certificate from
  Certum / Sectigo / DigiCert. Sign the .exe with `signtool sign`
  before Inno Setup compilation. See Microsoft docs for details.
- Note: unsigned installers also get a slower "build reputation"
  through SmartScreen — every download contributes. Code signing
  short-circuits that.

### Installer is huge
Yes, ~1.5-2 GB compressed. That's the cost of bundling cuDNN + Qt + ONNX
Runtime. No realistic way to shrink without dropping GPU inference. For
comparison, NVIDIA's own apps (like Broadcast Suite) are similar size.

### Update notifier doesn't fire
The notifier needs the running app's version to be LOWER than the
latest GitHub release's tag. If they match, the app correctly says
"you're up to date". To force-test, temporarily set `__version__ = "0.0.1"`
in core\version.py and re-launch (don't rebuild the installer for this
test — just edit + run from source, or rebuild quickly).
