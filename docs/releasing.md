# Releasing

Maintainer checklist for preparing and publishing an add-on release.

## Prepare the package

1. Review the files and Git history intended for publication. Exclude
   credentials, study data, editor backups, profiles, and test logs.
2. Update `ADDON_VERSION` in `taskhero_anki/api.py`, the changelog, release notes,
   and the Unix timestamp in `taskhero_anki/manifest.json`'s `mod` field.
3. Run `make release` and the full Anki/Qt checks described in
   [Contributing](../CONTRIBUTING.md). Run the disposable local API tests when
   server contracts change. CI runs only the dependency-free suite; it does not
   replace the Anki/Qt or optional API checks.
4. In an isolated Anki profile, check install/restart, connection, review
   progress, Undo, batch rewards, daily habit completion, retry, and Disconnect.
   Check that updating or disabling/re-enabling preserves saved progress, and
   uninstalling leaves Anki functional. Use no real credentials in screenshots.
5. Review the archive contents and verify its checksum from `dist/`:

   ```bash
   sha256sum --check taskhero-for-anki.ankiaddon.sha256
   ```

Rebuild after any runtime or package-metadata change. Publish built packages as
release downloads, not as files in Git.

Before the final connected smoke test, confirm that the deployed TaskHero API
supports the required `GET /me.dayBoundary` contract. The source and package
checks alone do not establish deployment readiness.

## Publish

1. Obtain approval for the candidate and its public source. Inspect the GitHub
   repository, commit metadata, and workflow logs while private; switch to
   public only with approval.
2. Upload the package to AnkiWeb using the project publisher account and the
   [listing description](ankiweb-description.md). Set the compatibility range
   to Anki 26.8.1 or later and preview the rendered description and links;
   convert the source formatting as needed for AnkiWeb.
3. Keep the installation code and manifest package identity at **1715570135**.
   Retain the `taskhero_anki` conflict so an earlier manual installation is
   disabled. Check that both file and AnkiWeb updates keep one active copy and
   preserve the profile's saved connection and reward history.
4. Rebuild after those changes, repeat the relevant checks, and publish matching
   source. Replace the changelog entry's "Unreleased" label with the actual
   release date. Create a version tag and GitHub release with the package,
   checksum, and [release notes](release-notes-1.0.0.md).
5. Update the same AnkiWeb listing with the final package. Install by its code
   in a clean profile and verify a review reward before announcing the release.
   Confirm the source, privacy and download links are publicly accessible.

Keep the public source and downloads matched to each released version.
