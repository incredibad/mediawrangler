package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	pluginv1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
)

func TestApplyEventsForwardsAndAcknowledges(t *testing.T) {
	var got []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		got = append(got, string(b))
	}))
	defer srv.Close()
	listener = srv.URL
	s := &spy{client: srv.Client()}
	resp, err := s.ApplyEvents(context.Background(), &pluginv1.WatchSyncApplyEventsRequest{
		Events: []*pluginv1.WatchSyncEvent{
			{EventId: "e1", Operation: pluginv1.WatchSyncOperation_WATCH_SYNC_OPERATION_MARK_UNWATCHED},
			{EventId: "e2", Operation: pluginv1.WatchSyncOperation_WATCH_SYNC_OPERATION_MARK_WATCHED},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(resp.Results) != 2 || resp.Results[0].Status != pluginv1.WatchSyncApplyStatus_WATCH_SYNC_APPLY_STATUS_APPLIED {
		t.Fatalf("results = %v", resp.Results)
	}
	if len(got) != 2 || !strings.Contains(got[0], "MARK_UNWATCHED") || !strings.Contains(got[0], `"e1"`) {
		t.Fatalf("listener got %v", got)
	}
}

func TestUnreachableListenerNeverFailsAnEvent(t *testing.T) {
	listener = "http://127.0.0.1:1"
	s := &spy{client: &http.Client{Timeout: 200 * 1e6}}
	resp, err := s.ApplyEvents(context.Background(), &pluginv1.WatchSyncApplyEventsRequest{
		Events: []*pluginv1.WatchSyncEvent{{EventId: "e1"}},
	})
	if err != nil || resp.Results[0].Status != pluginv1.WatchSyncApplyStatus_WATCH_SYNC_APPLY_STATUS_APPLIED {
		t.Fatalf("resp=%v err=%v", resp, err)
	}
}
