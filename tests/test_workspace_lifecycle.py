from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from arcaea_uploader.api_client import ApiError
from arcaea_uploader.planner import PlanError, read_json
from arcaea_uploader.server_tasks import (finish_task, load_task, run_task, save_task, task_complete)
from arcaea_uploader.workspace_session import entries, load_session, remember
from arcaea_uploader.workspaces import (commit_workspace, list_workspaces, verify_workspace_api,
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
        save_task(self.path, task)
        result = self.publish()
        self.assertEqual(result["server_task_file"], str(self.path))
        self.assertTrue((self.batch / "prepared/database/songs/validid/0.aff").is_file())
        self.assertTrue((self.batch / "bundle/7.0.256.5.cb").is_file())
        self.assertEqual(load_task(self.path)["constants"]["validid"]["2"], 9.8)
        with self.assertRaisesRegex(PlanError, "03 尚未"):
            commit_workspace(self.root / "releases", self.batch)
        self.finish()
        self.assertTrue(self.batch.exists(), "Successful 03 must not implicitly commit")
        self.assertIn("待确认提交", list_workspaces(self.root / "releases")[0]["state"])
        history = commit_workspace(self.root / "releases", self.batch)
        self.assertFalse(self.batch.exists())
        record = read_json(history)
        self.assertTrue(record["committed"])
        self.assertTrue(record["cleanup_complete"])
        self.assertEqual(load_task(self.root / "releases" / record["server_task_file"])["constants"]["validid"]["2"], 9.8)

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
