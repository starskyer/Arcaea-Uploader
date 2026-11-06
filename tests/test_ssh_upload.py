"""Real SSH/SFTP wire tests against an isolated in-process server."""

from contextlib import contextmanager
import errno
from pathlib import Path
import os
import socket
import threading
from tempfile import TemporaryDirectory
import unittest

import paramiko

from arcaea_uploader.planner import PlanError, digest
from arcaea_uploader.settings import default_settings
from arcaea_uploader.ssh_upload import SSHUpload


class Auth(paramiko.ServerInterface):
    def __init__(self, key):
        self.key = key

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == "fixture" and key == self.key else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class Files(paramiko.SFTPServerInterface):
    def __init__(self, server, *, root):
        super().__init__(server)
        self.root = root

    def path(self, name):
        path = self.root / name.lstrip("/")
        if not path.resolve().is_relative_to(self.root):
            raise OSError(errno.EACCES, "outside fixture")
        return path

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).lstat())
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    stat = lstat

    def open(self, path, flags, attr):
        try:
            fd = os.open(self.path(path), flags, 0o644)
            stream = os.fdopen(fd, "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb")
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = handle.writefile = stream
            return handle
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def _operate(self, action, *args):
        try:
            action(*args)
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def mkdir(self, path, attr):
        return self._operate(self.path(path).mkdir)

    def rmdir(self, path):
        return self._operate(self.path(path).rmdir)

    def remove(self, path):
        return self._operate(self.path(path).unlink)

    def rename(self, old, new):
        if self.path(new).exists():
            return paramiko.SFTP_FAILURE
        return self._operate(self.path(old).rename, self.path(new))

    def posix_rename(self, old, new):
        return self._operate(self.path(old).replace, self.path(new))

    def chattr(self, path, attr):
        return paramiko.SFTP_OK


class SSHUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host_key = paramiko.RSAKey.generate(2048)
        cls.client_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.remote = self.root / "remote"
        self.database = self.remote / "srv/server/latest version/database"
        self.database.mkdir(parents=True)
        self.settings = default_settings()
        self.settings["ssh"].update(host="127.0.0.1", username="fixture", server_project_root="/srv/server",
            private_key_path=str(self.root / "client.pem"), known_hosts_path=str(self.root / "known_hosts"))
        self.client_key.write_private_key_file(self.settings["ssh"]["private_key_path"])
        self.source = self.root / "song.aff"
        self.source.write_bytes(b"chart fixture")
        self.entry = {"source": str(self.source), "path": "songs/example/2.aff",
                      "sha256": digest(self.source), "existing_sha256": None}

    def tearDown(self):
        self.temp.cleanup()

    @contextmanager
    def server(self, *, trust=True):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        port = listener.getsockname()[1]
        self.settings["ssh"]["port"] = port
        if trust:
            known = paramiko.HostKeys()
            known.add(f"[127.0.0.1]:{port}", self.host_key.get_name(), self.host_key)
            known.save(self.settings["ssh"]["known_hosts_path"])
        finished = threading.Event()
        active = []
        def serve():
            try:
                connection, _ = listener.accept()
                transport = paramiko.Transport(connection)
                active.append(transport)
                transport.add_server_key(self.host_key)
                transport.set_subsystem_handler("sftp", paramiko.SFTPServer, Files, root=self.remote)
                transport.start_server(server=Auth(self.client_key))
                finished.wait(15)
            except (OSError, EOFError, paramiko.SSHException):
                pass
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield
        finally:
            finished.set()
            for transport in active:
                transport.close()
            listener.close()
            worker.join(timeout=5)

    def test_real_sftp_upload_space_paths_hashes_and_idempotent_retry(self):
        unrelated = self.database / "preserve.db"
        unrelated.write_bytes(b"never touch")
        with self.server(), SSHUpload(self.settings) as transport:
            transport.publish("batch", [self.entry])
            transport.publish("batch", [self.entry])
        self.assertEqual((self.database / self.entry["path"]).read_bytes(), self.source.read_bytes())
        self.assertEqual(unrelated.read_bytes(), b"never touch")
        self.assertFalse((self.database / ".arcaea-uploader.lock").exists())
        self.assertFalse(list(self.database.rglob("*.part")))

    def test_conflict_aborts_entire_manifest_before_any_file_upload(self):
        conflict = self.database / self.entry["path"]
        conflict.parent.mkdir(parents=True)
        conflict.write_bytes(b"remote exclusive chart")
        other = dict(self.entry, path="songs/another/2.aff")
        with self.server(), SSHUpload(self.settings) as transport:
            with self.assertRaisesRegex(PlanError, "已有不同"):
                transport.publish("batch", [other, self.entry])
        self.assertFalse((self.database / other["path"]).exists())
        self.assertEqual(conflict.read_bytes(), b"remote exclusive chart")

    def test_known_placeholder_can_be_atomically_replaced(self):
        target = self.database / self.entry["path"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"placeholder")
        entry = dict(self.entry, existing_sha256=digest(target))
        with self.server(), SSHUpload(self.settings) as transport:
            transport.publish("batch", [entry])
        self.assertEqual(target.read_bytes(), self.source.read_bytes())

    def test_first_connection_requires_explicit_fingerprint_acceptance(self):
        with self.server(trust=False):
            with self.assertRaisesRegex(PlanError, "未信任"):
                with SSHUpload(self.settings):
                    self.fail("must reject")
        calls = []
        with self.server(trust=False):
            with SSHUpload(self.settings, on_host_key=lambda *args: calls.append(args) or True) as transport:
                transport.publish("batch", [self.entry])
        self.assertTrue(calls[0][2].startswith("SHA256:"))
        self.assertTrue(Path(self.settings["ssh"]["known_hosts_path"]).is_file())

    def test_changed_host_key_is_never_accepted_by_unknown_key_callback(self):
        with self.server():
            hosts = paramiko.HostKeys()
            hosts.add(f"[127.0.0.1]:{self.settings['ssh']['port']}", "ssh-rsa", self.client_key)
            hosts.save(self.settings["ssh"]["known_hosts_path"])
            with self.assertRaises(paramiko.BadHostKeyException):
                with SSHUpload(self.settings, on_host_key=lambda *_: self.fail("changed key must reject")):
                    pass

    def test_missing_database_path_fails_instead_of_creating_wrong_deployment(self):
        self.settings["ssh"]["server_project_root"] = "/wrong"
        with self.server():
            with self.assertRaises(PlanError):
                with SSHUpload(self.settings):
                    pass
        self.assertFalse((self.remote / "wrong").exists())

    def test_preparation_bundle_local_install_and_real_ssh_publish_together(self):
        from test_publishing import PublishingTests
        from arcaea_uploader.publishing import publish_update
        from arcaea_uploader.server_tasks import load_task
        case = PublishingTests("test_complete_two_stage_workflow_with_final_review_preview_and_task")
        case.setUp()
        try:
            plan, batch = case.prepare()
            with self.server():
                case.settings["ssh"] = self.settings["ssh"].copy()
                result = publish_update(case.settings, case.root, batch)
            self.assertTrue(batch.exists(), "SSH upload does not finalize the workspace")
            self.assertTrue((self.database / "songs/validid/2.aff").is_file())
            self.assertEqual((self.database / "bundle/7.0.256.5.cb").read_bytes(),
                             (case.database / "bundle/7.0.256.5.cb").read_bytes())
            self.assertEqual(load_task(result["server_task_file"])["publication"]["kind"], "verified")
        finally:
            case.doCleanups()
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
