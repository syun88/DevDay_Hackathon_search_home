"""Run real MCP stdio -> local backend -> source calls; print a small evidence report.

No fixture replaces the search or property address call. A failure exits nonzero.
Run with the interpreter that has requirements.txt installed.
"""
import asyncio
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters


async def require_call(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    if result.is_error or not isinstance(result.structured_content, dict):
        raise RuntimeError(f"{name} failed: {result.content}")
    return result.structured_content


async def main():
    command = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).with_name("server.py").resolve())])
    async with Client(command) as client:
        tools = await client.list_tools()
        names = sorted(tool.name for tool in tools.tools)
        assert names == ["get_capabilities", "get_property_address", "search_properties", "search_public_housing"]
        capabilities = await require_call(client, "get_capabilities")
        conditions = {"lat": 35.681236, "lon": 139.767125, "radius_km": 1, "max_rent_man": 15, "page": 1}
        page = await require_call(client, "search_properties", conditions)
        assert page.get("page") == 1 and page.get("page_size") == 20
        assert page.get("results"), "Real search returned no candidates; address proof requires one candidate"
        sample = page["results"][0]
        address = await require_call(client, "get_property_address", {"source_id": sample["source_id"]})
        assert address.get("address") and address.get("source_id") == sample["source_id"]
        public = await require_call(client, "search_public_housing", {"source": "tokyo", "area": "千代田", "offset": 0})
        invalid = await client.call_tool("get_property_address", {"source_id": "https://example.com"})
        assert invalid.is_error
        print(json.dumps({
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "sdk_version": importlib.metadata.version("mcp"),
            "transport": "stdio subprocess; official MCP Client initializes connection",
            "tools": names,
            "health": capabilities["backend_health"],
            "search_conditions": conditions,
            "search": {key: page.get(key) for key in ("source", "fetched_at", "page", "page_size", "upstream_totalhits", "inspected_count", "has_next", "cached", "availability_status")},
            "returned_count": len(page["results"]),
            "sample": {key: sample.get(key) for key in ("source_id", "source_url", "address", "rent_jpy", "station_access", "availability_status", "within_radius_verified")},
            "property_address": address,
            "public_housing": {"source": "tokyo", "area": "千代田", "returned_count": len(public.get("records", [])), "total": public.get("total"), "fetched_at": public.get("fetched_at")},
            "invalid_source_id_rejected": invalid.is_error,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
