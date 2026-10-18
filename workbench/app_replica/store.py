"""One atomic authority, with recoverable human/worker-readable projections.

The lock covers metadata only. Callers must never run a model or await a process
while holding it. Projections are replaceable views, not independent journals.
"""

from contextlib import contextmanager
import fcntl
import json
import hashlib
import re
import os
from pathlib import Path
import stat
import tempfile


class ReplicaError(ValueError):
    pass


class Store:
    def __init__(self, workspace):
        from .paths import lexical, no_symlinks
        self.workspace = no_symlinks(lexical(workspace, base=Path.cwd(), strict=False))
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.authority = self.workspace / "coordination/state.json"

    def metadata_path(self, relative):
        path = self.workspace / relative
        if path.is_symlink() or not path.resolve().is_relative_to(self.workspace):
            raise ReplicaError(f"Metadata path escapes workspace: {relative}")
        # Refuse symlink components, including links that currently point inside.
        for part in path.relative_to(self.workspace).parents:
            if (self.workspace / part).is_symlink():
                raise ReplicaError(f"Metadata directory is a symlink: {relative}")
        return path

    @contextmanager
    def locked(self, *, allow_legacy=False, allow_relocation=False):
        path = self.metadata_path("coordination/state.lock")
        self._mkdir(path.parent)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ReplicaError("Metadata lock must be a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            authority = self.metadata_path("coordination/state.json")
            try:
                if authority.exists() and not authority.is_file():
                    raise ReplicaError("Committed state must be a regular file")
                state = json.loads(authority.read_text())
            except FileNotFoundError:
                state = None
            except (OSError, ValueError) as exc:
                raise ReplicaError(f"Cannot read committed state: {exc}") from exc
            if state is not None:
                if not isinstance(state, dict) or state.get("schema") not in {1, 2, 3}:
                    raise ReplicaError("Unsupported or invalid replica state")
                if state["schema"] == 1 and not allow_legacy:
                    raise ReplicaError("Legacy schema 1 requires explicit migrate before reads/writes")
                if state["schema"] in {2, 3}:
                    from .paths import PathBindings
                    bound_workspace = (state.get('relocation', {}).get('target_workspace', self.workspace)
                                       if allow_relocation else self.workspace)
                    PathBindings.from_state(bound_workspace, state)
                    from .snapshot import Inventory, Scopes
                    state["inventory"] = Inventory(self, state["inventory_ref"], Scopes(self, state["scope_ref"]))
            yield state
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _mkdir(self, directory):
        missing = []
        while not directory.exists():
            missing.append(directory)
            directory = directory.parent
        for path in reversed(missing):
            path.mkdir(exist_ok=True)
            self._fsync_dir(path.parent)

    @staticmethod
    def _fsync_dir(path):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _sync_parents(self, directory):
        # A previous process may have died between rename/mkdir and directory
        # fsync. Reusing its visible files must also make those names durable.
        while directory.is_relative_to(self.workspace):
            self._fsync_dir(directory)
            if directory == self.workspace:
                break
            directory = directory.parent

    def write_bytes(self, relative, content):
        target = self.metadata_path(relative)
        self._mkdir(target.parent)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".publish-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
            self._sync_parents(target.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def write_json(self, relative, value):
        content = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                              allow_nan=False) + "\n").encode("utf-8")
        target = self.metadata_path(relative)
        try:
            if target.exists() and not target.is_file():
                raise ReplicaError("Metadata target must be a regular file")
            if target.read_bytes() == content:
                return
        except FileNotFoundError:
            pass
        self.write_bytes(relative, content)

    def blob_path(self, reference, namespace):
        if (not isinstance(reference, dict)
                or not re.fullmatch(r"[0-9a-f]{64}", str(reference.get("sha256", "")))
                or reference.get("path") != f"coordination/{namespace}/{reference['sha256']}.json"
                or not isinstance(reference.get("bytes"), int) or reference["bytes"] < 0):
            raise ReplicaError("Invalid immutable data reference")
        return self.metadata_path(reference["path"])

    def read_blob(self, reference, namespace):
        path = self.blob_path(reference, namespace)
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ReplicaError("Immutable data must be a regular file")
                content = handle.read()
            if len(content) != reference["bytes"] or hashlib.sha256(content).hexdigest() != reference["sha256"]:
                raise ReplicaError(f"Immutable data hash mismatch: {path}")
            return json.loads(content)
        except (OSError, ValueError) as exc:
            raise ReplicaError(f"Cannot read immutable data: {path}: {exc}") from exc

    def write_blob(self, namespace, value):
        from .snapshot import encoded
        content = encoded(value)
        digest = hashlib.sha256(content).hexdigest()
        reference = {"path": f"coordination/{namespace}/{digest}.json", "sha256": digest, "bytes": len(content)}
        path = self.blob_path(reference, namespace)
        if path.exists():
            # Never overwrite a corrupt immutable file under an existing name.
            self.read_blob(reference, namespace)
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ReplicaError("Immutable data must be a regular file")
                os.fsync(handle.fileno())
            self._sync_parents(path.parent)
        else:
            self.write_bytes(reference["path"], content)
        return reference

    def receipt(self, record):
        return self.read_blob(record["ref"], "receipts") if "ref" in record else record

    def commit(self, state):
        from .snapshot import Inventory
        if not isinstance(state["inventory"], Inventory):
            inventory = Inventory(self)
            for identifier, record in state["inventory"].items():
                inventory[identifier] = record
            state["inventory"] = inventory
        state["inventory_ref"] = state["inventory"].flush()
        state["scope_ref"] = state["inventory"].scopes.flush()
        for identifier, receipt in state["receipts"].items():
            if "ref" not in receipt:
                state["receipts"][identifier] = {"fingerprint": receipt["fingerprint"],
                                                "ref": self.write_blob("receipts", receipt)}
        # Publish every immutable dependency before the authority rename. A
        # crash before this write leaves only harmless unreferenced blobs.
        disk = {key: value for key, value in state.items() if key != "inventory"}
        self.write_json("coordination/state.json", disk)

    def publish(self, state):
        marker = {"authority": "coordination/state.json", "revision": state["revision"],
                  "editable": False}

        if state.get("schema") == 3:
            marker["path_bindings"] = "coordination/path-bindings.json"
            marker["binding_revision"] = state["relocation"]["binding_revision"]

        def project(relative, record):
            target = self.metadata_path(relative)
            if target.exists() and not target.is_file():
                raise ReplicaError("Projection must be a regular file")
            try:
                previous = json.loads(target.read_text())
                if isinstance(previous, dict):
                    prior_marker = previous.pop("_projection", {})
                    valid_marker = (isinstance(prior_marker, dict)
                        and prior_marker.get("authority") == marker["authority"]
                        and prior_marker.get("editable") is False
                        and prior_marker.get("path_bindings") == marker.get("path_bindings")
                        and prior_marker.get("binding_revision") == marker.get("binding_revision")
                        and type(prior_marker.get("revision")) is int
                        and 0 < prior_marker["revision"] <= state["revision"])
                    if previous == record and valid_marker:
                        return
            except (FileNotFoundError, ValueError):
                pass
            self.write_json(relative, {**record, "_projection": marker})

        if state.get("schema") == 3:
            project("coordination/path-bindings.json", state["relocation"])
        project("project.json", state["project"])
        project("inventory/manifest.json", {"reference": state["inventory_ref"], "scope_reference": state["scope_ref"],
                "count": len(state["inventory"]),
                "counts_by_kind": state["inventory"].manifest["counts_by_kind"]})
        project("inventory/summary.json", state["inventory_status"])
        project("inventory/entries.json", {"entries": state["entries"]})
        for collection, template in (("classes", "classes/{id}.json"),
                                     ("tasks", "tasks/{id}/task.json"),
                                     ("runs", "coordination/runs/{id}.json"),
                                     ("messages", "messages/{id}.json"),
                                     ("mvps", "mvp/{id}/mvp.json")):
            for key, value in state[collection].items():
                # IDs were validated at request time; check persisted keys too.
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,249}", key):
                    raise ReplicaError("Invalid persisted projection identifier")
                project(template.format(id=key), value)

    def migrate(self, *, expected_revision, expected_sha256):
        """Explicit, lock-protected schema switch; safe to retry after any crash."""
        from .snapshot import Inventory
        if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
                or not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise ReplicaError("Migration requires an exact revision and SHA-256")
        with self.locked(allow_legacy=True) as state:
            if state is None:
                raise ReplicaError("Replica workspace is not initialized")
            if state["schema"] == 3:
                raise ReplicaError("Schema 3 is already adopted; schema-1 migrate is unsupported")
            if state["schema"] == 2:
                migration = state.get("storage_migration", {})
                if (migration.get("legacy_revision") != expected_revision
                        or migration.get("legacy_sha256") != expected_sha256):
                    raise ReplicaError("Workspace already migrated from a different snapshot")
                # Verify backup and all referenced data on explicit recovery.
                self.verify_backup(migration["backup"], expected_sha256)
                for key in state["inventory"].manifest["shards"]:
                    state["inventory"]._shard(key)
                    state["inventory"].cache.clear()
                for key in state["inventory"].scopes.manifest["shards"]:
                    state["inventory"].scopes._shard(key)
                    state["inventory"].scopes.cache.clear()
                for receipt in state["receipts"].values():
                    self.receipt(receipt)
                self.publish(state)
                return {"ok": True, "migrated": True, "replayed": True, "revision": state["revision"],
                        "migration": migration}
            if state["revision"] != expected_revision:
                raise ReplicaError("Legacy revision changed; stop writers and recheck snapshot")
            # Copy the exact committed bytes, not a normalized JSON rendering.
            backup = f"coordination/legacy/{expected_sha256}.json"
            target = self.metadata_path(backup)
            self._mkdir(target.parent)
            digest = hashlib.sha256()
            temporary = None
            try:
                with self.authority.open("rb") as source, tempfile.NamedTemporaryFile(
                        dir=target.parent, prefix=".backup-", delete=False) as destination:
                    temporary = Path(destination.name)
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                        destination.write(block)
                    destination.flush()
                    os.fsync(destination.fileno())
                if digest.hexdigest() != expected_sha256:
                    raise ReplicaError("Legacy SHA-256 changed; no authority switch")
                if target.exists():
                    self.verify_backup(backup, expected_sha256)
                else:
                    os.replace(temporary, target)
                    temporary = None
                    self._fsync_dir(target.parent)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self.verify_backup(backup, expected_sha256)
            inventory = Inventory(self)
            for identifier, obj in state["inventory"].items():
                inventory[identifier] = obj
            # Exact round-trip comparison before committing the new reference.
            inventory.flush()
            inventory.cache.clear()
            verified = Inventory(self, inventory.reference, inventory.scopes)
            if len(verified) != len(state["inventory"]):
                raise ReplicaError("Migration inventory count mismatch")
            for key in sorted(verified.manifest["shards"]):
                for identifier in verified._shard(key):
                    if verified[identifier] != state["inventory"][identifier]:
                        raise ReplicaError(f"Migration inventory mismatch: {identifier}")
                verified.cache.clear()
            state["inventory"] = inventory
            state["schema"] = 2
            state["storage_migration"] = {"legacy_revision": expected_revision,
                "legacy_sha256": expected_sha256, "backup": backup,
                "inventory_ref": inventory.reference}
            # The lock has remained held since the revision check. Old-format
            # processes waiting for this lock will reject schema 2 on entry.
            state["revision"] += 1
            self.commit(state)
            error = None
            try:
                self.publish(state)
            except (OSError, ReplicaError) as exc:
                error = str(exc)
            return {"ok": error is None, "migrated": True, "committed": True,
                    "revision": state["revision"], "migration": state["storage_migration"],
                    **({"projection_error": error} if error else {})}

    def verify_backup(self, relative, expected):
        path = self.metadata_path(relative)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ReplicaError("Migration backup must be a regular file")
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual == expected:
                os.fsync(stream.fileno())
        if actual != expected:
            raise ReplicaError("Migration backup SHA-256 mismatch")
        self._sync_parents(path.parent)

    def export_inventory(self, output):
        """Stream a complete compatibility projection without retaining all shards."""
        from .engine import _output_path
        from .snapshot import encoded
        with self.locked() as state:
            if state is None:
                raise ReplicaError("Replica workspace is not initialized")
            from .paths import resolve_path
            output = resolve_path(self.workspace, state, str(output))
            if output.absolute() == self.workspace / "inventory/objects.json":
                output = self.metadata_path("inventory/objects.json")
            else:
                output = Path(_output_path(self, state, str(output)))
            self._mkdir(output.parent)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".export-", delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(b'{"objects":{')
                    first = True
                    inventory = state["inventory"]
                    for key in sorted(inventory.manifest["shards"]):
                        for identifier in sorted(inventory._shard(key)):
                            if not first:
                                stream.write(b",")
                            first = False
                            stream.write(encoded(identifier).rstrip(b"\n") + b":" + encoded(inventory[identifier]).rstrip(b"\n"))
                        inventory.cache.clear()
                    marker = {"authority": "coordination/state.json", "revision": state["revision"],
                              "editable": False, "inventory_ref": state["inventory_ref"]}
                    if state.get("schema") == 3:
                        marker.update(path_bindings="coordination/path-bindings.json", binding_revision=state["relocation"]["binding_revision"])
                    stream.write(b'},"_projection":' + encoded(marker).rstrip(b"\n") + b"}\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                with temporary.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                os.replace(temporary, output)
                temporary = None
                self._fsync_dir(output.parent)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return {"ok": True, "revision": state["revision"], "objects": len(state["inventory"]),
                    "path": str(output), "sha256": digest}
