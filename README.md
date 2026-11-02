# MediaWrangler

A self-hosted hub for a home media setup. It reads your media server, your *arr apps and
the media share itself, keeps its own copy of what it finds, and shows you where they
disagree: a show Sonarr has but Jellyfin doesn't, a file on disk nothing knows about, an
episode one person has watched in Jellyfin but not in Silo. It can then fix those
mismatches for you and keep watch state in step between apps.

It runs as one Docker container with a web UI. Nothing is changed in your apps or on your
disk until you switch it on.

## What it does

- **Overview:** each connected app, when it last synced, and a summary of what's out of
  step.
- **Metadata:** every show and movie, per library, with whether each app has it. Missing
  or mismatched titles are flagged.
- **Physical:** the actual files on the share against what each app thinks is there, down
  to folders and episodes. Orphaned files, missing files and duplicates show up here.
- **Watched:** per person, what each app says they have watched, side by side. You can add
  Yamtrack and Trakt exports as extra columns, to bring old history in.
- **Activity:** every sync, every change received from an app, and every change
  MediaWrangler made, with before and after.
- **Fixes:** a Fix menu on mismatched rows. It can refresh or re-identify a title in an
  app, add a folder to Sonarr or Radarr, tell Sonarr which episode a file is, rescan a
  folder, or move an orphaned file to the trash. Each fix shows a preview first.
- **Watch-state sync:** once switched on, a title marked played in one app is marked
  played in the others. An unwatch is passed on only when MediaWrangler actually saw it
  happen. Changes are queued, sent at each app's pace and logged.
- **Accounts:** it can create a person's missing account in another app, so everyone
  exists everywhere.
- **Live updates:** with webhooks set up, changes in your apps reach MediaWrangler in
  seconds. Without them it relies on scheduled syncs.

### Supported apps

| App | Read | Writes |
|---|---|---|
| Jellyfin | Libraries, files, users, watch state | Watch state, accounts, refresh and identify |
| Silo | Libraries, files, profiles, watch state | Watch state, accounts, refresh and identify |
| Sonarr / Radarr | Series, movies, files | Adding a folder as a series or movie, rescans, episode matching |
| Sportarr | Events and files | Rescans |
| The media share | Every file in your library folders | Moves files to the trash (only if allowed) |
| Yamtrack, Trakt | Watch history from an export file | Nothing |

You can connect several instances of each app, for example a second Radarr for 4K.

## Install

You need Docker with the Compose plugin, and the media share mounted on the Docker host.

1. Make a folder for MediaWrangler and download
   [`docker-compose.yml`](docker-compose.yml) into it.
2. Edit the values in it. The ones that matter:

   | Setting | What it is |
   |---|---|
   | `MW_PUBLIC_URL` | The address you'll open it at, e.g. `http://192.0.2.10:8790` |
   | `MW_HOST_MEDIA_DIR` | Where the media share is mounted on the host |
   | `MW_MEDIA_FOLDERS` | The library folders on the share, e.g. `TV,Movies` (empty means all) |
   | `MW_HOST_DATA_DIR` | Where the database, backups and key are kept (default `./data`) |
   | `PUID` / `PGID` | The user and group it runs as. They need read access to the share, and write access if you'll let it trash files |
   | `TZ` | Your timezone, e.g. `Europe/London`, for schedules and times shown |

   If you'd rather not edit the file, put the same settings in a `.env` file next to it
   (see [`.env.example`](.env.example)).
3. Start it:

   ```sh
   docker compose up -d
   ```
4. Open `MW_PUBLIC_URL` and create the admin account.

To update later:

```sh
docker compose pull && docker compose up -d
```

Image tags: `latest` is the current release, `0.12.102` and so on pin a version, and `dev`
follows development (may be unstable).

## Getting started in the app

1. **Settings → Connectors:** add each app with its address and API key. The Test button
   checks it before you save.
2. **Settings → Libraries:** group each app's libraries (e.g. "TV", "Movies") so they're
   compared like with like.
3. **Sync now** on the Overview, then look at Metadata, Physical and Watched.
4. **Settings → Users:** link the same person across apps if their names don't match.
5. When you're happy with what it shows, switch on the parts you want under
   **Settings → Writes**.
6. Optional: set sync schedules on each connector, and set up webhooks (below).

## The encryption key

The API keys you give MediaWrangler for your apps are stored encrypted in its database. The
key that encrypts them is a file called `secret.key`.

**You don't need to do anything to set it up.** On first start MediaWrangler creates
`secret.key` in the data folder, and every backup it makes includes it.

Look after it like a password. Without it, the stored API keys can't be read. Nothing else
is lost, but you'd have to enter every app's API key again. So if you move or restore the
database by hand, copy `secret.key` with it.

### Keeping the key out of the data folder (optional)

If your data folder goes somewhere less safe, such as an off-site backup, you can keep the
key apart and give it to MediaWrangler as a Docker secret:

1. Create the key once:

   ```sh
   mkdir -p secrets
   openssl rand -base64 48 > secrets/secret.key
   chmod 600 secrets/secret.key
   ```

   Or, if MediaWrangler has already made one, move `data/secret.key` to
   `secrets/secret.key` instead, so your stored API keys keep working.
2. Add this to `docker-compose.yml`:

   ```yaml
   services:
     mediawrangler:
       environment:
         MW_SECRET_KEY_FILE: /run/secrets/mw_secret_key
       secrets:
         - mw_secret_key

   secrets:
     mw_secret_key:
       file: ./secrets/secret.key
   ```
3. Run `docker compose up -d`.

Keep a copy of the key somewhere safe, because backups no longer include it.

## Webhooks

Webhooks let your apps tell MediaWrangler about changes as they happen. They arrive on a
second port (`MW_WEBHOOK_PORT`, default 8791). It accepts only signed requests and has no
login page, but keep it on your LAN and never put it behind a public proxy.

Each connector's page in **Settings → Connectors** shows the webhook address and secret for
that app, and what to tick:

- **Jellyfin:** a Generic destination in the Webhook plugin.
- **Sonarr / Radarr / Sportarr:** a Webhook connection under Settings → Connect.
- **Silo:** the [Event Webhooks plugin](plugins/silo-webhooks/README.md) in this repo.

## LAN-only or internet-facing

- **LAN-only:** set `MW_PUBLIC_URL=http://<host>:8790`. Two-factor sign-in is optional.
- **Internet-facing:** put it behind a reverse proxy with TLS, set
  `MW_PUBLIC_URL=https://...` and `MW_TRUSTED_PROXIES` to the proxy's IP, then turn on
  two-factor in **Settings → Security**. Secure cookies then only work over https, so use the
  https address from your LAN too.

Settings → Security also has active sessions and a sign-in log. Repeated failed sign-ins
lock the account and the IP out for a while.

## Safety

- **Read-only until you say so.** Watch-state writes, account creation and file changes are
  all off at first, each with its own switch under Settings → Writes. There's also a pause
  that keeps queued changes without sending them.
- **Files are never deleted directly.** "Move to trash" puts a file in a trash folder on the
  share, where it can be restored for 30 days (Settings → Trash).
- **Everything is logged.** Each change MediaWrangler makes is in the Activity log with its
  before and after.
- **Nightly backups.** A full backup is made at 02:30 every night, and the newest 14 are kept
  (Settings → Backups). You can also back up or restore by hand from that page.

## License

[MIT](LICENSE)
