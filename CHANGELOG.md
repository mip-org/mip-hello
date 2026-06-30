# Changelog

## 2026-06-30

- Converted to the shared MIP channel build system. Package recipes are now
  `source.yaml` (renamed from `recipe.yaml`); `.github/workflows/` are thin
  callers delegating to the reusable workflows in `mip-org/mip_channel_tools`
  (`@main`). Dropped the per-channel `scripts/` and `site/` directories (now
  provided by the tooling repo) and added `.gitattributes` and `CLAUDE.md`.
