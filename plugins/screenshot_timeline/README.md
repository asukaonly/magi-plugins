# Screenshot Timeline

A macOS plugin that continuously captures your screen, runs local OCR via Apple Vision Framework, and feeds the resulting text into magi's L1 memory.

## What it does

- Smart-triggered screenshots (active-window timer + window-switch + optional keyboard triggers)
- Local OCR via `VNRecognizeTextRequest` — no network, no LLM calls
- Burst aggregation: consecutive captures of the same window cluster into one L1 event
- Privacy guards: ships with a default blocklist for password managers and incognito windows; user-extendable
- Storage: full-resolution originals retained for 30 days (configurable), thumbnails permanent

## Permissions

| Permission | Required for | Prompted when |
|---|---|---|
| Screen Recording | All capture | First capture attempt |
| Accessibility | Active-window title; keyboard triggers; panic hotkey | First probe / when user enables keyboard triggers |

If you only grant Screen Recording, capture works but window titles fall back to empty strings and keyboard triggers are unavailable.

## Architecture (overview)

Python sensor inside the plugin spawns a long-lived Swift child process (`bin/magi-vision-helper`) that owns ScreenCaptureKit and Vision Framework. The sensor handles triggers, burst aggregation, privacy guards, and emits standard `SensorOutput` payloads via the magi ingestion gateway.

See `docs/superpowers/specs/2026-05-21-screenshot-timeline-design.md` in the main magi repo for the full design.

## Settings

Open Settings → Extensions → Screenshot Timeline. Key fields:

| Field | Purpose |
|---|---|
| Capture scope | `active_window`, `full_screen`, `hybrid` (default), `all_displays` |
| Active window interval | Default 10s |
| Full-screen interval | Default 5min (used in hybrid/full_screen) |
| OCR languages | BCP-47 tags. Default `en-US, zh-Hans` |
| Original retention (days) | Default 30. Thumbnails are always permanent |
| Keyboard triggers | Off by default. Requires Accessibility |
| App blocklist | Bundle IDs (glob supported). Defaults include password managers + Keychain |
| Window title blocklist | Substrings. Default empty |
| Panic hotkey | Default `Option+Shift+P` |

## Manual E2E checklist

Use after a fresh install to verify end-to-end behavior:

### 1. Install + enable
- [ ] Install via Settings → Extensions → Marketplace (or symlink for dev)
- [ ] Enable the plugin; macOS prompts for Screen Recording permission
- [ ] Grant the permission and toggle the sensor on

### 2. Live capture
- [ ] Open Safari, navigate to a few different pages
- [ ] Wait 1–2 minutes
- [ ] In the Memory Workbench, filter by source = `screenshot_timeline`
- [ ] Verify L1 events appear with sensible window titles and OCR text

### 3. Burst behavior
- [ ] Stay on one window for 5+ minutes — confirm a single burst with multiple captures
- [ ] Switch to a different window — confirm the previous burst closed and a new one started

### 4. Privacy
- [ ] Open 1Password; verify no events are produced while it is frontmost
- [ ] Open a Chrome incognito window; verify no events are produced while it is frontmost
- [ ] Press the panic hotkey (default Option+Shift+P); verify no events for 60 seconds

### 5. Storage
- [ ] Check `~/.magi/data/resources/screenshots/<today>/` — both `_orig.jpg` and `_thumb.jpg` files exist
- [ ] Open Settings → Extensions → Screenshot Timeline → Storage; verify storage indicator shows non-zero usage

### 6. Retention (test by clock skew)
- [ ] Reduce `original_retention_days` to 0 and trigger a maintenance run
- [ ] Verify all `_orig.jpg` files in the date dir are deleted; thumbnails remain

## Known limitations (v1)

- macOS only (Windows version is a separate plugin, not in this release)
- Browser URL extraction is intentionally conservative — only window titles
- Pass-2 vision-LLM enrichment is reserved in the metadata schema but not exposed
- Helper binary is committed unsigned in the dev tree; release builds will use a signed/notarized binary distributed via `magi-plugins` GitHub Releases (separate workflow)
- The full-screen interval timer is declared in settings but not separately wired in v1 — hybrid mode currently treats every tick as `active_window` (follow-up work)
- Keyboard triggers UI is declared but the `CGEventTap` listener isn't wired yet — toggle is harmless but has no effect (follow-up work)

## Development

```bash
# Run all Python tests
cd plugins/screenshot_timeline
pytest tests/ -v

# Rebuild the Swift helper
cd helper
swift build -c release
cp .build/release/magi-vision-helper ../bin/magi-vision-helper

# Sanity-check the binary
echo '{"id":"req_1","op":"probe_active_window"}' | ./bin/magi-vision-helper
```

## License

MIT — same as the magi project.
