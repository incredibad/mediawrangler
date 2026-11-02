package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"
)

// resolver turns profile ids into names through Silo's admin API, when the
// admin has given the plugin an API key. Without one only ids are known.
type resolver struct {
	client *http.Client

	mu        sync.Mutex
	names     map[string]string
	fetchedAt time.Time
	problem   string // why the last lookup failed, "" when it worked
}

const (
	namesTTL      = 10 * time.Minute
	namesMinRetry = 30 * time.Second
)

func newResolver(client *http.Client) *resolver {
	return &resolver{client: client, names: map[string]string{}}
}

// name returns the profile's display name, or "" when it is not known.
func (r *resolver) name(ctx context.Context, s Settings, id string) string {
	if s.SiloURL == "" || s.SiloAPIKey == "" || id == "" {
		return ""
	}
	r.mu.Lock()
	name, known := r.names[id]
	age := time.Since(r.fetchedAt)
	r.mu.Unlock()
	if known && age < namesTTL {
		return name
	}
	if !known && age < namesMinRetry {
		return ""
	}
	if err := r.refresh(ctx, s); err != nil {
		r.mu.Lock()
		r.problem = err.Error()
		r.mu.Unlock()
		return name
	}
	r.mu.Lock()
	r.problem = ""
	r.mu.Unlock()
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.names[id]
}

// allowed reports whether a profile passes the admin's profile list. Entries
// match a profile's id exactly, or its name without regard to case.
func (r *resolver) allowed(ctx context.Context, s Settings, id string) bool {
	if len(s.Profiles) == 0 {
		return true
	}
	name := ""
	for _, want := range s.Profiles {
		if want == id {
			return true
		}
	}
	name = r.name(ctx, s, id)
	for _, want := range s.Profiles {
		if name != "" && strings.EqualFold(want, name) {
			return true
		}
	}
	return false
}

type listResponse[T any] struct {
	Items []T `json:"items"`
}

func (r *resolver) get(ctx context.Context, s Settings, path string, out any) error {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.SiloURL+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+s.SiloAPIKey)
	resp, err := r.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("silo answered %d for %s", resp.StatusCode, path)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// refresh reads every account's profiles. It only reads.
func (r *resolver) refresh(ctx context.Context, s Settings) error {
	// Silo sends account ids as text ("1"); accept numbers too.
	var users listResponse[struct {
		ID any `json:"id"`
	}]
	r.mu.Lock()
	r.fetchedAt = time.Now() // also throttles failures
	r.mu.Unlock()
	if err := r.get(ctx, s, "/api/v2/admin/users", &users); err != nil {
		return err
	}
	names := map[string]string{}
	for _, user := range users.Items {
		var profiles listResponse[struct {
			ID   string `json:"id"`
			Name string `json:"name"`
		}]
		if err := r.get(ctx, s, fmt.Sprintf("/api/v2/admin/users/%v/profiles", user.ID), &profiles); err != nil {
			return err
		}
		for _, p := range profiles.Items {
			names[p.ID] = p.Name
		}
	}
	r.mu.Lock()
	r.names = names
	r.mu.Unlock()
	return nil
}

// lookupProblem explains why a profile name could not be matched, or "" when
// nothing is wrong with the lookup itself.
func (r *resolver) lookupProblem(s Settings) string {
	if s.SiloURL == "" || s.SiloAPIKey == "" {
		return "the profile list has names but no Silo URL and API key are set, so only profile ids can match"
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.problem != "" {
		return "looking up profile names in Silo failed: " + r.problem
	}
	return ""
}
