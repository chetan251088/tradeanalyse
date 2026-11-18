"""One raw rollingoption request, to see what Dhan answers right now."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dhan_download as dd  # noqa: E402

get_auth_token, get_token, api = dd.bootstrap()
token = get_auth_token(sys.argv[1])
day = sys.argv[2] if len(sys.argv) > 2 else "2025-01-09"
payload = {
    "exchangeSegment": "NSE_FNO", "interval": "1", "securityId": 13, "instrument": "OPTIDX",
    "expiryFlag": "WEEK", "expiryCode": 1, "strike": "ATM", "drvOptionType": "CALL",
    "requiredData": ["close", "spot"], "fromDate": day, "toDate": day,
}
try:
    res = api("/v2/charts/rollingoption", token, "POST", json.dumps(payload))
except Exception as e:
    print("EXCEPTION", type(e).__name__, str(e)[:500])
else:
    text = json.dumps(res)
    print(text[:600] if "errorCode" in text or "status" in text else f"OK, {len((res.get('data') or {}).get('ce', {}).get('timestamp', []))} bars")
