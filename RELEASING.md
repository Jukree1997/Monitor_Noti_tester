# Releasing a new version

The app's update notifier reads `https://api.github.com/repos/Jukree1997/Monitor_Noti_tester/releases/latest`
on startup. To ship a new version that installed customers will see:

## Steps

1. **Bump the version** in `core/version.py`:
   ```python
   __version__ = "1.0.1"
   ```
   Follow [semver](https://semver.org/): `MAJOR.MINOR.PATCH`.

2. **Commit + tag**:
   ```bash
   git add core/version.py
   git commit -m "Release v1.0.1"
   git tag v1.0.1
   git push origin main --tags
   ```

3. **Build the installer** (separate packaging task — see future
   `PACKAGING.md` once PyInstaller spec is in place):
   ```bash
   pyinstaller MNT.spec    # produces dist/MNT-Setup-1.0.1.exe
   ```

4. **Publish the GitHub Release**:
   ```bash
   gh release create v1.0.1 ./dist/MNT-Setup-1.0.1.exe \
     --title "v1.0.1" \
     --notes "$(cat CHANGELOG-1.0.1.md)"
   ```
   - The `tag_name` (here `v1.0.1`) is what the update checker
     compares against `core.version.__version__`. The leading `v` is
     stripped before comparison.
   - The `body` (release notes) is displayed in the update dialog,
     truncated to ~500 chars with "see full notes on the release page"
     if longer.
   - The installer attached to the release is what the user downloads
     when they click the **Download** button (their browser opens the
     release page).

## Verifying the update notifier picked up the new release

On a dev machine with the previous version installed:

1. Wait up to 24h (auto-check is cached), OR open the app and click
   **Help → Check for updates…** to force an immediate check.
2. The update dialog should appear with the new version + release
   notes + Download button.
3. Clicking Download opens
   `https://github.com/Jukree1997/Monitor_Noti_tester/releases/tag/v1.0.1`
   in the browser.

## Skipping or pre-releasing

- **Pre-release** (beta): mark the GitHub release as "Pre-release". The
  current update checker uses `releases/latest`, which **skips
  pre-releases** — so betas won't notify customers. This is intentional
  for v1 (no beta channel yet).
- **Skipped versions**: if a user clicks "Skip this version" in the
  update dialog, that exact version string is stored in their
  QSettings. The next release with a different version string will
  notify them again.
