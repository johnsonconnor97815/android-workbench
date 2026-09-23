#!/usr/bin/env python3
"""Start upstream PyGhidra-MCP with the verified local Chroma model cache."""
import argparse
from pathlib import Path
import sys
import tempfile


def project_arguments(root, remaining):
    """Keep each managed stdio instance's persistent project and index separate."""
    if root is None or any(arg in ('-h', '--help', '--version') for arg in remaining):
        return remaining
    # An explicit path also allows reopening a saved project after its owner exits.
    if any(arg == '--project-path' or arg.startswith('--project-path=') for arg in remaining):
        return remaining
    root = root.expanduser().resolve()
    if any(part.startswith('.') for part in root.parts):
        raise RuntimeError('Ghidra project paths cannot contain dot-prefixed components')
    sessions = root / 'sessions'
    sessions.mkdir(parents=True, exist_ok=True)
    project = Path(tempfile.mkdtemp(prefix='session-', dir=sessions))
    print(f'Ghidra MCP project directory: {project}', file=sys.stderr, flush=True)
    # Keep the directory after shutdown so analysis data can be reopened explicitly.
    return ['--project-path', str(project), *remaining]


def main():
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--model-cache', required=True, type=Path)
    parser.add_argument('--project-root', type=Path)
    args, remaining = parser.parse_known_args()
    # Chroma 1.x hardcodes Path.home() here and has no cache-path env option.
    # Redirect only this model cache; retain the real user HOME for other tools.
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    ONNXMiniLM_L6_V2.DOWNLOAD_PATH = args.model_cache.resolve()
    if not (args.model_cache / 'onnx/model.onnx').is_file():
        raise RuntimeError('Missing verified ONNX model; rerun mcp_setup.py install --servers ghidra')
    from pyghidra_mcp.server import main as server_main
    sys.argv = ['pyghidra-mcp', *project_arguments(args.project_root, remaining)]
    server_main()


if __name__ == '__main__':
    main()
