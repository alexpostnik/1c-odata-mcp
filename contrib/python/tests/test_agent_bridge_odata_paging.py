"""Synthetic OData v3 paging and continuation boundaries for the configured policy."""
from __future__ import annotations

import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "agent_bridge"))

from agent_bridge import odata
from agent_bridge.core import Bridge


_METADATA = b'''<Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
<EntityType Name="Doc"><Property Name="Ref_Key"/><Property Name="Date"/></EntityType>
<EntityContainer Name="Default"><EntitySet Name="Document_Test" EntityType="Test.Doc"/>
</EntityContainer></Schema>'''
_PROFILE = {
    "serviceUrl": "https://example.invalid/odata/standard.odata/",
    "username": "odata-readonly",
}


class ODataPagingTests(unittest.TestCase):
    def test_bridge_reuses_metadata_for_pages_and_explicit_metadata_refreshes_it(self):
        bridge = Bridge({
            "targets": {"test": {**_PROFILE, "environment": "test"}},
        })
        calls = []

        def fake_get(_client, url):
            calls.append(url)
            if url.endswith("$metadata"):
                return _METADATA
            return b'{"d":{"results":[{"Ref_Key":"synthetic"}]}}'

        with patch("agent_bridge.odata._get", side_effect=fake_get):
            for offset in range(10):
                bridge.odata_read_page(
                    "test", "Document_Test", limit=1,
                    cursor=None if offset == 0 else str(offset),
                )

            self.assertEqual(sum(url.endswith("$metadata") for url in calls), 1)
            self.assertEqual(sum(not url.endswith("$metadata") for url in calls), 10)

            refreshed = bridge.odata_metadata("test")
            self.assertEqual(len(refreshed["entitySets"]), 1)
            bridge.odata_read_page("test", "Document_Test", limit=1)

        self.assertEqual(sum(url.endswith("$metadata") for url in calls), 2)
        self.assertEqual(sum(not url.endswith("$metadata") for url in calls), 11)

    def test_bridge_keeps_caches_separate_by_target_alias(self):
        bridge = Bridge({
            "targets": {
                "test": {**_PROFILE, "environment": "test", "username": "test-reader"},
                "prod": {**_PROFILE, "environment": "prod", "username": "prod-reader"},
            },
        })
        calls = []

        def fake_get(_client, url):
            calls.append(url)
            if url.endswith("$metadata"):
                return _METADATA
            return b'{"d":{"results":[]}}'

        with patch("agent_bridge.odata._get", side_effect=fake_get):
            bridge.odata_read_page("test", "Document_Test", limit=1)
            bridge.odata_read_page("prod", "Document_Test", limit=1)

        self.assertIsNot(bridge._odata_client("test"), bridge._odata_client("prod"))
        self.assertEqual(sum(url.endswith("$metadata") for url in calls), 2)

    def test_profile_client_reuses_http_session_and_keychain_credential(self):
        class FakeResponse:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_content(self, chunk_size):
                self.chunk_size = chunk_size
                yield b"{}"

        class FakeSession:
            def __init__(self):
                self.auth = None
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse()

        sessions = []

        def session_factory():
            session = FakeSession()
            sessions.append(session)
            return session

        client = odata.ODataClient(_PROFILE, target_alias="test", session_factory=session_factory)
        url = _PROFILE["serviceUrl"] + "$metadata"
        with patch("agent_bridge.odata._password", return_value="synthetic-password") as password:
            self.assertEqual(odata._get(client, url), b"{}")
            self.assertEqual(odata._get(client, url), b"{}")

        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0].calls), 2)
        self.assertEqual(password.call_count, 1)
        self.assertEqual(sessions[0].auth, ("odata-readonly", "synthetic-password"))
        self.assertTrue(all(call[1]["allow_redirects"] is False for call in sessions[0].calls))
        self.assertTrue(all(call[1]["verify"] is True for call in sessions[0].calls))

    def test_five_rows_limit_two_without_server_next_link(self):
        rows = [{"Ref_Key": str(n)} for n in range(1, 6)]
        requests = []

        def fake_get(_profile, url):
            if url.endswith("$metadata"):
                return _METADATA
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            requests.append(query)
            start = int(query.get("$skip", ["0"])[0])
            top = int(query["$top"][0])
            return json.dumps({"d": {"results": rows[start:start + top]}}).encode()

        found = []
        cursor = None
        with patch("agent_bridge.odata._get", side_effect=fake_get):
            for _ in range(4):
                page = odata.read_page(_PROFILE, "Document_Test", limit=2,
                                       from_date="2012-01-01", to_exclusive="2013-01-01",
                                       date_property="Date", cursor=cursor)
                found.extend(row["Ref_Key"] for row in page["rows"])
                cursor = page["nextCursor"]
                if not page["hasMore"]:
                    break
        self.assertEqual(found, ["1", "2", "3", "4", "5"])
        self.assertIsNone(cursor)
        self.assertEqual([q.get("$skip", ["0"])[0] for q in requests], ["0", "2", "4"])
        self.assertEqual({q["$filter"][0] for q in requests},
                         {"Date ge datetime'2012-01-01T00:00:00' and Date lt datetime'2013-01-01T00:00:00'"})
        self.assertEqual({q["$orderby"][0] for q in requests}, {"Ref_Key asc"})
        self.assertEqual({q["$top"][0] for q in requests}, {"2"})

    def test_exact_multiple_uses_empty_probe(self):
        urls = []
        def fake_get(_profile, url):
            if url.endswith("$metadata"):
                return _METADATA
            urls.append(url)
            start = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("$skip", ["0"])[0])
            values = [{"Ref_Key": str(n)} for n in range(4)][start:start + 2]
            return json.dumps({"d": {"results": values}}).encode()
        with patch("agent_bridge.odata._get", side_effect=fake_get):
            first = odata.read_page(_PROFILE, "Document_Test", limit=2)
            second = odata.read_page(_PROFILE, "Document_Test", limit=2, cursor=first["nextCursor"])
            third = odata.read_page(_PROFILE, "Document_Test", limit=2, cursor=second["nextCursor"])
        self.assertEqual([first["nextCursor"], second["nextCursor"], third["nextCursor"]],
                         ["2", "4", None])
        self.assertEqual(len(urls), 3)

    def test_json_light_next_link_and_restarted_numeric_cursor(self):
        next_link = ("https://example.invalid/odata/standard.odata/Document_Test"
                     "?$top=2&$skip=2")
        calls = []
        def fake_get(_profile, url):
            if url.endswith("$metadata"):
                return _METADATA
            calls.append(url)
            if len(calls) == 1:
                return json.dumps({"value": [{"Ref_Key": "a"}, {"Ref_Key": "b"}],
                                   "odata.nextLink": next_link}).encode()
            return b'{"value":[{"Ref_Key":"c"}]}'
        with patch("agent_bridge.odata._get", side_effect=fake_get):
            first = odata.read_page(_PROFILE, "Document_Test", limit=2)
            self.assertEqual(first["nextCursor"], "2")
            # The offset is sufficient to resume in another process with the same validated query.
            second = odata.read_page(_PROFILE, "Document_Test", limit=2, cursor="2")
        self.assertEqual(second["rows"][0]["Ref_Key"], "c")
        self.assertIn("%24skip=2", calls[1])

    def test_cursor_and_server_link_tampering_fail_closed(self):
        with patch("agent_bridge.odata._get", return_value=_METADATA):
            for bad in ("-1", "2x", "10000001", "https://example.invalid/", "", " 2"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    odata.read_page(_PROFILE, "Document_Test", limit=2, cursor=bad)
        for link in (
            "https://attacker.invalid/odata/standard.odata/Document_Test?$skip=2",
            "https://example.invalid/odata/standard.odata/Other?$skip=2",
            "?$top=200&$skip=2",
            "?$top=1&$skip=2",
            "?$skip=3",
            "?$skiptoken=opaque",
            "?$filter=Date%20eq%20datetime%272012-01-01T00:00:00%27&$skip=2",
            "?$orderby=Date%20asc&$skip=2",
        ):
            def fake_get(_profile, url):
                if url.endswith("$metadata"):
                    return _METADATA
                return json.dumps({"d": {"results": [{"Ref_Key": "a"}, {"Ref_Key": "b"}],
                                         "__next": link}}).encode()
            with self.subTest(link=link), patch("agent_bridge.odata._get", side_effect=fake_get):
                with self.assertRaises(odata.ODataError):
                    odata.read_page(_PROFILE, "Document_Test", limit=2,
                                    from_date="2012-01-01", to_exclusive="2013-01-01",
                                    date_property="Date")

    def test_empty_page_cannot_return_nonadvancing_next_link(self):
        def fake_get(_profile, url):
            if url.endswith("$metadata"):
                return _METADATA
            return b'{"d":{"results":[],"__next":"?$skip=0&$top=2"}}'
        with patch("agent_bridge.odata._get", side_effect=fake_get):
            with self.assertRaisesRegex(odata.ODataError, "does not advance"):
                odata.read_page(_PROFILE, "Document_Test", limit=2)


if __name__ == "__main__":
    unittest.main()
