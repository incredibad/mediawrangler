// Spike plugin: a watch_sync_provider that only reports what Silo sends it.
//
// Every event Silo hands to ApplyEvents is posted as JSON to a listener on the
// LAN (set at build time with -ldflags "-X main.listener=http://host:port"),
// and acknowledged as applied. It never reads remote state and never talks to
// anything else.
package main

import (
	"bytes"
	"context"
	_ "embed"
	"fmt"
	"net/http"
	"os"
	"time"

	pluginv1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
	"github.com/Silo-Server/silo-plugin-sdk/pkg/pluginsdk/runtime"
	"google.golang.org/protobuf/encoding/protojson"
)

//go:embed manifest.json
var manifestJSON []byte

var (
	version  = "0.0.2"
	listener = "" // set with -ldflags
)

type spy struct {
	pluginv1.UnimplementedWatchSyncProviderServer
	client *http.Client
}

func (s *spy) account() *pluginv1.WatchSyncAccount {
	return &pluginv1.WatchSyncAccount{ExternalSubject: "mw-spy", Username: "mw-spy", DisplayName: "MediaWrangler spy"}
}

func (s *spy) ExchangeAPIKey(_ context.Context, req *pluginv1.WatchSyncExchangeAPIKeyRequest) (*pluginv1.WatchSyncCredentialResponse, error) {
	return &pluginv1.WatchSyncCredentialResponse{
		Credentials: &pluginv1.WatchSyncCredentials{AccessToken: "spy", TokenType: "api_key"},
		Account:     s.account(),
	}, nil
}

func (s *spy) GetAccount(context.Context, *pluginv1.WatchSyncGetAccountRequest) (*pluginv1.WatchSyncGetAccountResponse, error) {
	return &pluginv1.WatchSyncGetAccountResponse{Account: s.account()}, nil
}

func (s *spy) RefreshCredentials(context.Context, *pluginv1.WatchSyncRefreshCredentialsRequest) (*pluginv1.WatchSyncCredentialResponse, error) {
	return &pluginv1.WatchSyncCredentialResponse{
		Credentials: &pluginv1.WatchSyncCredentials{AccessToken: "spy", TokenType: "api_key"},
		Account:     s.account(),
	}, nil
}

func (s *spy) ListRemoteState(context.Context, *pluginv1.WatchSyncListRemoteStateRequest) (*pluginv1.WatchSyncListRemoteStateResponse, error) {
	return &pluginv1.WatchSyncListRemoteStateResponse{CompleteSnapshot: true}, nil
}

func (s *spy) ApplyEvents(ctx context.Context, req *pluginv1.WatchSyncApplyEventsRequest) (*pluginv1.WatchSyncApplyEventsResponse, error) {
	results := make([]*pluginv1.WatchSyncApplyResult, 0, len(req.GetEvents()))
	for _, ev := range req.GetEvents() {
		body, err := protojson.Marshal(ev)
		if err != nil {
			body = []byte(fmt.Sprintf(`{"marshal_error":%q}`, err.Error()))
		}
		s.post(ctx, body)
		results = append(results, &pluginv1.WatchSyncApplyResult{
			EventId: ev.GetEventId(),
			Status:  pluginv1.WatchSyncApplyStatus_WATCH_SYNC_APPLY_STATUS_APPLIED,
		})
	}
	return &pluginv1.WatchSyncApplyEventsResponse{Results: results}, nil
}

// consumer logs the name and payload of every Silo event it is subscribed to.
type consumer struct {
	pluginv1.UnimplementedEventConsumerServer
	spy *spy
}

func (c *consumer) HandleEvent(ctx context.Context, req *pluginv1.HandleEventRequest) (*pluginv1.HandleEventResponse, error) {
	payload, err := protojson.Marshal(req.GetPayload())
	if err != nil {
		payload = []byte(fmt.Sprintf("%q", err.Error()))
	}
	body := fmt.Sprintf(`{"kind":"event_consumer","event_name":%q,"event_id":%q,"payload":%s}`,
		req.GetEventName(), req.GetEventId(), payload)
	c.spy.post(ctx, []byte(body))
	return &pluginv1.HandleEventResponse{}, nil
}

// post is best effort: a missing listener must never make Silo retry or fail.
func (s *spy) post(ctx context.Context, body []byte) {
	if listener == "" {
		fmt.Fprintf(os.Stderr, "mw-spy event: %s\n", body)
		return
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, listener, bytes.NewReader(body))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if resp, err := s.client.Do(req); err == nil {
		resp.Body.Close()
	}
}

func main() {
	s := &spy{client: &http.Client{Timeout: 3 * time.Second}}
	runtime.ServeManifest(manifestJSON, version, runtime.CapabilityServers{
		WatchSyncProvider: s,
		EventConsumer:     &consumer{spy: s},
	})
}
