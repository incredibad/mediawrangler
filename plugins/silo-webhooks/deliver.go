package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"time"

	pluginv1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
)

// deliveryTimeout stays well inside Silo's 10-second limit for an event
// handler, so a slow receiver never makes Silo give up on the plugin.
const deliveryTimeout = 5 * time.Second

// Profile identifies the profile an event is about, when it names one.
type Profile struct {
	ID   string `json:"id"`
	Name string `json:"name,omitempty"`
}

// Body is the JSON every webhook carries. See docs/REFERENCE.md.
type Body struct {
	Event      string         `json:"event"`
	EventID    string         `json:"event_id"`
	DeliveryID string         `json:"delivery_id"`
	Timestamp  string         `json:"timestamp"`
	Version    int            `json:"version"`
	Profile    *Profile       `json:"profile,omitempty"`
	Data       map[string]any `json:"data"`
}

// sign returns the X-Silo-Signature value, in the same form Silo uses for its
// own webhooks: "t=<epoch>,v1=<hex hmac-sha256 of "<epoch>.<body>">".
func sign(secret string, timestamp int64, body []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(strconv.AppendInt(nil, timestamp, 10))
	mac.Write([]byte{'.'})
	mac.Write(body)
	return fmt.Sprintf("t=%d,v1=%s", timestamp, hex.EncodeToString(mac.Sum(nil)))
}

func randomID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// warningEvent is not a Silo event: the plugin sends it to say why events are
// being held back (see docs/REFERENCE.md), at most every warnEvery.
const (
	warningEvent = "plugin.warning"
	warnEvery    = 10 * time.Minute
)

type consumer struct {
	pluginv1.UnimplementedEventConsumerServer
	lastWarn time.Time
	cfg      *store
	res      *resolver
	client   *http.Client
	version  string
	now      func() time.Time
	logError func(format string, args ...any)
}

func newConsumer(cfg *store, version string) *consumer {
	client := &http.Client{Timeout: deliveryTimeout}
	return &consumer{
		cfg:     cfg,
		res:     newResolver(client),
		client:  client,
		version: version,
		now:     time.Now,
		logError: func(format string, args ...any) {
			fmt.Fprintf(os.Stderr, "event-webhooks: "+format+"\n", args...)
		},
	}
}

// HandleEvent forwards one Silo event, when the admin has ticked it.
//
// Returning an error makes Silo retry the same event (same event_id) after
// 10 s, 30 s, 1 min, 3 min and 15 min. Only failures that a retry can fix are
// returned: network errors, timeouts, 5xx, 408 and 429. Any other answer
// (a 4xx such as 401 or 404) is logged and dropped, because repeating it would
// only repeat the failure.
func (c *consumer) HandleEvent(ctx context.Context, req *pluginv1.HandleEventRequest) (*pluginv1.HandleEventResponse, error) {
	s := c.cfg.get()
	if s.URL == "" || !s.Events[req.GetEventName()] {
		return &pluginv1.HandleEventResponse{}, nil
	}
	data := map[string]any{}
	if req.GetPayload() != nil {
		data = req.GetPayload().AsMap()
	}
	profileID, _ := data["profile_id"].(string)
	if profileID != "" && !c.res.allowed(ctx, s, profileID) {
		return &pluginv1.HandleEventResponse{}, c.warnIfBroken(ctx, s, req, profileID)
	}
	body := Body{
		Event:      req.GetEventName(),
		EventID:    req.GetEventId(),
		DeliveryID: randomID(),
		Timestamp:  c.now().UTC().Format(time.RFC3339),
		Version:    1,
		Data:       data,
	}
	if profileID != "" {
		body.Profile = &Profile{ID: profileID, Name: c.res.name(ctx, s, profileID)}
	}
	return &pluginv1.HandleEventResponse{}, c.post(ctx, s, body)
}

func (c *consumer) post(ctx context.Context, s Settings, body Body) error {
	raw, err := json.Marshal(body)
	if err != nil {
		c.logError("could not encode %s: %v", body.Event, err)
		return nil // retrying cannot fix this
	}
	ctx, cancel := context.WithTimeout(ctx, deliveryTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, s.URL, bytes.NewReader(raw))
	if err != nil {
		c.logError("bad webhook URL: %v", err)
		return nil
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", "silo-event-webhooks/"+c.version)
	req.Header.Set("X-Silo-Event", body.Event)
	req.Header.Set("X-Silo-Event-Id", body.EventID)
	req.Header.Set("X-Silo-Delivery", body.DeliveryID)
	if s.Secret != "" {
		req.Header.Set("X-Silo-Signature", sign(s.Secret, c.now().Unix(), raw))
	}
	resp, err := c.client.Do(req)
	if err != nil {
		return fmt.Errorf("webhook for %s failed: %w", body.Event, err)
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		return nil
	case resp.StatusCode >= 500, resp.StatusCode == http.StatusRequestTimeout, resp.StatusCode == http.StatusTooManyRequests:
		return fmt.Errorf("webhook for %s answered %d", body.Event, resp.StatusCode)
	default:
		c.logError("%s answered %d for %s; not retrying", req.URL.Host, resp.StatusCode, body.Event)
		return nil
	}
}

// warnIfBroken tells the receiver when a profile filter is holding events back
// because it cannot do its job (no way to look names up, or the lookup fails),
// so a misconfiguration is visible instead of silent. Filtering by id, or by a
// name that resolves, never warns.
func (c *consumer) warnIfBroken(ctx context.Context, s Settings, req *pluginv1.HandleEventRequest, profileID string) error {
	needsNames := false
	for _, want := range s.Profiles {
		if !looksLikeID(want) {
			needsNames = true
		}
	}
	problem := c.res.lookupProblem(s)
	if !needsNames || problem == "" || c.now().Sub(c.lastWarn) < warnEvery {
		return nil
	}
	c.lastWarn = c.now()
	c.logError("%s", problem)
	return c.post(ctx, s, Body{
		Event:      warningEvent,
		EventID:    "warning-" + randomID(),
		DeliveryID: randomID(),
		Timestamp:  c.now().UTC().Format(time.RFC3339),
		Version:    1,
		Data: map[string]any{
			"message":       problem,
			"held_back":     req.GetEventName(),
			"for_profile":   profileID,
			"profile_list":  s.Profiles,
			"repeats_every": warnEvery.String(),
		},
	})
}

// looksLikeID reports whether a profile entry is an id (a UUID) rather than a name.
func looksLikeID(entry string) bool {
	if len(entry) != 36 {
		return false
	}
	for i, r := range entry {
		switch {
		case i == 8 || i == 13 || i == 18 || i == 23:
			if r != '-' {
				return false
			}
		case !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f' || r >= 'A' && r <= 'F'):
			return false
		}
	}
	return true
}
