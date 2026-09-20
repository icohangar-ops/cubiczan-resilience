# Publishing cubiczan-resilience

Publish each language package from its subdirectory. Do not publish until you have the matching registry credentials.

Repository: https://github.com/icohangar-ops/cubiczan-resilience

## Consuming before npm credentials exist — versioned git tag

Until `@cubiczan/resilience` is on a registry, consumers install straight from
git. A root-level manifest makes the repo root installable; its `prepare`
script builds `typescript/dist` at install time (npm or bun both run
`prepare` for git dependencies — bun consumers must list the package under
`trustedDependencies` to allow it).

```bash
# after tagging (see below)
npm install github:icohangar-ops/cubiczan-resilience#typescript-v0.2.0
# or with bun:
bun add github:icohangar-ops/cubiczan-resilience#typescript-v0.2.0
```

Tagging a release (from the repo root, after bumping the version in
`typescript/package.json` — and the root shim's manifest, which must stay in
lockstep):

```bash
git tag typescript-v0.2.0 && git push origin typescript-v0.2.0
```

Bun consumers additionally need, in their own `package.json`:

```json
{ "trustedDependencies": ["@cubiczan/resilience"] }
```

## npm — `@cubiczan/resilience`

```bash
cd typescript
npm login                    # once; scoped packages need an npm account with publish rights
npm test
npm run build                # also runs via prepublishOnly
npm publish --access public
```

## PyPI — `cubiczan-resilience`

```bash
cd python
python -m pip install --upgrade build twine
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

Use a PyPI API token (`__token__` / `pypi-...`) when prompted, or set `TWINE_USERNAME` / `TWINE_PASSWORD`.

## crates.io — `resilient-call`

```bash
cd rust
cargo login                  # paste a crates.io API token once
cargo test
cargo publish --dry-run      # optional sanity check
cargo publish
```

## Notes

- Bump the version in `typescript/package.json`, `python/pyproject.toml`, and/or `rust/Cargo.toml` before each release.
- `prepublishOnly` on the TypeScript package runs `npm run build` automatically.
- Do not `cargo publish` without a crates.io token; dry-run does not need one.
