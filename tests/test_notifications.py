"""
tests/test_notifications.py — Mixtape

Tests for notifications generated when friends interact with a shared song.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def setup(app):
    with app.app_context():
        sharer = User(username="aaliya", email="aaliya@example.com")
        rater = User(username="kenji", email="kenji@example.com")
        db.session.add_all([sharer, rater])
        db.session.commit()
        song = Song(title="Night Drive", artist="Neon Pilots", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        return {"sharer": sharer.id, "rater": rater.id, "song": song.id}


def test_rating_notifies_song_sharer(app, setup):
    """Rating someone else's song creates a 'song_rated' notification for the sharer."""
    with app.app_context():
        rate_song(setup["rater"], setup["song"], 5)

        notifs = get_notifications(setup["sharer"])
        assert len(notifs) == 1
        assert notifs[0]["type"] == "song_rated"
        assert "kenji" in notifs[0]["body"]
        assert "Night Drive" in notifs[0]["body"]


def test_rating_own_song_does_not_notify(app, setup):
    """Rating your own shared song does not notify you."""
    with app.app_context():
        rate_song(setup["sharer"], setup["song"], 4)
        assert get_notifications(setup["sharer"]) == []


def test_invalid_rating_does_not_notify(app, setup):
    """A rejected rating (score out of range) creates no notification."""
    with app.app_context():
        with pytest.raises(ValueError):
            rate_song(setup["rater"], setup["song"], 6)
        assert get_notifications(setup["sharer"]) == []
