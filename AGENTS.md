# Agent Instructions

## Versioning

Every time you commit and push, increment the patch version by 1 in `personal_cycling_agent/config.json`:

```json
  "version": "X.Y.Z",  // bump Z by 1 on each commit
```

Example: `0.0.1` → `0.0.2` → `0.0.3`

**CRITICAL: `personal_cycling_agent/config.json` is a JSON file, NOT YAML.**
Home Assistant's add-on store requires valid JSON. Never rename it to `.yaml`
or write YAML content into it — HA will reject the add-on entirely.