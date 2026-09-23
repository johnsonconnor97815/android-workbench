#!/usr/bin/env python3
"""Run the installed Semgrep MCP tools over local stdio without HTTP OAuth setup."""
import argparse
import os


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-t', '--transport', choices=['stdio'], default='stdio')
    args = parser.parse_args()
    os.environ['SEMGREP_MCP'] = 'true'

    from mcp.server.fastmcp import FastMCP
    from semgrep.mcp.server import deregister_tools, register, server_lifespan

    # Semgrep 1.177.0's CLI fetches OAuth metadata even for stdio. Only HTTP
    # needs that setup; reuse the upstream tools and lifecycle for local IPC.
    server = FastMCP('Semgrep', stateless_http=False, json_response=True,
                     lifespan=server_lifespan)
    register(server)
    deregister_tools(server, args.transport)
    server.run(transport=args.transport)


if __name__ == '__main__':
    main()
