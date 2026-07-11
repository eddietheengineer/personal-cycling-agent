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
## Development Workflow

For local development without pushing to GitHub after every change:

### Option A: Local `/addons` directory (quickest)
1. Copy the `personal_cycling_agent/` folder to `/addons/personal_cycling-agent/` on your HA host (via Samba or SSH).
2. In HA Add-on Store → ⋮ → **Check for updates** → install from **Local add-ons** section.
3. Edit files locally, then click **Rebuild** in the add-on UI. No git push needed.

### Option B: VS Code Devcontainer (isolated, official HA approach)
1. Open this repo in VS Code with the Dev Containers extension.
2. VS Code will prompt to reopen in container — accept.
3. Run **Terminal → Run Task → Start Home Assistant**.
4. Open `http://localhost:7123` — your add-on is available as a local add-on.
5. Edit files in VS Code and click **Rebuild** in the local HA UI.