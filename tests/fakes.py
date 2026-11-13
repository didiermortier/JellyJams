"""Test doubles shared by the test modules."""


class FakeJellyfin:
    """Stands in for JellyfinAPI and records every write."""

    def __init__(self, existing=(), audio=None):
        self.existing = [dict(p) for p in existing]
        self.audio = audio or []
        self.created, self.deleted, self.scans = [], [], 0

    # reads
    def test_connection(self):
        return True

    def get_audio_items(self):
        return self.audio

    def get_users(self):
        return [{"Id": "u1", "Name": "didier"}]

    def list_playlists(self, user_id=None):
        return list(self.existing)

    def find_playlists_by_name(self, name, user_id=None):
        return [p for p in self.existing if p["Name"].lower() == name.lower()]

    # writes
    def create_playlist(self, name, track_ids, user_id=None, is_public=True):
        new_id = f"new-{len(self.created) + 1}"
        self.created.append((name, list(track_ids)))
        return {"success": True, "playlist_id": new_id, "track_count": len(track_ids)}

    def delete_playlist(self, playlist_id):
        self.deleted.append(playlist_id)
        return True

    def trigger_library_scan(self):
        self.scans += 1
        return True
