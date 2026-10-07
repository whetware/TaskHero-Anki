# Contributing

Bug reports, documentation improvements, and code contributions are welcome.

## Report a problem

Include your Anki and add-on versions, operating system, what you expected, and
steps to reproduce the problem. Remove tokens and personal study data from logs
and screenshots. Report security issues [privately](SECURITY.md).

## Work on the code

The add-on uses Python's standard library and Anki's bundled APIs. For an
overview of the code, see [Architecture](docs/architecture.md).

Python, Qt and Anki retain their own licenses and are supplied by the Anki
installation, not bundled with this add-on. No additional third-party libraries
or assets are bundled.

Run tests and build the add-on from the repository root:

```bash
make test
make build
```

Run `make release` to check the source, run tests, and build a reproducible
archive with a checksum.

To include the Anki/Qt tests, set `PYTHON` to Anki's bundled Python executable
and `PYTHONPATH` to its `app_packages` directory, then run:

```bash
QT_QPA_PLATFORM=offscreen make release-anki
```

This includes rendering and resizing the reviewer badge in Qt WebEngine.
Without Anki's packages, the Qt tests are skipped. The optional API tests require
`TASKHERO_ANKI_LOCAL_API_KEY` for a disposable local account and target
`127.0.0.1:3000`. Never use a production token or account for these tests.

## Before opening a pull request

- Add tests for behavior changes and check the result in Anki.
- Keep network requests off the reviewer's UI thread.
- Preserve separate state for each profile and duplicate-safe reward retries.
- Never send or log study content or credentials.
- Keep editor backups, caches, profiles, databases, test logs, and built
  `.ankiaddon` files out of Git.

Contributions are licensed under [AGPL-3.0-or-later](LICENSE).
