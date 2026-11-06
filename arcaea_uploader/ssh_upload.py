"""Verified, additive SFTP publication over SSH; never execute shell commands."""

from base64 import b64encode
from contextlib import contextmanager
import errno
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import stat
from uuid import uuid4

from .planner import PlanError, digest


def remote_database(settings):
    ssh = settings["ssh"]
    for key in ("host", "username", "server_project_root"):
        if not ssh.get(key) or any(ord(c) < 32 for c in ssh[key]):
            raise PlanError(f"请填写 SSH {key}；当前尚不支持本机部署模式。")
    project = PurePosixPath(ssh["server_project_root"])
    relative = PurePosixPath(ssh["database_relative_path"])
    if (not project.is_absolute() or ".." in project.parts or relative.is_absolute()
            or ".." in relative.parts or not relative.parts):
        raise PlanError("远端项目须为绝对路径，database 须为项目内的相对路径。")
    return str(project / relative)


class SSHUpload:
    def __init__(self, settings, *, on_host_key=None):
        self.settings = settings
        self.root = remote_database(settings)
        self.on_host_key = on_host_key
        self.client = self.sftp = None

    def __enter__(self):
        import paramiko
        ssh = self.settings["ssh"]
        known = Path(ssh["known_hosts_path"]).expanduser()
        key = Path(ssh["private_key_path"]).expanduser()
        if not ssh["known_hosts_path"] or not key.is_file():
            raise PlanError("请配置已知主机文件和有效的 SSH 私钥。")
        client = self.client = paramiko.SSHClient()
        if known.is_file():
            client.load_host_keys(str(known))
        callback = self.on_host_key

        class ConfirmKey(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, connection, hostname, server_key):
                fingerprint = "SHA256:" + b64encode(sha256(server_key.asbytes()).digest()).decode().rstrip("=")
                if callback is None or not callback(hostname, server_key.get_name(), fingerprint):
                    raise PlanError("未信任 SSH 主机密钥，未上传文件。请核对指纹或配置 known_hosts 后重试。")
                # Do not replace another process's host-key entry.
                if known.is_file():
                    connection.load_host_keys(str(known))
                current = connection.get_host_keys().lookup(hostname)
                if current and server_key.get_name() in current and current[server_key.get_name()] != server_key:
                    raise PlanError("SSH 主机密钥已变更，请人工核对。")
                connection.get_host_keys().add(hostname, server_key.get_name(), server_key)
                known.parent.mkdir(parents=True, exist_ok=True)
                connection.save_host_keys(str(known))

        client.set_missing_host_key_policy(ConfirmKey())
        try:
            client.connect(ssh["host"], port=ssh["port"], username=ssh["username"], key_filename=str(key),
                           allow_agent=False, look_for_keys=False, timeout=20, auth_timeout=30, banner_timeout=30)
            client.get_transport().set_keepalive(30)
            self.sftp = client.open_sftp()
            self.sftp.get_channel().settimeout(120)
            # Require an existing database; do not silently create a wrong deployment.
            self._directory(self.root, create=False)
            return self
        except Exception:
            client.close()
            raise

    def __exit__(self, *_):
        if self.sftp:
            self.sftp.close()
        if self.client:
            self.client.close()

    def _stat(self, path):
        try:
            return self.sftp.lstat(path)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return None
            raise

    def _directory(self, path, *, create=True):
        cursor = PurePosixPath("/")
        for part in PurePosixPath(path).parts[1:]:
            cursor /= part
            info = self._stat(str(cursor))
            if info is None and create:
                self.sftp.mkdir(str(cursor))
                info = self._stat(str(cursor))
            if info is None or not stat.S_ISDIR(info.st_mode):
                raise PlanError(f"远端目录不存在或包含链接：{cursor}")

    def _path(self, relative):
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise PlanError("上传清单路径越界。")
        return str(PurePosixPath(self.root) / path)

    def _digest(self, path):
        info = self._stat(path)
        if info is None:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise PlanError(f"远端目标不是普通文件：{path}")
        value = sha256()
        with self.sftp.open(path, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
        return value.hexdigest()

    @contextmanager
    def _lock(self, batch_id):
        path = self.root + "/.arcaea-uploader.lock"
        try:
            self.sftp.mkdir(path)
        except OSError:
            raise PlanError("远端存在上传锁。若上一任务已中断，请核实没有其他上传进程，再手动删除 database/.arcaea-uploader.lock。") from None
        try:
            with self.sftp.open(path + "/owner", "w") as stream:
                stream.write(batch_id + "\n")
            yield
        finally:
            # A dropped connection intentionally leaves a visible lock.
            self.sftp.remove(path + "/owner")
            self.sftp.rmdir(path)

    def publish(self, batch_id, entries, *, on_log=None):
        log = on_log or (lambda *_: None)
        # The caller orders charts/audio first, CB next, bundle JSON last.
        with self._lock(batch_id):
            for entry in entries:
                target = self._path(entry["path"])
                self._directory(str(PurePosixPath(target).parent))
                current = self._digest(target)
                if current not in {None, entry.get("existing_sha256"), entry["sha256"]}:
                    raise PlanError(f"远端已有不同资源，保留并停止：{entry['path']}")
            for entry in entries:
                source, target = Path(entry["source"]), self._path(entry["path"])
                if digest(source) != entry["sha256"]:
                    raise PlanError("上传源文件发生变化，停止上传。")
                if self._digest(target) == entry["sha256"]:
                    log("info", f"远端核验一致，复用 {entry['path']}")
                    continue
                temporary = target + ".uploader-" + uuid4().hex + ".part"
                try:
                    self.sftp.put(str(source), temporary, confirm=True)
                    self.sftp.chmod(temporary, 0o644)
                    if self._digest(temporary) != entry["sha256"]:
                        raise PlanError("远端暂存文件 SHA-256 不一致。")
                    current = self._digest(target)
                    if current not in {None, entry.get("existing_sha256"), entry["sha256"]}:
                        raise PlanError(f"上传期间目标变化：{entry['path']}")
                    if current is None:
                        self.sftp.rename(temporary, target)
                    else:
                        # OpenSSH POSIX rename replaces atomically, never remove first.
                        self.sftp.posix_rename(temporary, target)
                    if self._digest(target) != entry["sha256"]:
                        raise PlanError("远端就位文件 SHA-256 不一致。")
                    log("info", f"上传并核验 {entry['path']}")
                finally:
                    if self._stat(temporary) is not None:
                        self.sftp.remove(temporary)
            # Verify the entire manifest again before issuing a receipt.
            for entry in entries:
                if self._digest(self._path(entry["path"])) != entry["sha256"]:
                    raise PlanError("远端最终清单核验失败，保留批次。")
