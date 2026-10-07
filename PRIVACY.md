# Privacy

Your study content stays in Anki. TaskHero for Anki shares the information
needed to award points and complete your chosen habit.

## What stays in Anki

The add-on does not send card text, answers, notes, tags, deck names, media,
scheduling details, or Anki profile names. It has no ads.

## What TaskHero receives

- Points earned and a review-count description, such as `Completed 10 reviews`.
- The habit you've chosen and its completion time. Creating “Study Anki” also
  sends that habit's name and settings.
- Identifiers that keep the same reward from being awarded twice, with
  `Anki` as the source.

The add-on does not send separate analytics or crash reports. TaskHero records
successful rewards for usage analytics, including the integration, reward type,
points awarded, and timestamps. These records are associated with your TaskHero
account; they do not contain card content or individual review history.

Your Anki token authenticates the connection over HTTPS. Requests identify the
add-on version, and TaskHero may receive connection information such as your IP
address. TaskHero's handling of account and server data is covered by its
[privacy policy](https://taskhero.app/privacy).

## What's saved on your computer

Each Anki profile has its own `taskhero_anki` folder containing:

- `credentials.json`: your Anki integration token.
- `settings.json`: your reward settings, selected habit, and TaskHero day settings.
- `state.sqlite3`: review identifiers and records of rewards waiting to be sent
  or already sent, including event IDs, delivery status and receipt IDs/times.
  Complete API response bodies are not retained. Database working files may
  also be present.

These files are separate from your cards and aren't included in Anki's
collection sync. The token relies on your computer's file permissions: it is
not separately encrypted or stored in the system keychain. Software with access
to your profile folder may be able to read it.

## Disconnecting

Choose **Tools → TaskHero for Anki → Settings… → Disconnect** to remove the
saved token, unlink the habit, and cancel unsent rewards. A request already
underway may still finish.

Disconnect resets your batch and daily-goal progress, even if you reconnect to
the same account. Reviews already present on this computer when you reconnect
are excluded. Unseen mobile reviews can still count after a later sync, even if
made while disconnected. Sync before reconnecting to exclude that history too.
Settings and past reward records remain on your computer to prevent duplicate
rewards.

Disconnect affects this Anki profile only. To invalidate the token everywhere,
revoke or replace it in TaskHero's **Integrations & API → Anki** card.

## Removing local data

Uninstalling alone doesn't erase these files, including a saved token. To remove
them:

1. Disconnect in the add-on settings.
2. Close Anki completely.
3. Delete only the `taskhero_anki` subfolder inside your Anki profile folder,
   not the profile itself.

This does not delete rewards or habit activity from TaskHero. For account-data
deletion, see [TaskHero's deletion instructions](https://taskhero.app/data-deletion).

Questions? Email [support@taskheroics.com](mailto:support@taskheroics.com).
