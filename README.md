# iCloud Photo Downloader

> Made by **[iServis Beograd - iPhone Srbija](https://www.iphone-srbija.com)**, an Apple
> device repair and service shop in Belgrade. We use this tool every day to move
> customers' photo libraries off iCloud.

A small, robust macOS app that downloads **all** photos and videos from an iCloud
account to a local folder. It is a simple graphical front-end around
[`icloudpd`](https://github.com/icloud-photos-downloader/icloud_photos_downloader),
the well-maintained, open-source iCloud Photos downloader.

It is built for real-world jobs: customer libraries that are 200-300 GB, flaky
connections, and transfers that may run for many hours.

## What it does (and does not do)

- **Read-only on iCloud.** It only downloads. It never deletes, moves, or changes
  anything in iCloud. The app refuses to pass any deletion flag to icloudpd, even
  if you try to add one in the advanced field.
- **Resumable, no overwrite.** If a transfer is interrupted (crash, reboot, network
  drop, or you press Stop), just press **Start** again. Files already on disk are
  skipped, not re-downloaded and never overwritten, so it continues where it left
  off. This is what makes very large libraries practical.
- **No 1000-photo limit.** The 1000-item cap you hit on iCloud.com is a limitation
  of the *website's* download button only. This app uses the iCloud API and pulls
  the entire library.
- **Handles two-factor authentication (2FA).** It prompts you for the 6-digit code
  Apple sends to your trusted devices.
- **Original quality, no conversion.** Files are downloaded exactly as stored in
  iCloud, at full resolution. HEIC stays HEIC, RAW stays RAW (DNG/CR2/...), video
  keeps its original format. Nothing is recompressed or converted to JPG. Photos,
  videos, Live Photos and RAW are all supported.

---

## 1. Prepare the iCloud account (important)

On the iPhone/iPad that is signed into the account you want to download:

1. **Turn ON** `Settings > [your name] > iCloud > Access iCloud Data on the Web`.
2. **Advanced Data Protection (ADP):** either turn it **OFF**
   (`Settings > [your name] > iCloud > Advanced Data Protection`), **or** leave it on
   and tick **"Account uses Advanced Data Protection"** in the app. With that option,
   the first sign-in shows an approval request on the iPhone/iPad/Mac. Tap **Allow**.

If web access is off (or ADP is on and the option isn't ticked), Apple returns an
`ACCESS_DENIED` error and nothing can be downloaded. The account should use
standard **two-factor authentication** (6-digit codes) or a **security key** (YubiKey);
both work.

> Tip: if you turned ADP off for the job, you can turn it back on afterwards.

---

## 2. Requirements

- macOS (works on **10.13 High Sierra** and newer).
- **Python 3.9 or newer** with Tkinter.
  - On High Sierra, Homebrew is no longer supported, so install Python from the
    official installer: <https://www.python.org/downloads/macos/>. Choose
    **Python 3.12** (it runs on High Sierra and includes the Tcl/Tk that the
    graphical window needs). On newer macOS, any current Python 3 from python.org
    is fine.

---

## 3. Install (one time)

1. Keep all the files in this folder together.
2. Open **Terminal**, then drag this folder onto the Terminal window after typing
   `cd ` (note the trailing space), and press Return. For example:

   ```
   cd "/path/to/iCloud Photos Download"
   ```
3. Run the setup script:

   ```
   bash install.sh
   ```

   It creates a private environment in `.venv` inside this folder and installs
   icloudpd there. It does **not** need administrator rights and does not change
   your system Python. Re-running it later upgrades icloudpd to the newest version.

---

## 4. Run

Double-click **`Launch iCloud Downloader.command`**.

> First time only: macOS Gatekeeper may say the file "cannot be opened because it
> is from an unidentified developer." Right-click the file → **Open** → **Open**,
> or allow it in `System Settings/Preferences > Security & Privacy`. You only need
> to do this once.

In the window:

1. **Apple ID (email)** — the iCloud account to download from.
2. **Password** — the iCloud password. (See the note on app-specific passwords in
   Troubleshooting if your account requires it.)
3. **Save photos to** — click **Browse...** and pick the destination folder.
4. (Optional) **Sign in** first — this authenticates and stores the session
   without downloading anything, so you can confirm the credentials and 2FA work.
5. Click **Start download**.
6. When prompted, enter the **6-digit verification code** Apple sends to your
   trusted device(s). If no code shows up, the dialog lists the trusted phone
   numbers. Type the letter shown next to one (e.g. `a`) to get the code by SMS
   instead. Accounts that use a security key are asked to touch the key.

You will see live status, a progress bar, counts (total / downloaded / already on
disk / remaining), speed, ETA, elapsed time, and a scrolling activity log.

### Stopping and resuming

Press **Stop** at any time, or just close the window. Nothing is lost. If the
connection drops by itself, **Auto-retry** restarts the download for you. The next
time you press **Start** with the same destination folder, the app skips everything
already downloaded and continues. After the first successful sign-in, the session
is remembered, so resuming usually does **not** ask for the password or 2FA again
(until Apple expires the session, typically after a couple of months).

### Where files are saved

Use the **Folder organization** dropdown to choose how files are grouped inside
your destination folder:

- **By month** -> `2024/06/IMG_0001.HEIC`
- **By day** -> `2024/06/15/IMG_0001.HEIC`
- **By year** -> `2024/IMG_0001.HEIC`
- **All in one folder** -> no subfolders

Live Photos are saved as a still image plus a matching video file.

> Tip: choose the organization **before** the first run. If you change it after some
> files are already downloaded, the new layout won't match the old one, so those
> files would be downloaded again into the new folders.

### How fast is it?

icloudpd downloads files one at a time. There is no "turbo" setting because Apple
throttles iCloud downloads on its side, so the speed is whatever your connection
and Apple allow. A 200-300 GB library can take several hours. Do **not** run
several copies at once against the same account to go faster -- that can trigger
Apple throttling or a temporary account lock. One run, left to finish (or resumed),
is the safe approach.

---

## Buttons and options

**Buttons**

- **Sign in** — signs in and saves the session without downloading (Start and Preview sign in by themselves too). Use it
  to confirm the password and 2FA before a long run.
- **Preview (count)** — does a "dry run": it counts how many items match your current
  settings and reports the number plus the free space on the destination drive,
  **without downloading anything**. Good for sizing a job.
- **Start download** — begins (or resumes) the download.
- **Stop** — stops the current task. Already-downloaded files are kept; press Start to
  resume.
- **Sign out** — clears the saved iCloud session (`~/.icloud_photo_downloader/session`)
  **and** the customer's details (Apple ID, password, destination folder, album, dates;
  Quick update is turned off). Use it between customers. General preferences such as
  folder organization stay. Your downloaded photos are not touched.

**Options**

- **Photos only (skip videos)** / **Skip Live Photo videos** — leave out videos, or
  just the short video part of Live Photos (fewer files).
- **Set photo date from EXIF (if missing)** — writes the capture date into the photo's
  EXIF when it's absent, so the files sort correctly elsewhere.
- **Save a log file in the destination** — writes an `icloud_download_log_<date>.txt`
  into the destination folder recording what was transferred (handy as a job record).
- **Folder organization** — group files by month, day, year, or all in one folder.
- **Quick update (incremental re-sync)** — checks newest-first and stops after the
  chosen number of already-downloaded files in a row. Turns a repeat run into a quick
  top-up instead of re-checking the whole library. (Leave it OFF for the first full
  download.)
- **Album** — download only one album. Click **List albums** first to see the exact
  names in the activity log, then type/paste one in. Empty = the whole library.
- **From / To (YYYY-MM-DD)** — only download items created on/after "From" and on/before
  "To". Either can be left empty.
- **Advanced icloudpd options** — append extra icloudpd flags for power users. Deletion
  flags are blocked on purpose.
- **Play a sound / notify when finished** — beep + macOS notification when a download
  completes (useful for multi-hour jobs).
- **Keep Mac awake while running** (on by default) — keeps the Mac from going to sleep
  while a download or preview runs (it uses macOS's built-in `caffeinate`). The screen
  may still turn off; that's fine. On a MacBook keep it plugged in and the lid open:
  closing the lid still puts it to sleep.
- **Auto-retry if the download stops with an error** (on by default) — if icloudpd stops
  unexpectedly (internet drop, Apple hiccup), the app waits 1, 2, then 5 minutes and
  starts again by itself. Files already on disk are skipped, so it continues where it
  left off. It gives up after 10 attempts in a row that download nothing new. It does
  **not** retry sign-in problems (wrong password, locked account, ACCESS_DENIED),
  because repeated sign-in attempts can get the account locked. Press **Stop** during
  the countdown to cancel.
- **Account uses Advanced Data Protection** — sign-in asks the Apple device to approve
  web access, so ADP can stay on. Approve the request on the device when it appears.

Your settings (everything except the password) are remembered between runs.

---

## 5. Troubleshooting

| Symptom | What to do |
|---|---|
| `ACCESS_DENIED` | Enable "Access iCloud Data on the Web" on the account, and either disable Advanced Data Protection or tick "Account uses Advanced Data Protection" (see section 1). |
| "Apple rejected the Apple ID or password" | Re-check the email/password. If it is correct, press **Sign out** (clears the saved session) and try again. |
| "iCloud is still preparing (indexing) this photo library" | Apple hasn't finished indexing the library yet (common right after turning on web access or a big upload). Wait a few hours and try again. |
| `ZONE_NOT_FOUND` / "iCloud setup is not complete" in the log | Usually harmless: some libraries are stored in a different iCloud zone, and icloudpd finds it automatically. If the run stops, sign in once at <https://www.icloud.com> and try again. |
| The 2FA dialog never appears, or sign-in hangs | Use **Sign in** by itself first. Make sure the account uses standard two-factor authentication. |
| "Could not find icloudpd" | Run `bash install.sh` again, and launch via `Launch iCloud Downloader.command` so it uses the right Python. |
| The graphical window does not open / Tkinter error | Install Python from python.org (it bundles Tcl/Tk) and re-run `install.sh`. |
| `pip` build/compiler error during install | Install Apple's command line tools: `xcode-select --install`, then re-run `install.sh`. |
| Transfer seems stuck on "Scanning iCloud library..." | For very large libraries the initial enumeration can take several minutes before downloads begin. This is normal. |

### Advanced

The **Advanced icloudpd options** field lets you append extra icloudpd command-line
flags (for example `--skip-screenshots`, `--skip-live-photos`, or `--recent 500` to only check the 500
most recently added items for a faster top-up). Run `.venv/bin/icloudpd --help` for
the full list. Deletion flags are blocked on purpose.

---

## Notes on safety and privacy

- The password is typed directly into the local icloudpd process through a
  pseudo-terminal; it is **not** placed on the command line, so it does not appear
  in `ps` output or shell history.
- The login session (cookies/tokens) is stored under
  `~/.icloud_photo_downloader/session`. Delete that folder to force a fresh sign-in,
  or to remove a customer's session after a job.
- icloudpd is open-source under the MIT license. This app simply drives it.

---

## Disclaimer

This is an independent project. It is **not affiliated with, endorsed by, or
sponsored by Apple Inc.** iCloud, iPhone and Mac are trademarks of Apple Inc.,
registered in the U.S. and other countries. The software is provided "as is", without
warranty of any kind (see [LICENSE](LICENSE)).

---

Made with care by [iServis Beograd - iPhone Srbija](https://www.iphone-srbija.com).
