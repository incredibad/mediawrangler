package main

import (
	"context"
	"fmt"
	"net/url"
	"strings"
	"sync"

	pluginv1 "github.com/Silo-Server/silo-plugin-sdk/pkg/pluginproto/silo/plugin/v1"
)

// configKey is the key of the one config entry the manifest declares.
const configKey = "webhook"

// Settings is what the admin form holds.
type Settings struct {
	URL        string
	Secret     string
	Events     map[string]bool
	Profiles   []string // profile ids or names; empty means every profile
	SiloURL    string
	SiloAPIKey string
}

// store holds the current settings. Silo calls Configure whenever they change.
type store struct {
	mu sync.RWMutex
	s  Settings
}

func (st *store) get() Settings {
	st.mu.RLock()
	defer st.mu.RUnlock()
	return st.s
}

// configure is the runtime.ConfigureFunc: parse, validate, keep.
func (st *store) configure(_ context.Context, entries []*pluginv1.ConfigEntry) error {
	for _, entry := range entries {
		if entry.GetKey() != configKey {
			continue
		}
		parsed, err := parseSettings(entry.GetValue().AsMap())
		if err != nil {
			return err
		}
		st.mu.Lock()
		st.s = parsed
		st.mu.Unlock()
	}
	return nil
}

func parseSettings(raw map[string]any) (Settings, error) {
	str := func(key string) string {
		v, _ := raw[key].(string)
		return strings.TrimSpace(v)
	}
	s := Settings{
		URL:        str("url"),
		Secret:     str("secret"),
		SiloURL:    strings.TrimRight(str("silo_url"), "/"),
		SiloAPIKey: str("silo_api_key"),
		Events:     map[string]bool{},
	}
	if s.URL != "" {
		u, err := url.Parse(s.URL)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
			return Settings{}, fmt.Errorf("webhook URL must be an http:// or https:// address")
		}
	}
	for _, name := range eventNames() {
		if on, _ := raw[switchKey(name)].(bool); on {
			s.Events[name] = true
		}
	}
	s.Profiles = splitList(str("profiles"))
	return s, nil
}

// splitList splits on commas, semicolons and newlines.
func splitList(text string) []string {
	var out []string
	for _, part := range strings.FieldsFunc(text, func(r rune) bool { return r == ',' || r == ';' || r == '\n' || r == '\r' }) {
		if part = strings.TrimSpace(part); part != "" {
			out = append(out, part)
		}
	}
	return out
}
