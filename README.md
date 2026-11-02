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

```sh
cp .env.example .env && chmod 600 .env        # then edit it
mkdir -p secrets && openssl rand -base64 48 > secrets/secret.key && chmod 600 secrets/secret.key
docker compose up -d --build
```

Open `MW_PUBLIC_URL` and create the admin account. Keep `secrets/secret.key`
safe: stored connector keys can't be decrypted without it.

- **LAN-only:** set `MW_PUBLIC_URL=http://<host>:8790`. Two-factor is optional.
- **Internet-facing:** put it behind a TLS reverse proxy, set
  `MW_PUBLIC_URL=https://...` and `MW_TRUSTED_PROXIES` to the proxy's IP, and
  turn on two-factor in Settings → Security. Secure cookies then only work over
  https, so use the https URL from the LAN too.

MediaWrangler does not change media files unless you allow it: mount the share
read-only (as in `compose.yaml`). `compose.yaml` mounts only the library folders in
use, plus a `.trash` folder, so nothing else on the share is visible to it; add a line
there for a new library. They are read-write so it can move orphaned files you choose
into a reversible trash folder (kept 30 days), once you switch on "File changes" under
Settings → Writes.

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
