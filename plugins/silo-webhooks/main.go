// Event Webhooks: a Silo plugin that sends the Silo events you tick to one web
// address as signed JSON. See README.md and docs/REFERENCE.md.
package main

import (
	"fmt"
	"os"

	"github.com/Silo-Server/silo-plugin-sdk/pkg/pluginsdk/runtime"
)

// version is set at build time with -ldflags "-X main.version=...".
var version = "0.1.0-beta.8"

func main() {
	if len(os.Args) > 1 && os.Args[1] == "docs" {
		fmt.Print(referenceMarkdown())
		return
	}
	manifest, err := buildManifest(version)
	if err != nil {
		panic(err)
	}
	cfg := &store{}
	runtime.ServeManifestWithOptions(manifest, version, runtime.CapabilityServers{
		EventConsumer: newConsumer(cfg, version),
	}, runtime.WithConfigure(cfg.configure))
}
