"""Content-addressed inventory shards; only explicit export walks every object.

Two hexadecimal SHA-256 digits select a bucket, independently of readable names.
Each transaction caches only buckets it accesses. Published files are immutable;
coordination/state.json selects the manifest and holds mutable scope decisions.
"""

from collections import Counter
from collections.abc import MutableMapping
from copy import deepcopy
import hashlib
import re

from .ids import canonical_json
from .store import ReplicaError


DEFAULT_SCOPE = {"disposition": "unknown", "reason": "Not yet decided", "evidence": []}


def encoded(value):
    return (canonical_json(value) + "\n").encode("utf-8")


def bucket(identifier):
    if not isinstance(identifier, str):
        raise KeyError(identifier)
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:2]


class Inventory(MutableMapping):
    def __init__(self, store, reference=None, scopes=None):
        self.store = store
        self.reference = reference
        self.scopes = scopes if scopes is not None else Scopes(store)
        self.manifest = store.read_blob(reference, "inventory/manifests") if reference else {
            "schema": 1, "partition": "sha256-id-prefix-2", "shards": {}, "count": 0,
            "counts_by_kind": {}}
        if (self.manifest.get("schema") != 1 or self.manifest.get("partition") != "sha256-id-prefix-2"
                or not isinstance(self.manifest.get("shards"), dict)):
            raise ReplicaError("Unsupported inventory manifest")
        for key, value in self.manifest["shards"].items():
            if not re.fullmatch(r"[0-9a-f]{2}", key) or not isinstance(value, dict):
                raise ReplicaError("Invalid inventory shard manifest")
            store.blob_path(value, "inventory/shards")
        if sum(value["count"] for value in self.manifest["shards"].values()) != self.manifest["count"]:
            raise ReplicaError("Inventory manifest count mismatch")
        self.cache = {}
        self.dirty = set()
        self.count = self.manifest["count"]

    def __deepcopy__(self, memo):
        # The referenced content cannot change; copying coordination must never
        # enumerate inventory. Preserve scope aliasing in the surrounding state.
        other = object.__new__(Inventory)
        memo[id(self)] = other
        other.store = self.store
        other.reference = self.reference
        other.scopes = deepcopy(self.scopes, memo)
        other.manifest = self.manifest
        other.cache = deepcopy(self.cache, memo) if self.dirty else {}
        other.dirty = set(self.dirty)
        other.count = self.count
        return other

    def _shard(self, key):
        if key not in self.cache:
            meta = self.manifest["shards"].get(key)
            records = self.store.read_blob(meta, "inventory/shards") if meta else {}
            if (not isinstance(records, dict) or (meta and len(records) != meta["count"])
                    or any(bucket(identifier) != key or record.get("id") != identifier
                           for identifier, record in records.items())):
                raise ReplicaError("Invalid inventory shard contents")
            self.cache[key] = records
        return self.cache[key]

    def __getitem__(self, identifier):
        record = deepcopy(self._shard(bucket(identifier))[identifier])
        record["scope"] = deepcopy(self.scopes.get(identifier, DEFAULT_SCOPE))
        return record

    def __setitem__(self, identifier, record):
        key = bucket(identifier)
        records = self._shard(key)
        value = deepcopy(record)
        scope = value.pop("scope", deepcopy(DEFAULT_SCOPE))
        if identifier not in records:
            self.count += 1
        if records.get(identifier) != value:
            records[identifier] = value
            self.dirty.add(key)
        self.set_scope(identifier, scope)

    def set_scope(self, identifier, scope):
        if scope == DEFAULT_SCOPE:
            self.scopes.pop(identifier, None)
        else:
            self.scopes[identifier] = deepcopy(scope)

    def __delitem__(self, identifier):
        raise ReplicaError("Inventory objects cannot be discarded")

    def __iter__(self):
        for key in sorted(set(self.manifest["shards"]) | self.dirty):
            yield from sorted(self._shard(key))

    def __len__(self):
        return self.count

    def scope_counts(self):
        counts = Counter({"unknown": self.count})
        counts["unknown"] -= len(self.scopes)
        counts.update(self.scopes.counts())
        return counts

    def flush(self):
        if self.reference is not None and not self.dirty:
            return self.reference
        shards = dict(self.manifest["shards"])
        for key in sorted(self.dirty):
            records = self.cache[key]
            sources = hashlib.sha256()
            for identifier, item in sorted(records.items()):
                sources.update(encoded([identifier, {field: item[field] for field in
                               ("carrier", "source", "sources", "origins") if field in item}]))
            shards[key] = {**self.store.write_blob("inventory/shards", records),
                           "count": len(records),
                           "counts_by_kind": dict(Counter(item["kind"] for item in records.values())),
                           "carriers": sorted({item["carrier"] for item in records.values() if item.get("carrier")}),
                           "source_sha256": sources.hexdigest()}
        kinds = Counter()
        for meta in shards.values():
            kinds.update(meta["counts_by_kind"])
        self.manifest = {"schema": 1, "partition": "sha256-id-prefix-2", "shards": shards,
                         "count": self.count, "counts_by_kind": dict(kinds)}
        self.reference = self.store.write_blob("inventory/manifests", self.manifest)
        self.dirty.clear()
        return self.reference


class Scopes(MutableMapping):
    """Sparse scope decisions with copy-on-write shards and manifest counts."""
    def __init__(self, store, reference=None):
        self.store = store
        self.reference = reference
        self.manifest = store.read_blob(reference, "scopes/manifests") if reference else {
            "schema": 1, "partition": "sha256-id-prefix-2", "shards": {}, "count": 0,
            "counts": {}}
        if self.manifest.get("schema") != 1 or self.manifest.get("partition") != "sha256-id-prefix-2":
            raise ReplicaError("Unsupported scope manifest")
        for key, value in self.manifest["shards"].items():
            if not re.fullmatch(r"[0-9a-f]{2}", key):
                raise ReplicaError("Invalid scope shard")
            store.blob_path(value, "scopes/shards")
        if sum(meta["count"] for meta in self.manifest["shards"].values()) != self.manifest["count"]:
            raise ReplicaError("Scope manifest count mismatch")
        self.cache = {}
        self.dirty = set()
        self.count = self.manifest["count"]

    def __deepcopy__(self, memo):
        other = object.__new__(Scopes)
        memo[id(self)] = other
        other.store, other.reference, other.manifest = self.store, self.reference, self.manifest
        other.cache = deepcopy(self.cache, memo) if self.dirty else {}
        other.dirty, other.count = set(self.dirty), self.count
        return other

    def _shard(self, key):
        if key not in self.cache:
            meta = self.manifest["shards"].get(key)
            records = self.store.read_blob(meta, "scopes/shards") if meta else {}
            if ((meta and len(records) != meta["count"])
                    or any(bucket(identifier) != key for identifier in records)):
                raise ReplicaError("Invalid scope shard contents")
            self.cache[key] = records
        return self.cache[key]

    def __getitem__(self, identifier):
        return deepcopy(self._shard(bucket(identifier))[identifier])

    def __setitem__(self, identifier, value):
        key = bucket(identifier)
        records = self._shard(key)
        if identifier not in records:
            self.count += 1
        if records.get(identifier) != value:
            records[identifier] = deepcopy(value)
            self.dirty.add(key)

    def __delitem__(self, identifier):
        key = bucket(identifier)
        del self._shard(key)[identifier]
        self.count -= 1
        self.dirty.add(key)

    def __iter__(self):
        for key in sorted(set(self.manifest["shards"]) | self.dirty):
            yield from sorted(self._shard(key))

    def __len__(self):
        return self.count

    def counts(self):
        counts = Counter(self.manifest["counts"])
        for key in self.dirty:
            counts.subtract(self.manifest["shards"].get(key, {}).get("counts", {}))
            counts.update(item["disposition"] for item in self.cache[key].values())
        return counts

    def flush(self):
        if self.reference is not None and not self.dirty:
            return self.reference
        shards = dict(self.manifest["shards"])
        for key in sorted(self.dirty):
            records = self.cache[key]
            shards[key] = {**self.store.write_blob("scopes/shards", records), "count": len(records),
                           "counts": dict(Counter(item["disposition"] for item in records.values()))}
        self.manifest = {"schema": 1, "partition": "sha256-id-prefix-2", "shards": shards,
                         "count": self.count, "counts": dict(self.counts())}
        self.reference = self.store.write_blob("scopes/manifests", self.manifest)
        self.dirty.clear()
        return self.reference
