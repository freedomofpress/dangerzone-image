# Updates

This repo follows [SemVer](https://semver.org/), so any updates to it should
bump the respective MAJOR.MINOR.PATCH field.

In order to update the Dangerzone image repo, you need to:

1. Bump the version fields in the following files:
   - `pyproject.toml`
   - `qubes/dangerzone-insecure-converter.spec`
2. Update the packages with `uv lock --upgrade`
3. Commit, send a PR, wait for CI tests to pass, and merge.
4. Tag the tip of the `main` branch with a command like `git tag -s 1.1.0 -m 1.1.0`
   - Resist the temptation to include the `v` prefix
