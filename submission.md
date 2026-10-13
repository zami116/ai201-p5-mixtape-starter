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

**How I found the root cause:** Simone's report said that some songs repeat two or three times while others appear once, which suggested the number of copies depends on something about the song. I followed `GET /songs/search` in `routes/songs.py`, which only calls `search_songs()` and counts what it gets back, into `services/search_service.py`. The query is `select(Song).outerjoin(song_tags, ...).filter(title/artist ILIKE ...)`. The `song_tags` association table is joined, but nothing in the query filters on it or selects from it. The docstring says each result includes a `tags` list, but that list is built by `Song.to_dict()` from the `Song.tags` relationship (`models.py`), not from the join. The condition that triggers the duplicates is therefore the number of rows a song has in `song_tags`: Simone's song had three tags and appeared three times. My reproduction confirmed it: a song with three tags returned 3 copies, and an untagged song returned 1.

**The root cause:** `search_songs()` outer-joined `song_tags` to `Song`. A join to a many-to-many table produces one result row per matching association row, so a song with N tags yields N rows for the same `Song`, and a song with 0 tags yields 1 row because of the outer join. `select(Song)` returns an entity for each row, and nothing deduplicated them. The join contributed nothing to the results, since tags are loaded separately through the `Song.tags` relationship. This is why the duplicates were conditional: untagged and single-tag songs appeared once, a three-tag song appeared three times.

**My fix:** Removed the `.outerjoin(song_tags, ...)` from the query in `search_songs()` (`services/search_service.py`), along with the now-unused `song_tags` import. I chose to remove the join rather than add `.distinct()` because the join is the source of the extra rows and is not needed. Adding `.distinct()` would have left a pointless join in the query and only hidden its effect.

**My side-effect check:** Ran the whole suite: all 13 tests pass, including the multi-tag, single-tag, no-tag, and no-match search tests. I confirmed that results still include tags: searching "borough" for a three-tag song returns exactly 1 result with `['rap', 'hiphop', 'boom-bap']`, so removing the join did not remove tag data from the response. The title/artist `ILIKE` filter is untouched, so matching behaves as before. Re-running my endpoint reproduction, `q=Anthem` now returns a count of 1.

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:** Posted to `POST /songs/<id>/listen` once per day on consecutive days, with the clock patched to noon UTC on each day, then read `GET /users/<id>/streak`. Fri 7 Jun 2024 gave 1, Sat 8 Jun gave 2, **Sun 9 Jun gave 1**, Mon 10 Jun gave 2, and Tue 11 Jun gave 3. Every consecutive-day listen incremented the streak except the one landing on a Sunday, which reset it to 1, matching the report that it only happens on Sundays. The existing test `test_streak_increments_on_sunday` also fails.

**How I found the root cause:** The report said the reset happened only on Sundays, so I treated the weekday as the discriminating variable and looked for date logic. I followed `POST /songs/<id>/listen` in `routes/songs.py` to `record_listening_event()` in `services/streak_service.py`, which creates the event and delegates the streak math to `update_listening_streak()`. `get_streak()` just reads the stored column, so it can't be the culprit. In `update_listening_streak()` the day gap is computed correctly as `(today - last_date).days`, and the three branches (same day, next day, otherwise) match the docstring's rules. But the "next day" branch has an extra clause: `days_since_last == 1 and today.weekday() != 6`. Python's `weekday()` returns 0 for Monday and 6 for Sunday, so this clause is false only on Sundays. I was confident this was the cause because it names the one day of the week that the report says fails, and none of the docstring's streak rules mentions a day of the week at all.

**The root cause:** The increment condition in `update_listening_streak()` was `days_since_last == 1 and today.weekday() != 6`. On a Sunday the second half is `False`, so a user who listened the day before (Saturday) fails the "listened yesterday" test and falls through to the `else` branch, which sets `listening_streak = 1`. The docstring says the streak resets only when more than one day has passed, so a Sunday listen following a Saturday listen was being treated as a skipped day. Listening again on Monday (`weekday() == 0`) passes the check, which is why the streak resumed counting from 1 to 2 instead of continuing from 12.

**My fix:** Removed the `and today.weekday() != 6` clause from the `elif` in `update_listening_streak()` (`services/streak_service.py`), so the branch is just `elif days_since_last == 1:`. The streak rules now depend only on how many calendar days have passed, as the docstring states.

**My side-effect check:** Ran `tests/test_streaks.py`: all 5 pass, including the previously failing Sunday test. I also checked both sides of the boundary directly against the function, starting from a streak of 12: listening Saturday then Sunday gives 13; a skipped day gives 1 whether the next listen lands on a Sunday or a Monday; and a second listen on the same Sunday leaves it at 12. Re-running my endpoint reproduction, the Fri–Tue sequence now produces 1, 2, 3, 4, 5 with no reset on Sunday.

### Issue #4 — I got notified when a friend added my song to a playlist but not when they rated it

**How I reproduced it:** Created a sharer (aaliya), a rater (kenji), and a song shared by aaliya. Called `POST /songs/<song_id>/rate` as kenji with `score=5`. The endpoint returned `201`, and the rating was saved. `GET /users/<aaliya_id>/notifications` returned `count=0` both before and after the rating, so no notification was ever created.

**How I found the root cause:** The hint said to compare the working notification path against the missing one line by line. Both `add_to_playlist()` and `rate_song()` live in `services/notification_service.py`, and `routes/songs.py` calls `rate_song()` directly with nothing else after it, so the route could not be where a notification was dropped. In `add_to_playlist()`, the final step is an `if song.shared_by != added_by_user_id:` block that calls `create_notification()` for the sharer. `rate_song()` validates the score, loads the song and rater, creates or updates the `Rating`, commits, and returns. It never calls `create_notification()`, and a search of the module showed `add_to_playlist()` was its only caller. The module docstring says notifications are generated when friends interact with a user's shared songs, and the `create_notification()` docstring even lists `'song_rated'` as an example type that nothing produced.

**The root cause:** Notifications are not created automatically. Each service function that performs a notifiable action has to call `create_notification()` itself. `add_to_playlist()` does, but `rate_song()` never did, so a rating was saved with no notification step at all. The code did not fail. It simply had no notification step.

**My fix:** After the rating is committed in `rate_song()` (`services/notification_service.py`), I added the same pattern used by `add_to_playlist()`: if `song.shared_by != user_id`, call `create_notification()` for the sharer with type `song_rated` and the body `"<rater> rated your song '<title>' <score>/5."`. The self-check keeps people from being notified about their own ratings, as in the playlist path. The notification is created after the commit, so an invalid score or a missing song, which raise earlier, produce none.

**My side-effect check:** Re-ran my endpoint reproduction: aaliya now has 1 `song_rated` notification after kenji's rating. I checked directly that rating your own song creates no notification, that an out-of-range score raises `ValueError` and creates none, and that re-rating the same song still updates the one `Rating` row (notifying again for the changed score). `add_to_playlist()` was not touched. Full suite: 16 passed.

**Regression test:** `tests/test_notifications.py` adds three tests: `test_rating_notifies_song_sharer`, `test_rating_own_song_does_not_notify`, and `test_invalid_rating_does_not_notify`. I confirmed that the first one fails when the fix is stashed and passes with it.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it:** Created a viewer (nova) who is friends with darius, and gave darius one listening event at 11pm on the previous day. With the clock pinned to 9am the next morning, I called `GET /feed/<nova_id>/listening-now`. It returned `count=1` with darius in it, ten hours after his last listen and on a different calendar day. Moving the pinned clock to 11pm that day, exactly 24 hours after the listen, was the first time the feed stopped including him, which matches the report that yesterday's listens "hang around until the same time the next day".

**How I found the root cause:** I followed `GET /feed/<id>/listening-now` in `routes/feed.py`, which only calls `get_friends_listening_now()` in `services/feed_service.py` and wraps the result. In that function the friend IDs come from `user.friends`, and the events are filtered with `ListeningEvent.listened_at >= cutoff`. The filter and the per-friend deduplication both looked correct, so the question was what `cutoff` is. It is `datetime.now(timezone.utc) - RECENT_THRESHOLD`, and `RECENT_THRESHOLD` is defined at the top of the module as `timedelta(hours=24)`. The report says "only friends who have listened *today*" should appear, and the sibling function `get_activity_feed()` explicitly documents that it is the one that is not filtered by recency. That made it clear the threshold was measuring the wrong thing.

**The root cause:** `get_friends_listening_now()` computed its cutoff as a rolling window, `now - 24 hours`, instead of the start of the current day. A rolling 24-hour window moves forward with the clock, so a listen at 11pm remains inside it until 11pm the next day. At 9am the cutoff was 9am yesterday, and darius's 11pm listen sat well after it. The feed's contract, "only friends who have listened today", was never expressed in the code. Its "today" was silently implemented as "within the last 24 hours".

**My fix:** In `services/feed_service.py`, the cutoff is now midnight UTC at the start of the current day: `datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)`. I removed the `RECENT_THRESHOLD` constant (nothing else used it) and the now-unused `timedelta` import, and updated the docstring to say "today (since midnight UTC)". The rest of the function is unchanged. UTC is used because the app stores and compares all timestamps in UTC, matching how the streak logic defines a calendar day.

**My side-effect check:** I checked both sides of the midnight boundary. A listen at 23:59:59 yesterday is excluded and one at 00:00:00 today is included. A listen at 8:30am today and one at 11pm yesterday behave as expected at 9am. The "yesterday" tests fail without the fix and pass with it, while the "today" tests pass either way, so the fix only removes stale entries. The most-recent-song-per-friend deduplication is untouched, and `get_activity_feed()` does not use the cutoff, so it still returns the latest events regardless of age. Full suite: 20 passed.

**Regression test:** `tests/test_feed.py` adds four tests that pin the clock to 9am and cover both sides of midnight: `test_listen_from_yesterday_evening_is_excluded`, `test_listen_just_before_midnight_is_excluded`, `test_listen_at_start_of_today_is_included`, and `test_listen_earlier_today_is_included`.
