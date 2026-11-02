package main

import "encoding/json"

const (
	pluginID    = "incredibad.event-webhooks"
	eventCapID  = "webhooks"
	displayName = "Event Webhooks"
	docsKey     = "docs"
)

// buildManifest returns the plugin's manifest as JSON. It is built from the
// event catalogue so the subscriptions, the switches and the docs cannot
// drift apart. The checksum placeholder is filled in by the SDK.
//
// Silo draws the settings form itself, from this description. It has plain
// text only (no markdown, no columns), and a multi-select is a wrapped row of
// buttons, so each event is an on/off switch with its explanation beside it,
// in collapsible sections. The reference documentation sits in a second
// settings panel under the form.
func buildManifest(version string) ([]byte, error) {
	props := map[string]any{
		"url":          map[string]any{"type": "string"},
		"secret":       map[string]any{"type": "string"},
		"profiles":     map[string]any{"type": "string"},
		"silo_url":     map[string]any{"type": "string"},
		"silo_api_key": map[string]any{"type": "string"},
	}
	fields := []map[string]any{
		{
			"key": "url", "label": "Webhook URL", "control": "ADMIN_FORM_CONTROL_TEXT",
			"description": "Every event you switch on is sent here as an HTTP POST with a JSON body. Use an address the Silo server can reach.",
			"placeholder": "https://example.com/silo-events",
			"validation":  map[string]any{"pattern": "^https?://.+"},
		},
		{
			"key": "secret", "label": "Secret", "control": "ADMIN_FORM_CONTROL_PASSWORD", "secret": true,
			"description": "Optional. When set, each request carries an X-Silo-Signature header (HMAC-SHA256) your receiver can verify. Sent in a header, never in the URL.",
		},
		{
			"key": "profiles", "label": "Only these profiles", "control": "ADMIN_FORM_CONTROL_TEXTAREA", "rows": 3,
			"description": "Optional. Profile ids or names, separated by commas or new lines. Empty means every profile. Only events about a profile are filtered. Names need the Silo URL and API key below.",
			"placeholder": "3f1c2b7a-5d4e-4a9b-8c6f-0e2d1a9b7c55",
		},
		{
			"key": "silo_url", "label": "Silo URL", "control": "ADMIN_FORM_CONTROL_TEXT",
			"description": "Optional. This server's own address, for example http://localhost:8090. With an API key it lets the plugin look up profile names.",
			"placeholder": "http://localhost:8090",
		},
		{
			"key": "silo_api_key", "label": "Silo API key", "control": "ADMIN_FORM_CONTROL_PASSWORD", "secret": true,
			"description": "Optional. Used only to read the list of accounts and profiles.",
		},
	}
	sections := []map[string]any{
		{"key": "destination", "title": "Destination", "field_keys": []string{"url", "secret"}},
	}
	for _, g := range groupOrder {
		var keys []string
		for _, e := range eventsIn(g) {
			key := switchKey(e.Name)
			keys = append(keys, key)
			props[key] = map[string]any{"type": "boolean"}
			fields = append(fields, map[string]any{
				"key": key, "label": e.Name, "control": "ADMIN_FORM_CONTROL_SWITCH",
				"description":   e.Description,
				"default_value": contains(defaultEvents, e.Name),
			})
		}
		sections = append(sections, map[string]any{
			"key": "events_" + g, "title": "Events: " + g, "field_keys": keys,
			"collapsible": true, "collapsed_default": g != GroupUsers,
		})
	}
	sections = append(sections,
		map[string]any{"key": "profiles", "title": "Profiles", "field_keys": []string{"profiles"}},
		map[string]any{
			"key": "names", "title": "Profile names (optional)", "collapsible": true, "collapsed_default": true,
			"field_keys": []string{"silo_url", "silo_api_key"},
		},
	)

	schemaJSON, err := json.Marshal(map[string]any{"type": "object", "additionalProperties": false, "properties": props})
	if err != nil {
		return nil, err
	}
	docsSchemaJSON, err := json.Marshal(map[string]any{
		"type": "object", "additionalProperties": false,
		"properties": map[string]any{"reference": map[string]any{"type": "string"}},
	})
	if err != nil {
		return nil, err
	}

	m := map[string]any{
		"plugin_id":        pluginID,
		"version":          version,
		"checksum":         "__CHECKSUM__",
		"silo_api_version": "v1",
		"supported_platforms": []map[string]string{
			{"os": "linux", "arch": "amd64"},
			{"os": "linux", "arch": "arm64"},
		},
		"capabilities": []map[string]any{
			{
				"type":          "event_consumer.v1",
				"id":            eventCapID,
				"display_name":  displayName,
				"description":   "Sends the Silo events you switch on to a web address as signed JSON.",
				"subscriptions": eventNames(),
			},
		},
		"global_config_schema": []map[string]any{
			{
				"key":         configKey,
				"title":       "Webhook",
				"description": "Where to send events, and which ones.",
				"json_schema": string(schemaJSON),
				"required":    false,
				"admin_form":  map[string]any{"submit_label": "Save", "fields": fields, "sections": sections},
			},
			{
				// Silo's form has no note or markdown block, so the documentation is
				// a read-only-in-practice text box in its own panel. Nothing reads it back.
				"key":         docsKey,
				"title":       "Documentation",
				"description": "How the webhooks look and behave. This text is for reading: changing it does nothing.",
				"json_schema": string(docsSchemaJSON),
				"required":    false,
				"admin_form": map[string]any{
					"submit_label": "Save",
					"fields": []map[string]any{{
						"key": "reference", "label": "Reference", "control": "ADMIN_FORM_CONTROL_TEXTAREA",
						"rows": 30, "multiline": true, "default_value": referenceText(),
						"description": "The same text is in docs/REFERENCE.md in the plugin's source.",
					}},
				},
			},
		},
		"category": "Integrations",
		"presentation": map[string]any{
			"display_name":         displayName,
			"summary":              "Send the Silo events you choose to a web address as signed JSON webhooks.",
			"description_markdown": presentationDescription(),
			"setup_markdown":       presentationSetup(),
			"license_spdx":         "MIT",
		},
	}
	return json.MarshalIndent(m, "", "  ")
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}
