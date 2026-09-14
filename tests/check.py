import asyncio, json
from pathlib import Path
from mcp import Client
from shared_memory_mcp.server import mcp

async def main():
    async with Client(mcp) as client:
        tools=await client.list_tools()
        assert len(tools.tools)==3
        manifest=json.loads(Path("server-card.json").read_text())
        assert manifest["tools"] == [t.model_dump(by_alias=True, exclude_none=True) for t in tools.tools]
        for name,args in [('search_notes',{'query':'caching'}),('read_note',{'note_id':'3'}),('search_notes',{'parent_id':'3'})]:
            result=await client.call_tool(name,args)
            assert not result.is_error, result
        print('PASS: MCP discovery, public search, full read, and string-ID reply filter.')
asyncio.run(main())
