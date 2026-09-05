# magi-plugins

Official plugin registry and plugin source repository for [Magi](https://github.com/asukaonly/magi).

This is a **companion repository** to the [Magi](https://github.com/asukaonly/magi) main repository. The main repo contains the core framework, desktop app, and the bundled `core-tools` plugin. All other optional plugins live here and are installed on demand via the in-app plugin marketplace.

## Connection runtime

All 22 plugins and three independently installed libraries target plugin protocol
2 and `magi-plugin-sdk` 0.2.0. A package is installed once; each configured
connection owns separate settings, credentials, state, and resources. There is
no legacy-plugin adapter or historical-data migration.

Existing native, filesystem, and network integrations declare `trusted_process`.
Activation requires the host's explicit trust decision. A worker process alone
does not sandbox local access; these packages do not claim restricted execution.

Plugins import only `magi_plugin_sdk` and their declared shared libraries.
`Plugin.configure(manifest=..., connection=..., context=...)` binds the instance.
Use `self.context.state_dir` for connection state, `resources_dir` for collected
content, and `credentials.get/set/delete` for secrets. Never infer a global
runtime directory or store credentials in ordinary plugin settings.

Pull sources return `SourceChangeBatch` with stable object identities, content
revisions, cursor progress and completion. Semantic `source_type` remains
independent of connection identity. Existing parsers still own source-specific
normalization. Tools and settings actions enter the host's typed operation
boundary; the host controls invocation identity, effects and retries.

`projection_sources` names the categories used by each package's projections.
The manifest also contains `settings_fields`, the complete schema the host
validates before loading plugin code.
Packages with onboarding also declare `activation_flow`; the host collects its
values before enabling or importing the package. Declarative `settings_actions`,
`settings_resources`, and `settings_ui_blocks` expose setup controls before enable.
Host-approved user actions and resource requests marked `requires_enabled = false`
may run in a scoped setup worker. Catalog export includes schemas, never resource
values or secrets. For multi-source packages, the
first registered source flow is primary (the knowledge tier for Local Documents
and Obsidian Vault); secondary tiers reuse that connection configuration.
Selectors grant no access by themselves: the host intersects them with the
connection's authorized data. Projection revision is the immutable package
version.

## How It Works

```
Magi App  →  Settings → Extensions → Marketplace
                          ↓
              Fetches registry.json from this repo
                          ↓
              Shows available plugins with install/update buttons
                          ↓
              Downloads the approved plugin package
                          ↓
              Verifies its complete package SHA-256
                          ↓
              Installs it to ~/.magi/plugins/<plugin_id>/
```

1. The Magi backend fetches `registry.json` from this repo's `main` branch.
2. The marketplace UI displays available plugins with metadata from the registry.
3. When the user clicks **Install**, the backend downloads the exact package
   identified by the registry and verifies the complete package SHA-256 before
   installation.
4. Rescanning discovers and validates the package manifest without importing its
   code. Installation alone creates no active source or channel.
5. The host renders the declared settings and activation flow for a connection.
   User-approved setup actions and resource requests can run in a scoped setup
   worker after the required trust decision; ordinary discovery runs no code.
6. The host validates settings and required credentials before enabling the
   connection. Its worker receives the explicit connection, private directories,
   and scoped credential port. Additional accounts use additional connections.

## Package coverage

All packages below declare protocol 2 and SDK 0.2.0. Libraries remain separate
installable dependencies. “Form” means a declarative initial activation flow;
actions and resources are listed only when callable before enable. Every package
also declares its complete settings schema (an empty list when none is needed).

| Package | Contributions | Projection sources | Before enable |
| --- | --- | --- | --- |
| [agent_history_core](plugins/agent_history_core/plugin.toml) | library | — | — |
| [apple-photos](plugins/apple-photos/plugin.toml) | source, tool | `photo_library_apple_photos` | Form, Resources |
| [browser_history_core](plugins/browser_history_core/plugin.toml) | library | — | — |
| [calendar](plugins/calendar_plugin/plugin.toml) | source | `calendar` | Form, Resources |
| [chatgpt-history](plugins/chatgpt-history/plugin.toml) | history_importer | — | — |
| [chrome-history](plugins/chrome-history/plugin.toml) | source | `chrome_history` | Form |
| [claude-code](plugins/claude-code/plugin.toml) | source | `claude_code_agent_history` | Form |
| [codex](plugins/codex/plugin.toml) | source | `codex_agent_history` | Form |
| [edge-history](plugins/edge-history/plugin.toml) | source | `edge_history` | Form |
| [firefox-history](plugins/firefox-history/plugin.toml) | source | `firefox_history` | Form |
| [git-activity](plugins/git_activity/plugin.toml) | source | `git_activity` | Form |
| [github-activity](plugins/github_activity/plugin.toml) | source | `github_activity` | Form, Actions |
| [local-documents](plugins/local-documents/plugin.toml) | source | `local_documents`, `local_documents_search` | Form |
| [local-photos](plugins/local-photos/plugin.toml) | source, tool | `photo_library_directory` | Form |
| [netease-music](plugins/netease_music/plugin.toml) | source | `netease_music` | Form |
| [obsidian-vault](plugins/obsidian-vault/plugin.toml) | source | `obsidian_vault`, `obsidian_vault_search` | Form |
| [photo_library_core](plugins/photo_library_core/plugin.toml) | library | — | — |
| [safari-history](plugins/safari-history/plugin.toml) | source | `safari_history` | Form, Resources |
| [screen-time](plugins/screen_time/plugin.toml) | source | `screen_time` | — |
| [screenshot_timeline](plugins/screenshot_timeline/plugin.toml) | source, tool | `screenshot_timeline` | Form, Actions, Resources |
| [steam-play-history](plugins/steam_play_history/plugin.toml) | source | `steam_play_history` | Form |
| [system-media](plugins/system_media/plugin.toml) | source | `system_media` | — |
| [telegram](plugins/telegram/plugin.toml) | channel | `telegram` | — |
| [terminal-history](plugins/terminal_history/plugin.toml) | source | `terminal_history` | Form |
| [weixin](plugins/weixin/plugin.toml) | channel | `weixin` | Actions, Resources |

`scripts/export-settings-fields.py` exports reviewed declarations;
`scripts/build-registry.py` copies those declarations and package identity into
`registry.json`. `version-history.json` records immutable published identities.

## Plugin Directory Layout

Every installable source has its own directory under `plugins/`. Shared code may
live in a hidden library package installed automatically through `depends_on`:

```
plugins/<plugin_name>/
├── plugin.toml          # Manifest (required)
├── plugin.py            # Entry point (required)
├── source.py            # Source implementation (if contribution_types includes "source")
├── normalizers.py       # Data normalizers
├── reader.py            # Data source readers
├── i18n/                # Localisation (optional)
│   ├── en.json
│   └── zh-CN.json
└── ...                  # Other plugin-specific modules
```

### plugin.toml

Header excerpt; each package must also export its complete settings and setup
schemas. The package table links to full manifests.

```toml
[plugin]
protocol_version = 2
min_sdk_version = "0.2.0"
execution_mode = "trusted_process"
projection_sources = ["chrome_history"]
id = "chrome-history"
name = "Chrome History"
version = "0.2.0"
description = "Local Google Chrome browsing history ingestion for the timeline."
author = "Magi Team"
entry_module = "plugin"
entry_class = "ChromeHistoryPlugin"
contribution_types = ["source"]
platforms = ["windows", "macos", "linux"]
```

## Usage

### Installing plugins (end users)

Open the Magi desktop app → **Settings → Extensions → Marketplace** → click **Install** on any plugin.

### Regenerating registry.json

After adding or updating a plugin, bump its version and regenerate the registry
index. Stage the complete package change first so the generator hashes the Git
snapshot exactly and never includes ignored caches or partial working-tree
changes:

```bash
python -m pip install -r scripts/registry-requirements.txt
git add plugins/<plugin-directory>
python scripts/build-registry.py
```

The generator freezes the staged Git index once, then reads every manifest,
asset, and package file from that single snapshot rather than mixing working
tree and index content. It rejects unsafe or non-portable paths and writes:

- `registry.json`, with a required `package_sha256` on every entry
- `version-history.json`, which permanently binds each
  `plugin_id@version` to one package SHA-256 and its sorted executable paths

Changing any tracked package file or executable permission therefore requires a
plugin version bump.
Versions must use canonical `MAJOR.MINOR.PATCH` form, such as `1.2.3`, and stay
within the host's 32-character limit.
The current registry version for a plugin can only move forward; it cannot add
a lower version or point back to an older published package.
The runtime package identity continues to cover only normalized package paths
and file bytes, so it remains stable across macOS, Linux, and Windows.
Executable permissions are separate publication metadata: the generator reads
Git executable modes from the same frozen tree, records their canonical paths
in version history, and rejects any same-version change. They are not added to
`package_sha256` and do not create a second runtime identity.
Generation imports `PluginManifest`, `PluginRegistryIndex`, plugin identifiers,
and version parsing directly from `magi-plugin-sdk`; it does not maintain a
second field or dependency-graph schema in this repository. The dedicated
`scripts/registry-requirements.txt` file selects the host SDK source used both
locally and in CI and must be pinned to an exact Magi commit before release.
The thin local adapter only enforces publication policy such as the capability
allowlist before writing protocol version `4`.
Commit both generated files with the plugin change. Never edit the history
manually.

Repository protection must require the registry CI job, an up-to-date branch,
and Code Owner approval for the identity workflow and history files. These
settings make the append-only checks effective when multiple changes land at
the same time.

### Adding a new plugin

1. Create a new directory under `plugins/`.
2. Add `plugin.toml` and `plugin.py` (see the plugin development guide in the [main repo docs](https://github.com/asukaonly/magi/blob/main/docs/plugin-development-guide.md)).
3. Export and review public schemas with `python scripts/export-settings-fields.py`.
4. Run SDK-only conformance and the package's behavior tests.
5. Stage the complete package, run `bash scripts/refresh.sh <plugin-directory>`,
   and commit its generated metadata with the source. Push only when requested.

### Local development

For development testing, add a development root containing the plugin package
to Magi's `plugins.scan_paths`. Keep this extra scan root outside the managed
`~/.magi/plugins/` install directory, then rescan from Settings → Extensions,
create and enable an explicitly trusted connection, and verify its worker loads. Do not manually copy or symlink a
package into the managed directory. If you choose the product's **Install from
local directory** action instead, Magi copies the package into its managed
directory and records the installation.

## Parent Repository

- **Main repo**: [github.com/asukaonly/magi](https://github.com/asukaonly/magi)
- **Plugin development guide**: [docs/plugin-development-guide.md](https://github.com/asukaonly/magi/blob/main/docs/plugin-development-guide.md)
- **Plugin architecture**: [docs/plugin-extension-architecture.md](https://github.com/asukaonly/magi/blob/main/docs/plugin-extension-architecture.md)

## SDK-only verification

CI installs `scripts/conformance-requirements.txt`, asserts that `magi` is absent,
then constructs declarations for every plugin and runs the package tests.
Declaration checks exercise each package's declared desktop platforms by
simulating the platform identifier; they do not invoke native collectors.
Unsupported platforms may omit runtime sources while the static manifest keeps
the package's complete setup catalog. Package tests check those platform gates.

Published field defaults must be independent of the developer's machine and
local installations. For example, Steam keeps its path default empty and detects
the local installation only when collecting data.

Run the same suite locally:

```bash
python -m pytest --import-mode=importlib scripts/test_sdk_conformance.py plugins -q
```

For coordinated SDK development, put the main worktree's `sdk/src` on
`PYTHONPATH`. Before publication, pin `scripts/registry-requirements.txt` to the
final published SDK commit, stage every changed package, and run
`bash scripts/refresh.sh`. The history refresh appends new package versions and
preserves every previously published entry unchanged. Include regenerated locks, registry and version history
with their source changes. Never publish an intermediate SDK hash.
