# MediaWrangler

A self-hosted hub that pulls media, file and watch-state data from your media
apps, stores it locally, and shows where they disagree. Later releases sync
watch state and users between apps and fix mismatches from the UI.

So far: Jellyfin, Silo, Sonarr, Radarr, Sportarr and the media share itself
are read and compared, both by title (metadata presence) and by file
(physical media), per library group, and watch state is compared per
person across Jellyfin and Silo.
See `CHANGELOG.md`.

## Run it

Download [`docker-compose.yml`](docker-compose.yml), set the values in it (or in a
`.env` file next to it, see [`.env.example`](.env.example)), then:

```sh
docker compose up -d
```

Open `MW_PUBLIC_URL` and create the admin account.

The API keys you give MediaWrangler for your apps are stored encrypted. The key for
that is made on first start as `secret.key` in the data folder, and every backup
includes it. If you move or restore the database by hand, take `secret.key` with it:
without it the stored app keys can't be read and have to be entered again. To keep
the key outside the data folder instead, point `MW_SECRET_KEY_FILE` at a file
holding it (e.g. a Docker secret).

- **LAN-only:** set `MW_PUBLIC_URL=http://<host>:8790`. Two-factor is optional.
- **Internet-facing:** put it behind a TLS reverse proxy, set
  `MW_PUBLIC_URL=https://...` and `MW_TRUSTED_PROXIES` to the proxy's IP, and
  turn on two-factor in Settings → Security. Secure cookies then only work over
  https, so use the https URL from the LAN too.

MediaWrangler does not change media files unless you allow it. The share is mounted
read-write so that, once you switch on "File changes" under Settings → Writes, it can
move orphaned files you choose into a reversible trash folder (kept 30 days).
`MW_MEDIA_FOLDERS` names the library folders it lists; nothing else on the share is
read.

## Develop

The dev image holds the dependencies; the source is bind-mounted.

```sh
docker build --target dev -t mediawrangler-dev .
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/app" -w /app mediawrangler-dev python -m pytest
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/app" -w /app -e RUFF_CACHE_DIR=/tmp/ruff mediawrangler-dev ruff check src tests
```

New migration after changing `models.py`:

```sh
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/app" -w /app -e MW_ALEMBIC_URL=sqlite:////tmp/m.db \
  mediawrangler-dev sh -c 'alembic upgrade head && alembic revision --autogenerate -m "describe it"'
```

Commits follow Conventional Commits; the version lives in `pyproject.toml`.
