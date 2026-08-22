# Publishing cubiczan-resilience

Publish each language package from its subdirectory. Do not publish until you have the matching registry credentials.

Repository: https://github.com/icohangar-ops/cubiczan-resilience

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
