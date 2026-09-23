#!/usr/bin/env python3
"""Tests for change parsing, novelty detection, the AI summary and message assembly (standard library only)."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import watch


def quiet():
    """Swallows the script's log lines so test output stays readable."""
    stack = contextlib.ExitStack()
    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
    stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
    return stack


class TestFindEntry(unittest.TestCase):
    def test_top_keys_lookup(self):
        parsed = {"gpt-6-sol": {"input_cost_per_token": 2.5e-06, "max_input_tokens": 400000}}
        src = {"extract": "top_keys"}
        entry = watch.find_entry(parsed, src, "gpt-6-sol")
        self.assertEqual(entry["max_input_tokens"], 400000)

    def test_key_lookup_nested(self):
        parsed = {"data": [{"id": "claude-opus-5", "limit": {"context": 200000}}]}
        src = {"extract": "key", "key": "id"}
        entry = watch.find_entry(parsed, src, "claude-opus-5")
        self.assertEqual(entry["limit"]["context"], 200000)

    def test_missing_id_returns_none(self):
        src = {"extract": "key", "key": "id"}
        self.assertIsNone(watch.find_entry({"data": []}, src, "nope"))

    def test_text_source_has_no_parsed_json(self):
        self.assertIsNone(watch.find_entry(None, {"extract": "regex"}, "gpt-6-sol"))


class TestSummarize(unittest.TestCase):
    def test_picks_price_and_context(self):
        entry = {"cost": {"input": 2.5, "output": 10}, "limit": {"context": 400000}, "name": "GPT-6 Sol"}
        out = watch.summarize(entry)
        self.assertIn("cost.input=2.5", out)
        self.assertIn("limit.context=400000", out)
        self.assertNotIn("name=", out)

    def test_picks_litellm_field_names(self):
        entry = {"input_cost_per_token": 2.5e-06, "max_input_tokens": 400000, "litellm_provider": "openai"}
        out = watch.summarize(entry)
        self.assertIn("input_cost_per_token=2.5e-06", out)
        self.assertIn("max_input_tokens=400000", out)

    def test_truncates_to_budget(self):
        entry = {f"cost_{i}": i for i in range(100)}
        out = watch.summarize(entry, budget=80)
        self.assertLessEqual(len(out), 80)

    def test_cache_fields_go_last(self):
        entry = {
            "cache_read_input_token_cost": 1.5e-06,
            "input_cost_per_token": 1.5e-05,
            "output_cost_per_token": 7.5e-05,
        }
        out = watch.summarize(entry)
        self.assertLess(out.index("input_cost_per_token="), out.index("cache_read_input_token_cost="))
        self.assertLess(out.index("output_cost_per_token="), out.index("cache_read_input_token_cost="))

    def test_entry_without_interesting_fields(self):
        self.assertEqual(watch.summarize({"name": "x", "owner": "y"}), "")

    def test_non_dict_entry(self):
        self.assertEqual(watch.summarize(None), "")
        self.assertEqual(watch.summarize("gpt-6-sol"), "")


class TestFormatChange(unittest.TestCase):
    def test_added_id_carries_details(self):
        parsed = {"gpt-6-sol": {"input_cost_per_token": 2.5e-06, "max_input_tokens": 400000}}
        src = {"name": "litellm-prices", "url": "https://example/x.json", "extract": "top_keys"}
        out = watch.format_change(src, ["gpt-6-sol"], [], parsed)
        self.assertIn("• litellm-prices", out)
        self.assertIn("+ gpt-6-sol", out)
        self.assertIn("max_input_tokens=400000", out)

    def test_removed_id_stays_plain(self):
        src = {"name": "openrouter", "url": "https://example/y"}
        out = watch.format_change(src, [], ["old-model"], None)
        self.assertIn("− old-model", out)
        self.assertNotIn("+", out.split("\n", 1)[1])

    def test_details_only_for_first_few(self):
        parsed = {f"gpt-{i}": {"max_input_tokens": 1000 + i} for i in range(watch.DETAIL_LIMIT + 3)}
        src = {"name": "n", "url": "u", "extract": "top_keys"}
        out = watch.format_change(src, sorted(parsed), [], parsed)
        self.assertEqual(out.count("max_input_tokens="), watch.DETAIL_LIMIT)

    def test_unknown_id_degrades_to_bare_line(self):
        src = {"name": "n", "url": "u", "extract": "top_keys"}
        out = watch.format_change(src, ["ghost"], [], {})
        self.assertIn("+ ghost", out)

    def test_novel_id_is_marked(self):
        src = {"name": "openrouter", "url": "u"}
        out = watch.format_change(src, ["openai/gpt-6-sol", "openai/gpt-5"], [], None, novel={"openai/gpt-6-sol"})
        self.assertIn("+ openai/gpt-6-sol 🆕", out)
        self.assertNotIn("gpt-5 🆕", out)

    def test_long_removal_list_is_collapsed(self):
        src = {"name": "litellm-prices", "url": "u"}
        removed = [f"old-{i:03d}" for i in range(watch.REMOVED_LIMIT + 136)]
        out = watch.format_change(src, [], removed, None)
        self.assertEqual(out.count("− old-"), watch.REMOVED_LIMIT)
        self.assertIn("… ещё 136", out)


class TestBaseName(unittest.TestCase):
    CASES = {
        "azureml://registries/azure-openai/models/gpt-6-sol": "gpt-6-sol",
        "azureml://registries/azureml-xai/models/grok-4-1-fast-reasoning/versions/3": "grok-4-1-fast-reasoning",
        "openai/gpt-6-sol-pro:batch": "gpt-6-sol-pro",
        "openrouter/sonoma-dusk-alpha": "sonoma-dusk-alpha",
        "anthropic/claude-opus-5.5": "claude-opus-5-5",
        "claude-opus-5-5": "claude-opus-5-5",
        "claude-haiku-4-5@20251001": "claude-haiku-4-5",
        "gpt-6-sol@eu": "gpt-6-sol",
        "us.anthropic.claude-opus-5-5": "claude-opus-5-5",
        "global.openai.gpt-6-luna": "gpt-6-luna",
        "bedrock/us-gov-east-1/anthropic.claude-opus-5-5": "claude-opus-5-5",
        "anthropic.claude-3-7-sonnet-20250219-v1:0": "claude-3-7-sonnet",
        "bedrock/us-west-2/1-month-commitment/anthropic.claude-v2:1": "claude-v2",
        "azure/gpt-5.4-pro-2026-03-05": "gpt-5-4-pro",
        "azure/us/gpt-5-nano-2025-08-07": "gpt-5-nano",
        "xai/grok-code-fast-1-0825": "grok-code-fast-1",
        "vertex_ai/gemini-2.0-flash": "gemini-2-0-flash",
        "@cf/openai/gpt-oss-120b": "gpt-oss-120b",
        "GPT-5.5": "gpt-5-5",
        "1024-x-1024/gpt-image-1.5": "gpt-image-1-5",
    }

    def test_real_ids_reduce_to_comparable_names(self):
        for raw, expected in self.CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(watch.base_name(raw), expected)


class TestIsKnown(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(watch.is_known("gpt-6-sol", {"gpt-6-sol"}))

    def test_provider_prefixed_variant_of_known_model(self):
        self.assertTrue(watch.is_known("databricks-gemini-2-5-flash", {"gemini-2-5-flash"}))
        self.assertTrue(watch.is_known("anthropic-claude-opus-4-8", {"claude-opus-4-8"}))

    def test_new_tier_of_known_model_is_new(self):
        self.assertFalse(watch.is_known("gpt-6-sol-pro", {"gpt-6-sol"}))

    def test_single_token_names_do_not_match_as_suffix(self):
        self.assertFalse(watch.is_known("codex-o3", {"o3"}))

    def test_unseen_name(self):
        self.assertFalse(watch.is_known("gpt-6-astra", {"gpt-6-sol", "gpt-6-luna"}))

    def test_serving_mode_of_known_model_is_known(self):
        self.assertTrue(watch.is_known("gpt-6-sol-fast", {"gpt-6-sol"}))
        self.assertTrue(watch.is_known("claude-opus-5-5-thinking", {"claude-opus-5-5"}))

    def test_serving_mode_of_unseen_model_is_new(self):
        self.assertFalse(watch.is_known("claude-opus-6-fast", {"claude-opus-5-5"}))

    def test_unversioned_alias_of_known_model_is_known(self):
        self.assertTrue(watch.is_known("grok-code-fast", {"grok-code-fast-1"}))

    def test_family_name_is_not_an_alias_of_a_named_model(self):
        self.assertFalse(watch.is_known("gpt-6", {"gpt-6-sol", "gpt-6-luna"}))


class TestLoadKnown(unittest.TestCase):
    def test_reads_every_ids_file(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "openrouter.ids.txt").write_text("openai/gpt-5\nanthropic/claude-opus-5.5\n", encoding="utf-8")
            Path(d, "litellm-prices.ids.txt").write_text("azure/us/gpt-5-nano-2025-08-07\n", encoding="utf-8")
            Path(d, "openrouter.json").write_text("{}", encoding="utf-8")
            self.assertEqual(watch.load_known(Path(d)), {"gpt-5", "claude-opus-5-5", "gpt-5-nano"})

    def test_missing_dir_means_nothing_known(self):
        self.assertEqual(watch.load_known(Path("/nonexistent/model-watch-data")), set())


def change(name, added, removed=(), parsed=None, extract="key", is_new=False, novel=(), count=None):
    src = {"name": name, "url": f"https://example/{name}", "extract": extract}
    return {"src": src, "added": list(added), "removed": list(removed), "parsed": parsed,
            "is_new": is_new, "novel": set(novel), "count": count if count is not None else len(added)}


class TestGroupNovel(unittest.TestCase):
    def test_same_model_across_sources_is_grouped(self):
        changes = [
            change("azure-foundry-playground", ["azureml://registries/azure-openai/models/gpt-6-sol"],
                   extract="regex", novel={"azureml://registries/azure-openai/models/gpt-6-sol"}),
            change("litellm-prices", ["gpt-6-sol", "azure/gpt-5"], extract="top_keys",
                   parsed={"gpt-6-sol": {"max_input_tokens": 1000000}, "azure/gpt-5": {}}, novel={"gpt-6-sol"}),
        ]
        groups = watch.group_novel(changes)
        self.assertEqual(list(groups), ["gpt-6-sol"])
        self.assertEqual([a["source"] for a in groups["gpt-6-sol"]], ["azure-foundry-playground", "litellm-prices"])
        self.assertEqual(groups["gpt-6-sol"][1]["details"], "max_input_tokens=1000000")

    def test_first_snapshot_is_not_novel(self):
        changes = [change("anthropic-api", [], is_new=True, count=12)]
        self.assertEqual(watch.group_novel(changes), {})


class TestBuildAiPayload(unittest.TestCase):
    def test_orders_by_breadth_and_caps(self):
        groups = {f"model-{i:02d}": [{"source": "feed-openai-codex", "id": f"model-{i:02d}"}]
                  for i in range(watch.AI_NOVEL_LIMIT + 5)}
        groups["gpt-6-sol"] = [{"source": s, "id": "gpt-6-sol"} for s in ("a", "b", "c")]
        payload = watch.build_ai_payload(groups, [])
        self.assertEqual(payload["novel"][0]["name"], "gpt-6-sol")
        self.assertEqual(payload["novel"][0]["sources"], ["a", "b", "c"])
        self.assertEqual(len(payload["novel"]), watch.AI_NOVEL_LIMIT)
        self.assertEqual(payload["more_novel"], 6)

    def test_caps_appearances_per_model(self):
        groups = {"claude-opus-5-5": [{"source": "models-dev", "id": f"v{i}"} for i in range(watch.AI_SEEN_IN_LIMIT + 5)]}
        entry = watch.build_ai_payload(groups, [])["novel"][0]
        self.assertEqual(len(entry["seen_in"]), watch.AI_SEEN_IN_LIMIT)
        self.assertEqual(entry["more_appearances"], 5)

    def test_other_changes_summarise_known_additions_and_removals(self):
        changes = [
            change("litellm-prices", ["azure/gpt-5", "gpt-6-sol"], removed=[f"old-{i}" for i in range(146)],
                   novel={"gpt-6-sol"}),
            change("anthropic-api", [], is_new=True, count=12),
            change("openrouter", []),
        ]
        other = {o["source"]: o for o in watch.build_ai_payload({}, changes)["other_changes"]}
        self.assertEqual(other["litellm-prices"]["added_known"], 1)
        self.assertEqual(other["litellm-prices"]["removed"], 146)
        self.assertEqual(other["litellm-prices"]["examples_added"], ["azure/gpt-5"])
        self.assertEqual(len(other["litellm-prices"]["examples_removed"]), 3)
        self.assertEqual(other["anthropic-api"], {"source": "anthropic-api", "first_snapshot": True, "ids": 12})
        self.assertNotIn("openrouter", other)


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.calls, self.response, self.error = [], response, error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(**kwargs):
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(**kwargs)))


def fake_response(text="Похоже на релиз GPT-6.", stop_reason="end_turn", model="claude-opus-5"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason, stop_details=None, model=model,
        usage=SimpleNamespace(input_tokens=1200, output_tokens=300),
    )


class TestAiSummary(unittest.TestCase):
    PAYLOAD = {"novel": [{"name": "gpt-6-sol", "sources": ["litellm-prices"], "seen_in": []}], "more_novel": 0,
               "other_changes": []}

    def test_off_without_key(self):
        with quiet():
            self.assertIsNone(watch.ai_summary(self.PAYLOAD, api_key=None))

    def test_request_shape(self):
        client = fake_client(response=fake_response())
        with quiet():
            watch.ai_summary(self.PAYLOAD, api_key="k", client=client)
        call = client.beta.messages.calls[0]
        self.assertEqual(call["model"], "claude-opus-5")
        self.assertEqual(call["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(call["fallbacks"], "default")
        self.assertEqual(call["output_config"], {"effort": "low"})
        self.assertEqual(call["max_tokens"], watch.AI_MAX_TOKENS)
        self.assertEqual(call["system"], watch.AI_SYSTEM_PROMPT)
        self.assertEqual(call["messages"][0]["role"], "user")
        sent = call["messages"][0]["content"]
        self.assertEqual(json.loads(sent[sent.index("{"):]), self.PAYLOAD)

    def test_returns_text_blocks_only(self):
        client = fake_client(response=fake_response(text="  Похоже на релиз GPT-6.  "))
        with quiet():
            self.assertEqual(watch.ai_summary(self.PAYLOAD, api_key="k", client=client), "Похоже на релиз GPT-6.")

    def test_refusal_falls_back_to_plain_report(self):
        client = fake_client(response=fake_response(text="", stop_reason="refusal"))
        with quiet():
            self.assertIsNone(watch.ai_summary(self.PAYLOAD, api_key="k", client=client))

    def test_api_error_never_raises(self):
        client = fake_client(error=RuntimeError("boom"))
        with quiet():
            self.assertIsNone(watch.ai_summary(self.PAYLOAD, api_key="k", client=client))

    def test_empty_text_is_no_summary(self):
        client = fake_client(response=fake_response(text="   "))
        with quiet():
            self.assertIsNone(watch.ai_summary(self.PAYLOAD, api_key="k", client=client))

    def test_logs_tokens_and_cost(self):
        client = fake_client(response=fake_response())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            watch.ai_summary(self.PAYLOAD, api_key="k", client=client)
        self.assertIn("input=1200 output=300", out.getvalue())
        self.assertIn("$0.0135", out.getvalue())  # 1200 * $5/M + 300 * $25/M


class TestFormatMessage(unittest.TestCase):
    BLOCKS = ["• litellm-prices (u)\n  + gpt-6-sol 🆕"]

    def test_novel_with_summary_is_loud_and_leads_with_it(self):
        text, silent = watch.format_message(self.BLOCKS, ["gpt-6-sol"], "Похоже на релиз GPT-6.")
        self.assertFalse(silent)
        self.assertTrue(text.startswith("model-watch: 🆕"))
        self.assertLess(text.index("Похоже на релиз"), text.index("• litellm-prices"))

    def test_novel_without_summary_names_the_models(self):
        text, silent = watch.format_message(self.BLOCKS, ["gpt-6-luna", "gpt-6-sol"], None)
        self.assertFalse(silent)
        self.assertIn("gpt-6-luna, gpt-6-sol", text.split("\n", 1)[0])

    def test_nothing_novel_is_silent(self):
        text, silent = watch.format_message(["• litellm-prices (u)\n  + azure/gpt-5"], [], None)
        self.assertTrue(silent)
        self.assertIn("новых моделей нет", text.split("\n", 1)[0])


class TestSendTelegram(unittest.TestCase):
    def sent_payload(self, **kwargs):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch.object(watch.urllib.request, "urlopen") as urlopen:
            watch.send_telegram("hello", **kwargs)
        return json.loads(urlopen.call_args[0][0].data.decode("utf-8"))

    def test_silent_message_disables_notification(self):
        self.assertIs(self.sent_payload(silent=True)["disable_notification"], True)

    def test_loud_by_default(self):
        self.assertNotIn("disable_notification", self.sent_payload())


class TestAiSelftest(unittest.TestCase):
    def test_sends_one_summary_of_the_recorded_example(self):
        client = fake_client(response=fake_response(text="Похоже на релиз GPT-6 и Claude Opus 5.5."))
        with quiet(), mock.patch.object(watch, "send_telegram") as send:
            code = watch.run_ai_selftest(api_key="k", client=client)
        self.assertEqual(code, 0)
        payload = json.loads(client.beta.messages.calls[0]["messages"][0]["content"].split("\n", 1)[1])
        names = {n["name"] for n in payload["novel"]}
        self.assertTrue({"gpt-6-sol", "gpt-6-luna", "claude-opus-5-5"} <= names)
        self.assertNotIn("gpt-5-4", names)  # known before the recorded run
        text = send.call_args[0][0]
        self.assertIn("самотест", text.split("\n", 1)[0])
        self.assertIn("Похоже на релиз GPT-6 и Claude Opus 5.5.", text)

    def test_fails_loudly_without_a_summary(self):
        with quiet(), mock.patch.object(watch, "send_telegram") as send:
            self.assertEqual(watch.run_ai_selftest(api_key=None), 1)
        send.assert_not_called()


class TestMainEndToEnd(unittest.TestCase):
    """Runs main() against a throwaway git repo with file:// sources: the real fetch → diff → message path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "data").mkdir()
        self.catalog = root / "catalog.json"
        self.prices = root / "prices.json"
        sources = [
            {"name": "catalog-a", "url": self.catalog.as_uri(), "kind": "json", "extract": "key", "key": "id"},
            {"name": "prices-b", "url": self.prices.as_uri(), "kind": "json", "extract": "top_keys"},
        ]
        (root / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
        (root / "data" / "catalog-a.ids.txt").write_text("gpt-5\n", encoding="utf-8")
        (root / "data" / "prices-b.ids.txt").write_text("claude-opus-4-8\n", encoding="utf-8")
        for args in (["init", "-q"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"]):
            subprocess.run(["git", *args], cwd=root, check=True)
        self.patches = [mock.patch.object(watch, "ROOT", root), mock.patch.object(watch, "DATA", root / "data"),
                        mock.patch.object(watch, "SOURCES", root / "sources.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_main(self, catalog, prices):
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        self.prices.write_text(json.dumps(prices), encoding="utf-8")
        with quiet(), mock.patch.object(watch, "send_telegram") as send, \
                mock.patch.object(watch, "ai_summary", return_value="Похоже на релиз GPT-6.") as ai:
            watch.main([])
        return send, ai

    def test_new_model_gets_an_ai_summary_and_a_loud_alert(self):
        send, ai = self.run_main(
            {"data": [{"id": "gpt-5"}, {"id": "gpt-6-sol", "limit": {"context": 1000000}}]},
            {"claude-opus-4-8": {}, "azure/gpt-5": {"max_input_tokens": 400000}},
        )
        payload = ai.call_args[0][0]
        self.assertEqual([n["name"] for n in payload["novel"]], ["gpt-6-sol"])
        self.assertEqual(payload["novel"][0]["seen_in"][0]["details"], "limit.context=1000000")
        text = send.call_args[0][0]
        self.assertFalse(send.call_args[1].get("silent", False))
        self.assertTrue(text.startswith("model-watch: 🆕"))
        self.assertIn("Похоже на релиз GPT-6.", text)
        self.assertIn("+ gpt-6-sol 🆕", text)
        self.assertIn("+ azure/gpt-5\n", text + "\n")
        self.assertNotIn("azure/gpt-5 🆕", text)

    def test_known_models_in_new_places_skip_the_ai_and_stay_silent(self):
        send, ai = self.run_main(
            {"data": [{"id": "gpt-5"}]},
            {"claude-opus-4-8": {}, "azure/gpt-5": {}, "bedrock/us.anthropic.claude-opus-4-8": {}},
        )
        ai.assert_not_called()
        self.assertTrue(send.call_args[1]["silent"])
        self.assertIn("новых моделей нет", send.call_args[0][0])

    def test_no_changes_send_nothing(self):
        send, ai = self.run_main({"data": [{"id": "gpt-5"}]}, {"claude-opus-4-8": {}})
        send.assert_not_called()
        ai.assert_not_called()


if __name__ == "__main__":
    unittest.main()
