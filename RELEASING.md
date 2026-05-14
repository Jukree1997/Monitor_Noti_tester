# Releasing a new version

The app's update notifier reads `https://api.github.com/repos/Jukree1997/Monitor_Noti_tester/releases/latest`
on startup. To ship a new version that installed customers will see:

## Steps (CI-automated, recommended)

GitHub Actions builds both the Linux AppImage and the Windows installer
automatically on tag push. You don't have to touch your Windows PC.

1. **Bump the version** in `core/version.py`:
   ```python
   __version__ = "1.0.1"
   ```
   Follow [semver](https://semver.org/): `MAJOR.MINOR.PATCH`.

2. **Commit + tag + push**:
   ```bash
   git add core/version.py
   git commit -m "Release v1.0.1"
   git tag v1.0.1
   git push origin main --tags
   ```

3. **Wait for CI** (~12-15 minutes). Watch progress at:
   `https://github.com/Jukree1997/Monitor_Noti_tester/actions`

   When both `build-linux` and `build-windows` succeed, a `release`
   job creates the GitHub Release with both installers attached.

4. **Done**. The new release is live at
   `https://github.com/Jukree1997/Monitor_Noti_tester/releases/latest`.

   Customers' running app will detect it within 24h (the auto-check
   interval) and show an update dialog. They click Download → browser
   opens the release page → they download the installer for their OS
   → run it.

## Steps (manual fallback)

If CI is broken or you need to ship a release without going through
GitHub Actions:

1. Bump `__version__` + tag as above.
2. On a Linux machine: `pyinstaller --clean --noconfirm MNT.spec && ./build_appimage.sh`
3. On a Windows machine: see `WINDOWS_BUILD.md`
4. `gh release create v1.0.1 MNT-1.0.1-x86_64.AppImage Output/MNT-Setup-1.0.1.exe --title "v1.0.1" --generate-notes`

## Dry-run testing the workflow

Before pushing a real tag, you can test the CI build without creating
a Release:

1. Go to https://github.com/Jukree1997/Monitor_Noti_tester/actions/workflows/release.yml
2. Click **Run workflow** → pick `main` → confirm.
3. CI builds both installers and uploads them as 7-day workflow
   artifacts. No release is created.
4. Download the artifacts from the workflow run page, test them
   locally.

This is the right move when you've changed `MNT.spec`, `MNT.iss`, or
`build_appimage.sh` and want to verify the builds still work before
committing to a real tag.

## What the release URLs look like (for reference)

- API endpoint the updater hits:
  `https://api.github.com/repos/Jukree1997/Monitor_Noti_tester/releases/latest`
- Release page customers land on after clicking Download in the app:
  `https://github.com/Jukree1997/Monitor_Noti_tester/releases/tag/v1.0.1`
- Direct installer URLs (linkable from your sales page):
  - `…/releases/download/v1.0.1/MNT-Setup-1.0.1.exe`
  - `…/releases/download/v1.0.1/MNT-1.0.1-x86_64.AppImage`

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
