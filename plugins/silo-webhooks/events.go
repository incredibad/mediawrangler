package main

import (
	"sort"
	"strings"
)

// Event is one Silo event a plugin can subscribe to. The list is the single
// source for the manifest's subscriptions, the checkboxes in Silo's admin
// form and docs/EVENTS.md (run `plugin docs`).
//
// Silo matches subscriptions by exact event name (no wildcards), so every
// name the plugin might forward has to be listed here.
type Event struct {
	Name        string
	Group       string
	Description string
	// Verified is true for events seen arriving on a live Silo. The others
	// come from reading Silo's source and may differ by version.
	Verified bool
}

// Groups, in the order the admin form and the docs show them.
const (
	GroupUsers   = "Watching and users"
	GroupLibrary = "Library and catalogue"
	GroupScans   = "Library scans"
	GroupJobs    = "Background jobs"
	GroupSystem  = "Server and administration"
)

// groupOrder is alphabetical, like the events inside each group.
var groupOrder = []string{GroupJobs, GroupLibrary, GroupScans, GroupSystem, GroupUsers}

var catalogue = []Event{
	// Watching and users
	{"user_state.changed", GroupUsers, "A profile's watched, progress, favourite, watchlist or not-interested state changed. A season or series marked watched or unwatched arrives as one event naming the season or series.", true},
	{"user_settings.changed", GroupUsers, "A profile or account setting changed.", false},
	{"user_disabled", GroupUsers, "An account was disabled.", false},
	{"user_deleted", GroupUsers, "An account was deleted.", false},
	{"user_sessions_revoked", GroupUsers, "An account's login sessions were revoked.", false},
	{"notification.created", GroupUsers, "A notification was created for a profile (new episode, next up, request update and so on).", false},
	{"download", GroupUsers, "Progress of a prepared download for a profile.", false},

	// Library and catalogue
	{"catalog.item.changed", GroupLibrary, "A movie, series or episode was added or changed in the catalogue. Data: change, content_id, library_id.", true},
	{"catalog.library.changed", GroupLibrary, "A library's contents changed after a scan. Data: library_id, reason, and counts (new, updated, missing, matched_files and so on).", true},
	{"library.changed", GroupLibrary, "A library changed (older name of catalog.library.changed, sent alongside it). Same data.", true},
	{"library.item_added", GroupLibrary, "An item was added to a library (older name). Data: library_id, new (how many).", true},
	{"metadata.updated", GroupLibrary, "An item's metadata was refreshed (older name). Data: content_id, library_id.", true},
	{"metadata_updated", GroupLibrary, "An item's metadata was refreshed.", false},
	{"markers_updated", GroupLibrary, "Intro, credits or other markers for an item changed. Data: file_id, source_id and marker_segments (kind, start_seconds, end_seconds).", true},
	{"marker_thumbnail_ready", GroupLibrary, "A marker thumbnail finished generating.", false},
	{"subtitle_timing_changed", GroupLibrary, "A subtitle's timing was changed.", false},
	{"subtitle_sync_updated", GroupLibrary, "Subtitle synchronisation was updated.", false},

	// Library scans
	{"scan.accepted", GroupScans, "A library scan was queued. Data: id, library_id, mode, path (for a partial scan), status, trigger (manual or autoscan).", true},
	{"scan.started", GroupScans, "A library scan started. Same data as scan.accepted, plus started_at.", true},
	{"scan.progress", GroupScans, "A running scan reported progress. Data as scan.accepted, plus result counts and the current scope. Very frequent: leave it off unless you need it.", true},
	{"scan.completed", GroupScans, "A library scan finished. The data has the scan id, library_id, mode, status, trigger, started and completed times, and a result with counts (new, updated, unchanged, missing, errors and so on).", true},
	{"scan.failed", GroupScans, "A library scan failed.", false},
	{"scan.cancelled", GroupScans, "A library scan was cancelled.", false},
	{"scan_complete", GroupScans, "A scan finished (server-wide notice, sent alongside scan.completed). Data: raw (the library id).", true},

	// Background jobs
	{"job.created", GroupJobs, "A background job was created.", false},
	{"job.progress", GroupJobs, "A background job reported progress. Frequent: leave it off unless you need it.", false},
	{"job.completed", GroupJobs, "A background job finished.", false},
	{"job.failed", GroupJobs, "A background job failed.", false},
	{"job.cancelled", GroupJobs, "A background job was cancelled.", false},

	// Server and administration
	{"playback_sessions_changed", GroupSystem, "The set of active playback sessions changed. Data: raw (the session id).", true},
	{"sessions.replaced", GroupSystem, "A playback session was replaced by another. Data is empty.", true},
	{"settings_changed", GroupSystem, "A server setting changed.", false},
	{"policy_changed", GroupSystem, "A server policy changed.", false},
	{"auth_providers_changed", GroupSystem, "Sign-in providers changed.", false},
	{"plugins_changed", GroupSystem, "A plugin was installed, removed, enabled, disabled or reconfigured. Data is empty.", true},
	{"marker_provider_config_changed", GroupSystem, "A marker provider's configuration changed.", false},
	{"node_pool_changed", GroupSystem, "The server's node list changed.", false},
	{"node_drain_changed", GroupSystem, "A node started or stopped draining.", false},
	{"node_unreachable", GroupSystem, "A node stopped responding.", false},
	{"download_storage.changed", GroupSystem, "Prepared-download storage changed.", false},
	{"admin_stats_invalidated", GroupSystem, "Cached administration statistics are stale.", false},
	{"audit_log_appended", GroupSystem, "A line was added to the audit log. Frequent on a busy server.", false},
	{"operational_log_appended", GroupSystem, "A line was added to the operational log. Very frequent: leave it off unless you need it.", false},
	{"events_notification", GroupSystem, "A server notification event.", false},
}

// defaultEvents are ticked when the plugin is first configured.
var defaultEvents = []string{"user_state.changed"}

// switchKey is the settings field for one event: "scan.completed" is
// "evt_scan__completed". Silo's admin form shows each as an on/off switch.
func switchKey(name string) string {
	return "evt_" + strings.ReplaceAll(name, ".", "__")
}

// eventsIn returns a group's events, alphabetically.
func eventsIn(group string) []Event {
	var out []Event
	for _, e := range catalogue {
		if e.Group == group {
			out = append(out, e)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// eventNames is every event the plugin can send.
func eventNames() []string {
	names := make([]string, len(catalogue))
	for i, e := range catalogue {
		names[i] = e.Name
	}
	return names
}
