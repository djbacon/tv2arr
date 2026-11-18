# Project instructions

## Required workflow

- Before starting new modifications, review and commit any completed, uncommitted work from previous tasks. Keep that commit separate from the new task's changes. Never include secrets or unfinished, unrelated work in the commit.
- After every modification, include in the final response the exact repository-relative files or folders that need to be uploaded to the server. State any required rebuild/restart steps. If no server upload is needed, say so explicitly.

## Versioning

- Maintain the release version in `VERSION` using `vMAJOR.MINOR.PATCH`. When making the next versioned change, create `VERSION` from the current version in `tv2arr/__init__.py` if it does not yet exist.
- Keep `tv2arr/__init__.py`'s `__version__` synchronized with `VERSION`, omitting the leading `v` in the Python value. Keep package/container version metadata synchronized when present.
- Major releases or redesigns increment MAJOR; new features increment MINOR; bugfixes increment PATCH. Reset lower components when incrementing a higher one.
- Bump once per delivered change using its highest applicable category. Documentation-only changes do not require a bump unless requested.
- If npm metadata is introduced, edit JSON version fields structurally: only the root `version` in `package.json`, and the root `version` plus `packages[""].version` in `package-lock.json`. Never globally replace lockfile version strings; dependency versions and ranges are unrelated. Verify lockfile repairs with an isolated clean `npm ci`.

## UI assets and secrets

- Use locally bundled Font Awesome icons only; do not add CDN-hosted icons, emoji/Unicode icon substitutes, or custom SVG icons. Mathematical score signs remain text.
- Never commit secrets or Cloudflare tunnel credentials.
