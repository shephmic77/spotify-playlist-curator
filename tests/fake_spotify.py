"""In-process fake of the Spotify Web API endpoints Phase 1 uses.

Built on ``httpx.MockTransport`` so the real ``SpotifyClient`` code (headers, paging,
retries) runs unchanged. Behaviour mirrors the documented post-Feb-2026 API:
- paging objects with ``next`` links that carry limit/offset/market
- ``GET /playlists/{id}/items`` returns 403 for playlists you neither own nor collaborate on
- playlist entries use ``item``; a fixture flag serves the deprecated ``track`` key instead
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
API = "https://api.spotify.com/v1"


def load_library() -> dict[str, Any]:
    return json.loads((FIXTURES / "library.json").read_text())


class FakeSpotify:
    def __init__(self, library: dict[str, Any] | None = None, token: str = "access-1") -> None:
        self.lib = copy.deepcopy(library or load_library())
        self.valid_tokens = {token}
        self.calls: list[tuple[str, str]] = []
        self.write_bodies: list[tuple[str, str, Any]] = []
        # path -> list of queued (status, headers, body) responses served before normal ones
        self.injected: dict[str, list[tuple[int, dict[str, str], Any]]] = {}
        self.on_request: Callable[[httpx.Request], None] | None = None
        self.snap_counter = 0

    # -- helpers for tests -------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def http(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), base_url=API)

    def inject(
        self,
        path: str,
        status: int,
        headers: dict[str, str] | None = None,
        body: Any = None,
        times: int = 1,
    ) -> None:
        self.injected.setdefault(path, []).extend([(status, headers or {}, body)] * times)

    def playlist(self, pid: str) -> dict[str, Any]:
        return next(p for p in self.lib["playlists"] if p["id"] == pid)

    def count(self, path_prefix: str) -> int:
        return sum(1 for _, p in self.calls if p.startswith(path_prefix))

    # -- request handling --------------------------------------------------

    def _track(self, key: str | None) -> dict[str, Any] | None:
        if key is None:
            return None
        if key == "__episode__":
            return copy.deepcopy(self.lib["episode"])
        if key.startswith("spotify:"):  # entries written through the API are stored as URIs
            for t in self.lib["tracks"].values():
                if t["uri"] == key:
                    return copy.deepcopy(t)
            tid = key.split(":")[-1]
            return {
                "type": "track",
                "id": tid,
                "uri": key,
                "name": f"Track {tid}",
                "artists": [{"id": "a", "name": "Someone"}],
                "album": {},
                "is_local": False,
            }
        return copy.deepcopy(self.lib["tracks"][key])

    def _page(self, request: httpx.Request, items: list[Any]) -> dict[str, Any]:
        q = parse_qs(urlparse(str(request.url)).query)
        limit = int(q.get("limit", ["20"])[0])
        offset = int(q.get("offset", ["0"])[0])
        assert 1 <= limit <= 50, f"limit {limit} outside documented 1..50"
        chunk = items[offset : offset + limit]
        nxt = None
        if offset + limit < len(items):
            params = {k: v[0] for k, v in q.items()}
            params.update(offset=str(offset + limit), limit=str(limit))
            nxt = f"{API}{request.url.path.removeprefix('/v1')}?{urlencode(params)}"
        return {
            "href": str(request.url),
            "limit": limit,
            "offset": offset,
            "total": len(items),
            "next": nxt,
            "previous": None,
            "items": chunk,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        self.calls.append((request.method, path))
        if self.on_request:
            self.on_request(request)

        queued = self.injected.get(path)
        if queued:
            status, headers, body = queued.pop(0)
            return httpx.Response(status, headers=headers, json=body)

        auth = request.headers.get("Authorization", "")
        if auth.removeprefix("Bearer ") not in self.valid_tokens:
            return httpx.Response(
                401, json={"error": {"status": 401, "message": "The access token expired"}}
            )

        m = request.method
        if m == "GET" and path == "/me":
            return httpx.Response(200, json=self.lib["me"])
        if m == "GET" and path == "/me/tracks":
            items = [
                {"added_at": e["added_at"], "track": self._track(e["track"])}
                for e in self.lib["liked"]
            ]
            return httpx.Response(200, json=self._page(request, items))
        if m == "GET" and path == "/me/playlists":
            items = []
            for p in self.lib["playlists"]:
                items.append(
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "description": p["description"],
                        "public": p["public"],
                        "collaborative": p["collaborative"],
                        "snapshot_id": p["snapshot_id"],
                        "owner": p["owner"],
                        "type": "playlist",
                        "uri": f"spotify:playlist:{p['id']}",
                        "items": {
                            "href": f"{API}/playlists/{p['id']}/items",
                            "total": len(p["entries"]),
                        },
                    }
                )
            return httpx.Response(200, json=self._page(request, items))
        if path.startswith("/playlists/") and path.endswith("/items"):
            pid = path.split("/")[2]
            try:
                p = self.playlist(pid)
            except StopIteration:
                return httpx.Response(404, json={"error": {"status": 404, "message": "Not found."}})
            if m == "GET":
                if p.get("forbidden"):
                    return httpx.Response(
                        403, json={"error": {"status": 403, "message": "Forbidden"}}
                    )
                key = "track" if p.get("legacy_track_field") else "item"
                items = []
                for k in p["entries"]:
                    obj = self._track(k)
                    items.append(
                        {
                            "added_at": "2026-05-01T00:00:00Z",
                            "added_by": {"id": p["owner"]["id"], "type": "user"},
                            "is_local": bool(obj and obj.get("is_local")),
                            key: obj,
                        }
                    )
                return httpx.Response(200, json=self._page(request, items))
            body = json.loads(request.content) if request.content else None
            self.write_bodies.append((m, path, body))
            if p["owner"]["id"] != self.lib["me"]["id"]:
                return httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
            entries = p["entries"]
            if m == "POST":
                uris = body["uris"]
                assert len(uris) <= 100
                pos = body.get("position")
                if pos is None:
                    entries.extend(uris)
                else:
                    entries[pos:pos] = uris
            elif m == "DELETE":
                assert len(body["items"]) <= 100
                gone = {x["uri"] for x in body["items"]}
                p["entries"] = [e for e in entries if (self._track(e) or {}).get("uri") not in gone]
            elif m == "PUT":
                assert len(body["uris"]) <= 100
                p["entries"] = list(body["uris"])
            self.snap_counter += 1
            p["snapshot_id"] = f"snap_{pid}_{self.snap_counter}"
            return httpx.Response(
                201 if m == "POST" else 200, json={"snapshot_id": p["snapshot_id"]}
            )
        if m == "POST" and path == "/me/playlists":
            body = json.loads(request.content)
            self.write_bodies.append((m, path, body))
            self.snap_counter += 1
            pid = f"pl_new{self.snap_counter}"
            new = {
                "id": pid,
                "name": body["name"],
                "description": body.get("description"),
                "public": body.get("public", True),
                "collaborative": False,
                "snapshot_id": f"snap_{pid}_0",
                "owner": self.lib["me"] | {"display_name": "Me"},
                "entries": [],
            }
            self.lib["playlists"].append(new)
            return httpx.Response(201, json={k: v for k, v in new.items() if k != "entries"})
        if m == "PUT" and path.startswith("/playlists/"):
            pid = path.split("/")[2]
            body = json.loads(request.content)
            self.write_bodies.append((m, path, body))
            p = self.playlist(pid)
            for k in ("name", "description", "public"):
                if k in body:
                    p[k] = body[k]
            return httpx.Response(200)
        return httpx.Response(404, json={"error": {"status": 404, "message": f"no route {path}"}})


class StaticTokens:
    """TokenProvider double: returns access-1, and access-2 after a forced refresh."""

    def __init__(self, token: str = "access-1") -> None:
        self.token = token
        self.refreshes = 0

    def access_token(self) -> str:
        return self.token

    def force_refresh(self) -> str:
        self.refreshes += 1
        self.token = f"access-{self.refreshes + 1}"
        return self.token
