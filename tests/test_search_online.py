from __future__ import annotations

import logging
import sys
import types
import unittest
from unittest.mock import patch


def _install_astrbot_stubs() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.api.message_components")

    class AstrBotConfig(dict):
        def save_config(self):
            self["_saved"] = True

    class Filter:
        @staticmethod
        def command(_name):
            return lambda function: function

        @staticmethod
        def llm_tool(**_kwargs):
            return lambda function: function

    class Star:
        def __init__(self, context=None):
            self.context = context

    api.AstrBotConfig = AstrBotConfig
    api.logger = logging.getLogger("test-danbooru")
    api.message_components = components
    event.AstrMessageEvent = object
    event.filter = Filter
    star.Context = object
    star.Star = Star
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.api.message_components": components,
        }
    )


_install_astrbot_stubs()

import main  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


def make_plugin(**overrides):
    config = main.SEARCH_API_DEFAULTS | overrides
    plugin = object.__new__(main.DanbooruPlugin)
    plugin.config = {main.SEARCH_API_SECTION: config, "enable_chinese_lookup": True}
    plugin.chinese_lookup_cache = {}
    plugin.tag_lookup_cache = {}
    return plugin


class ConfidenceTests(unittest.TestCase):
    def test_high_confidence_directly_resolves(self):
        plugin = make_plugin(high_confidence_threshold=0.78)
        candidates = [
            {"name": "hatsune_miku", "semantic_score": 1.0, "final_score": 0.96},
            {"name": "hatsune_miku_(heartbeat)", "semantic_score": 1.0, "final_score": 0.90},
        ]
        official, ambiguous, suggestions = plugin._pick_chinese_official("初音未来", candidates)
        self.assertEqual(official, "hatsune_miku")
        self.assertFalse(ambiguous)
        self.assertEqual(suggestions, candidates)

    def test_medium_confidence_returns_configured_candidates(self):
        plugin = make_plugin(high_confidence_threshold=0.78, candidate_limit=7)
        candidates = [
            {"name": f"candidate_{index}", "semantic_score": 0.7 - index / 100}
            for index in range(10)
        ]
        official, ambiguous, suggestions = plugin._pick_chinese_official("模糊词", candidates)
        self.assertIsNone(official)
        self.assertTrue(ambiguous)
        self.assertEqual(len(suggestions), 7)

    def test_multiple_full_semantic_scores_need_a_clear_lead(self):
        plugin = make_plugin(high_confidence_threshold=0.78, high_confidence_margin=0.05)
        candidates = [
            {"name": "alice_one", "semantic_score": 1.0, "final_score": 0.93},
            {"name": "alice_two", "semantic_score": 1.0, "final_score": 0.91},
        ]
        official, ambiguous, _suggestions = plugin._pick_chinese_official("爱丽丝", candidates)
        self.assertIsNone(official)
        self.assertTrue(ambiguous)

    def test_admin_parameter_validation(self):
        plugin = make_plugin()
        self.assertEqual(plugin._parse_search_api_param("candidate_limit", "9"), 9)
        self.assertFalse(plugin._parse_search_api_param("use_segmentation", "关"))
        with self.assertRaises(ValueError):
            plugin._parse_search_api_param("candidate_limit", "4")


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_chinese_search_uses_search_endpoint_and_scores(self):
        plugin = make_plugin()
        fake = FakeClient(
            {
                "results": [
                    {
                        "tag": "serafuku",
                        "cn_name": "水手服",
                        "category": "General",
                        "count": 12345,
                        "final_score": 0.88,
                        "semantic_score": 0.93,
                        "source": "水手服",
                        "layer": "中文核心词",
                    }
                ]
            }
        )
        with patch.object(main.httpx, "AsyncClient", return_value=fake):
            result = await plugin._fetch_chinese_candidates("水手服")
        self.assertEqual(fake.calls[0][0], f"{main.DEFAULT_SEARCH_API_BASE_URL}/search")
        self.assertEqual(fake.calls[0][1]["json"]["query"], "水手服")
        self.assertEqual(result[0]["name"], "serafuku")
        self.assertEqual(result[0]["semantic_score"], 0.93)

    async def test_artist_lookup_only_uses_artists_endpoint(self):
        plugin = make_plugin()
        fake = FakeClient({"results": [{"artist": "example_artist"}]})
        with patch.object(main.httpx, "AsyncClient", return_value=fake):
            result = await plugin._fetch_recommended_artists(["flat_color", "1girl"])
        self.assertEqual(result["results"][0]["artist"], "example_artist")
        self.assertEqual(fake.calls[0][0], f"{main.DEFAULT_SEARCH_API_BASE_URL}/artists")
        self.assertEqual(fake.calls[0][1]["json"]["tags"], ["flat_color", "1girl"])
        self.assertEqual(len(fake.calls), 1)

    async def test_cold_start_transport_error_is_retried(self):
        plugin = make_plugin(cold_start_retries=1, cold_start_retry_delay_seconds=0)
        first = FakeClient(error=main.httpx.ConnectError("Space is waking"))
        second = FakeClient(payload={"results": []})
        with patch.object(main.httpx, "AsyncClient", side_effect=[first, second]):
            payload = await plugin._post_search_online("search", {"query": "水手服"})
        self.assertEqual(payload, {"results": []})
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)


if __name__ == "__main__":
    unittest.main()
