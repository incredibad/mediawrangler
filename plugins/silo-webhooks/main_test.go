package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	pluginv1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
	"github.com/Silo-Server/silo-plugin-sdk/pkg/pluginsdk/manifest"
	"google.golang.org/protobuf/types/known/structpb"
)

func TestManifestIsValidAndSubscribesToEveryEvent(t *testing.T) {
	raw, err := buildManifest("9.9.9")
	if err != nil {
		t.Fatal(err)
	}
	m, err := manifest.LoadWithChecksum(raw, "9.9.9")
	if err != nil {
		t.Fatalf("SDK rejected the manifest: %v", err)
	}
	var caps []*pluginv1.CapabilityDescriptor = m.GetCapabilities()
	if len(caps) != 1 || caps[0].GetType() != "event_consumer.v1" {
		t.Fatalf("capabilities = %v", caps)
	}
	if got, want := len(caps[0].GetSubscriptions()), len(eventNames()); got != want {
		t.Fatalf("subscriptions = %d, want %d", got, want)
	}
	schemas := m.GetGlobalConfigSchema()
	if len(schemas) != 2 || schemas[0].GetKey() != configKey || schemas[1].GetKey() != docsKey {
		t.Fatalf("global_config_schema = %v", schemas)
	}
	// One switch per event, in alphabetical order inside each section.
	form := schemas[0].GetAdminForm()
	switches := map[string]string{}
	for _, f := range form.GetFields() {
		if f.GetControl() == pluginv1.AdminFormControl_ADMIN_FORM_CONTROL_SWITCH {
			switches[f.GetKey()] = f.GetLabel()
		}
	}
	if len(switches) != len(eventNames()) {
		t.Fatalf("switches = %d, events = %d", len(switches), len(eventNames()))
	}
	for _, sec := range form.GetSections() {
		var labels []string
		for _, k := range sec.GetFieldKeys() {
			if l, ok := switches[k]; ok {
				labels = append(labels, l)
			}
		}
		if !sort.StringsAreSorted(labels) {
			t.Fatalf("section %q is not alphabetical: %v", sec.GetKey(), labels)
		}
	}
	var titles []string
	for _, sec := range form.GetSections() {
		if strings.HasPrefix(sec.GetKey(), "events_") {
			titles = append(titles, sec.GetTitle())
		}
	}
	if !sort.StringsAreSorted(titles) {
		t.Fatalf("event sections are not alphabetical: %v", titles)
	}
	seen := map[string]bool{}
	for _, name := range eventNames() {
		if seen[name] {
			t.Fatalf("event %q listed twice", name)
		}
		seen[name] = true
	}
}

func TestDocumentationPanelIsPlainReadableText(t *testing.T) {
	text := referenceText()
	for _, want := range []string{"X-Silo-Signature", "user_state.changed (seen on a live Silo)", "scan.completed", "Delivery and retries", "EVENTS"} {
		if !strings.Contains(text, want) {
			t.Errorf("documentation text lacks %q", want)
		}
	}
	for _, bad := range []string{"`", "**", "| ---", "~~~", "<!--"} {
		if strings.Contains(text, bad) {
			t.Errorf("documentation text still has markdown %q", bad)
		}
	}
	if strings.Index(text, "Background jobs") > strings.Index(text, "Watching and users") {
		t.Error("event groups are not alphabetical")
	}
}

func TestSwitchKeysAreUnique(t *testing.T) {
	seen := map[string]string{}
	for _, name := range eventNames() {
		k := switchKey(name)
		if other, dup := seen[k]; dup {
			t.Fatalf("%q and %q share the key %q", name, other, k)
		}
		seen[k] = name
	}
}

func TestReferenceDocIsUpToDate(t *testing.T) {
	onDisk, err := os.ReadFile("docs/REFERENCE.md")
	if err != nil {
		t.Skip("docs/REFERENCE.md not generated yet")
	}
	if string(onDisk) != referenceMarkdown() {
		t.Fatal("docs/REFERENCE.md is stale: run ./build.sh")
	}
}

func TestParseSettings(t *testing.T) {
	s, err := parseSettings(map[string]any{
		"url":                           "https://example.com/hook",
		"secret":                        " s3cret ",
		switchKey("user_state.changed"): true,
		switchKey("scan.completed"):     true,
		switchKey("job.failed"):         false,
		"evt_not__a__real__event":       true,
		"profiles":                      "alex, Jordan\nabc-123;",
		"silo_url":                      "http://localhost:8090/",
	})
	if err != nil {
		t.Fatal(err)
	}
	if s.Secret != "s3cret" || s.SiloURL != "http://localhost:8090" {
		t.Fatalf("settings = %+v", s)
	}
	if !s.Events["user_state.changed"] || !s.Events["scan.completed"] || s.Events["job.failed"] || len(s.Events) != 2 {
		t.Fatalf("events = %v", s.Events)
	}
	if strings.Join(s.Profiles, "|") != "alex|Jordan|abc-123" {
		t.Fatalf("profiles = %v", s.Profiles)
	}
	for _, bad := range []string{"ftp://x/y", "not a url", "http://"} {
		if _, err := parseSettings(map[string]any{"url": bad}); err == nil {
			t.Fatalf("%q accepted", bad)
		}
	}
	if _, err := parseSettings(map[string]any{}); err != nil {
		t.Fatalf("an empty config must be allowed: %v", err)
	}
}

type received struct {
	header http.Header
	body   []byte
}

// receiver is a test webhook endpoint answering with a chosen status.
func receiver(t *testing.T, status int) (*httptest.Server, func() []received) {
	var mu sync.Mutex
	var got []received
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		mu.Lock()
		got = append(got, received{r.Header.Clone(), b})
		mu.Unlock()
		w.WriteHeader(status)
	}))
	t.Cleanup(srv.Close)
	return srv, func() []received { mu.Lock(); defer mu.Unlock(); return append([]received(nil), got...) }
}

func newTestConsumer(s Settings) *consumer {
	st := &store{s: s}
	c := newConsumer(st, "test")
	c.now = func() time.Time { return time.Date(2026, 10, 27, 1, 2, 3, 0, time.UTC) }
	c.logError = func(string, ...any) {}
	return c
}

func event(t *testing.T, name, id string, payload map[string]any) *pluginv1.HandleEventRequest {
	p, err := structpb.NewStruct(payload)
	if err != nil {
		t.Fatal(err)
	}
	return &pluginv1.HandleEventRequest{EventName: name, EventId: id, Payload: p}
}

func TestForwardsTickedEventsOnlyWithSignatureAndHeaders(t *testing.T) {
	srv, got := receiver(t, 204)
	c := newTestConsumer(Settings{URL: srv.URL, Secret: "hunter2", Events: map[string]bool{"user_state.changed": true}})
	ctx := context.Background()

	if _, err := c.HandleEvent(ctx, event(t, "scan.completed", "e0", map[string]any{"x": 1})); err != nil || len(got()) != 0 {
		t.Fatalf("an unticked event was sent: %v %v", err, got())
	}
	payload := map[string]any{"change": "watched", "played": false, "content_id": "season-tvdb-1-2", "profile_id": "p1"}
	if _, err := c.HandleEvent(ctx, event(t, "user_state.changed", "evt-1", payload)); err != nil {
		t.Fatal(err)
	}
	reqs := got()
	if len(reqs) != 1 {
		t.Fatalf("requests = %d", len(reqs))
	}
	h := reqs[0].header
	if h.Get("X-Silo-Event") != "user_state.changed" || h.Get("X-Silo-Event-Id") != "evt-1" || h.Get("X-Silo-Delivery") == "" {
		t.Fatalf("headers = %v", h)
	}
	if want := sign("hunter2", c.now().Unix(), reqs[0].body); h.Get("X-Silo-Signature") != want {
		t.Fatalf("signature %q, want %q", h.Get("X-Silo-Signature"), want)
	}
	var body Body
	if err := json.Unmarshal(reqs[0].body, &body); err != nil {
		t.Fatal(err)
	}
	if body.Event != "user_state.changed" || body.EventID != "evt-1" || body.Version != 1 || body.Timestamp != "2026-10-27T01:02:03Z" {
		t.Fatalf("body = %+v", body)
	}
	if body.Profile == nil || body.Profile.ID != "p1" || body.Data["content_id"] != "season-tvdb-1-2" || body.Data["played"] != false {
		t.Fatalf("body = %+v", body)
	}
}

func TestSignatureMatchesSilosOwnScheme(t *testing.T) {
	// Known answer: HMAC-SHA256 keyed "secret" over "1700000000.{}".
	if got := sign("secret", 1700000000, []byte("{}")); !strings.HasPrefix(got, "t=1700000000,v1=") || len(got) != len("t=1700000000,v1=")+64 {
		t.Fatalf("signature = %q", got)
	}
	if sign("a", 1, []byte("x")) == sign("b", 1, []byte("x")) || sign("a", 1, []byte("x")) == sign("a", 2, []byte("x")) {
		t.Fatal("signature ignores the secret or the timestamp")
	}
}

func TestUnsignedWhenNoSecret(t *testing.T) {
	srv, got := receiver(t, 200)
	c := newTestConsumer(Settings{URL: srv.URL, Events: map[string]bool{"job.failed": true}})
	if _, err := c.HandleEvent(context.Background(), event(t, "job.failed", "j1", map[string]any{"id": "7"})); err != nil {
		t.Fatal(err)
	}
	if h := got()[0].header; h.Get("X-Silo-Signature") != "" {
		t.Fatalf("signature sent without a secret: %v", h)
	}
}

func TestRetryDecisions(t *testing.T) {
	cases := []struct {
		status    int
		wantRetry bool
	}{{200, false}, {204, false}, {400, false}, {401, false}, {404, false}, {408, true}, {429, true}, {500, true}, {503, true}}
	for _, tc := range cases {
		srv, _ := receiver(t, tc.status)
		c := newTestConsumer(Settings{URL: srv.URL, Events: map[string]bool{"job.failed": true}})
		_, err := c.HandleEvent(context.Background(), event(t, "job.failed", "j", map[string]any{}))
		if (err != nil) != tc.wantRetry {
			t.Errorf("status %d: err = %v, want retry = %v", tc.status, err, tc.wantRetry)
		}
	}
	// An unreachable receiver is retried.
	c := newTestConsumer(Settings{URL: "http://127.0.0.1:1/", Events: map[string]bool{"job.failed": true}})
	if _, err := c.HandleEvent(context.Background(), event(t, "job.failed", "j", map[string]any{})); err == nil {
		t.Fatal("a network error must be retried")
	}
}

func TestNothingIsSentUntilConfigured(t *testing.T) {
	c := newTestConsumer(Settings{Events: map[string]bool{"job.failed": true}})
	if _, err := c.HandleEvent(context.Background(), event(t, "job.failed", "j", map[string]any{})); err != nil {
		t.Fatal(err)
	}
}

func TestProfileFilterByIDAndByName(t *testing.T) {
	var apiCalls int
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		apiCalls++
		if r.Header.Get("Authorization") != "Bearer key123" {
			w.WriteHeader(401)
			return
		}
		switch r.URL.Path {
		case "/api/v2/admin/users":
			_, _ = w.Write([]byte(`{"items":[{"id":"1"},{"id":"2"}]}`))
		case "/api/v2/admin/users/1/profiles":
			_, _ = w.Write([]byte(`{"items":[{"id":"p-alex","name":"alex"}]}`))
		case "/api/v2/admin/users/2/profiles":
			_, _ = w.Write([]byte(`{"items":[{"id":"p-ali","name":"Jordan"}]}`))
		default:
			w.WriteHeader(404)
		}
	}))
	defer api.Close()
	srv, got := receiver(t, 204)
	events := map[string]bool{"user_state.changed": true, "scan.completed": true}
	ctx := context.Background()
	send := func(c *consumer, name, profile string) {
		payload := map[string]any{"change": "watched"}
		if profile != "" {
			payload["profile_id"] = profile
		}
		if _, err := c.HandleEvent(ctx, event(t, name, "e", payload)); err != nil {
			t.Fatal(err)
		}
	}

	// By id: no Silo API needed.
	const idTim, idAli = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
	c := newTestConsumer(Settings{URL: srv.URL, Events: events, Profiles: []string{idTim}})
	send(c, "user_state.changed", idTim)
	send(c, "user_state.changed", idAli)
	send(c, "scan.completed", "") // not about a profile: always sent
	if n := len(got()); n != 2 {
		t.Fatalf("by id: %d requests, want 2", n)
	}

	// By name, with the Silo API to resolve it; the webhook then carries the name.
	c = newTestConsumer(Settings{URL: srv.URL, Events: events, Profiles: []string{"jORDAN"}, SiloURL: api.URL, SiloAPIKey: "key123"})
	before := len(got())
	send(c, "user_state.changed", "p-alex")
	send(c, "user_state.changed", "p-ali")
	reqs := got()[before:]
	if len(reqs) != 1 {
		t.Fatalf("by name: %d requests, want 1", len(reqs))
	}
	var body Body
	_ = json.Unmarshal(reqs[0].body, &body)
	if body.Profile == nil || body.Profile.Name != "Jordan" {
		t.Fatalf("profile = %+v", body.Profile)
	}
	if apiCalls == 0 || apiCalls > 6 {
		t.Fatalf("silo API calls = %d (the list should be cached)", apiCalls)
	}

	// Names without an API key cannot match.
	c = newTestConsumer(Settings{URL: srv.URL, Events: events, Profiles: []string{"alex"}})
	before = len(got())
	send(c, "user_state.changed", "p-alex")
	after := got()[before:]
	var warn Body
	if len(after) != 1 {
		t.Fatalf("a name without a Silo API key produced %d requests, want one warning", len(after))
	}
	_ = json.Unmarshal(after[0].body, &warn)
	if warn.Event != warningEvent {
		t.Fatalf("a name matched without a Silo API key: %s", warn.Event)
	}
}

func TestWarnsWhenProfileNamesCannotBeLookedUp(t *testing.T) {
	deny := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(403) }))
	defer deny.Close()
	srv, got := receiver(t, 204)
	events := map[string]bool{"user_state.changed": true}
	ctx := context.Background()
	send := func(c *consumer, profile string) {
		if _, err := c.HandleEvent(ctx, event(t, "user_state.changed", "e", map[string]any{"profile_id": profile})); err != nil {
			t.Fatal(err)
		}
	}
	names := func() []string {
		var out []string
		for _, r := range got() {
			var b Body
			_ = json.Unmarshal(r.body, &b)
			out = append(out, b.Event)
		}
		return out
	}

	// A name with a key Silo refuses: the event is held back, and the receiver is told once.
	c := newTestConsumer(Settings{URL: srv.URL, Events: events, Profiles: []string{"alex"}, SiloURL: deny.URL, SiloAPIKey: "bad"})
	send(c, "p-alex")
	send(c, "p-alex")
	if got := names(); len(got) != 1 || got[0] != warningEvent {
		t.Fatalf("requests = %v, want one %s", got, warningEvent)
	}
	var body Body
	_ = json.Unmarshal(got()[0].body, &body)
	if msg, _ := body.Data["message"].(string); !strings.Contains(msg, "403") || body.Data["held_back"] != "user_state.changed" {
		t.Fatalf("warning data = %v", body.Data)
	}
	// ...and again after the quiet period.
	c.now = func() time.Time { return time.Date(2026, 10, 27, 1, 20, 0, 0, time.UTC) }
	send(c, "p-alex")
	if len(got()) != 2 {
		t.Fatalf("requests = %d, want a second warning after 10 minutes", len(got()))
	}

	// Names but no Silo URL and key at all: the same warning, naming the cause.
	srv2, got2 := receiver(t, 204)
	c = newTestConsumer(Settings{URL: srv2.URL, Events: events, Profiles: []string{"alex"}})
	send(c, "p-alex")
	var w Body
	_ = json.Unmarshal(got2()[0].body, &w)
	if msg, _ := w.Data["message"].(string); !strings.Contains(msg, "no Silo URL and API key") {
		t.Fatalf("message = %q", msg)
	}

	// Filtering by id needs no lookup, so a non-matching profile is dropped silently.
	srv3, got3 := receiver(t, 204)
	c = newTestConsumer(Settings{URL: srv3.URL, Events: events, Profiles: []string{"3f1c2b7a-5d4e-4a9b-8c6f-0e2d1a9b7c55"}})
	send(c, "someone-else")
	if len(got3()) != 0 {
		t.Fatalf("an id filter produced %d requests", len(got3()))
	}
}

func TestAccountIDsMayBeNumbersOrText(t *testing.T) {
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v2/admin/users":
			_, _ = w.Write([]byte(`{"items":[{"id":7}]}`)) // a number this time
		case "/api/v2/admin/users/7/profiles":
			_, _ = w.Write([]byte(`{"items":[{"id":"p7","name":"seven"}]}`))
		default:
			w.WriteHeader(404)
		}
	}))
	defer api.Close()
	res := newResolver(api.Client())
	if got := res.name(context.Background(), Settings{SiloURL: api.URL, SiloAPIKey: "k"}, "p7"); got != "seven" {
		t.Fatalf("name = %q", got)
	}
}

func TestLooksLikeID(t *testing.T) {
	if !looksLikeID("3f1c2b7a-5d4e-4a9b-8c6f-0e2d1a9b7c55") || looksLikeID("alex") || looksLikeID("3f1c2b7a-5d4e-4a9b-8c6f-0e2d1a9b7c5z") {
		t.Fatal("looksLikeID is wrong")
	}
}
