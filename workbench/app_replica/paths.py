"""Explicit filesystem operands for adopted workspaces; stored provenance stays raw."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .store import ReplicaError


def lexical(value, *, base=None, strict=True):
    if not isinstance(value, (str, Path)) or not str(value) or '\x00' in str(value):
        raise ReplicaError('Path must be a nonempty filesystem operand')
    path = Path(value).expanduser()
    if strict and '..' in path.parts:
        raise ReplicaError('Path traversal is unsupported for bound operands')
    if not path.is_absolute():
        if base is None:
            raise ReplicaError('Relative path requires an explicit base')
        path = Path(base) / path
    return Path(os.path.normpath(path))


def no_symlinks(path):
    path = Path(path)
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise ReplicaError(f'Symlink filesystem component: {component}')
    return path


def read_regular(path):
    path = no_symlinks(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ReplicaError(f'Expected regular file: {path}')
        return stream.read()


def hash_regular(path):
    path = no_symlinks(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ReplicaError(f'Expected regular file: {path}')
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class PathBindings:
    def __init__(self, workspace, rules=(), revision=0):
        self.workspace = lexical(workspace, base=Path.cwd(), strict=False)
        self.rules = tuple((lexical(r['from']), lexical(r['to'])) for r in rules)
        self.revision = revision

    @classmethod
    def from_state(cls, workspace, state):
        binding = (state or {}).get('relocation', {})
        if state and state.get('schema') == 3:
            if (binding.get('format') != 1 or type(binding.get('binding_revision')) is not int
                    or binding['binding_revision'] < 1
                    or binding.get('target_workspace') != str(lexical(workspace, base=Path.cwd(), strict=False))
                    or binding.get('continuation_backend') != 'native'):
                raise ReplicaError('Bound state requires its exact target workspace and valid native binding')
            no_symlinks(lexical(workspace, base=Path.cwd(), strict=False))
        return cls(workspace, binding.get('bindings', ()), binding.get('binding_revision', 0))

    def path(self, value, *, base=None, operational=True):
        path = lexical(value, base=base or self.workspace, strict=bool(self.revision))
        if not self.revision:
            return path.resolve()
        # Destinations must not be remapped. Rule validation forbids chaining.
        matches = [(old, new) for old, new in self.rules if path.is_relative_to(old)]
        if len(matches) > 1:
            raise ReplicaError('Ambiguous binding roots')
        if matches:
            old, new = matches[0]
            path = new / path.relative_to(old)
        if operational:
            roots = [self.workspace, *(new for _, new in self.rules)]
            if not any(path.is_relative_to(root) for root in roots):
                raise ReplicaError(f'Unmapped operational path outside bound clone: {path}')
            no_symlinks(path)
        return path

    def resolve(self, kind, value, *, exists=False, base=None):
        original = deepcopy(value)
        suffix = ''
        if isinstance(value, dict):
            if kind != 'locator' or 'path' not in value:
                raise ReplicaError('Structured locator requires path')
            result = self.resolve('path', value['path'], exists=exists, base=base)
            effective = deepcopy(value)
            effective['path'] = result['effective']
            return {**result, 'original': original, 'effective': effective, 'resolution_kind': 'source'}
        if not isinstance(value, str):
            raise ReplicaError('Resolution value must be a string or source locator')
        if kind not in {'path', 'locator', 'file_uri'}:
            raise ReplicaError('Resolution kind must be path, locator or file_uri')
        scheme = re.match(r'^([A-Za-z][A-Za-z0-9+.-]*):(?://|/)', value)
        opaque = re.match(r'^(?:https?|content|apk|dex|jar|zip):', value) or re.match(r'^L[^;]+;(?:(?:->|\.).*)?$', value)
        if opaque or (scheme and scheme.group(1) != 'file'):
            return {'original': original, 'effective': value, 'local_path': None,
                    'suffix_or_fragment': '', 'binding_revision': self.revision, 'resolution_kind': 'opaque'}
        if kind == 'file_uri':
            uri = urlsplit(value)
            if (uri.scheme != 'file' or uri.netloc not in {'', 'localhost'}
                    or '!' in uri.path or re.search(r'%(?![0-9a-fA-F]{2})', uri.path)):
                raise ReplicaError('Unsupported file URI authority or compound URI')
            path = self.path(unquote(uri.path), base=base)
            effective = urlunsplit(('file', uri.netloc, quote(str(path), safe='/:'), uri.query, uri.fragment))
            suffix = ('?' + uri.query if uri.query else '') + ('#' + uri.fragment if uri.fragment else '')
        else:
            raw = value
            if kind == 'locator':
                if '\n' in value or any(token in value for token in ('; ', ' && ', ' | ', '$(', '`')):
                    raise ReplicaError('Opaque command/prose is not a locator')
                raw, marker, fragment = value.partition('#')
                suffix = marker + fragment
                match = re.search(r':\d+(?:-\d+)?(?::\d+)?$', raw)
                if match:
                    literal = self.path(raw, base=base)
                    if literal.exists():
                        raise ReplicaError('Ambiguous locator filename; use path kind or structured locator')
                    raw, suffix = raw[:match.start()], match.group() + suffix
            path = self.path(raw, base=base)
            effective = str(path) + suffix
        return {'original': original, 'effective': effective, 'local_path': str(path),
                'suffix_or_fragment': suffix, 'binding_revision': self.revision, 'resolution_kind': kind,
                **({'exists_if_requested': path.exists()} if exists else {})}


def resolve_path(workspace, state, value, **options):
    return PathBindings.from_state(workspace, state).path(value, **options)


def resolve_locator(workspace, state, value):
    return PathBindings.from_state(workspace, state).resolve('locator', value)


def effective_project(workspace, state):
    resolver = PathBindings.from_state(workspace, state)
    project = deepcopy(state['project'])
    if not resolver.revision:
        return project
    for key in ('execution_root', 'output_path', 'output_dir', 'output', 'analysis_root', 'source_path', 'source_dir', 'workbench_source'):
        if isinstance(project.get(key), str):
            # Workbench can already be a registered current checkout outside the clone.
            project[key] = str(resolver.path(project[key], operational=key != 'workbench_source'))
    if state['relocation'].get('workbench_source'):
        project['workbench_source'] = state['relocation']['workbench_source']
    for key in ('source_paths', 'output_paths'):
        if isinstance(project.get(key), list):
            project[key] = [str(resolver.path(item)) for item in project[key]]
    project['_path_binding'] = {'workspace': str(resolver.workspace), 'revision': resolver.revision,
                                'projection': str(resolver.workspace / 'coordination/path-bindings.json'), 'bindings': list((state['relocation']['bindings']))}
    return project


def effective_writer_index(workspace, state):
    index = {}
    for raw, owner in state['writers'].items():
        path = str(resolve_path(workspace, state, raw))
        if path in index and index[path][1] != owner:
            raise ReplicaError(f'Output already has a logical writer through a binding alias: {path}')
        index.setdefault(path, (raw, owner))
    return index
