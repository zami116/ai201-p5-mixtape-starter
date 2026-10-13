"""
tests/test_feed.py — Mixtape

Tests for the "Friends Listening Now" feed.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch
from app import create_app, db
from models import User, Song, ListeningEvent, friendships
import services.feed_service as feed_service
from services.feed_service import get_friends_listening_now


class FrozenDatetime(datetime):
    """datetime whose now() is pinned to 9am UTC on 2024-06-10 (a Monday)."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2024, 6, 10, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


def _setup(listened_at):
    """Create a viewer, a friend, and one listening event by the friend at listened_at."""
    viewer = User(username="nova", email="nova@example.com")
    friend = User(username="darius", email="darius@example.com")
    db.session.add_all([viewer, friend])
    db.session.commit()
    song = Song(title="Night Drive", artist="Neon Pilots", shared_by=friend.id)
    db.session.add(song)
    db.session.commit()
    db.session.execute(friendships.insert().values(user_id=viewer.id, friend_id=friend.id))
    db.session.add(ListeningEvent(user_id=friend.id, song_id=song.id, listened_at=listened_at))
    db.session.commit()
    return viewer.id


def _feed(viewer_id):
    with patch.object(feed_service, "datetime", FrozenDatetime):
        return get_friends_listening_now(viewer_id)


def test_listen_from_yesterday_evening_is_excluded(app):
    """A listen at 11pm yesterday must not appear at 9am today."""
    with app.app_context():
        viewer_id = _setup(datetime(2024, 6, 9, 23, 0, tzinfo=timezone.utc))
        assert _feed(viewer_id) == []


def test_listen_just_before_midnight_is_excluded(app):
    with app.app_context():
        viewer_id = _setup(datetime(2024, 6, 9, 23, 59, 59, tzinfo=timezone.utc))
        assert _feed(viewer_id) == []


def test_listen_at_start_of_today_is_included(app):
    with app.app_context():
        viewer_id = _setup(datetime(2024, 6, 10, 0, 0, 0, tzinfo=timezone.utc))
        feed = _feed(viewer_id)
        assert [item["friend"]["username"] for item in feed] == ["darius"]


def test_listen_earlier_today_is_included(app):
    with app.app_context():
        viewer_id = _setup(datetime(2024, 6, 10, 8, 30, tzinfo=timezone.utc))
        assert len(_feed(viewer_id)) == 1
