# Versioning

- **`v1.0.0`, `v1.0.1`, …:** immutable tags, each with a GitHub release and notes.
- **`v1`:** a moving tag that points at the latest `v1.x.y`.
- **Inputs:** only ever added within v1, never renamed or removed. Defaults change only where they fail closed.
- **The pinned pgrun CLI:** its version and checksum change only in a release whose notes say so.

For security-sensitive repositories, pin a full commit SHA and let Dependabot propose updates:

```yaml
      - uses: pgrundev/ci-action@<full commit sha>  # v1.0.0
```
