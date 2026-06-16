"""One-shot script: refresh stock name cache from mootdx."""
import json
from pathlib import Path
from src.data.mootdx_helper import get_quotes

cache_dir = Path.home() / ".vibe-trading" / "cache"
cache_dir.mkdir(parents=True, exist_ok=True)
cache_file = cache_dir / "stock_names.json"

NULL_BYTE = chr(0)
names = {}
client = get_quotes(timeout=15)
for market_id, suffix in [(0, ".SZ"), (1, ".SH")]:
    df = client.stocks(market=market_id)
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            code = str(row.get("code", "")).strip()
            name = str(row.get("name", "")).strip()
            name = name.replace(NULL_BYTE, "")
            if code and name and len(code) == 6:
                names[code + suffix] = name

cache_file.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
print(f"Saved {len(names)} stock names to {cache_file}")

for code in ["000001.SZ", "000725.SZ", "002230.SZ", "600519.SH", "300750.SZ",
             "512010.SH", "159915.SZ", "513100.SH"]:
    print(f"  {code}: {names.get(code, 'NOT FOUND')}")
