# Silo Event Webhooks (beta)

A [Silo](https://github.com/Silo-Server/silo-server) plugin that sends the Silo events you switch on to
one web address, as signed JSON. It works like the Jellyfin Webhook plugin: pick the events, point
it at a URL, optionally limit user events to chosen profiles. Anything that can receive an HTTP
POST can use it: a script, Home Assistant, n8n, a Discord relay, your own service.

- Switch on any of 44 events: watched and unwatched, favourites, library changes, scans, jobs, server
  changes and more.
- One destination URL, with an optional secret. Requests are signed with the same scheme Silo uses
  for its own webhooks (`X-Silo-Signature: t=<epoch>,v1=<hmac-sha256>`), so existing verifiers work.
- Optional profile filter, by profile id or by name.
- Delivery uses Silo's own retry queue (about 10 s, 30 s, 1 min, 3 min, 15 min), with a stable
  `event_id` for de-duplication.

**Everything a receiver's author needs is in [docs/REFERENCE.md](docs/REFERENCE.md)**: the request
and its headers, the JSON body, signature checks in Python and JavaScript, delivery and retry
rules, profile filtering, and every event with what it means. The same text is shown on the
plugin's page inside Silo. The file is generated from the code (`plugin docs`), so it matches the
build.

## Install

Build (needs Docker, nothing else):

```sh
./build.sh 0.1.0-beta.8        # runs the tests, builds dist/event-webhooks-linux-{amd64,arm64}, writes docs/REFERENCE.md
```

Install the binary for your Silo host's architecture from Silo's admin area (Plugins, upload), or:

```sh
curl -H "Authorization: Bearer $SILO_ADMIN_KEY" \
     -F archive=@dist/event-webhooks-linux-amd64 \
     https://silo.example.com/api/v2/admin/plugins/uploads
```

Then open the plugin's settings and fill in the form.

## Settings

| Setting | |
| --- | --- |
| Webhook URL | Required. Every event you switch on is POSTed here. Silo may refuse private (LAN) addresses unless its notification settings allow them. |
| Secret | Optional. Adds the `X-Silo-Signature` header. |
| Events to send | The checkboxes. Defaults to `user_state.changed`. |
| Only these profiles | Optional. Profile ids or names, one per line or comma separated. |
| Silo URL and API key | Optional. Lets the plugin look up profile names, so you can filter by name and so `profile.name` is filled in. Read-only use. |

## A minimal receiver

```python
# pip install flask
import hashlib, hmac, time
from flask import Flask, request

SECRET = "the secret you set"
seen = set()
app = Flask(__name__)

@app.post("/silo-events")
def silo_events():
    raw = request.get_data()
    parts = dict(p.split("=", 1) for p in request.headers["X-Silo-Signature"].split(","))
    expected = hmac.new(SECRET.encode(), parts["t"].encode() + b"." + raw, hashlib.sha256).hexdigest()
    if abs(time.time() - int(parts["t"])) > 300 or not hmac.compare_digest(expected, parts["v1"]):
        return "bad signature", 401
    event = request.get_json()
    if event["event_id"] in seen:          # Silo delivers at least once
        return "", 204
    seen.add(event["event_id"])
    print(event["event"], event["data"])
    return "", 204
```

## Notes

- Silo matches plugin subscriptions by exact event name (no wildcards), so the plugin lists every
  event it knows about and filters by your switches at run time. Events marked "seen" in the reference
  have been observed on a live Silo; the rest come from reading Silo's source and may differ by
  version.
- A whole season or series marked watched or unwatched is **one** `user_state.changed` event naming
  the season or series, not one per episode.
- The plugin only reads from Silo (profile names) and only writes to the URL you give it.

## Licence

Set in `manifest.go` (`license_spdx`). The Silo plugin SDK it builds on is Apache-2.0.
