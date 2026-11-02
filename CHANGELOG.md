# Changelog

All notable changes to MediaWrangler are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). Before 1.0, each minor version is
one build stage of the roadmap.

## [Unreleased]

## [0.12.102] - 2026-11-02

### Changed
- Deploy with one `docker-compose.yml` that pulls `incredibad/mediawrangler` from Docker Hub;
  its settings sit in the file (or an optional `.env`). It replaces `compose.yaml`.
- No secret key to make by hand: the key that encrypts your apps' API keys is made on first
  start as `secret.key` in the data folder and goes into every backup. `MW_SECRET_KEY_FILE`
  still works for keeping it elsewhere. The README says what the key is for.
- Images are built and pushed by GitHub Actions: `dev` → `:dev`, `main` → `:latest` and the
  version, for amd64 and arm64, after lint and tests pass.

## [0.12.101] - 2026-11-02

### Changed

- A watch change reported by webhook goes straight through: Local takes in only the person and
  title the webhook names, only that title's writes are planned, and the queue is sent. This takes
  about 0.2 seconds in MediaWrangler (it was a full pass over everyone, 18 seconds before 0.12.100
  and about 5 after), so the time from a tap in one app to the other is mostly the apps' own.
  The full Local refresh and write cycle still run after every read, as the safety net for
  anything a webhook missed. More than 500 new changes at once (a read's worth) take the full
  path instead.

## [0.12.100] - 2026-11-02

### Changed

- A watch change reaches the other app in about 8 seconds instead of 20 to 35. Each Local update
  and each write plan rebuilt every app's list of items (about 27,600 each for Jellyfin and Silo)
  once per person, 16 times over; it is now built once per pass and shared. On a copy of the live
  database a Local update went from 11.7 to 3.5 seconds, and planning for everyone from 6.0 to 1.9.

## [0.12.99] - 2026-11-02

### Fixed

- An unwatch in Silo could be undone instead of reaching Jellyfin. While Local was still taking
  in a change (it lagged about 24 seconds during a full Jellyfin read), a write was planned from
  the old Local: Silo now showed the episode unwatched while Local still had it watched, so
  MediaWrangler marked it watched in Silo again, and that write's echo then undid the unwatch in
  Local. Writes for a person are now neither planned nor sent until Local has taken in every
  change their apps have reported (echoes of MediaWrangler's own writes aside).

## [0.12.98] - 2026-11-02

### Changed

- An unwatch that an app reports by webhook is now sent to the other apps straight away. Before,
  it had to stand for 30 seconds, and nothing woke the writer once those 30 seconds were up, so
  it waited for the next unrelated event or sync (38 seconds to almost 2 minutes in practice, and
  possibly hours). A quick watch, unwatch, watch still ends at parity: each queued write is checked
  against Local just before it is sent. Unwatches seen only by comparing reads still need two reads
  10 minutes apart.

## [0.12.97] - 2026-11-01

### Changed

- Sync now, and every scheduled full sync, first asks the app to scan its libraries (Jellyfin's
  Scan All Libraries, a Silo scan of each library, Sonarr/Radarr's rescan of everything,
  Sportarr's Disk Scan) and waits up to 30 minutes for Jellyfin and the arrs to finish before
  reading. Before this, a sync only re-read what the app already had, so folders deleted on the
  share stayed listed until the app noticed by itself. Watch-state syncs and reads started by a
  webhook do not scan. Skipped while writes are off or paused; each request goes in the audit log.

### Fixed

- Fix → Scan in Jellyfin on a folder deleted from the share now scans the folder above it: a
  show whose folder is gone cannot drop itself, so the old request left it listed.

## [0.12.96] - 2026-11-01

### Changed
- Trash: files an arr has no record of can be moved 500 at a time (was 25), the same limit as
  leftover folders. Each file is still checked against the arr before it moves.

## [0.12.95] - 2026-11-01

### Fixed
- Physical: files on the share that Sportarr has no record of can be moved to the trash from the
  folder page, as for Sonarr and Radarr. The check refused them ("Sportarr does not track this
  folder") because Sportarr's leagues carry no folder; the league is now found by the files it holds.

## [0.12.94] - 2026-11-01

### Fixed
- Physical: after a Sportarr rescan, MediaWrangler reads that league again (`GET /api/leagues/{id}`
  and its files), so its count updates. Sportarr's leagues carry no folder, so the league is found
  by the files it holds in the folder.

## [0.12.93] - 2026-11-01

### Added
- Physical: the Fix menu offers "Ask Sportarr to rescan its library" on the Sports tab. Sportarr
  has no rescan of one league, so it runs its Disk Scan (`POST /api/task/scheduled/disk-scan/trigger`):
  it checks every tracked file and looks for untracked ones. It gives the scan no id to wait on, so
  MediaWrangler reads Sportarr straight away and the page may only catch up on the next sync.

## [0.12.92] - 2026-11-01

### Added
- Settings → Libraries has Move up / Move down on each library. The order sets the library tabs on
  every page (Presence, Physical, Watch states and the rest) and the order of the tables on the overview.

## [0.12.91] - 2026-11-01

### Added

- Physical: a folder can be marked "Not managed by an arr" from its Fix menu, with an optional
  reason, for a show kept out of Sonarr and Radarr on purpose (One Pace: a fan edit with no TVDB
  entry). Its arr cells turn grey ("Not managed · by choice"), it stops counting as "On disk, not in
  any arr", and the arrs' totals on the Overview and Physical pages count its files from the share,
  so there is no red difference from the master. "Manage again" takes the mark off. Nothing is sent
  to any app; both changes are in the audit log, and the marks are part of the settings export.

## [0.12.90] - 2026-11-01

### Fixed

- The "Change which film … thinks this is" search marks the result with the same IDs as the
  other apps' version when they hold it under another title in the same folder on the share
  (Silo and Radarr's Sailor Moon Cosmos Part 1). Before, only a title match counted.

## [0.12.89] - 2026-11-01

### Fixed

- A film that two apps play from the same file but matched to different titles (Jellyfin read
  Sailor Moon Cosmos Part 1 by an old IMDb ID) is now a possible mismatch on the Metadata page.
  The rows share no name, so before, neither row's Fix menu offered to change Jellyfin's match.
  The Fix entry says "film" for films.

## [0.12.88] - 2026-11-01

### Changed
- After a leftover delete (or "Ask Jellyfin to drop this show"), the toast no longer waits for Jellyfin to drop the empty show: it finishes once Jellyfin has been told, and MediaWrangler checks in the background (every 10 seconds, up to 5 minutes) and drops the show from its pages when Jellyfin has. Jellyfin only drops a show whose folder is gone after its file watcher's 60-second delay, which every other change in the same library restarts, so three deletes in a row kept the first toast waiting for over two minutes. A non-recursive refresh of the library does not drop it (measured), so there is no faster call.

## [0.12.87] - 2026-11-01

### Added
- Each app's column heading (Metadata, Physical media, a folder's files, Watch status) links to that app's web UI, in a new tab. The media share has no link.
- "View in <app>" buttons next to the title on a folder page, one for each app that has the show or film, opening its page there: Jellyfin's details page, Silo's item page, Sonarr's and Radarr's title page (their address uses a slug, so each is asked when the page loads; an app that cannot be reached shows no button). Sportarr has no button.

## [0.12.86] - 2026-11-01

### Added
- "Ask Jellyfin to drop this show" in a row's Fix menu on the Metadata page, for a show Jellyfin lists with no files whose folder is gone from the share (after deleting its files). Jellyfin is told the folder was deleted, as its own file watcher would, and drops the show after its watcher's delay (about 70 seconds measured); the toast waits until it has. Needs writes to your apps switched on; recorded in the audit log as "Told app a folder is gone". Shows only: a film's entry goes with its file.

### Fixed
- "Delete the leftover files…" removed the emptied folder, but Jellyfin kept listing the show with no episodes. Once the folder is gone, Jellyfin is now told so, and the toast waits until it drops the show.

## [0.12.85] - 2026-11-01

### Fixed
- "Tell Sonarr which episode it is" left the file where it was: Sonarr imports a file that is already inside the show's folder in place, without moving or renaming it, so the update afterwards waited for the old file to go. Sonarr is now asked to rename that one file (its RenameFiles command, naming only this file, never the rest of the show), which moves it into the episode's season folder under its naming format. The toast shows the new name.

## [0.12.84] - 2026-11-01

### Added
- On a show's folder page, next to the files Sonarr has no record of, a second card: "Tell Sonarr which episode it is". Pick the file, then the episode from Sonarr's own list (in episode order, specials last, with a search box; episodes that already have a file cannot be picked). The preview shows how Sonarr sees the file (quality, languages, any reason it would refuse it) and the exact call. Sonarr's Manual Import then moves the file into that episode's season folder and names it with its own naming format; every other app is then brought up to date with the folder. It needs writes to your apps switched on, and is recorded in the audit log as "Imported as an episode".
- The two cards sit side by side, each half the width (one above the other on a narrow screen).

### Fixed
- A test's lint exception had been moved to the wrong line by the formatter in 0.12.82.

## [0.12.83] - 2026-11-01

### Changed
- "Delete the leftover files…" is now under the share in a row's Fix menu, not under the arr; its hint names the arr that does not track the folder.

## [0.12.82] - 2026-11-01

### Added
- "Delete the leftover files…" in a Physical row's fix menu, for a folder on the share that Sonarr or Radarr does not track (a title removed from the arr whose files stayed). It lists every file in the folder, of any type, and moves them all to the trash (kept 30 days, can be put back), then brings every other app up to date with the folder as after any delete, and removes the emptied folder. It is refused if the arr tracks the folder. Up to 500 files at once.

## [0.12.81] - 2026-11-01

### Added
- In the "Add to Sonarr/Radarr" and "Tell Silo what this folder is" search, each result is tagged with the apps that already hold a title with one of its IDs, so the right match stands out.

## [0.12.80] - 2026-11-01

### Changed
- A Radarr or Sonarr notification (a download, upgrade, rename or delete) reads just that film or show again, instead of the whole app (Sonarr was read in full 46 times in three days, about 43 seconds each). A Jellyfin "item added" or "item deleted" reads just its film or show, instead of a full Jellyfin read of about 85 seconds. The whole app is read only when the event does not say which title it is about, and still at most once every 10 minutes.
- These reads start 10 seconds after an app's burst of events ends, instead of 60.
### Fixed
- A full read that failed while saving its result stayed "running" forever, and no later read of that app could start: Jellyfin had not been read since 06:55 on 1 November. The read now ends as failed, with the reason, and the next one can start.

### Other
- Jellyfin's Webhook plugin now sends "item added" and "item deleted" again: its two item tasks had no schedule (they last ran in December 2024) and now run every 30 seconds.

## [0.12.79] - 2026-11-01

### Changed
- After a delete, a put back, "Rescan in every app" or "Ask an app to look for new files", every app is brought up to date with the folder at the same time, and each toast line ticks as soon as that app agrees with the share. Jellyfin refreshes the folder's own entry directly (it dropped a deleted copy in 1 second when measured), instead of being told the folder changed and waiting out its file watcher's 60-second delay. Silo scans the folder (under a second). Sonarr and Radarr rescan the one title. Each app is then read for what it lists in the folder now, not the entries MediaWrangler remembered, so a copy that was put back, re-added or given a new id is seen and stored.
- Each app gets one answer: it matches the share, or the exact files it still lists or does not list yet. Jellyfin and Silo get up to 60 seconds, an arr's rescan up to 2 minutes. The "Check once more" step, the 15-second pause before it, the reminders to scan again and the "stopped changing for 5 minutes" rule are gone.
- Put back now also brings every app up to date with the folder, so the restored file shows in each app without a separate rescan.

## [0.12.78] - 2026-11-01

### Changed
- The media share is mounted once, whole and read-write, at `/media`, instead of one mount per library folder plus the trash. A move to the trash is now a rename on the share (instant) instead of a copy and delete between mounts, which took 85 seconds to 3 minutes for a film. `MW_MEDIA_FOLDERS` in `.env` names the library folders, so Music, eBooks and the other folders still do not appear under "Not in a library".

## [0.12.77] - 2026-11-01

### Fixed
- Asking Radarr (and Radarr 4K) to rescan one film rescanned every film: Radarr's RescanMovie takes `movieId`, and the `movieIds` list sent until now was ignored. Every delete and fix that touched a film started a whole-library disk scan in Radarr.
- "Rescan in every app" and "Ask Jellyfin to look for new files" on a film count every copy Jellyfin keeps for it (one entry per copy), including a copy put back from the trash, whose old entry MediaWrangler had marked gone. Before, they read only the entries MediaWrangler knew about. With two copies on the share it waited the full time and then reported "Jellyfin reports 1 files; the share has 2" (seen on The Core after a Put back).

### Changed
- Put back on the Trash page runs in the background with a toast, instead of holding the page while the file is moved. Putting back a file that is already being put back is refused.
- The tests no longer wait for real between an app's reads (3 seconds per read, 5 to 12 seconds after a fix); the delete tests take about 30 seconds instead of minutes.

## [0.12.76] - 2026-11-01

### Fixed
- A delete no longer fails as a whole when one read of an app fails while it is being waited on (Silo still answering "too many requests" after its retries, or a timeout). The read is logged and tried again until the 10-minute limit; before, the error ended the job and the other apps and the final check were skipped.
- A full read of an app no longer brings back a file or entry that a delete confirmed as gone while the read was running. The read can list a title before the app dropped the file, and saving it at the end marked the file present again (how The Boondock Saints was left behind). The next read corrects it if the app really still has the file.

## [0.12.75] - 2026-11-01

### Changed
- A Silo scan of one folder now reads only the titles in that folder, not the whole app. Every download (Silo scans the folder an arr imported into) and every fix or delete that asked Silo to scan a folder used to start a full Silo read: about 6 minutes, hundreds of "too many requests" answers, and up to 66 times in two days. A folder MediaWrangler has not seen yet is found by name; if that fails, or the scan was of a whole library, the whole app is read as before (still at most once per 10 minutes).

## [0.12.74] - 2026-11-01

### Changed
- Deleting a file now shows its stages in the toast: move to the trash, read the share, one line per app, and a final check, each with a spinner, tick or cross, ending in Completed or Failed with the reason.
- Every app is asked to scan at once, rather than one after another. Each is then waited on for up to 10 minutes (it was 90 seconds with no change), asked again every 2 minutes, and all are read once more at the end to be sure the file stayed gone.

## [0.12.73] - 2026-10-31

### Fixed
- A play an app has but Local lacks is now filled in on every watch refresh. Plays made in Silo's Sport library while its shows were unconfirmed were never applied (changes are read as they happen, and confirming a show does not replay them), so they never reached Jellyfin. Only a play newer than Local's own record of the title is taken: a title unwatched in Local since is not played again, and nothing in Local is lowered.

## [0.12.72] - 2026-10-31

### Fixed
- Silo's Sport shows now sync watches with Jellyfin. They matched Jellyfin's by title only, which counts as unconfirmed, and unconfirmed shows are left out of watch sync. A show matched by title alone now counts as confirmed when another app's show under the same title has exactly the same seasons and episodes (at least two).

## [0.12.71] - 2026-10-31

### Fixed
- Deleting a duplicate copy no longer ends with a "still lists the removed file" warning when the app dropped the file while its other entries were being read. The warning now shows only if the app still lists the file after every wait.

## [0.12.70] - 2026-10-31

### Fixed
- "Ask Jellyfin (or Silo) to look for new files" now waits until the app's files for the folder match the share's, reading every entry the app holds for it, instead of pausing 12 seconds and reading one entry once. Jellyfin takes about a minute to drop a deleted copy, and keeps one entry per copy, so the scan worked but the page kept showing the old files. If the app has not caught up after two minutes the result says how many files it lists against the share's.

## [0.12.69] - 2026-10-31

### Fixed
- After deleting one of two copies of a film, the follow-up now waits for each of the app's other entries that listed the file to drop it (Jellyfin keeps one entry per copy, and takes a little while). It read them once, three seconds after the rescan request, before Jellyfin had dropped the deleted copy, and never again, so the page kept showing a file that was already gone.

## [0.12.68] - 2026-10-31

### Fixed
- When an app no longer has a show or film, the folder record it kept for it (Silo's list of scanned folders) is dropped too. A film Silo had dropped still showed a ✓ in its column on the Physical page until the next full Silo read.

## [0.12.67] - 2026-10-31

### Fixed
- A folder deleted from the share but still held by an app (Jellyfin or Silo) now offers that app's "Ask … to look for new files" in the Fix menu. It was only offered for a count difference, so the one action that could clear a leftover was missing.

## [0.12.66] - 2026-10-31

### Fixed
- Asking Silo to scan a folder that has been deleted from the share now scans Silo's whole library for it, with the dialog saying so. Silo refuses (HTTP 400) to scan a path that no longer exists, so the one-folder scan could never make it drop a film or show whose folder was gone.

## [0.12.65] - 2026-10-31

### Fixed
- When an app refuses the rescan request (Silo answers HTTP 400 for a folder that is already gone from the share), the job no longer waits minutes for a file count that cannot arrive. It reads the title once, says the app did not accept the request, and finishes.

## [0.12.64] - 2026-10-31

### Fixed
- After deleting a duplicate film file, the app's item for that copy was left on record. Jellyfin keeps one item per copy of a film and the follow-up read looked at only one of them, so the Jellyfin column kept showing the deleted file. Every item that still lists a removed file is now read again and dropped if the app no longer has it.

## [0.12.63] - 2026-10-31

### Changed
- Deleting unusual files (stubs, archives, programs) now also asks any app that still lists one of them to rescan that folder, and reads the title again. Silo counts `._` stubs and kept them after they were deleted; before this you had to ask it to rescan by hand. An app that never had the files is not asked.

## [0.12.62] - 2026-10-31

### Fixed
- The Metadata page no longer shows a "possible mismatch" row for a title Radarr or Sonarr tracks with no files when the same app also tracks the version the media servers have (a remake, or a 4K list synced from the regular Radarr). A title the app has files for, or an app that tracks only the other version, is still flagged.

## [0.12.61] - 2026-10-31

### Changed
- The Metadata page no longer has the separate "Possible mismatches" section: the same rows are in the table, tagged "possible mismatch".

## [0.12.60] - 2026-10-31

### Fixed
- A film that Silo lists in two libraries (a 1080p and a 4K copy) was filed under the wrong library after a single-title re-read, which put the 1080p file in the 4K folder's list and raised "1 path from Silo map outside this group's folders". Each file now belongs to the library whose folder holds it, as in a full read.
- A full read now reads every film's version files again, instead of reusing the ones stored the first time. A film file renamed on disk (Radarr adding `[MA]`) stayed on record in Silo's column for ever, shown as a duplicate.

## [0.12.59] - 2026-10-31

### Fixed
- "Ask Silo to look for new files" (and the other Silo fixes that first have to find the title) failed with `HTTP 422` when the title was not already known. Silo's catalog needs a profile on the request and the search did not send one.

## [0.12.58] - 2026-10-31

### Fixed
- A second delete can no longer be started for files that are already being moved to the trash. The server refuses it, the preview says "is already being deleted", and on the folder page those files show "deleting…" with their box disabled. Ticked files lock the moment you confirm a delete.

## [0.12.57] - 2026-10-31

### Changed
- The Fix button is in the accent colour (solid when hovered or open) so it stands out in the table.

## [0.12.56] - 2026-10-31

### Fixed
- A tall Fix menu near the bottom of the window ran off the screen with no way to reach the rest. It now opens upward when there is more room above, is capped to the room available and scrolls inside itself; scrolling the menu no longer closes it.

## [0.12.55] - 2026-10-31

### Fixed
- The Fix dropdown's descriptions ran outside its box: the title line's "no wrap" was inherited by the menu. The menu wraps its own text again, and the descriptions are shorter.

## [0.12.54] - 2026-10-31

### Changed
- Metadata page, title column: the name, ids and tags stay on one line (a long name is cut with an ellipsis instead of wrapping) and the Fix menu is centred vertically at the right end.

## [0.12.53] - 2026-10-31

### Changed
- **One Fix menu per row**, at the right end of the title/folder cell, on both the Physical and Metadata pages, instead of one in every cell. Its actions are grouped under the app they apply to (Jellyfin, Silo, Sonarr, Media share), followed by "This show" (rescan in every app, list the files).

## [0.12.52] - 2026-10-31

### Fixed
- The master's cell is flagged when another column has more files than it does. The master is the reference, so it was never compared with itself: a new episode that Silo, Sonarr or the share had but Jellyfin had not scanned yet showed no issue on Jellyfin's cell and no Fix menu to ask it to scan. It now counts as a count difference for the master and gets the menu (Sheriff Country).

## [0.12.51] - 2026-10-31

### Fixed
- In the comparison tables, cells are centred vertically: titles and plain numbers no longer sit above centre when another cell in the row is taller (a Fix button, two lines).

## [0.12.50] - 2026-10-31

### Fixed
- The table border now follows the rounded corners: it is a real border on the table (separate-borders model) instead of an outline, with the corner cells rounded to sit inside it. Row lines are drawn as a top border on each visible row after the first, so rows hidden by a filter do not leave a doubled line at the bottom.

## [0.12.49] - 2026-10-31

### Fixed
- The Physical and Metadata pages' tables had no `id`, but the filter script finds its table by id, so the script crashed as soon as it loaded: the instant filtering, the filtered highlight and the Clear filters button did nothing there, and the plain "Filter" button stayed visible. The two tables now carry their ids (Watched and Activity already did). Checked against the real rendered pages in a simulated browser: filter, search and Clear filters all work and the Filter button is hidden.

## [0.12.48] - 2026-10-31

### Changed
- Tables have rounded corners and a slim outer border like the cards (drawn as an outline inside the box, so Fix menus and tooltips are not clipped).
- In the comparison tables the folder/title column now carries the tint of the odd columns, so the shading alternates from the first column; before, the title and the first app column were the same colour.

## [0.12.47] - 2026-10-31

### Changed
- Removed the "Filtered" badge from the filter bar; the tint, the outlined controls and the Clear filters button stay.

## [0.12.46] - 2026-10-31

### Changed
- The filter bar centres its controls vertically, puts the "N of M folders" count at the right end of the row, and has more space above the table.

## [0.12.45] - 2026-10-31

### Changed
- A list that is not showing everything is easy to notice: when the filter is not the default (All issues) or the search box or app choice is set, the filter bar is tinted with an accent border and a "Filtered" badge, the controls that were changed are outlined, and a "Clear filters" button brings back the default view. It updates as you type or choose, and is right on first paint (Physical, Metadata, Watched, Activity).

## [0.12.44] - 2026-10-31

### Fixed
- After confirming a fix, the page stayed dimmed until clicked: the dialog was closed and then reopened by a second handler. It now closes and stays closed.
- Following a toast's "Check the page" link dismisses the toast.
- For fixes on a folder, that link now goes to the Physical page filtered to the show (every row shown, so it is still there once the issue is gone), not to the folder's file list.

## [0.12.43] - 2026-10-31

### Added
- The Media share cell on the Physical page has a Fix menu for a row with an issue: **Re-read this folder on the share** (read-only: a background job with a toast, sends nothing to any app and needs no write switch), Rescan this show in every app, and List the files. Before, only the app cells had one, so a share that was behind had no fix on its own cell.

## [0.12.42] - 2026-10-31

### Changed
- **Fixes that change things now run as background jobs with a toast.** Deleting files (to the trash), rescanning a show in every app, asking an app to scan or refresh, changing which show an app thinks something is, adding to Sonarr/Radarr and telling Silo what a folder is: you confirm in the dialog, which closes at once, and the work carries on on the server (the move, the request to each app, the wait for each app, the re-read). A toast at the bottom right shows what it is doing and how it ended. Toasts persist across page loads and navigation (every page polls for them); when a job finishes the toast gets a "Check the page" link to the folder or show it was about (the page is never reloaded for you) and a dismiss ✕; a note or an error keeps the toast until you dismiss it. Jobs are held in memory, so a restart forgets them.
- Up to three jobs run at once.

## [0.12.41] - 2026-10-31

### Fixed
- After files are moved to the trash, an app gets 90 idle seconds (180 in all) before it is reported as still listing them. Jellyfin took about a minute and a half to drop a file after being asked, so the 45-second wait still gave a premature warning (Birds Of A Feather); the page itself caught up when Jellyfin did.

### Changed
- On a folder's page the trash button reads "Delete…", is red, and stays greyed out until at least one file is ticked (it still moves files to the trash, and the dialog says so).

## [0.12.40] - 2026-10-31

### Fixed
- The unusual-files check flagged every `._.DS_Store` (343 files, nearly one per folder), burying the real ones. A macOS `._` stub is now flagged only when it carries a video extension, as the stubs that were counted as episodes did.

## [0.12.39] - 2026-10-31

### Added
- **Unusual files.** The share scan now keeps files that do not belong in a library, flagged and never counted as media: archives (`.zip`, `.zipx`, `.rar`, `.7z`...), programs and scripts (`.exe`, `.bat`, `.js`...), unfinished downloads (`.part`, `.crdownload`...) and the hidden `._` stubs macOS leaves beside files copied to a share. A folder with any gets an "unusual files" tag and appears under All issues and a new "Unusual files" filter; the disk column's summary counts them. The folder page lists them, with a warning that an archive in place of a video is a common fake release ("do not open or extract"), and they can be moved to the trash (File changes switch) without an app vouching for them. Seen with 48 Hours, where a `.zipx` took the place of an episode. New column `media_file.flag` (migration 0024); the `._` stubs, which used to be counted as videos, are no longer.

## [0.12.38] - 2026-10-31

### Added
- **Rescan this show in every app** in the Physical page's Fix menu, for files that arrived or changed outside the apps (a download not yet picked up). The dialog lists the exact request for each app that has the title; it then reads the folder on the share again, asks each app to rescan just that folder, and reads the show from each until its file count matches the share's (Sonarr and Radarr are believed once they say their task is done), then reloads the page. An app whose count still differs is named. Needs the write switch. Shares its machinery with the after-trash rescan.

## [0.12.37] - 2026-10-31

### Changed
- Silo is compared only on the files attached to an episode. Its admin file list is used only to give those files their paths; its other records (attached to no episode: a removed file nothing cleaned up, a second copy of an episode) are not Silo files, so they are no longer shown or counted anywhere. The "no episode in Silo" extras are gone.

## [0.12.36] - 2026-10-31

### Fixed
- After files are moved to the trash, an app is no longer reported as "still listing" files it has dropped or cannot drop. Jellyfin was given only 20 idle seconds and takes longer to start rescanning (it now gets 45, within a 120-second limit). Silo's records attached to no episode are ignored, since they are not files it serves and nothing removes them.
- The dialog showed two Close buttons when an app note was shown; now one.

## [0.12.35] - 2026-10-31

### Changed
- For every Silo series, the files Silo counts are the ones attached to an episode (read from the seasons' episode lists); the admin file list is used only to give those files their paths. Before, the episode check ran only for a show whose admin list had more records than episodes. The Silo read takes longer.

## [0.12.34] - 2026-10-31

### Fixed
- A re-read of Silo copied each show's file paths from MediaWrangler's previous copy and let them override Silo's fresh list (introduced in 0.12.14), so a file that left Silo never dropped out of MediaWrangler and the "no episode in Silo" marking never applied in a sync. A show's files are now always read afresh; only a film's are reused.

## [0.12.33] - 2026-10-31

### Fixed
- The sync progress text read "reading seriess"; it now says "reading series".

## [0.12.32] - 2026-10-31

### Fixed
- Silo's file count no longer includes records Silo keeps for files it does not play. Its admin file list can hold records attached to no episode (a file that was removed and never cleaned up; Silo's scans never remove them). When that list has more records than the show has episodes, the episode lists are read to see which files are attached; the rest are shown as extras marked "no episode in Silo" and are not counted. Battlestar Galactica read 77 against 75 on the share for this reason.

## [0.12.31] - 2026-10-31

### Changed
- The change-show dialog says up front that Jellyfin can take a minute or two to answer (it re-downloads the show's details and images); Battlestar Galactica took about 1.5 minutes.

## [0.12.30] - 2026-10-31

### Changed
- A title Sonarr or Radarr tracks under other IDs than the media servers' now has its own row, tracked in the arr and "Not in master" in the servers, and appears in the Possible mismatches list beside the servers' version, exactly like The Stand. Before, it only put a warning on the servers' row.

## [0.12.29] - 2026-10-31

### Changed
- The arr look-alike note in a "Not tracked" cell is a hover tooltip on a warning-coloured "Not tracked ⚠" instead of a long sentence that squeezed the columns.

## [0.12.28] - 2026-10-31

### Changed
- Removed the year allowance from the arr look-alike check. Any title an arr tracks under the same name but other IDs than the media servers' is flagged, whatever the years say: if the metadata does not match, it is shown.

## [0.12.27] - 2026-10-31

### Fixed
- The arr look-alike check also trusts the "(2003)" in an arr's title. Sonarr lists Battlestar Galactica as year 2005 but names it "Battlestar Galactica (2003)", so with Jellyfin and Silo at 2003 it was missed by the one-year allowance in 0.12.26.

## [0.12.26] - 2026-10-31

### Fixed
- A title that Sonarr or Radarr tracks under other IDs than the media servers (same name, close year, paired with a different show) is now flagged. The row gets the "possible mismatch" tag, appears under All issues, shows "Sonarr has “…” with other IDs (…): probably the same show matched wrongly" in the arr's cell, and the Fix menu appears on the Jellyfin and Silo cells. Before, it only said "Not tracked", with no flag and no fix. Seen with Battlestar Galactica (TMDB 71365 in Jellyfin and Silo, TMDB 1972 in Sonarr).

## [0.12.25] - 2026-10-31

### Changed
- After files are removed, MediaWrangler stops waiting for an app once its file list has not changed for 20 seconds (it used to wait the full 60). An app that never drops the files, such as Silo for the stale entries, no longer holds the dialog for a minute.

## [0.12.24] - 2026-10-31

### Fixed
- The note shown when an app keeps listing removed files, and the 0.12.23 changelog entry, wrongly said Silo only drops files at a full library scan. Silo's own history shows its scans do delete missing files; what is not known is why a scan requested through its API does not. Wording corrected.

## [0.12.23] - 2026-10-31

### Changed
- After files are removed, an app that still lists them once the wait is over (60 seconds) is now named in the dialog, with why, instead of the page just reloading. Real case: Silo accepted the folder scan (202) but kept two deleted files; it refuses a scan of a file that is gone, and its API has no call to delete a file record. Silo's own scans triggered by Sonarr and Radarr do remove missing files (its scan history shows files_deleted), and a manual `POST /api/v2/scan` does not appear in that history, so why it does nothing is not yet known.

## [0.12.22] - 2026-10-31

### Added
- After files are moved to the trash, MediaWrangler reads that folder on the share again, asks each app that has the title (Jellyfin, Silo, Sonarr/Radarr) to rescan it, and reads the title from each until it no longer lists the removed files (up to 90 seconds each), then rematches and reloads the page by itself. The dialog shows which step it is on. Asking the apps needs the write switch, as for any Fix; with it off, the share and the apps are still read.

## [0.12.21] - 2026-10-31

### Changed
- Moving a file to a trash folder on a different mount (which is what a separate `.trash` mount is) is a copy, not a rename. It now uses the kernel's copy call, about twice as fast on a network share (measured: 256 MB in 1.5 s against 3.2 s), and the dialog says beforehand that the files will be copied and roughly how long it will take.

## [0.12.20] - 2026-10-31

### Changed
- `compose.yaml` now mounts only the library folders in use (TV, Movies, Movies4K, Anime, Sport) and a `.trash` folder read-write, on top of the read-only share; the rest of the share stays read-only. This replaces the `MW_MEDIA_MODE` setting, which is gone. The trash folder is `/media/.trash`.
- Writes and Trash pages report which library folders (and the trash folder) are read-only, instead of judging the whole share.

## [0.12.19] - 2026-10-31

### Added
- A **Check all** box above the orphaned-files list (half-ticked when only some are), and more space above the "Move the ticked files" button.
- Choose where trashed files go: **Settings → Writes → Trash folder**. Empty keeps the old behaviour (a `.mediawrangler-trash` folder inside each library folder); or name one folder for everything (it must exist, be writable and sit outside every library folder; files are sorted by date and library). A move to another disk is copied, checked for size, then the original is removed; a failed copy removes what it wrote. The named folder itself is never removed. Each trashed file remembers its folder (migration 0023).

## [0.12.18] - 2026-10-31

### Added
- **Move orphaned files to a trash folder** (file changes, off by default). On a folder's page, tick the files an arr tracks the folder for but has no record of, see exactly what will move, and move them. They are renamed into `.mediawrangler-trash/<date>/` inside their library folder (instant, same disk), recorded, and can be put back from the new **Settings → Trash** page; files older than 30 days are deleted for good (only while the switch is on). Nothing is ever deleted outright by a click.
- Every move re-checks that the switch is on, the file is a plain video (not a link) inside a library folder and the folder chosen, it is still there, and the arr — asked afresh — still has no record of it; if any file fails, nothing is moved. At most 25 files per move; none is ticked to start with; each move, restore and deletion is in the audit log (new "File changes" filter).
- **Settings → Writes** gets a "File changes" switch and shows whether the share is mounted read-write. The share stays read-only until you set `MW_MEDIA_MODE=rw` in `.env` (compose.yaml reads it).
- New table `trash_item` (migration 0022).

## [0.12.17] - 2026-10-31

### Added
- Physical page fixes: **Add to Sonarr/Radarr** for a folder on the share that no arr tracks (searches the arr's own database, adds it unmonitored with the folder it is already in, nothing is downloaded), and **Tell Silo what this folder is** for a folder Silo skipped (search or fill in the title and ids; sets Silo's own folder override, then asks Silo to read that folder). Each shows the exact call, needs the write switch, and is audited.
- A folder's page lists the files on disk that an arr tracks the folder but has no record of (a second copy, an unmatched file), with `rm` lines to copy. MediaWrangler never deletes files.
- Metadata rows that differ by episode or special counts, or duplicates, now have a Fix menu entry that points to the Physical page.

## [0.12.16] - 2026-10-31

### Changed
- After a fix, MediaWrangler reads only that one show or film from the app, not the whole library. Jellyfin, Silo, Sonarr and Radarr each have a single-title read. A scan that makes an app pick up a new folder looks the new title up by its folder.
- Sonarr, Radarr and Silo's metadata refresh report when they have finished, so the dialog waits for the app to say it is done before reading it. Jellyfin and Silo scans give no signal, so they get a short pause.

### Added
- A change-show fix now moves the person's watch history (plays and progress) from the old show to the new one, episode for episode, so the old show is left with none of it. Done only when no app still has the old show, and only for episodes the new show has; history for the rest stays where it was. Moved plays are recorded in Local's events as "moved".

## [0.12.15] - 2026-10-31

### Changed
- The "Silo's view of this folder" box is gone from the folder page; Silo is a column in the file table.
- The "duplicates" tag sits on the same line as the folder name on the Physical page.

## [0.12.14] - 2026-10-31

### Fixed
- Silo's files are now read. For each series, MediaWrangler asks Silo for its file list (`/api/v2/admin/items/{id}/files`), so Physical shows Silo's real file counts, flags duplicates Silo holds, and the folder page has a Silo column again. Before, only Silo's episode count was known (an earlier note that Silo cannot list files was wrong). The library read is slower: one extra request per series.

## [0.12.13] - 2026-10-31

### Fixed
- A folder's page on Physical always has a Silo section: its episode count compared with the master, its folder states, or "not picked up". Silo has no column in the file table because it does not list files, and the page now says so.

## [0.12.12] - 2026-10-31

### Changed
- Every fix now finishes by itself: the dialog waits a few seconds for the app, MediaWrangler reads the app again (re-reading the fixed item's details), then the page reloads with the new result. No manual sync needed.

## [0.12.11] - 2026-10-31

### Added
- Physical page: a Fix ▾ menu on cells with a problem. Ask Jellyfin or Silo to look for new files in that one folder, ask Sonarr or Radarr to rescan the show, re-download a server's details for the show, or list the files in the folder. Each shows the exact request first, needs the write switch, and is recorded in the audit log (new "Scans and refreshes asked for" filter). Nothing here touches files.
- Physical page banners point to the Libraries page.
- Fix buttons grey out and show a spinner while the request runs.

### Changed
- After changing which show an app thinks an item is, MediaWrangler reads that app again and re-reads that one item's details, so the new IDs show up.
- The Fix button sits at the right of the cell.

## [0.12.10] - 2026-10-31

### Changed
- Metadata table cells keep their status, episode count, notes and Fix button on one line (wrapping when narrow); the Fix button looks like a button.
- The Fix menu no longer appears for rows that differ only by episode or special counts (missing specials, duplicate files). Changing the show cannot fix those; they need the Physical-page fixes.

## [0.12.9] - 2026-10-30

### Fixed

- Jellyfin's search results show their year (it sends a premiere date, not a year, for some providers).

## [0.12.8] - 2026-10-30

### Added

- Fix from the page (the first part of 0.11). Metadata cells that show a problem get a **Fix ▾** menu. The
  first fix is "Change which show Jellyfin/Silo thinks this is…": a dialog searches that app's own metadata
  (Jellyfin `RemoteSearch`, Silo `match/search`), lists the candidates with the ones that carry another app's
  ID for the same title first, then shows the exact call and sends it only when you press "Change in <app>".
  The write needs the global write switch on, and each attempt is recorded in the audit log as "Changed
  match" (a new filter there). If Silo answers with a new content id for the item it is noted in the log.
- Unconfirmed matches now use the same menu for "these are the same show" / "these are different shows"
  (local decisions; nothing is sent to an app), with a line saying when each is right.
- No posters yet: the site's security policy only allows its own images.

## [0.12.7] - 2026-10-30

### Fixed

- The master dropdown on Connectors sits on the same line as its label.

## [0.12.6] - 2026-10-30

### Changed

- The General tab is gone. "Select master" is now a dropdown beside Add connector on Settings -> Connectors
  and applies as soon as it is changed; hovering the label explains what the master does today (the other
  apps are compared against it, its libraries and titles are the reference, new accounts are copied from its
  users). The old text promised that its unwatches alone would sync from 1.2, which no longer applies:
  MediaWrangler's own record (Local) decides watch status. `/settings/general` redirects to Connectors.
- The anime ID list is downloaded about once a day instead of once a week (it had gained 200 entries in four
  days); the Connectors page says so.

## [0.12.5] - 2026-10-30

### Fixed

- Placing a library on the Libraries page (or taking one out) now rebuilds the path rules and re-matches at
  once. Before, the new rules only appeared after the next sync, so the rows showed "No rule" meanwhile.

### Removed

- Deleting a library. The master makes them and a deleted one could not be added back, so the button only led
  to a dead end.

## [0.12.4] - 2026-10-30

### Changed

- The Options on each library are now a stacked form: every setting has a label and a plain-words line under
  it (what it does and when to change it), Save is its own button, and Delete sits in a separate box that says
  nothing changes in your apps.

## [0.12.3] - 2026-10-30

### Changed

- Settings -> Library groups is now **Libraries**: one section per library (named after the master's
  library), with a row per app showing the library it uses there, the folder it reports, what that folder is
  treated as (the path rule, in plain words) and a status. A missing app has a "Not set" row with a picker; a
  library with no media share folder says so instead of the Physical page showing path-map errors. Options
  (name, compare titles, imported titles) are folded away; libraries in no section are listed at the bottom.
  "Add a group" is gone: the master makes them.

### Fixed

- A media share folder could not be put back into a library once taken out: the page only offered groups of
  the library's own kind and disk folders have none. This is the likely reason the Anime folder stayed
  unplaced, leaving Anime with no path rules.

## [0.12.2] - 2026-10-30

### Added

- Settings export and import: one JSON file with connectors (keys in plain text, so it works under any
  `secret.key`), the master, library groups and members, match decisions, people with their app switches
  and filters, and the links between people and app accounts. Import adds and updates by name and deletes
  nothing; libraries and accounts a sync has not read yet become placeholders it fills in; a history switch
  that is turned on gets `since` = the import time. Settings -> Backups has Export and Import.
- Restore: from a listed backup, from an uploaded file (Settings -> Backups), or from the first-run screen.
  The file is checked and refused if its schema is newer than this version or it is not a MediaWrangler
  database, then staged; the app restarts and swaps it in before the database opens, after saving the old
  database as `pre-restore-*.db` (and the old `secret.key`). Plain `.db` copies restore too.
- Backups can be deleted from the page.

### Changed

- Every backup now holds `secret.key` and `settings.json` as well as the database, with nothing locked by a
  passphrase; keeping the file safe is up to the owner. The "Download with secret.key" form and the
  passphrase helpers are gone.
- Nightly backups keep the newest 7 (was 14). Backups made by hand are kept until deleted.

## [0.12.1] - 2026-10-30

### Fixed

- Local stopped updating for anyone whose batch of changes held an unwatch and then a watch (or progress) of
  the same title: the new row was inserted before the old one's delete was flushed and tripped the unique
  key, the whole refresh failed and was retried forever, blocking everyone after them too. A removed row is
  now deleted straight away, a title watched and then unwatched within one batch is simply forgotten
  instead of failing to delete an unsaved row, and one person's failure is logged and skipped instead of
  stopping the rest. (Seen on zach and zeet after the 0.12.0 restart: 78 changes were waiting.)

## [0.12.0] - 2026-10-30

### Added

- Full backups (Settings -> Backups). A backup is one `.tar.gz` with a consistent copy of the database (made
  with SQLite's online backup, so the app keeps running; checked with `integrity_check`) and a manifest
  (app version, schema, time, row counts). A nightly one is made at 02:30 local time and the newest 14 are
  kept; manual ones ("Back up now") are never removed automatically, and older plain `.db` copies in the folder are
  listed but left alone. Each can be downloaded.
- "Download with secret.key": a fresh backup with the master key inside, locked with a passphrase you type
  (scrypt plus Fernet). The key is never in a nightly or ordinary backup.

## [0.10.12] - 2026-10-30

### Added

- A Silo account made from Settings -> Users gets its per-account key made at once, so its watch state can be
  read and written without a visit to the connector page.
- A Silo account can be made without typing a password: it is created with a made-up one (never shown) and a
  one-time set-password link from Silo is shown once for you to pass on (`delivery: link`). Jellyfin accounts
  still need a typed temporary password. If the key or the link fails the account still stands and the result
  says what to do.

## [0.10.11] - 2026-10-30

### Fixed

- Jellyfin's repeated "unwatched" ticks at position 0 (a client that has just opened an episode reports
  every 3 s) are dropped from Live events like the ticks with a position, since they change nothing.

## [0.10.10] - 2026-10-30

### Fixed

- Progress during a long viewing was noted once and then never again: each webhook tick (about every 3 s)
  was compared with the previous tick, which never moved a minute, so the stored position and the change log
  went stale. A position is now noted when it is five minutes from the last one noted
  (`watch.PROGRESS_STEP_SECONDS`), which leaves a trail of a row every few minutes.
- Jellyfin's position ticks that change nothing are no longer stored or shown in Live events (they were about
  4,000 rows a day, and pushed real events out of the 5,000 kept).
- Live events pushed to an open page show the time in the same 24-hour form as the rows the server draws.

## [0.10.9] - 2026-10-30

### Fixed

- The Live events tab's App column is filled for events that arrive while the page is open, not only after
  a refresh.

### Changed

- Audit log table: colour-coded tags for every action (Played, Unplayed, Progress, Marked watched, Marked
  unwatched, Created account) and result (Done, Recorded, Replaced file, Skipped, Ignored, Error), each in a
  colour of its own, the same in the 24-hour summary. Columns renamed "Action" and "Details", the "When"
  heading is gone and the App column now comes before User.

## [0.10.8] - 2026-10-30

### Changed

- In the Audit log, the Why column describes skipped, replaced and failed writes in words: what happened,
  why, and what it means (for example "Skipped before sending: Jellyfin no longer lists this item (replaced
  or removed), so there was nothing to write to."). It works for older rows too. The raw error text stays
  beside the badge for errors only.

## [0.10.7] - 2026-10-30

### Changed

- In the Audit log the detail under a result (such as a replaced file) now sits beside its badge on one
  line, with the tooltip for the rest.

## [0.10.6] - 2026-10-30

### Changed

- The Audit log explains each received change: "Reported by webhook via Jellyfin: user1 played it",
  "Found by a read of Silo: ...", and, for a change that is MediaWrangler's own write being noted or an app
  reporting it back within 30 seconds, "reported it back after MediaWrangler's own write, so there was nothing
  new to apply" with the result "Ignored" (others show "Recorded"). New "Ignored echoes" filter. Watch events
  now record how they were learned (`watch_event.via`, migration 0021; older rows say "Reported by").
- Why text for new writes is plainer: "Local has it watched on 13 Jan 2015 (seen in Jellyfin); Silo did not".

## [0.10.5] - 2026-10-30

### Changed

- The Activity page uses the full window width like the other comparison pages, and the Audit log table is
  striped with the time, direction, what and result columns on one line each. The "(was unplayed)" note
  is gone from the What column.

## [0.10.4] - 2026-10-30

### Changed

- Activity now has three tabs: Sync runs, Audit log and Live events. The Audit log merges the old Watch
  changes tab with the write audit log that was on the Writes page, so it shows everything MediaWrangler
  heard from the apps and everything it did to them, newest first, with a filter (received, sent, played,
  unplayed, progress, accounts created, errors, replaced files, skipped) and a 24-hour summary.
  `/activity?tab=changes` still works. The Writes page keeps the switches, queue and preview, and links to it.
- The Why column and the detail under each result stay on one line; a styled tooltip shows the full text
  when it is cut off.

## [0.10.3] - 2026-10-30

### Changed

- A write to an item Jellyfin no longer knows (a 404, typically after Radarr or Sonarr upgraded the file and
  Jellyfin gave it a new id) is no longer an error. It is not retried, is recorded as "Replaced file", and
  asks for a library re-read at once so the new id is learned. Nothing is lost: Local still holds the watch.
- The Audit log says what happened: a summary of the last 24 hours, plain-language "What" and "Why"
  columns (for example "Watched 13 Jan 2015, seen in Jellyfin"), and the reason shown under each result
  instead of in a tooltip. New `why` columns on `write_job` and `audit_log` (migration 0020); older rows
  have none.

## [0.10.2] - 2026-10-30

### Fixed

- A title paired with another app's only by name and year (low confidence) no longer takes part in
  syncing: what is watched is neither read into Local nor written out for it, so it cannot cross to a
  different show with the same name. Queued writes for such titles are dropped as stale. On the live data
  this covers 10 Silo series (nine sport leagues and one anime).

## [0.10.1] - 2026-10-30

### Changed

- An unwatch found only by comparing reads now needs two reads at least ten minutes apart to agree
  before it counts (new `watch_state.missing_since`, migration 0019). A read that comes back short, or
  one repeated a few seconds later by a webhook, can no longer unwatch anything. Unwatches an app
  reports as they happen (Jellyfin and Silo webhooks) still apply at once.

## [0.10.0] - 2026-10-30

### Added

- Per person, per app sync switches (new `user_sync` table). **History**: the Writes page now has one
  switch per person per app instead of one per person; an unwatch is written only if it happened after
  that app's switch was turned on. People who were switched on keep writing to every app they have a
  user in (migration 0018). The old `writes_mode`/`writes_since` columns are left in place unused.
- **Accounts**: Settings -> Users lists people who have a user in one app but not another. Switching one on
  adds it to a dry run showing the libraries the new user may open (matched through the library groups);
  accounts are made only after a temporary password is typed for each and confirmed. Jellyfin
  (`POST /Users/New` plus a library policy) and Silo (`POST /api/v2/admin/users`, default profile, placeholder
  email, forced password change). Each is audited; passwords are never stored or logged.

## [0.9.21] - 2026-10-29

### Changed

- "Apply from" is now named for every title with something played, including
  where all the sources already agree (it showed a dash there). Local still
  wins a tie.

## [0.9.20] - 2026-10-29

### Changed

- The Watched page's "Apply from" column now counts Local. Where Local has as
  much played as the best app, Local is the source (then the master); an app
  that differs from Local alone also now shows an Apply from.

## [0.9.19] - 2026-10-28

### Changed
- The +/− difference figures are small badges (red for fewer, amber for more, green for same) everywhere, right-aligned when they sit in a table cell.
- The Connectors table on the Overview has the same alternating shaded columns as the other pages.

## [0.9.18] - 2026-10-28

### Added
- Jellyfin existence check. When items drop off a user's played list, Jellyfin is asked, as that user, whether they still exist (one batched call). Items it no longer has or the user can no longer open (a replaced file, a library taken away) drop out quietly and are not logged as unwatched; only items it confirms are still there count. If Jellyfin can't be asked, nothing changes and the items are checked again on the next read. Gone items also queue a library read so the new items are known. Apps that can't be asked (Silo) behave as before.

## [0.9.17] - 2026-10-28

### Fixed
- An unwatch of one copy of a title (a 1080p and a 4K item, or a file Jellyfin replaced under a new id) no longer unplays it in Local while another copy is still played in that app. Before, the replaced item's "unplayed" event dropped the title from Local, and an unwatch could then be written on to Silo.

## [0.9.16] - 2026-10-28

### Added
- A Total row in each Watched summary box, between Not played and In progress.

## [0.9.15] - 2026-10-28

### Fixed
- The Watched page's Local column now counts the plays Local holds for titles only the files have (0.9.14 stored them but the page ignored them, so Local still showed the old numbers).

## [0.9.14] - 2026-10-28

### Added
- Local now holds the plays of titles no app has. Each show or film in a Trakt or Yamtrack file that Jellyfin and Silo lack gets a canonical item (keyed like the matcher keys its own, e.g. `series:tvdb:123`, or by title and year when the file has no ids) and its plays go into Local. It runs once per set of imported files (and again when the anime list first loads), and changes nothing Local already has.
- The matcher no longer cleans away a canonical item Local holds plays for. When an app later gets such a title it reuses the row, or, when the keys differ but an id or the title and year match, moves Local's rows onto the app's copy. Local then shows the plays there and the write queue applies them to the new app.

## [0.9.13] - 2026-10-28

### Fixed
- A restart now carries on with the write queue. Jobs left queued (and not paused) are sent as soon as the app is back, instead of waiting for the next sync.

## [0.9.12] - 2026-10-28

### Changed
- The queue is a player-style bar that never wraps: a state badge (Off, Paused, Running, Done, Idle) that updates live, a fixed-width progress bar and a fixed-width "done / total" count. The per-app counts and the explanatory text are gone.

## [0.9.11] - 2026-10-28

### Fixed
- The live queue counted only jobs queued or finished in the last five minutes, so after a pause it showed "50/50, 150/150". Every pending job now counts, however long it has waited, so the total is fixed from the start.
- A preview taken while writes are queued said "Nothing to write: every app agrees with Local". It now says how many are already queued.

## [0.9.10] - 2026-10-28

### Changed
- The live queue is a single row inside the "Writing to the apps" card, aligned right: progress bar, written / total, speed, time left and per-app counts.

## [0.9.9] - 2026-10-28

### Added
- A live Queue panel on the Writes page: written / total with a progress bar, speed and time left, progress per app, and failures. It refreshes every second while anything is queued (every two seconds otherwise), and the audit log refreshes with it, so nothing needs a page reload.

## [0.9.8] - 2026-10-28

### Added
- Safety net for stale writes. Before sending, and again each cycle for failed jobs, a queued write is checked: the item must still be in the app, the user must still reach its library, and Local must still say the same. Otherwise the job is marked skipped (with the reason in the audit log) instead of sent or retried. A job that failed because the app replaced the item stops counting as a failure after the next sync.

## [0.9.7] - 2026-10-28

### Fixed
- Writes skip Jellyfin libraries a user can't open. Jellyfin answers 404 for those items, so every write to them failed. The user's allowed libraries are read with the user list (migration 0017).

## [0.9.6] - 2026-10-28

### Changed
- The Writes preview shows one row per record with a "Writes to" column listing every app it will be written to (replacing the one-row-per-app layout). Show all counts records.

## [0.9.5] - 2026-10-28

### Changed

- The Writes preview has a "Known from" column: the apps and imports whose plays Local holds for the title (for an unwatch, the app it happened in).

## [0.9.4] - 2026-10-28

### Changed

- The Writes preview lists the first 100 writes, with a Show all link that lists every one.

## [0.9.3] - 2026-10-28

### Changed

- Switching a user off (or All off) cancels what was queued for them; Pause still keeps the queue. A job queued earlier can no longer fire later, out of date.

## [0.9.2] - 2026-10-28

### Fixed

- **Migration 0016 wiped data.** Rebuilding the `app_user` table with foreign keys on deleted Local, the Trakt/Yamtrack imports and the links between app users and people. Restored from the pre-0.9.0 backup, with Local catching up on the events since. Migrations now run with foreign keys off (and a test checks it), and 0016 does nothing on SQLite, which does not enforce column lengths.

### Changed

- Writes page: the All toggle sits in the header of the On column, and the top row says whether writing is off, paused or sending.

## [0.9.1] - 2026-10-28

### Changed

- Settings → Writes: Writing, Paused, All users and each user's mode are toggles that save as they change (no Save button). The show filter is a text box that grows when you click it, one show per line, and saves as you type. The preview shows the watch date for each write.

## [0.9.0] - 2026-10-28

### Added

- Watch-state writes to Jellyfin and Silo, off by default: a global switch, a pause, and a per-user mode with an optional title filter (Settings → Writes). Preview lists exactly what would be sent.
- Rules: whatever Local says is played is marked played in each app that has the title and does not show it played (Jellyfin keeps the original date; Silo dates it now). An unwatch is written only if MediaWrangler saw it happen after the user was switched on, and after it has stood for 30 seconds; never inferred from a difference.
- An outbox (`write_job`) drained at once at each app's pace, with retries; pausing keeps what is queued. Every attempt is recorded in `audit_log` with before and after.
- Our own writes come back as ordinary events and change nothing in Local, so they cannot loop.

## [0.8.21] - 2026-10-28

### Changed

- Connector add/edit: Test connection sits on the right of the Save and Cancel row and tests the form as it stands, before saving, on a new connector too. It says only "Works" or "Failed" (with the reason when it fails). The separate Connection card is gone.

## [0.8.20] - 2026-10-28

### Added

- Silo keys are checked against Silo at the start of every watch read. A key someone deleted is marked missing, the account shows as unreadable, and a "Make a new key" button (with the usual preview and confirm) replaces it for just that account.
- A leftover "MediaWrangler" key in Silo that MediaWrangler no longer holds (its secret cannot be recovered) is deleted before the new key is made, and the preview lists that call.

## [0.8.19] - 2026-10-28

### Added

- Silo: read every account's watch state. On the Silo connector page, MediaWrangler shows what it would send (one `POST /api/v2/admin/api-keys` per account without a key), and on confirmation makes the keys, stores them encrypted and reads those accounts. Keys can be revoked per account. This is the one place MediaWrangler writes to Silo, and only when you confirm it.
- When an app becomes readable for someone Local already holds, what it says is folded in by the seeding rule (most progress wins; nothing is lowered or removed).

### Changed

- Each source reads its users' watch state in parallel (8 at a time) instead of one after another, so adding readable Silo accounts adds little time.

## [0.8.18] - 2026-10-28

### Fixed

- The user picker sits top right in the page header, with its label and dropdown on one line (the label was inheriting a stacked layout).

## [0.8.17] - 2026-10-28

### Changed

- Connectors card: the next sync time has its own column, and Sync everything sits top right beside the master.

## [0.8.16] - 2026-10-28

### Changed

- One standard order for apps: media servers (master first), the media share, then Sonarr, the Radarrs and Sportarr. Physical presence used to put the share and arrs first.

## [0.8.15] - 2026-10-28

### Changed

- Overview: Metadata shows how many titles Sonarr, Radarr and Sportarr track; Watched shows each app's played count per user next to Local.

## [0.8.14] - 2026-10-28

### Changed

- Overview summaries are compact grids: one row per group and one column per app (Metadata, Physical), with Watched and Activity side by side.

## [0.8.13] - 2026-10-28

### Changed

- The Overview summaries for Metadata, Physical, Watched and Activity are tables instead of cards of boxes.

## [0.8.12] - 2026-10-28

### Changed

- Overview restyled like the Watch states page (summary boxes with rows, full-width layout) and gains a summary table for each page: Metadata, Physical, Watched and Activity.

## [0.8.11] - 2026-10-28

### Changed

- Main menu: "Presence" is now "Metadata" and "Watch" is now "Watched".
- The possible-mismatch explanation moved from above the table into each row.

## [0.8.10] - 2026-10-28

### Changed

- The explanatory notes under the summary boxes on Metadata presence and Physical presence are gone.

## [0.8.9] - 2026-10-27

### Added
- Metadata presence shows Sonarr and Radarr as columns: Tracked or Not tracked
  for each title a media server has, and how many titles each tracks. They are
  not compared with the master (an arr tracks titles it has no files for), so
  they raise no issues and add no rows of their own.

## [0.8.8] - 2026-10-27

### Changed
- The picker says "User" (not "Person") and is a plain label and dropdown on one
  line next to the page title.
- Tabs: inactive ones are darker, and the first tab lines up with the left edge
  of the panel below.

## [0.8.7] - 2026-10-27

### Changed
- Tabs (library groups, Activity, Settings) look like tabs: attached to the
  panel below, with the current one raised and marked in the accent colour.
- Watch states: the person picker is a labelled pill in the page header,
  instead of a loose dropdown.
- Page titles are bolder, the current main-menu item is tinted, and the top
  bar has a soft shadow.

## [0.8.6] - 2026-10-27

### Changed
- The summary figures have no boxes of their own now: each app's box lists its
  figures as rows with a divider line between them.
- Metadata presence, Physical media (and a folder's files), a series' episodes
  and Not in any app use the same summary boxes and table style as Watch
  states: wider page, banded columns, a stronger header row, and long titles
  cut short with the full name on hover.

## [0.8.5] - 2026-10-27

### Changed
- Watch states: the Local column now sits at the right, just before Apply from
  (after any imported files).
- The summary at the top is back to a box per app with a row per figure
  (Played, Not played, In progress, Read), each figure in its own box.

## [0.8.4] - 2026-10-27

### Removed
- The mass-unwatch hold added in 0.8.3. A held batch waited for a decision and
  stopped Local reading that app user's later changes, which would freeze Local
  for anyone who doesn't check Watch states. Local now applies every change as
  it arrives; unwatching keeps every dated play, so nothing is lost. A guard
  that can't block belongs with the writes in 0.9.

## [0.8.3] - 2026-10-27

### Added
- **Local**, MediaWrangler's own record of what each person has watched, built
  read-only first (nothing is written to any app). Each person is filled once
  from the union of what their readable sources say (the most progress wins:
  played beats part-watched, the furthest position among part-watched), then
  kept up to date from the changes each app reports, not by comparing states.
  Watching or unwatching in any app changes Local.
- Local keeps a dated list of plays that unwatching never deletes. Plays of one
  title from different apps on the same day, or the day either side, are one
  play with every app named.
- A change log with the reason for every Local change.
- A batch of 50 or more unwatches from one app user is held, not applied, with a
  banner on Watch states to apply it or set it aside.
- Watch states: a Local column (between the apps and the imports), an "≠L"
  badge where an app and Local disagree, and a "Differs from Local" filter.
  People whose Silo profile can't be read are filled from Jellyfin alone, as
  the existing notice says.
- Local is brought up to date at start-up as well as after every sync and webhook.
- Titles that only an imported file has, and no app does, aren't in Local (they
  have no stable id) and stay on the "Not in any app" tab.

## [0.8.2] - 2026-10-27

### Fixed
- A burst of webhooks (a whole series marked watched in Jellyfin) overloaded
  the listener: each event scanned the whole catalogue to find its item, many
  ran at once and ran out of database connections, and some events were lost.
  Items and users are now found directly, the listener answers at once and
  applies events one at a time in the order they arrived (so a watch followed
  by an unwatch can no longer be applied the wrong way round), and a full queue
  answers 503 so the sender can try again.
- Webhooks can still be missed (Jellyfin does not retry), so a quick read of
  who watched what now follows a burst of applied changes, within about 20
  seconds and not more than once every two minutes, to correct anything lost.

## [0.8.1] - 2026-10-27

### Added
- Webhooks from Jellyfin and the arrs. Jellyfin's Webhook plugin can now tell
  MediaWrangler when a user's played state or position is saved (applied to
  the stored state at once: played, unplayed, or a resume point), when a user
  starts or stops playing (logged), and when items are added or deleted (a
  Jellyfin sync, once per burst). Sonarr and Radarr tell it when something is
  imported, upgraded, renamed, deleted, added or removed (logged, then a sync
  of that app). Jellyfin and the arrs cannot sign requests, so they send the
  per-app secret in an `X-MW-Token` header, compared in constant time; each
  app has its own secret and receiver address
  (`/hooks/jellyfin/<id>`, `/hooks/arr/<id>`). Settings → Connectors shows the
  card and makes the secret for each of them.
- Nothing is sent to these apps by this release. Adding the destination in each
  app is a change to that app and is done separately, with your approval.

## [0.8.0] - 2026-10-27

### Added
- Webhooks from Silo, live. MediaWrangler listens on a second port (8791, set
  with MW_WEBHOOK_PORT; 0 turns it off) that is separate from the login page,
  has no sessions, and accepts only requests signed with a per-app secret
  (Silo's own signing scheme, five-minute replay window, 256 KB limit). It is
  for the LAN: publish it to the LAN, never through a public proxy.
- What Silo's Event Webhooks plugin sends is acted on. Watched, unwatched,
  favourite and watchlist changes are stored the moment they happen, and a
  season or series marked (un)watched is expanded to its episodes. Finished
  plays are read from Silo's admin playback history when sessions change: a
  completed play becomes watched, an unfinished one a resume point. A scan or
  library change triggers a Silo sync, at most once per burst and not within
  ten minutes of the last one. This applies to people whose watch state
  MediaWrangler reads (today your own profile); other profiles' events are
  logged with a note, until user sync (0.10).
- Activity has a new "Live events" tab: recent events from your apps, with new
  ones appearing at the top as they arrive.
- Settings → Connectors → Silo has a "Webhooks from Silo" card: the address to
  give the plugin, and a secret generated once (kept encrypted).
- Migration 0011 adds `source.webhook_secret_enc` and `webhook_event`.
- Not in this release: Jellyfin and the arrs. Adding a webhook destination to
  those apps changes them, so it waits for your go-ahead.

## [0.7.5] - 2026-10-26

### Added
- Imports now keep every play, not just the latest: each Trakt history entry
  (including rewatches) and each Yamtrack row for an episode or film is stored
  with its date and what the file called it. Several Yamtrack rows for the
  same film become one film with several plays. The Imports table shows the
  total plays, and the Played tooltip in the Yamtrack and Trakt columns says
  how many plays are recorded. Nothing else changes: this is stored for the
  Local database later, and no deduplication across apps is attempted yet.
- Migration 0010 adds `watch_import_play`. Existing imports have no plays until
  the file is imported again.

## [0.7.4] - 2026-10-26

### Changed
- Watch states: progress bars have subtler stripes, richer and darker fills
  (lighter text contrast in dark mode too), a blue "some played" colour so it
  can't be mistaken for the amber/red differences, and no end-cap border.
- "Apply from" now also applies when a title isn't in an app (including titles
  only an imported file has): it names the source with the most played.
- Watch states pages use most of the window width, so the last column (Apply
  from) is no longer cut off with four or more apps.
- The header row is more distinct from the table body.
- The summary figures (Played, Not played, In progress, Read) are separate
  boxes in a row across each app's summary.

## [0.7.3] - 2026-10-26

### Changed
- Watch states titles no longer wrap: long ones are cut with an ellipsis, and
  hovering shows the full title in a tooltip only when it is cut. (Add
  `data-tip-truncated` and the `.trunc` class to any element to do the same.)

## [0.7.2] - 2026-10-26

### Changed
- Watch states: progress bars are now an inset, outlined pill with a textured
  gradient fill and a solid end cap, so rows of full cells no longer merge
  into one block of colour. The difference from the master (−N / +N) sits at
  the right of the cell, the played/total count at the left.
- Films get the same bar (full when played, partial for a part-watched film,
  empty when unplayed) with no dates in the cell; the date is in the tooltip.
- The Played figure at the top of each summary table is larger and bolder.

## [0.7.1] - 2026-10-26

### Changed
- Watch states, redesigned for readability:
  - Each app's summary is a small table: Played, Not played, In progress,
    Read. The +/- numbers and the reminder text above the table are gone.
  - The table's app columns alternate in background colour. A cell shows
    played/total with a red −N (played in the master, not here) or amber +N
    (played here, not in the master); hovering explains each number.
  - Series cells have a progress bar behind them: grey for nothing played,
    amber for some, green for completed. Film cells are colour coded the same
    way, without a bar.
  - Specials are counted with the regular episodes (the tooltip says how many
    are included). The "Played state differs" and "Specials differ" filters
    are merged into "All differences".
  - A new last column, "Apply from", names the app that would be used to copy
    watched status to the others: the one with the most played, the master on
    a tie, shown only where apps differ. Imported files can be the source.
    Nothing is written; applying arrives in 1.2.
- One tooltip for the whole app: the `dx.tip` template macro, or a `data-tip`
  attribute on any element (static/tooltip.js). The +/- badges on Overview,
  Presence and Physical media use it too.

## [0.7.0] - 2026-10-26

### Changed
- Titles in an imported history that no app has now appear in Watch states'
  regular lists (TV, Anime, Movies) instead of only on the "Not in any app"
  tab: one row per title, "not in app" under every real app, and what the
  Yamtrack and Trakt columns say was watched. They are never counted as
  differences, so "All differences" stays as it was; a new "Not in any app"
  filter shows just these rows.
- Which list a title goes in: films go to the films group; a show goes to the
  anime group when the anime ID list knows its TVDB or TMDB id, otherwise to
  the shows group. Each library group has a new setting in Settings → Library
  groups, "Imported titles no app has" (Automatic, Shows, Anime, Films or
  Nowhere). Automatic picks from the group's name: "Anime" takes anime, other
  series groups take shows (not Sport), movie groups take films (not 4K). Only
  one group takes each kind, the first in the list.
- The "Not in any app" tab now holds only what can't be placed: anime the ID
  list has no usable ids for, and titles whose list is set to Nowhere.
- Migration 0009 adds `library_group.import_landing`.

## [0.6.5] - 2026-10-26

### Fixed
- Anime the ID list maps to a whole show without naming a season (Dragon Ball
  GT, Dragon Ball Super, Gantz) were left as "Can't be compared". They now
  count as the show's first season. Entries the list has no ids for at all
  (e.g. Gantz 2nd Stage) still can't be placed.

## [0.6.4] - 2026-10-26

### Fixed
- Settings → Connectors failed with an Internal Server Error when an import
  made before 0.6.3 existed (its stored counts had no anime figure).

## [0.6.3] - 2026-10-26

### Added
- Yamtrack's MyAnimeList anime now match your libraries through the public
  community anime-lists file (MAL id to TVDB/TMDB/IMDb id and season), so
  they appear in the Yamtrack column of Watch states instead of "Not in any
  app". Yamtrack only records how many episodes were watched, so an entry
  counts as the first N episodes of the season the list maps it to; anime
  films match as films. Anime the list doesn't know, or can't place, stay on
  the "Not in any app" tab as "Can't be compared".
- The list is downloaded by MediaWrangler and refreshed weekly (retrying every
  six hours after a failure, and always keeping the last good copy). It is the
  only thing fetched from outside your network. Settings → Connectors shows
  when it was last updated and has a "Download now" button.
- Migration 0008 adds `mal` and `progress` to imported items. Existing Yamtrack
  imports need to be imported again to pick the anime up.

## [0.6.2] - 2026-10-26

### Changed
- Titles in an imported history that none of your apps has now show on Watch
  states, on a "Not in any app" tab beside the library groups. One row per
  title, a column per import (Yamtrack, Trakt), with filters: All titles,
  Films, Series, Can't be compared (Yamtrack's MyAnimeList anime), and an
  import selector. The link and page under Settings are gone.

## [0.6.1] - 2026-10-26

### Changed
- Watch history imports moved to their own section on Settings → Connectors.
  The person is chosen on the same form as the file; "Unlink" removes that
  file's data for that person; a person can have a Yamtrack and a Trakt import
  at the same time. (Was a separate page under Watch states.)

## [0.6.0] - 2026-10-26

### Added
- Watch history imports (Watch states → "Watch history imports"). Upload a
  Yamtrack CSV export or a Trakt export zip for one person (you must choose
  who; there is no default). Each import appears as another column in that
  person's Watch states, headed "Yamtrack [file import]" or "Trakt [file
  import]", compared with the master like every other app (`+N` / `-N` / `✓`,
  and the "Played state differs" filter). Importing again for the same person
  and kind replaces the earlier import.
- "Not in any app" page for each import: titles in the file that none of your
  apps has in a library, plus Yamtrack's MyAnimeList anime, which have no
  episode list and so can't be compared.
- Read-only: files are only read and stored inside MediaWrangler; nothing is
  sent to Yamtrack, Trakt or any app.
- Migration 0007 adds `watch_import` and `watch_import_item`.

## [0.5.1] - 2026-10-26

### Changed
- Each sync schedule is now Manual (default), Every day at a 24-hour time, or
  Every N minutes/hours, so the full sync can be kept to an off-peak hour. A
  daily time waits for its hour even on a connector that has never synced, and
  a failed daily run retries after 30 minutes, doubling up to 4 hours.
- Migration 0006 adds `source.schedule_at` and `source.quick_at`.

## [0.5.0] - 2026-10-26

### Added
- Scheduled syncs. Each connector has a full-sync interval and, for Jellyfin
  and Silo, a watch-state interval. Both are blank (off) by default. A
  watch-state sync reads only users and what each has watched, so it can run
  often; a full sync also does this. Intervals get up to 10% jitter, a failing
  connector waits 2x, 4x then 8x its interval, and a source is never synced
  twice at once. Nothing about the schedule is stored: it is worked out from
  the last run, so it survives a restart. The Overview shows the next run.
- Activity page with two tabs: sync runs (what ran, how it started, result,
  time taken) and the watch change log (who played or unplayed what, in which
  app). Both filter instantly like the comparison pages.

### Changed
- Migration 0005 adds `source.quick_minutes`.

## [0.4.9] - 2026-10-26

### Changed
- Physical media now compares with the master like every other page: the
  summary badges, the per-folder count differences and the "File count
  differs from the master" filter. What is on the share still decides
  "not on disk", "not picked up", "skipped" and "not in any arr".
- The master has a star in front of its name wherever apps are named:
  summaries, column headings, the Overview and the Settings lists.
- Headings at the top of Overview, Watch states, Presence and Physical media
  all read "Name [connector type]" (with the star for the master), instead
  of the mix of master / reference / on disk / arr / media server tags.

## [0.4.8] - 2026-10-26

### Added
- Relative +/- next to the summary numbers on every page, compared with the
  master (or the media share on Physical media): red is fewer, amber is more,
  a green tick is the same.
  - Presence: each app's title count, plus signed missing / not-in-master
    counts, and Present on the Overview.
  - Physical media: files, folders and size per app against the media share,
    plus signed counts of folders not picked up, skipped or not in any arr.
  - Overview: series, movies and episodes of each media server against the
    master (arrs aren't compared there, since they also track wanted titles).
  - Watch states: in progress against Jellyfin. Played totals keep their
    per-episode differences, which can't cancel out.

## [0.4.7] - 2026-10-26

### Fixed
- Applying a filter could end on a raw JSON error ("int_parsing ... source").
  A form sends an empty `source=` when "Any app" is chosen, and Watch states,
  Presence and Physical media rejected it. A blank or non-numeric `user`,
  `group` or `source` in the address now simply means "not set".

## [0.4.6] - 2026-10-26

### Fixed
- The disk scan skipped video types outside its list, so a folder of `.ogm`
  files (Virtua Fighter, 24 episodes) showed as "not on disk" while every
  app had it. It now counts `.ogm .ogv .divx .rmvb .3gp .mts .asf .vob` as
  well. Sync the media share to pick the files up.

## [0.4.5] - 2026-10-26

### Changed
- Physical media says why an app has nothing for a folder: an arr that
  doesn't know the show reads "Not in Sonarr", one that tracks it but
  imported nothing reads "No files", and a media server reads "Not picked
  up", instead of a bare "None".

## [0.4.4] - 2026-10-26

### Changed
- Presence and Physical media work like Watch states now: every row is on
  one page, the filter, app picker and search update instantly in the
  browser, the paging is gone, and the address bar follows along. Clicking a
  count in a summary filters in place, and group tabs keep the current
  filter. Presence's "Same title / Different" buttons return to the filter
  as it is at that moment.
- Possible mismatches on Presence is always shown, whatever the filter.
- Presence and Physical views are cached like Watch views (until the next
  sync or change), and the overview reuses the Presence ones. Cold loads are
  under a second and repeat loads take well under 150 ms.
- The three tables share one set of filter definitions and one filter bar.

### Removed
- The Watch states warning that one app was read minutes before another.
  Each app's last-read time is still shown in its summary.

## [0.4.3] - 2026-10-26

### Changed
- Watch states filter order: "All differences" (played state or specials)
  is first and the default; "All titles" is last.
- Filtering and searching happen in the browser, so they're instant: the
  page carries every title once and the filter, app picker and search box
  only show and hide rows. Clicking a difference count in the summary
  filters in place. The address bar follows along, so a filtered view can
  be bookmarked or shared.
- Every record is on the page; the paging is gone.
- Faster loads: watch views are read as plain columns instead of tens of
  thousands of ORM objects (about 3x faster), and a built view is reused
  until the next sync or change made through the UI. Cold loads are under a
  second; repeat loads take a few tens of milliseconds.

### Fixed
- The Person dropdown on Watch states did nothing: its inline handler is
  blocked by the content-security policy. It is now wired up from a script
  file and keeps the current filter when you switch person.

## [0.4.2] - 2026-10-26

### Changed
- Watch states lists every title in the group by default ("All titles"),
  watched or not, including series with no episodes. "Watched in any app"
  is the old "Everything watched" view.

## [0.4.1] - 2026-10-26

### Changed
- Watch states summary shows each app's differences right after its played
  total, colour coded: red −N played only in the reference app, amber +N
  played only in this one, green ✓ when they agree. Totals alone could hide
  differences that cancel out.

### Added
- When each app's watch state was last read, and a warning when one app was
  read more than 5 minutes before another (changes made since, including
  ones Scrob copies between apps, don't show until it's synced again).

## [0.4.0] - 2026-10-26

### Added
- Users from Jellyfin (every user, via the admin key) and Silo (every
  profile of every account), linked to people by name; relink in
  Settings → Users, where hand-set links are kept.
- Watch state read with each sync: Jellyfin played and resumable items for
  all users; Silo's progress list for the key's own account's profiles (Silo
  refuses other accounts' profiles even for admin keys, so other Silo users
  show as "not readable").
- A change log of watch state: after the first read, every played,
  unplayed and real progress change is recorded. Items that vanished from
  an app (a deleted file) don't count as unwatched.
- Watch states view per person and group: played counts per app, played only
  in the reference app or only elsewhere, specials separately, in progress;
  only episodes both apps have are compared. Series drill-down per episode.
  A film counts as watched if any copy of it was (Jellyfin keeps 1080p and
  4K copies apart; Silo merges them).

### Changed
- Yamtrack is no longer planned as a live connector; MediaWrangler keeps the
  watch history itself. A Yamtrack/Trakt history import comes later.

## [0.3.0] - 2026-10-26

### Added
- Sonarr, Radarr and Sportarr ingest: root folders as libraries, series and
  movies as items, and every file the arr has on record (Sonarr files mapped
  to S/E via the episode list; Sportarr through its native league API).
- The media share as a built-in source ("Media share"), added automatically
  when mounted. It walks only top-level folders that belong to a library
  group, counts video files, and keeps extras (trailers, featurettes,
  samples) out of comparisons. A deleted share source isn't re-added.
- Jellyfin files from the episode and movie paths it reports; Silo's scanned
  and skipped folders (with skip reasons), episode counts per series folder,
  and every version file of each movie.
- Path maps derived from library groups (each library's folder maps to the
  share folder in the same group, e.g. Radarr `/movies` => `/media/Movies`).
  Editing a map by hand keeps it; clearing it goes back to automatic.
  Stored paths are re-mapped whenever a map changes.
- Physical media view per group and folder: on disk but not picked up by an
  app, skipped by an app (with the reason), on disk but not in any arr, not
  on disk, file counts that differ from disk, duplicate files; path-map
  failures listed with examples. Folder drill-down lines up every file
  across disk, arrs and Jellyfin.
- File counts per connector on the overview.

### Changed
- The presence matrix compares media servers only; arr items are stored for
  the physical view.

### Fixed
- Silo's `observed_file_count` for scanned folders is not a file count, so
  it is no longer used; Silo's episode counts and movie versions are.

## [0.2.0] - 2026-10-26

### Added
- Read-only ingest from Jellyfin (10.11 `/Items`, paged; virtual "missing"
  episodes skipped) and Silo (native catalog, paged; per-item details for the
  full ID set and paths, fetched once and reused; 429 backoff).
- Manual sync per connector or all at once, run in the background with live
  progress on the overview, and sync-run history with counts.
- Library groups: created from the master's libraries, with other apps'
  libraries mapped by folder name (e.g. Silo "Shows" at `.../TV` joins
  Jellyfin "TV"). Editable in Settings → Library groups; Sport starts outside
  the presence matrix.
- Master media server setting (Settings → General), defaulting to Jellyfin.
- Matching engine: TVDB → TMDB → IMDb for series and TMDB → IMDb for movies,
  never joining items that disagree on a higher-ranked ID; title + year
  fallback as unconfirmed (low confidence); confirm/reject decisions that
  survive re-sync.
- Metadata presence matrix per group: missing in an app, not in master,
  unconfirmed matches, possible mismatches (same title, different identity),
  regular episode and specials counts compared separately, duplicates; with
  filters, search and paging. Overview summary per group.

### Fixed
- Silo items are keyed per library, since Silo reuses one content ID for a
  title in two libraries (Movies and Movies 4K).

## [0.1.2] - 2026-10-26

### Fixed
- Silo connection test now says when the URL points at Silo's Jellyfin-compat
  port instead of reporting a bare HTTP 404.

## [0.1.1] - 2026-10-26

### Changed
- Minimum admin password length lowered from 14 to 10 characters.

## [0.1.0] - 2026-10-26

### Added
- Single-container app: FastAPI, SQLite (WAL) with Alembic migrations run at
  startup, server-rendered HTMX UI with light and dark themes.
- First-run setup creating the single admin account; sign-up closes afterwards.
- Login with argon2id passwords (at least 14 characters), optional TOTP with
  replay protection and one-time recovery codes, suggested at setup.
- Lockout after repeated failures per IP (10) and per account (5) for
  15 minutes, and a sign-in log in Settings → Security.
- Server-side sessions (7-day idle, 30-day absolute expiry) with
  `__Host-` cookies in internet-facing mode, active-session list and sign-out.
- CSRF protection (double-submit token plus Origin check), strict CSP and
  security headers, HSTS when `MW_PUBLIC_URL` is https.
- Client IPs from `X-Forwarded-For` only when the peer is in
  `MW_TRUSTED_PROXIES`.
- Connector interface and registry, path prefix maps, and connectors with
  "test connection" for Jellyfin, Silo (native API), Sonarr, Radarr and
  Sportarr (native API).
- Settings → Connectors: add, edit, test and delete connector instances. API
  keys are encrypted at rest with the master key and never sent back to the
  browser.
- API keys for MediaWrangler's own API; `/api/health` and `/api/v1/status`.
- Docker image running as `PUID`/`PGID`, healthcheck, `compose.yaml` with the
  media share mounted read-only, and `.env.example`.
- `scripts/fetch-api-specs` and API spec snapshots in `docs/api/`.

[Unreleased]: https://github.com/incredibad/mediawrangler/compare/v0.4.9...HEAD
[0.4.9]: https://github.com/incredibad/mediawrangler/compare/v0.4.8...v0.4.9
[0.4.8]: https://github.com/incredibad/mediawrangler/compare/v0.4.7...v0.4.8
[0.4.7]: https://github.com/incredibad/mediawrangler/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/incredibad/mediawrangler/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/incredibad/mediawrangler/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/incredibad/mediawrangler/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/incredibad/mediawrangler/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/incredibad/mediawrangler/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/incredibad/mediawrangler/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/incredibad/mediawrangler/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/incredibad/mediawrangler/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/incredibad/mediawrangler/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/incredibad/mediawrangler/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/incredibad/mediawrangler/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/incredibad/mediawrangler/releases/tag/v0.1.0
