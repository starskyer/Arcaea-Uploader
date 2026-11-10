from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arcaea_uploader.api_client import ApiError
from arcaea_uploader.planner import PlanError
from arcaea_uploader.server_tasks import (OPTIONS, PAST_FROM, PAST_TO, RATING_OPERATION,
    RESOURCE_OPERATIONS, confirm_uploaded, load_task, make_task, missing_constants,
    parse_constant, purchase_specs, read_reviewed, run_task, save_task, task_lock, task_path, analyze_purchases,
    confirm_purchase_skips, set_purchase_override, task_complete)


class FakeAPI:
    base_url = "https://example.test/api/v1"

    def __init__(self):
        self.items, self.purchases, self.songs = {}, {}, {}
        self.calls, self.fail_after = [], None

    def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        parts = path.strip("/").split("/")
        if method == "GET":
            if path == "/system/operations":
                return [*RESOURCE_OPERATIONS, RATING_OPERATION]
            if parts[0] == "items":
                value = self.items.get((parts[1], parts[2]))
            elif parts[0] == "purchases":
                value = self.purchases.get(parts[1])
                if value is not None and len(parts) == 3:
                    return deepcopy(value["items"])
            elif parts[0] == "songs":
                value = self.songs.get(parts[1])
            else:
                raise AssertionError(path)
            if value is None:
                raise ApiError(method, path, 200, -121 if parts[0] == "items" else -2)
            return deepcopy(value)
        if path.startswith("/system/operations/"):
            value = {}
        elif parts[0] == "items":
            key = (body["type"], body["item_id"])
            if key in self.items:
                raise ApiError(method, path, 200, -122)
            value = self.items[key] = {"id": key[1], "type": key[0], "is_available": int(body["is_available"])}
        elif parts[0] == "purchases":
            if method == "POST":
                name = body["purchase_name"]
                if name in self.purchases:
                    raise ApiError(method, path, 200, -4)
                value = self.purchases[name] = {"name": name, **deepcopy(body), "items": []}
            else:
                value = self.purchases[parts[1]]
                for item in body["create"]:
                    value["items"].append(deepcopy(item))
        elif parts[0] == "songs":
            if method == "POST":
                if body["song_id"] in self.songs:
                    raise ApiError(method, path, 200, -4)
                value = self.songs[body["song_id"]] = deepcopy(body)
            else:
                value = self.songs[parts[1]]
                if "name" in body:
                    value["name"] = body["name"]
                current = {c["difficulty"]: c["chart_const"] for c in value["charts"]}
                current.update({c["difficulty"]: c["chart_const"] for c in body.get("charts", [])})
                value["charts"] = [{"difficulty": c, "chart_const": current[c]} for c in range(5)]
        else:
            raise AssertionError(path)
        if self.fail_after == (method, path):
            self.fail_after = None
            raise ApiError(method, path)  # The server committed, response lost.
        return deepcopy(value)

    def get_optional(self, path):
        try:
            return self.request("GET", path)
        except ApiError as error:
            if error.missing:
                return None
            raise

    def writes(self):
        return [(m, p, body) for m, p, body in self.calls if m != "GET"]


class ServerTaskTests(unittest.TestCase):
    def test_purchase_preview_joins_catalog_rows_groups_packs_and_keeps_errors_visible(self):
        rows = [{"id": "a", "title": "A", "set": "p", "purchase": "a"},
                {"id": "b", "title": "B", "set": "p", "purchase": "p"},
                {"id": "c", "title": "C", "set": "p", "purchase": "p"},
                {"id": "free", "purchase": ""}, {"id": "free2", "purchase": None},
                {"id": "invalid", "set": "p", "purchase": "null"}]
        analysis = analyze_purchases({"songs": rows})
        self.assertEqual(len(analysis["purchases"]), 2)
        pack = next(entry for entry in analysis["purchases"] if entry["spec"]["kind"] == "pack")
        self.assertEqual(pack["spec"]["price"], 500)
        self.assertEqual(pack["item"], {"item_id": "p", "type": "pack", "is_available": True})
        self.assertEqual(pack["purchase_item"], {"purchase_name": "p", "item_id": "p", "type": "pack", "amount": 1})
        self.assertEqual([row["id"] for row in pack["songs"]], ["b", "c"])
        self.assertFalse(pack["discount_active"])
        self.assertFalse(pack["vouchers_allowed"])
        self.assertEqual(len(analysis["free_songs"]), 2)
        self.assertEqual(analysis["invalid_songs"][0]["id"], "invalid")
        with self.assertRaises(PlanError):
            purchase_specs({"songs": rows})

    def test_purchase_preview_rejects_shared_name_with_different_categories(self):
        analysis = analyze_purchases({"songs": [{"id": "same", "set": "pack", "purchase": "same"},
                                               {"id": "other", "set": "same", "purchase": "same"}]})
        self.assertEqual(analysis["purchases"], [])
        self.assertEqual(len(analysis["invalid_songs"]), 2)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "songlist"
        self.api = FakeAPI()

    def tearDown(self):
        self.temp.cleanup()

    def task(self, rows=None, actual=None):
        rows = rows or [{"id": "a", "title": "Fallback", "title_localized": {"en": "English"},
                         "set": "pack", "purchase": "a", "rating": 11}]
        actual = actual or {r["id"]: [2] for r in rows}
        document = {"songs": rows}
        self.source.write_text(json.dumps(document), encoding="utf-8")
        task = read_reviewed(make_task("batch", document, actual), self.source)
        confirm_uploaded(task)
        return task

    def test_exact_operation_order_prices_and_no_vouchers(self):
        rows = [{"id": "a", "title": "A", "set": "p", "purchase": "a"},
                {"id": "b", "title": "B", "set": "p", "purchase": "p"},
                {"id": "c", "title": "C", "set": "p", "purchase": "p"}]
        task = self.task(rows)
        for sid in task["actual"]:
            task["constants"][sid]["2"] = 9.7
        run_task(task, self.api)
        writes = self.api.writes()
        self.assertEqual([p for _, p, _ in writes[:3]], ["/system/operations/" + n for n in RESOURCE_OPERATIONS])
        self.assertEqual([(m, p) for m, p, _ in writes[3:9]], [
            ("POST", "/items"), ("POST", "/purchases"), ("PATCH", "/purchases/a/items"),
            ("POST", "/items"), ("POST", "/purchases"), ("PATCH", "/purchases/p/items")])
        self.assertEqual(writes[-1][1], "/system/operations/" + RATING_OPERATION)
        self.assertEqual(len(self.api.purchases), 2)
        for name, price, kind in (("a", 100, "single"), ("p", 500, "pack")):
            purchase = self.api.purchases[name]
            self.assertEqual((purchase["price"], purchase["orig_price"]), (price, price))
            self.assertEqual((purchase["discount_from"], purchase["discount_to"]), (PAST_FROM, PAST_TO))
            self.assertEqual(purchase["discount_reason"], "")
            self.assertEqual(purchase["items"], [{"id": name, "type": kind, "amount": 1}])

    def test_missing_constants_require_confirmation_preserve_old_and_new_unknown(self):
        task = self.task(actual={"a": [2, 3]})
        with self.assertRaises(PlanError):
            run_task(task, self.api)
        self.assertEqual(self.api.calls, [])
        self.api.songs["a"] = {"song_id": "a", "name": "English", "charts": [
            {"difficulty": i, "chart_const": 9.7 if i == 2 else -1} for i in range(5)]}
        run_task(task, self.api, allow_missing=True)
        values = [c["chart_const"] for c in self.api.songs["a"]["charts"]]
        self.assertEqual(values, [-1, -1, 9.7, -1, -1])
        self.assertEqual(missing_constants(task), ["a/2", "a/3"])
        other = self.task()
        api = FakeAPI()
        run_task(other, api, allow_missing=True)
        self.assertEqual([c["chart_const"] for c in api.songs["a"]["charts"]], [-1] * 5)
        self.assertEqual(api.songs["a"]["name"], "English")

    def test_existing_purchase_is_preserved_even_when_prices_differ(self):
        task = self.task()
        set_purchase_override(task, ["a"], "custom", name="a", kind="single", price=200)
        self.api.purchases["a"] = {"name": "a", "price": 77, "orig_price": 99, "items": []}
        run_task(task, self.api, allow_missing=True)
        self.assertEqual(self.api.purchases["a"]["price"], 77)
        self.assertEqual(self.api.items, {})
        self.assertFalse(any(path.startswith("/purchases") for _, path, _ in self.api.writes()))

    def test_confirmed_uninferred_purchases_are_warned_and_other_operations_continue(self):
        task = self.task(rows=[{"id": "special", "title": "Special", "set": "p", "purchase": "null"},
                               {"id": "valid", "title": "Valid", "set": "p", "purchase": "valid"}])
        before = self.source.read_bytes()
        with self.assertRaisesRegex(PlanError, "弹窗中确认"):
            run_task(task, self.api, allow_missing=True)
        self.assertEqual(self.api.calls, [])
        confirm_purchase_skips(task)
        logs = []
        run_task(task, self.api, allow_missing=True, on_log=lambda level, message: logs.append((level, message)))
        self.assertEqual(set(self.api.purchases), {"valid"})
        self.assertEqual(set(self.api.songs), {"special", "valid"})
        self.assertTrue(any(level == "warning" and "跳过 special" in message for level, message in logs))
        self.assertEqual(self.api.writes()[-1][1], "/system/operations/" + RATING_OPERATION)
        self.assertEqual(self.source.read_bytes(), before)
        path = task_path(self.root, task["id"])
        save_task(path, task)
        self.assertTrue(task_complete(load_task(path)))

    def test_purchase_skip_confirmation_does_not_survive_changed_review(self):
        task = self.task(rows=[{"id": "a", "title": "A", "set": "p", "purchase": "null"}])
        confirm_purchase_skips(task)
        self.source.write_text(json.dumps({"songs": [{"id": "a", "title": "A", "set": "p", "purchase": "another"}]}), encoding="utf-8")
        task = read_reviewed(task, self.source)
        confirm_uploaded(task)
        with self.assertRaisesRegex(PlanError, "弹窗中确认"):
            run_task(task, self.api, allow_missing=True)
        self.assertEqual(self.api.calls, [])

    def test_manual_purchase_edits_persist_across_review_and_invalidate_completion(self):
        task = self.task(rows=[{"id": "a", "title": "A", "set": "p", "purchase": "p"},
                               {"id": "b", "title": "B", "set": "p", "purchase": "p"}])
        before = self.source.read_bytes()
        set_purchase_override(task, ["a", "b"], "custom", name="p", kind="pack", price="650")
        path = task_path(self.root, task["id"])
        save_task(path, task)
        task = read_reviewed(load_task(path), self.source)
        self.assertEqual(len(task["purchase_candidates"]), 1)
        self.assertEqual(task["purchase_candidates"][0]["price"], 650)
        run_task(task, self.api, allow_missing=True)
        self.assertEqual((self.api.purchases["p"]["price"], self.api.purchases["p"]["orig_price"]), (650, 650))
        self.assertTrue(task_complete(task))
        set_purchase_override(task, ["a", "b"], "auto")
        self.assertFalse(task_complete(task))
        self.assertEqual(task["purchase_candidates"][0]["price"], 500)
        self.assertEqual(task["purchase_overrides"], {})
        self.assertEqual(self.source.read_bytes(), before)

    def test_manual_skip_needs_confirmation_and_invalid_custom_values_do_not_change_plan(self):
        task = self.task()
        for price in (True, -1, "9.5", 2147483648):
            with self.subTest(price=price), self.assertRaises(PlanError):
                set_purchase_override(task, ["a"], "custom", name="a", kind="single", price=price)
        self.assertEqual(task["purchase_overrides"], {})
        set_purchase_override(task, ["a"], "skip")
        self.assertEqual(task["purchase_preview"]["skipped_songs"][0]["id"], "a")
        with self.assertRaises(PlanError):
            run_task(task, self.api, allow_missing=True)
        confirm_purchase_skips(task)
        run_task(task, self.api, allow_missing=True)
        self.assertFalse(self.api.purchases)
        self.assertIn("a", self.api.songs)

    def test_gateway_timeout_keeps_refresh_uncertain_and_resumes_after_completed_steps(self):
        task = self.task()
        failed_path = "/system/operations/refresh_song_file_cache"
        original_request = self.api.request
        attempts, logs = [], []
        def timeout_once(method, path, body=None):
            if method == "POST" and path == failed_path:
                attempts.append(path)
                if len(attempts) == 1:
                    self.api.calls.append((method, path, deepcopy(body)))
                    raise ApiError(method, path, 504)
            return original_request(method, path, body)
        self.api.request = timeout_once
        with self.assertRaises(ApiError):
            run_task(task, self.api, allow_missing=True, on_log=lambda level, message: logs.append(message))
        self.assertEqual(task["state"], "uncertain")
        self.assertEqual(task["progress"]["refresh:refresh_content_bundle_cache"]["state"], "done")
        self.assertEqual(task["progress"]["refresh:refresh_song_file_cache"]["state"], "uncertain")
        self.assertNotIn("refresh:refresh_world_map_cache", task["progress"])
        self.assertFalse(self.api.songs)
        self.assertFalse(self.api.purchases)
        self.assertTrue(any("结果未确认" in message for message in logs))
        run_task(task, self.api, allow_missing=True)
        self.assertEqual(len(attempts), 2)
        bundle_calls = [(m, p) for m, p, _ in self.api.writes() if p.endswith("/refresh_content_bundle_cache")]
        self.assertEqual(len(bundle_calls), 1)
        self.assertTrue(task_complete(task))

    def test_purchase_response_loss_reconciles_without_duplicate_purchase(self):
        task = self.task()
        self.api.fail_after = ("POST", "/purchases")
        with self.assertRaises(ApiError):
            run_task(task, self.api, allow_missing=True)
        self.assertIn("a", self.api.purchases)
        self.assertEqual(task["state"], "uncertain")
        run_task(task, self.api, allow_missing=True)
        self.assertEqual(sum(p == "/purchases" for _, p, _ in self.api.writes()), 1)
        self.assertEqual(len(self.api.purchases["a"]["items"]), 1)

    def test_constants_reuse_preflight_and_verify_the_complete_write_response(self):
        task = self.task()
        task["options"]["purchases"] = False
        task["constants"]["a"]["2"] = 9.7
        run_task(task, self.api)
        reads = [(m, p) for m, p, _ in self.api.calls if m == "GET" and p == "/songs/a"]
        self.assertEqual(len(reads), 1)
        self.assertEqual(self.api.songs["a"]["charts"][2]["chart_const"], 9.7)
        self.assertTrue(task_complete(task))

    def test_constants_with_nothing_to_change_do_not_repeat_read_or_write(self):
        task = self.task()
        task["options"]["purchases"] = False
        self.api.songs["a"] = {"song_id": "a", "name": "English",
                               "charts": [{"difficulty": i, "chart_const": 9.7 if i == 2 else -1} for i in range(5)]}
        run_task(task, self.api, allow_missing=True)
        self.assertEqual(sum(m == "GET" and p == "/songs/a" for m, p, _ in self.api.calls), 1)
        self.assertFalse(any(p in ("/songs", "/songs/a") for _, p, _ in self.api.writes()))
        self.assertTrue(task_complete(task))

    def test_partial_write_response_falls_back_to_a_fresh_verification_read(self):
        task = self.task()
        task["options"]["purchases"] = False
        original = self.api.request
        def partial_reply(method, path, body=None):
            data = original(method, path, body)
            return {"song_id": "a"} if method == "POST" and path == "/songs" else data
        self.api.request = partial_reply
        run_task(task, self.api, allow_missing=True)
        self.assertEqual(sum(m == "GET" and p == "/songs/a" for m, p, _ in self.api.calls), 2)
        self.assertTrue(task_complete(task))

    def test_wrong_write_result_is_not_accepted_as_completed(self):
        task = self.task()
        task["options"]["purchases"] = False
        task["constants"]["a"]["2"] = 9.7
        original = self.api.request
        def wrong_result(method, path, body=None):
            data = original(method, path, body)
            if method == "POST" and path == "/songs":
                self.api.songs["a"]["charts"][2]["chart_const"] = 8.0
                data = deepcopy(self.api.songs["a"])
            return data
        self.api.request = wrong_result
        with self.assertRaisesRegex(PlanError, "核验不一致"):
            run_task(task, self.api)
        self.assertEqual(task["progress"]["constants:a"]["state"], "failed")
        self.assertNotIn("refresh:" + RATING_OPERATION, task["progress"])

    def test_wrong_song_id_does_not_pass_the_no_write_fast_path(self):
        task = self.task()
        task["options"]["purchases"] = False
        self.api.songs["a"] = {"song_id": "wrong", "name": "English",
                               "charts": [{"difficulty": i, "chart_const": -1} for i in range(5)]}
        with self.assertRaisesRegex(PlanError, "请求 ID 不一致"):
            run_task(task, self.api, allow_missing=True)
        self.assertEqual(task["progress"]["constants:a"]["state"], "failed")

    def test_resume_skips_completed_song_reads_and_reconciles_only_pending_song(self):
        task = self.task(rows=[{"id": "a", "title": "A"}, {"id": "b", "title": "B"}])
        task["options"]["purchases"] = False
        original = self.api.request
        def lose_b_reply(method, path, body=None):
            result = original(method, path, body)
            if method == "POST" and path == "/songs" and body["song_id"] == "b":
                raise ApiError(method, path)
            return result
        self.api.request = lose_b_reply
        with self.assertRaises(ApiError):
            run_task(task, self.api, allow_missing=True)
        self.assertEqual(task["progress"]["constants:a"]["state"], "done")
        before = len(self.api.calls)
        self.api.request = original
        run_task(task, self.api, allow_missing=True)
        reads = [(m, p) for m, p, _ in self.api.calls[before:] if m == "GET" and p.startswith("/songs/")]
        self.assertEqual(reads, [("GET", "/songs/b")])
        self.assertEqual(sum(m == "POST" and p == "/songs" for m, p, _ in self.api.calls), 2)
        self.assertTrue(task_complete(task))

    def test_song_response_loss_reconciles_without_duplicate_song(self):
        task = self.task()
        self.api.fail_after = ("POST", "/songs")
        with self.assertRaises(ApiError):
            run_task(task, self.api, allow_missing=True)
        run_task(task, self.api, allow_missing=True)
        self.assertEqual(sum(p == "/songs" and m == "POST" for m, p, _ in self.api.writes()), 1)

    def test_repeat_is_idempotent_and_later_constant_edit_refreshes_again(self):
        task = self.task()
        run_task(task, self.api, allow_missing=True)
        before = len(self.api.writes())
        run_task(task, self.api, allow_missing=True)
        self.assertEqual(len(self.api.writes()), before)
        task["constants"]["a"]["2"] = 9.8
        run_task(task, self.api)
        self.assertEqual([(m, p) for m, p, _ in self.api.writes()[before:]], [
            ("PUT", "/songs/a"), ("POST", "/system/operations/" + RATING_OPERATION)])

    def test_reviewed_songlist_changes_purchase_and_ignores_unrelated_songs(self):
        task = self.task()
        self.source.write_text(json.dumps({"songs": [
            {"id": "a", "title": "A", "set": "newpack", "purchase": "newpack"},
            {"id": "unrelated", "title": "Other", "set": "other", "purchase": "other"}]}), encoding="utf-8")
        with self.assertRaises(PlanError):
            run_task(task, self.api, allow_missing=True)
        task = read_reviewed(task, self.source)
        self.assertIsNone(task["publication"])
        confirm_uploaded(task)
        run_task(task, self.api, allow_missing=True)
        self.assertEqual(set(self.api.purchases), {"newpack"})

    def test_ambiguous_purchase_invalid_input_and_different_endpoint_block(self):
        task = self.task()
        task["songs"][0]["purchase"] = "unknown"
        with self.assertRaises(PlanError):
            run_task(task, self.api, allow_missing=True)
        self.assertFalse(self.api.writes())
        task = self.task()
        task["input_errors"] = ["unresolved original package"]
        with self.assertRaises(PlanError):
            run_task(task, self.api, allow_missing=True)
        task["input_errors"] = []
        task["endpoint"] = "https://different.test/api/v1"
        with self.assertRaises(PlanError):
            run_task(task, self.api, allow_missing=True)

    def test_options_allow_all_steps_to_be_skipped_without_network(self):
        task = self.task()
        task["options"] = dict.fromkeys(OPTIONS, False)
        run_task(task, self.api)
        self.assertEqual(task["state"], "skipped")
        self.assertEqual(self.api.calls, [])

    def test_constant_format_and_no_rating_inference(self):
        for value in (True, False, -2, "-0.1", "-1.1", "9.75", "NaN", "1e1"):
            with self.subTest(value=value), self.assertRaises(PlanError):
                parse_constant(value)
        self.assertEqual(parse_constant("9"), 9.0)
        for value, expected in ((0, 0.0), ("0.0", 0.0), ("-0.0", 0.0), (-1, -1.0), ("-1.0", -1.0)):
            with self.subTest(value=value):
                self.assertEqual(parse_constant(value), expected)
        self.assertIsNone(parse_constant(" "))
        task = self.task()
        self.assertIsNone(task["constants"]["a"]["2"])

    def test_explicit_unrank_constants_are_not_missing_and_keep_api_units(self):
        task = self.task(actual={"a": [2, 3]})
        task["constants"]["a"].update({"2": 0.0, "3": -1})
        self.assertEqual(missing_constants(task), [])
        path = task_path(self.root, task["id"])
        save_task(path, task)
        task = load_task(path)
        run_task(task, self.api)
        values = [c["chart_const"] for c in self.api.songs["a"]["charts"]]
        self.assertEqual(values, [-1, -1, 0.0, -1, -1])
        payload = next(body for method, endpoint, body in self.api.writes()
                       if (method, endpoint) == ("POST", "/songs"))
        self.assertEqual(payload["charts"][3]["chart_const"], -1, "API values use real units; the server scales them")

    def test_explicit_unrank_overwrites_ranked_constants_while_blank_preserves(self):
        task = self.task(actual={"a": [2, 3, 4]})
        task["constants"]["a"].update({"2": 0.0, "3": -1})
        self.api.songs["a"] = {"song_id": "a", "name": "English", "charts": [
            {"difficulty": i, "chart_const": 9.7} for i in range(5)]}
        self.assertEqual(missing_constants(task), ["a/4"])
        run_task(task, self.api, allow_missing=True)
        self.assertEqual([c["chart_const"] for c in self.api.songs["a"]["charts"]], [-1, -1, 0.0, -1, 9.7])
        patch = next(body for method, endpoint, body in self.api.writes()
                     if (method, endpoint) == ("PUT", "/songs/a"))
        self.assertNotIn(4, [chart["difficulty"] for chart in patch["charts"]])

    def test_legacy_zero_response_is_not_accepted_as_precise_verification(self):
        class LegacyZeroAPI(FakeAPI):
            def request(self, method, path, body=None):
                result = super().request(method, path, body)
                if isinstance(result, dict) and "charts" in result:
                    result = deepcopy(result)
                    for chart in result["charts"]:
                        if chart["chart_const"] == 0:
                            chart["chart_const"] = -1
                return result
        api = LegacyZeroAPI()
        task = self.task()
        task["constants"]["a"]["2"] = 0.0
        with self.assertRaisesRegex(PlanError, "0.0 回显为 -1"):
            run_task(task, api)
        self.assertEqual(api.songs["a"]["charts"][2]["chart_const"], 0.0)
        self.assertEqual(task["progress"]["constants:a"]["state"], "failed")
        # Once server serialization is fixed, resume by reading the already-written zero.
        api.request = FakeAPI.request.__get__(api)
        before = len(api.writes())
        run_task(task, api)
        self.assertEqual([path for _, path, _ in api.writes()[before:]], ["/system/operations/" + RATING_OPERATION])
        self.assertTrue(task_complete(task))

    def test_durable_task_lock_revision_and_preserved_error_state(self):
        task = self.task()
        task["input_errors"] = ["pending"]
        path = task_path(self.root, task["id"])
        save_task(path, task)
        stale = deepcopy(task)
        loaded = load_task(path)
        self.assertEqual(loaded["input_errors"], ["pending"])
        save_task(path, loaded)
        with self.assertRaises(PlanError):
            save_task(path, stale)
        with task_lock(path):
            with self.assertRaises(PlanError):
                save_task(path, loaded)
        self.assertNotIn("Token", path.read_text(encoding="utf-8"))

    def test_malformed_followup_provenance_is_rejected_on_load(self):
        task = self.task()
        path = task_path(self.root, task["id"])
        save_task(path, task)
        valid = {"source_task_id": "archive", "origin_task_id": "archive", "source_task_file": "archive.json",
                 "source_task_sha256": "a" * 64}
        for key, value in (("source_task_id", "invalid/path"), ("origin_task_id", None),
                           ("source_task_file", 42), ("source_task_sha256", "not-a-hash")):
            with self.subTest(key=key):
                data = deepcopy(task)
                data["followup"] = {**valid, key: value}
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(PlanError):
                    load_task(path)


if __name__ == "__main__":
    unittest.main()
