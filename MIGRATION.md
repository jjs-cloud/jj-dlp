# Migration (future work)

This rebuild targets a clean install — there is no importer from any prior
version of this app, and none is planned as part of this rebuild (Doc 1
§22). This file exists only to document where a future importer would plug
in, if one is ever written.

## Extension point

A migration importer would not need its own storage format or write path.
It would:

1. Read whatever the source data lives in (a prior app's config files, an
   export format, etc.) — entirely its own concern, outside `jj_dlp/core`.
2. Construct plain Python dicts matching the shapes in Doc 1 §3
   (`config/app.json` §3.3, a `config/sites/<label>.json` per site §3.4,
   `config/priority.json` §3.5, `config/theme.json` §3.6).
3. Hand those dicts to the existing save functions — the same ones the
   Config tab and management overlay already call:

   - `core.config.app.save(data_dir, app_config_obj)`
   - `core.config.sites.create_site(data_dir, label, plugin_id)` followed
     by `core.config.sites.save_site(data_dir, label, data)`
   - `core.config.priority.set_order(...)` / `set_bypass(...)` /
     `set_override(...)` per entry
   - `core.theme.store.save_theme(...)` for any imported custom themes

No new module needs to know about atomic writes, backups, or schema
validation — `core/config/storage.py` and `core/config/schema.py` already
handle that for any caller, importer included.

## Non-goals for now

- No source-format parser exists yet; this file doesn't assume which
  prior app or export shape a future importer would target.
- No CLI flag or UI entry point for "import" exists yet — adding one is a
  small, separate addition once an importer module exists.
