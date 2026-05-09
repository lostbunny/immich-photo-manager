"""Entry point: python -m immich_mcp_server"""

import os
import sys


def main():
    # Default to stdio + loopback host for safer local-only operation.
    # Override with MCP_TRANSPORT=http and MCP_HOST=0.0.0.0 if you really
    # want to expose the server on the LAN.
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if transport == "stdio":
        from .server import mcp
        mcp.run(transport="stdio")
    else:
        import uvicorn
        port = int(os.environ.get("MCP_PORT", "8626"))
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        print(f"Immich MCP Server starting on {host}:{port}")
        uvicorn.run(
            "immich_mcp_server.server:app",
            host=host,
            port=port,
            log_level="info",
        )


if __name__ == "__main__":
    main()
