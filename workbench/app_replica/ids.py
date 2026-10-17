"""Stable identities for original objects and durable workflow records."""

import hashlib
import json


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def stable_id(kind, *parts):
    digest = hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()
    return f"{kind}-{digest}"


def object_id(carrier, kind, signature):
    """Use original carrier/signature, never a replacement's readable name."""
    return stable_id("object", carrier, kind, signature)


def task_id(role, mode, object_id=None, class_id=None):
    # Class membership is a relation, not a second implementation identity.
    return stable_id("task", role, mode, object_id, class_id if object_id is None else None)
