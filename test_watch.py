#!/usr/bin/env python3
"""Тесты для разбора и форматирования изменений (стандартная библиотека, без зависимостей)."""

import unittest

import watch


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


if __name__ == "__main__":
    unittest.main()
