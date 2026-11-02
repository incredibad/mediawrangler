package main

import (
	"fmt"
	"sort"
	"strings"
)

// md turns § into a backtick, so markdown with inline code can live in Go raw strings.
func md(text string) string { return strings.ReplaceAll(text, "§", "`") }

const overviewDoc = `
Event Webhooks sends the events a Silo server produces to one web address, as JSON.
You switch on the events to send, and can limit user events to chosen profiles.
Anything that can receive an HTTP POST can use it: a script, Home Assistant, n8n,
a Discord relay, or another service of your own.

Events are sent as they happen, one request per event. Silo hands each event to
the plugin at least once, so a receiver should expect an occasional repeat (see
**Delivery and retries**).
`

const requestDoc = `
## The request

Every event is one §POST§ with a JSON body.

| Header | Value |
| --- | --- |
| §Content-Type§ | §application/json§ |
| §User-Agent§ | §silo-event-webhooks/<version>§ |
| §X-Silo-Event§ | The event name, for example §user_state.changed§ |
| §X-Silo-Event-Id§ | Silo's id for the event. The same value on every retry of the same event |
| §X-Silo-Delivery§ | A new random id for each attempt |
| §X-Silo-Signature§ | Only when a secret is set: §t=<unix seconds>,v1=<hex HMAC-SHA256>§ |

Body:

~~~json
{
  "event": "user_state.changed",
  "event_id": "01J9ZK3Q8W6T0X5N2V4R7B1CDE",
  "delivery_id": "9f2c1d7e0b8a4c63a1e5d2f08b7c9a41",
  "timestamp": "2026-10-27T01:23:45Z",
  "version": 1,
  "profile": { "id": "3f1c2b7a-5d4e-4a9b-8c6f-0e2d1a9b7c55", "name": "alex" },
  "data": {
    "change": "watched",
    "content_id": "episode-tvdb-366263-1-23",
    "played": true,
    "profile_id": "3f1c2b7a-5d4e-4a9b-8c6f-0e2d1a9b7c55"
  }
}
~~~

| Field | Meaning |
| --- | --- |
| §event§ | The event name, as in the list below |
| §event_id§ | Silo's id for the event. Use it to ignore repeats |
| §delivery_id§ | Unique to this attempt; useful in logs |
| §timestamp§ | When this attempt was sent (UTC, RFC 3339). It is not when the event happened: Silo does not stamp events |
| §version§ | Version of this envelope, currently §1§ |
| §profile§ | Present when the event is about a profile. §name§ is only filled in when the plugin has a Silo API key (see Setup) |
| §data§ | Silo's own payload for the event, passed through unchanged. Its fields are Silo's, not this plugin's, and can change with Silo |
`

const signatureDoc = `
## Verifying the signature

If you set a secret, check §X-Silo-Signature§ before trusting a request. It follows
the same scheme as Silo's own webhooks (the Stripe convention): the signature is the
HMAC-SHA256, keyed with your secret, of the string §<t>.<raw request body>§, where
§t§ is the number in the header. Compare against the raw bytes you received, not a
re-encoded copy, and reject old timestamps (five minutes is a good limit).

~~~python
import hashlib, hmac, time

def verify(secret: str, header: str, raw_body: bytes, tolerance=300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    t, sig = parts["t"], parts["v1"]
    if abs(time.time() - int(t)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), t.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
~~~

~~~js
const crypto = require("crypto");

function verify(secret, header, rawBody, tolerance = 300) {
  const parts = Object.fromEntries(header.split(",").map((p) => p.split("=")));
  if (Math.abs(Date.now() / 1000 - Number(parts.t)) > tolerance) return false;
  const expected = crypto
    .createHmac("sha256", secret)
    .update(parts.t + "." + rawBody)
    .digest("hex");
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(parts.v1));
}
~~~
`

const deliveryDoc = `
## Delivery and retries

- Answer with any **2xx** status to accept an event. Do it quickly: the plugin waits 5 seconds.
- A **network error, timeout, 5xx, 408 or 429** makes Silo retry the same event (same §event_id§) after about 10 seconds, 30 seconds, 1 minute, 3 minutes and 15 minutes, then it gives up.
- Any other status (for example **401** or **404**) is logged and **not** retried, since repeating it would fail the same way.
- Delivery is **at least once** and **not ordered**. Keep the §event_id§ values you have handled and skip repeats, and do not assume events arrive in the order they happened.
- An event can be lost if Silo stops before it has stored it, or if Silo's delivery queue is full. Use a periodic read of Silo's API if you need certainty.
- Silo delivers a handful of events at a time across all its plugins. Receivers that are slow or down delay other plugins' events as well as these.
`

const profilesDoc = `
## Limiting events to profiles

The **Profiles** box takes profile ids or names, separated by commas or new lines. Leave it empty to send events for every profile.

- Only events whose data has a §profile_id§ are filtered. Server-wide events (scans, jobs, settings and so on) are not about a profile and are always sent if switched on.
- Names are matched without regard to case, and only work when a Silo URL and API key are set under **Profile names**. Without them, use profile ids: they are in the §profile_id§ of any event, and in Silo's admin screens.
- The API key is used only to read the list of accounts and their profiles (§GET /api/v2/admin/users§ and §/api/v2/admin/users/{id}/profiles§). Give it the least access Silo allows.
`

const warningDoc = `
## The warning event

If you limit events to profiles **by name** and the plugin cannot look the names up (no Silo URL and API key, or Silo refused the key), user events would be held back with no sign of why. So the plugin sends one extra request saying so, at most every 10 minutes:

~~~json
{
  "event": "plugin.warning",
  "data": {
    "message": "looking up profile names in Silo failed: silo answered 403 for /api/v2/admin/users",
    "held_back": "user_state.changed",
    "for_profile": "3f1c2b7a-5d4e-4a9b-8c6f-0e2d1a9b7c55",
    "profile_list": ["alex"],
    "repeats_every": "10m0s"
  }
}
~~~

It is not a Silo event and cannot be switched off; ignore any §event§ you do not recognise. Filtering by profile id never needs the lookup, so it never warns.

Events that come from Silo's internal bus (for example §scan_complete§ and §playback_sessions_changed§) carry a single value in §data.raw§ (a library id, a session id) instead of named fields.
`

const userStateDoc = `
## About §user_state.changed§

The most useful event, confirmed on a live Silo for watched, favourite and watchlist changes. Its §data§ is:

| Field | Meaning |
| --- | --- |
| §profile_id§ | The profile it happened to |
| §change§ | §watched§, §favorite§ and §watchlist§ are confirmed on a live Silo. Silo's source also has §progress§ and §not_interested§, which have not been seen |
| §content_id§ | What changed. Silo's own id: §episode-tvdb-<series>-<season>-<episode>§, §season-tvdb-<series>-<season>§, §series-tvdb-<id>§ and similar (films use a §movie-§ id, which has not been checked) |
| §series_id§ | The series, when the item belongs to one |
| §played§ | §true§ or §false§, with §change: "watched"§. §false§ is an unwatch |
| §is_favorite§, §in_watchlist§, §not_interested§ | The new value, with the matching §change§ |

Marking a whole season or series watched or unwatched produces **one** event that names the season or series, not one per episode. Expand it yourself, using the
episodes you already know for that season.
`

// referenceMarkdown is docs/REFERENCE.md: everything a receiver's author needs.
func referenceMarkdown() string {
	var b strings.Builder
	b.WriteString("# Silo Event Webhooks: reference\n\n")
	b.WriteString("<!-- Generated by `plugin docs`. Edit docs.go and events.go, not this file. -->\n")
	b.WriteString(strings.TrimSpace(md(overviewDoc)) + "\n\n")
	b.WriteString(strings.TrimSpace(md(requestDoc)) + "\n\n")
	b.WriteString(strings.TrimSpace(md(signatureDoc)) + "\n\n")
	b.WriteString(strings.TrimSpace(md(deliveryDoc)) + "\n\n")
	b.WriteString(strings.TrimSpace(md(profilesDoc)) + "\n\n")
	b.WriteString(strings.TrimSpace(md(warningDoc)) + "\n\n")
	b.WriteString(eventsMarkdown())
	b.WriteString("\n" + strings.TrimSpace(md(userStateDoc)) + "\n")
	return b.String()
}

// eventsMarkdown is the table of every event, by group.
func eventsMarkdown() string {
	var b strings.Builder
	b.WriteString("## Events\n\n")
	b.WriteString("Silo matches subscriptions by exact name, so these are all the events the plugin can send. ")
	b.WriteString("Events marked **seen** have been observed on a live Silo. The rest come from reading Silo's source code, ")
	b.WriteString("and their names or data may differ between Silo versions.\n")
	byGroup := map[string][]Event{}
	for _, e := range catalogue {
		byGroup[e.Group] = append(byGroup[e.Group], e)
	}
	for _, g := range groupOrder {
		events := byGroup[g]
		sort.SliceStable(events, func(i, j int) bool { return events[i].Name < events[j].Name })
		fmt.Fprintf(&b, "\n### %s\n\n| Event | Seen | What it means |\n| --- | --- | --- |\n", g)
		for _, e := range events {
			seen := ""
			if e.Verified {
				seen = "yes"
			}
			fmt.Fprintf(&b, "| `%s` | %s | %s |\n", e.Name, seen, e.Description)
		}
	}
	return b.String()
}

// presentationDescription is the short text in Silo's "About" panel. The full
// reference is under the settings form (a Documentation panel), and in
// docs/REFERENCE.md.
func presentationDescription() string {
	return md("**Beta.** Sends the Silo events you switch on to one web address as signed JSON, like the Jellyfin Webhook plugin. " +
		"Fifteen of the events below have been seen on a live Silo (marked in the documentation); the rest come from Silo's source code and may differ.\n\n" +
		"The request format, signature check, retry rules and the full event list are in the **Documentation** panel under the settings.")
}

func presentationSetup() string {
	return strings.TrimSpace(md(`
1. Enter the **Webhook URL** your receiver listens on. Use an address Silo's server can reach; Silo may block private addresses unless its notification settings allow them.
2. Optionally set a **Secret**. Requests then carry an §X-Silo-Signature§ header you can verify.
3. Switch on the **events** to send, in the groups below. Start with §user_state.changed§ (watched, unwatched, favourites).
4. Optionally list **profiles** to limit user events to. To use profile names instead of ids, fill in the Silo URL and API key under **Profile names**.
5. Save. Events are sent from then on; nothing is replayed.
`))
}

// referenceText is the reference as plain text, for Silo's settings form,
// which shows text exactly as given (no markdown): tables become lists and
// inline code loses its backticks.
func referenceText() string {
	var out []string
	inCode := false
	lines := strings.Split(referenceMarkdown(), "\n")
	for i := 0; i < len(lines); i++ {
		line := lines[i]
		switch {
		case strings.HasPrefix(line, "<!--"):
			continue
		case strings.HasPrefix(line, "~~~"):
			inCode = !inCode
			continue
		case inCode:
			out = append(out, "    "+line)
		case strings.HasPrefix(line, "# "):
			out = append(out, strings.ToUpper(strings.TrimPrefix(line, "# ")), "")
		case strings.HasPrefix(line, "## "):
			out = append(out, "", strings.ToUpper(strings.TrimPrefix(line, "## ")), strings.Repeat("=", len(strings.TrimPrefix(line, "## "))))
		case strings.HasPrefix(line, "### "):
			out = append(out, "", strings.TrimPrefix(line, "### "), strings.Repeat("-", len(strings.TrimPrefix(line, "### "))))
		case strings.HasPrefix(line, "|"):
			cells := strings.Split(strings.Trim(line, "| "), " | ")
			if i+1 < len(lines) && strings.HasPrefix(lines[i+1], "| ---") {
				continue // header row; the separator line below is skipped too
			}
			if strings.HasPrefix(line, "| ---") {
				continue
			}
			if len(cells) == 3 { // event table: name, seen, meaning
				seen := ""
				if strings.TrimSpace(cells[1]) == "yes" {
					seen = " (seen on a live Silo)"
				}
				out = append(out, "  "+cells[0]+seen+": "+cells[2])
			} else {
				out = append(out, "  "+strings.Join(cells, ": "))
			}
		default:
			out = append(out, line)
		}
	}
	text := strings.ReplaceAll(strings.Join(out, "\n"), "`", "")
	text = strings.ReplaceAll(text, "**", "")
	return strings.TrimSpace(text) + "\n"
}
