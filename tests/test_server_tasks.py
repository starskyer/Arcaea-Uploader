from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arcaea_uploader.api_client import ApiError
from arcaea_uploader.planner import PlanError
from arcaea_uploader.server_tasks import (OPTIONS, PAST_FROM, PAST_TO, RATING_OPERATION,
    RESOURCE_OPERATIONS, confirm_uploaded, load_task, make_task, missing_constants,
    parse_constant, purchase_specs, read_reviewed, run_task, save_task, task_lock, task_path)


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
        self.api.purchases["a"] = {"name": "a", "price": 77, "orig_price": 99, "items": []}
        run_task(task, self.api, allow_missing=True)
        self.assertEqual(self.api.purchases["a"]["price"], 77)
        self.assertEqual(self.api.items, {})
        self.assertFalse(any(path.startswith("/purchases") for _, path, _ in self.api.writes()))

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
        for value in (True, -1, "9.75", "NaN", "1e1", 0):
            with self.subTest(value=value), self.assertRaises(PlanError):
                parse_constant(value)
        self.assertEqual(parse_constant("9"), 9.0)
        self.assertIsNone(parse_constant(" "))
        task = self.task()
        self.assertIsNone(task["constants"]["a"]["2"])

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


if __name__ == "__main__":
    unittest.main()
