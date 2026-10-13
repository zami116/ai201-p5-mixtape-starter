# Mixtape Bug Hunt — Submission

## Codebase Map

### Overview

Mixtape is a Flask + Flask-SQLAlchemy app backed by SQLite (`instance/mixtape.db`). It is organised in three layers: **routes** parse HTTP input and format JSON, **services** hold all business logic and database access, and **models** define the schema. Routes never touch the database directly (apart from one simple user lookup); they call a service function and translate a `ValueError` into a 400 or 404 response.

### Files and their responsibilities

| File | Role |
| --- | --- |
| `app.py` | Application factory `create_app(config)`. Creates the shared `db = SQLAlchemy()` object, sets config (DB URI defaults to `sqlite:///mixtape.db`, overridable with `DATABASE_URL` or a config dict, which the tests use for in-memory SQLite), registers four blueprints under URL prefixes, and calls `db.create_all()`. |
| `models.py` | Seven models: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, `Notification`. Three association tables: `friendships` (user to user, many-to-many), `song_tags` (song to tag), and `playlist_entries` (playlist to song). `playlist_entries` also carries `position`, `added_by` and `added_at`, so playlist order is explicit. `Rating` has a unique constraint on (`user_id`, `song_id`), so a user has at most one rating per song. `User` stores `listening_streak` and `last_listened_at` directly on the row. Each model has a `to_dict()` used for JSON output. Primary keys are UUID strings. |
| `routes/songs.py` (`/songs`) | `GET /search?q=`, `GET /<id>`, `POST /<id>/rate`, `POST /<id>/listen`. Delegates to `search_service`, `notification_service.rate_song`, and `streak_service.record_listening_event`. |
| `routes/playlists.py` (`/playlists`) | `POST /` create, `GET /<id>`, `GET /<id>/songs`, `POST /<id>/songs`. Delegates to `playlist_service` for reads and creation, and to `notification_service.add_to_playlist` for adding a song. |
| `routes/users.py` (`/users`) | `GET /<id>`, `GET /<id>/streak`, `GET /<id>/notifications` (optional `unread_only`), `POST /notifications/<id>/read`. |
| `routes/feed.py` (`/feed`) | `GET /<id>/listening-now` and `GET /<id>/activity`, both delegating to `feed_service`. |
| `services/streak_service.py` | `record_listening_event` creates a `ListeningEvent` and calls `update_listening_streak`, which compares the calendar date of `last_listened_at` to today and updates `listening_streak` and `last_listened_at` on the `User`. `get_streak` reads the stored value. |
| `services/feed_service.py` | `get_friends_listening_now` resolves the user's friend IDs, queries `ListeningEvent` rows for those friends within a recency window defined by the `RECENT_THRESHOLD` constant, and keeps the newest event per friend. `get_activity_feed` returns the latest N friend events with no time filter. |
| `services/search_service.py` | `search_songs` runs a case-insensitive title/artist `ILIKE` query on `Song`, joined to `song_tags`, using the SQLAlchemy 2.0 `select()` style. `get_song` fetches one song by ID. |
| `services/notification_service.py` | Despite the name, this module is the home for any action that can generate a notification. It contains `create_notification` (the single insert helper), `add_to_playlist`, `rate_song`, `get_notifications` (newest first, optional unread filter), and `mark_as_read`. |
| `services/playlist_service.py` | `create_playlist`, `get_playlist_songs` (a query joining `Song` to `playlist_entries`, ordered by `position`), `get_playlist` (metadata only), and `get_user_playlists`. |
| `seed_data.py` | Populates the database with users, friendships, songs, tags, listening events, and playlists for manual testing. |
| `tests/` | pytest suites for streaks, search, and playlists. Each builds an app via `create_app` with an in-memory SQLite URI and calls service functions directly. |

### Data flow: a user rates a song

1. The client sends `POST /songs/<song_id>/rate` with JSON `{"user_id": ..., "score": ...}`.
2. `routes/songs.py: rate()` reads the body and returns a 400 if `user_id` or `score` is missing. It then calls `rate_song(user_id, song_id, int(score))` from `services/notification_service.py`.
3. `rate_song()` checks that the score is in 1–5. It then loads the `Song` and the rating `User` with `db.session.get`, raising `ValueError` if either is missing.
4. It queries `Rating` for an existing row with this (`user_id`, `song_id`). If one exists, it updates `score`. Otherwise it inserts a new `Rating`. Then it commits.
5. The `Rating` is returned to the route, which serialises it with `to_dict()` and responds `201`. A `ValueError` becomes a `400` with the message.

### Data flow: a user adds a song to a playlist

1. `POST /playlists/<playlist_id>/songs` with `{"song_id", "added_by"}` reaches `routes/playlists.py: add_song()`.
2. The route calls `add_to_playlist()` in `services/notification_service.py`.
3. That function loads the `Song`, the adding `User`, and the `Playlist`. It appends the song to `playlist.songs` if not already present, and commits. This writes a row into `playlist_entries`.
4. If the adder is not the person who shared the song (`song.shared_by`), it calls `create_notification()` with type `song_added_to_playlist` for the sharer. That inserts a `Notification` row.
5. The recipient later reads it through `GET /users/<id>/notifications`, which calls `get_notifications()`.

### Data flow: a user listens to a song

1. `POST /songs/<song_id>/listen` goes to `routes/songs.py: listen()`, which calls `record_listening_event()` in `services/streak_service.py`.
2. That function takes the current UTC time, creates a `ListeningEvent`, and calls `update_listening_streak(user, now)`. The streak is computed from `user.last_listened_at` and the new `now`, and both fields are updated on the `User` row.
3. Everything is committed together. The new event is what `feed_service` later queries for the "listening now" and activity feeds, and the `User` row is what `GET /users/<id>/streak` reads.

### Patterns I noticed

- **Thin routes, fat services.** Every route does parsing and response formatting and then hands off immediately to a service function. Services signal failure by raising `ValueError`, and routes map that to an HTTP status.
- **Services own their own commits.** Each service function that changes data calls `db.session.commit()` itself, so one request is generally one transaction.
- **Side effects live where the action lives.** Notification creation is not a separate step in the routes. It is called from inside the service function that performs the triggering action, through the shared `create_notification()` helper.
- **Denormalised state on `User`.** Streak data (`listening_streak`, `last_listened_at`) is stored on the user row and updated at write time, rather than derived from `ListeningEvent` history on read.
- **Explicit ordering for playlists.** The `position` column on `playlist_entries` defines playlist order, so reads sort by it rather than by insertion order.
- **Time handling.** Timestamps are generated in UTC via `datetime.now(timezone.utc)`. SQLite returns them as naive datetimes, so services that compare them re-attach UTC where needed.
- **Dependency direction.** `routes` → `services` → `models` → `app` (for `db`). Blueprints are imported inside `create_app` to avoid a circular import, which is why the app must be launched through the factory (`FLASK_APP=app:create_app flask run`) rather than `python app.py`.

---

## Root Cause Analyses

_(One entry per fixed bug. Each entry's "How I reproduced it" is filled in below; the remaining fields are added as each fix is committed.)_

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it:** Created a playlist and inserted three songs into `playlist_entries` with positions 1, 2, 3 (the same way `seed_data.py` populates playlists). After each insert I called `GET /playlists/<id>/songs`. The endpoint returned `count=0` after 1 song, `count=1` after 2 songs, and `count=2` after 3 songs: always one fewer than the playlist holds, and the missing song was always the newest. The existing tests `test_playlist_returns_all_songs` and `test_playlist_returns_songs_in_order` also fail.

**How I found the root cause:** I followed the call chain top-down: `GET /playlists/<id>/songs` in `routes/playlists.py` calls `get_playlist_songs()` in `services/playlist_service.py`, and the route does nothing but `len()` and `jsonify` the list it receives. That ruled out the route. In the service, the query joins `Song` to `playlist_entries`, filters by playlist, and orders by `position`. The query result itself looked correct, so I checked what happens between the query and the return. The final line is `return [song.to_dict() for song in songs[:-1]]`. I was confident this was the cause, and not just a suspicious spot, because the report says the missing song is always the newest and that adding another song reveals the previous one. That is exactly what dropping the last element of a list ordered by ascending position would do.

**The root cause:** `get_playlist_songs()` documents that it "returns all songs in the playlist", and its query correctly fetches every row ordered by `position`. But the return statement slices the result with `songs[:-1]`, which discards the final element. Because the list is sorted by ascending position, the final element is always the most recently added song. A playlist with N songs therefore returned N-1. Adding a song shifts the previous last song out of the slice's reach, which explains why adding a new song "freed" the old one and hid the new one.

**My fix:** Changed `songs[:-1]` to `songs` in `get_playlist_songs()` (`services/playlist_service.py`), so every queried song is serialised. This removes the slice, which was the only step that dropped data. The query and its `position` ordering were already correct, so nothing else needed to change.

**My side-effect check:** Ran `tests/test_playlists.py`: all 3 pass, including the ordering test, so songs still come back in ascending `position`. Re-ran my reproduction through the real endpoint: counts are now 1, 2, 3 after each insert instead of 0, 1, 2. The boundary case of an empty playlist still returns an empty list, since the query returns nothing. `get_playlist()` and `get_user_playlists()` don't use this function, so they are unaffected.

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it:** Created a song "Crown Heights Anthem" by "Borough Kings" with three tags, plus three songs with no tags. `GET /songs/search?q=Anthem` returned `count=3`, with the same song three times, matching the number of tags. `GET /songs/search?q=Song0` (an untagged song) returned `count=1`. So the duplication is conditional on a song having multiple tags (one extra row per tag). The existing test `test_search_no_duplicates_multi_tag_song` also fails.

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:** Posted to `POST /songs/<id>/listen` once per day on consecutive days, with the clock patched to noon UTC on each day, then read `GET /users/<id>/streak`. Fri 7 Jun 2024 gave 1, Sat 8 Jun gave 2, **Sun 9 Jun gave 1**, Mon 10 Jun gave 2, and Tue 11 Jun gave 3. Every consecutive-day listen incremented the streak except the one landing on a Sunday, which reset it to 1, matching the report that it only happens on Sundays. The existing test `test_streak_increments_on_sunday` also fails.
