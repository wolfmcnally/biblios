### Reproducible builds

For a reproducible build suitable for use in CI or scripts, please:

- Pin to a specific version
- Install with lockfile dependencies
- Always install the latest CSS assets

```bash
cargo install mdbook-admonish --vers "1.5.0" --locked
mdbook-admonish install path/to/your/book
```

