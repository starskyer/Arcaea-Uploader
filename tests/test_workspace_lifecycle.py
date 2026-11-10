from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from arcaea_uploader.api_client import ApiError
from arcaea_uploader.planner import PlanError, read_json, digest
from arcaea_uploader.server_tasks import (finish_task, load_task, run_task, save_task, task_complete, set_purchase_override,
    create_followup, archive_followup, read_reviewed)
from arcaea_uploader.workspace_session import entries, load_session, remember
from arcaea_uploader.workspaces import (bind_workspace_api, commit_workspace, list_workspaces, verify_workspace_api,
                                       workspace_settings)


class WorkspaceLifecycleTests(unittest.TestCase):
    def setUp(self):
        from test_publishing import PublishingTests
        from test_server_tasks import FakeAPI
        self.fixture = PublishingTests("test_complete_two_stage_workflow_with_final_review_preview_and_task")
        self.fixture.setUp()
        self.root, self.settings = self.fixture.root, self.fixture.settings
        self.settings["ssh"].update(host="one.example.test", username="one", private_key_path=str(self.root / "one.pem"))
        self.settings["api"] = {"base_url": FakeAPI.base_url, "token_env": "TOKEN_FOR_SERVER_ONE"}
        self.plan, self.batch = self.fixture.prepare()
        self.path = self.batch / "server-task.json"
        self.api = FakeAPI()

    def tearDown(self):
        self.fixture.doCleanups()
        self.fixture.tearDown()

    def publish(self):
        return self.fixture.publish(self.batch)

    def finish(self):
        task = load_task(self.path)
        run_task(task, self.api, allow_missing=True, persist=lambda task: save_task(self.path, task))
        return task

    def test_files_placeholders_constants_and_purchase_plan_stay_together_until_commit(self):
        self.assertTrue(self.path.is_file())
        task = load_task(self.path)
        self.assertEqual(task["purchase_candidates"][0]["name"], "validid")
        task["constants"]["validid"]["2"] = 9.8
        set_purchase_override(task, ["validid"], "custom", name="validid", kind="single", price=125)
        save_task(self.path, task)
        result = self.publish()
        self.assertEqual(result["server_task_file"], str(self.path))
        self.assertTrue((self.batch / "prepared/database/songs/validid/0.aff").is_file())
        self.assertTrue((self.batch / "bundle/7.0.256.5.cb").is_file())
        self.assertEqual(load_task(self.path)["constants"]["validid"]["2"], 9.8)
        self.assertEqual(load_task(self.path)["purchase_overrides"]["validid"]["price"], 125)
        with self.assertRaisesRegex(PlanError, "03 尚未"):
            commit_workspace(self.root / "releases", self.batch)
        self.finish()
        self.assertEqual(self.api.purchases["validid"]["price"], 125)
        self.assertTrue(self.batch.exists(), "Successful 03 must not implicitly commit")
        self.assertIn("待确认提交", list_workspaces(self.root / "releases")[0]["state"])
        history = commit_workspace(self.root / "releases", self.batch)
        self.assertFalse(self.batch.exists())
        record = read_json(history)
        self.assertTrue(record["committed"])
        self.assertTrue(record["cleanup_complete"])
        self.assertEqual(load_task(self.root / "releases" / record["server_task_file"])["constants"]["validid"]["2"], 9.8)

    def test_execution_binding_preserves_proxy_choice_when_restoring_the_workspace(self):
        for use_proxy in (False, True):
            with self.subTest(use_proxy=use_proxy):
                self.settings["api"]["use_system_proxy"] = use_proxy
                bind_workspace_api(self.root / "releases", self.batch, self.settings)
                restored = workspace_settings(self.root / "releases", self.batch, self.settings, self.root)
                self.assertIs(restored["api"]["use_system_proxy"], use_proxy)
        self.settings["api"].pop("use_system_proxy")
        bind_workspace_api(self.root / "releases", self.batch, self.settings)
        restored = workspace_settings(self.root / "releases", self.batch, self.settings, self.root)
        self.assertTrue(restored["api"]["use_system_proxy"])

    def committed_archive(self):
        self.publish()
        self.finish()
        history = commit_workspace(self.root / "releases", self.batch)
        archive = self.root / "releases/.server-tasks" / (self.batch.name + ".json")
        return archive, history

    def test_committed_archive_blocks_mutation_execution_and_legacy_overwrites(self):
        from arcaea_uploader.workspaces import write_json
        archive, history = self.committed_archive()
        original, summary = digest(archive), digest(history)
        task = load_task(archive)
        self.assertTrue(task.get("archive"))
        self.assertTrue(task_complete(task))
        calls = len(self.api.calls)
        with self.assertRaisesRegex(PlanError, "归档"):
            run_task(task, self.api, allow_missing=True)
        with self.assertRaisesRegex(PlanError, "归档"):
            set_purchase_override(task, ["validid"], "skip")
        with self.assertRaisesRegex(PlanError, "归档"):
            read_reviewed(task, self.root / "missing-songlist")
        task["constants"]["validid"]["2"] = 9.9
        with self.assertRaisesRegex(PlanError, "归档"):
            save_task(archive, task)
        task.pop("archive")
        with self.assertRaisesRegex(PlanError, "归档"):
            save_task(archive, task)
        self.assertEqual(len(self.api.calls), calls)
        self.assertEqual((digest(archive), digest(history)), (original, summary))
        # Older versions wrote no archive marker. Identify them from .history
        # in memory, without migrating/reformatting the original artifact.
        legacy = read_json(archive)
        legacy.pop("archive")
        write_json(archive, legacy)
        before = digest(archive)
        loaded = load_task(archive)
        self.assertEqual(loaded["archive"]["kind"], "workspace_commit")
        self.assertEqual(digest(archive), before)
        with self.assertRaisesRegex(PlanError, "归档"):
            save_task(archive, loaded)
        legacy["constants"]["validid"]["2"] = 9.9
        legacy["state"] = "draft"
        write_json(archive, legacy)
        dirty = load_task(archive)
        self.assertTrue(dirty["archive"]["modified_after_commit"])
        _, child = create_followup(archive, self.root / "releases")
        self.assertEqual(child["constants"]["validid"]["2"], 9.9)

    def test_followup_is_separate_resumable_and_does_not_rewrite_original_commit(self):
        archive, history = self.committed_archive()
        original = digest(archive), digest(history)
        child_path, child = create_followup(archive, self.root / "releases")
        self.assertNotEqual(child_path, archive)
        self.assertFalse(child["options"]["refresh_resources"])
        self.assertFalse(child["managed_purchases"])
        self.assertFalse(child.get("archive"))
        self.assertEqual(child["followup"]["source_task_id"], self.batch.name)
        self.assertEqual(child["followup"]["source_task_sha256"], original[0])
        child["constants"]["validid"]["2"] = 9.9
        set_purchase_override(child, ["validid"], "custom", name="validid", kind="single", price=150)
        save_task(child_path, child)
        before_calls = len(self.api.calls)
        self.api.fail_after = ("PUT", "/songs/validid")
        with self.assertRaises(ApiError):
            run_task(child, self.api, allow_missing=True, persist=lambda task: save_task(child_path, task))
        self.assertFalse(load_task(child_path).get("archive"))
        child = load_task(child_path)
        run_task(child, self.api, allow_missing=True, persist=lambda task: save_task(child_path, task))
        archive_followup(child_path, child)
        self.assertTrue(task_complete(load_task(child_path)))
        self.assertEqual(load_task(child_path)["archive"]["kind"], "followup_complete")
        self.assertEqual(self.api.songs["validid"]["charts"][2]["chart_const"], 9.9)
        self.assertEqual(self.api.purchases["validid"]["price"], 100, "Existing purchase prices remain intact")
        writes = [(m, p) for m, p, _ in self.api.calls[before_calls:] if m != "GET"]
        self.assertEqual(writes, [("PUT", "/songs/validid"), ("POST", "/system/operations/refresh_all_score_rating")])
        self.assertEqual((digest(archive), digest(history)), original)
        next_path, next_task = create_followup(child_path, self.root / "releases")
        self.assertNotEqual(next_path, child_path)
        self.assertEqual(next_task["followup"]["origin_task_id"], self.batch.name)
        self.assertEqual(next_task["followup"]["source_task_id"], child["id"])

    def test_followup_keeps_endpoint_and_refuses_source_replacement(self):
        from test_server_tasks import FakeAPI
        archive, _ = self.committed_archive()
        _, child = create_followup(archive, self.root / "releases")
        other = FakeAPI()
        other.base_url = "https://other.example.test/api/v1"
        with self.assertRaisesRegex(PlanError, "另一 API"):
            run_task(child, other, allow_missing=True)
        self.assertFalse(other.calls)
        with self.assertRaisesRegex(PlanError, "不能更换"):
            read_reviewed(child, self.root / "another-songlist")

    def test_followup_preserves_explicitly_disabled_options(self):
        task = load_task(self.path)
        task["options"] = {key: False for key in task["options"]}
        save_task(self.path, task)
        archive, _ = self.committed_archive()
        path, child = create_followup(archive, self.root / "releases")
        self.assertFalse(any(child["options"].values()))
        before = len(self.api.calls)
        run_task(child, self.api, persist=lambda task: save_task(path, task))
        archive_followup(path, child)
        self.assertEqual(len(self.api.calls), before)
        self.assertTrue(load_task(path).get("archive"))

    def test_cleanup_retry_preserves_the_first_archive_and_commit_timestamp(self):
        self.publish()
        self.finish()
        with patch("arcaea_uploader.workspaces._delete", side_effect=OSError("synthetic directory busy")):
            with self.assertRaises(OSError):
                commit_workspace(self.root / "releases", self.batch)
        archive = self.root / "releases/.server-tasks" / (self.batch.name + ".json")
        history = self.root / "releases/.history" / (self.batch.name + ".json")
        before = digest(archive)
        when = read_json(history)["completed_at"]
        commit_workspace(self.root / "releases", self.batch)
        self.assertEqual(digest(archive), before)
        self.assertEqual(read_json(history)["completed_at"], when)
        self.assertTrue(read_json(history)["cleanup_complete"])

    def test_recovered_config_keeps_server_identity_and_never_snapshots_env_secrets(self):
        other = deepcopy(self.settings)
        other["ssh"].update(host="other.example.test", username="other")
        other["api"].update(base_url="https://other.example.test/api/v1", token_env="OTHER_TOKEN")
        restored = workspace_settings(self.root / "releases", self.batch, other, self.root)
        self.assertEqual(restored["ssh"]["host"], "one.example.test")
        self.assertEqual(restored["api"]["token_env"], "TOKEN_FOR_SERVER_ONE")
        self.assertEqual(restored["local"]["assets_root"], str(self.fixture.assets))
        with self.assertRaisesRegex(PlanError, "不一致"):
            verify_workspace_api(self.root / "releases", self.batch, other)
        secret = "never-store-this-test-credential"
        self.settings["ssh"]["password"] = secret
        with patch.dict(os.environ, {"TOKEN_FOR_SERVER_ONE": secret}):
            self.publish()
        self.assertNotIn(secret, (self.batch / "workspace.json").read_text(encoding="utf-8"))
        self.assertNotIn(secret, self.path.read_text(encoding="utf-8"))

    def test_03_error_and_later_input_changes_keep_workspace_and_block_commit(self):
        self.publish()
        self.api.fail_after = ("POST", "/purchases")
        with self.assertRaises(ApiError):
            self.finish()
        self.assertTrue(self.batch.exists())
        with self.assertRaises(PlanError):
            commit_workspace(self.root / "releases", self.batch)
        self.finish()
        task = load_task(self.path)
        task["constants"]["validid"]["2"] = 10.1
        save_task(self.path, task)
        self.assertFalse(task_complete(load_task(self.path)))
        with self.assertRaises(PlanError):
            commit_workspace(self.root / "releases", self.batch)
        self.finish()
        commit_workspace(self.root / "releases", self.batch)
        self.assertFalse(self.batch.exists())

    def test_explicit_skip_of_all_api_options_still_requires_final_commit(self):
        self.publish()
        task = load_task(self.path)
        task["options"] = {key: False for key in task["options"]}
        finish_task(task, skipped=True)
        save_task(self.path, task)
        self.assertTrue(self.batch.exists())
        history = commit_workspace(self.root / "releases", self.batch)
        self.assertEqual(read_json(history)["server_operations"], "skipped")

    def test_02_is_idempotent_after_upload_and_does_not_reset_finished_03(self):
        from test_publishing import MemoryUpload
        result = self.publish()
        self.finish()
        confirmation = load_task(self.path)["publication"]["confirmation_id"]
        with patch.object(MemoryUpload, "publish", side_effect=AssertionError("must not upload again")):
            again = self.publish()
        self.assertEqual(again["version"], result["version"])
        self.assertEqual(load_task(self.path)["publication"]["confirmation_id"], confirmation)
        self.assertTrue(task_complete(load_task(self.path)))

    def test_manually_modified_review_after_02_cannot_be_committed(self):
        self.publish()
        self.finish()
        source = Path(self.plan["review_songlist"])
        source.write_bytes(source.read_bytes() + b"\n")
        with self.assertRaisesRegex(PlanError, "不一致"):
            commit_workspace(self.root / "releases", self.batch)
        self.assertTrue(self.batch.exists())

    def test_confirmation_revision_is_checked_before_cleanup(self):
        self.publish()
        task = self.finish()
        revision = task["revision"]
        task["constants"]["validid"]["2"] = 9.9
        save_task(self.path, task)
        with self.assertRaisesRegex(PlanError, "确认期间"):
            commit_workspace(self.root / "releases", self.batch, expected_revision=revision)
        self.assertTrue(self.batch.exists())

    def test_final_human_purchase_edits_are_reflected_in_workspace_candidates(self):
        source = Path(self.plan["review_songlist"])
        document = read_json(source)
        document["songs"][0]["purchase"] = "newpack"
        source.write_text(json.dumps(document), encoding="utf-8")
        self.publish()
        task = load_task(self.path)
        self.assertEqual(task["purchase_candidates"][0]["kind"], "pack")
        self.assertEqual(task["purchase_candidates"][0]["price"], 500)

    def test_profile_catalog_remembers_pending_work_across_roots_and_restart(self):
        catalog = self.root / "profile.workspaces.json"
        remember(catalog, self.root / "releases", self.batch)
        another_root = self.root / "another-workspace-root"
        remember(catalog, another_root)
        self.assertEqual(len(load_session(catalog)["roots"]), 2)
        self.assertEqual(entries(catalog)[0]["path"], str(self.batch))
        self.publish()
        self.assertTrue(entries(catalog), "Uploaded pending work stays discoverable after restart")
        self.finish()
        commit_workspace(self.root / "releases", self.batch)
        self.assertEqual(entries(catalog), [])


if __name__ == "__main__":
    unittest.main()
