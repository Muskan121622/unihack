import os
import sys

filepath = 'v2_pipeline/run_1000_rows.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add import socket
content = content.replace(
    'import os, sys, csv, json, time, traceback',
    'import os, sys, csv, json, time, traceback, socket'
)

# 2. Add check_internet function
check_internet_func = '''
def check_internet():
    try:
        # Try to connect to Cloudflare DNS
        socket.create_connection(("1.1.1.1", 53), timeout=3)
        return True
    except OSError:
        pass
    print("\\n[!!!] CRITICAL: No internet connection detected! [!!!]")
    print("[!!!] Aborting to prevent empty CSVs. Reconnect to Wi-Fi and restart. [!!!]")
    sys.exit(1)

'''
content = content.replace('def _alt_mpns(debug_entry: dict) -> list[str]:', check_internet_func + 'def _alt_mpns(debug_entry: dict) -> list[str]:')

# 3. Add to the start of main
content = content.replace(
    'def main():\\n    banner("v2 Pipeline',
    'def main():\\n    check_internet()\\n    banner("v2 Pipeline'
)

# 4. Add inside the per-product loop
content = content.replace(
    'for idx, input_row in enumerate(rows_to_run):\\n        mpn',
    'for idx, input_row in enumerate(rows_to_run):\\n        check_internet()\\n        mpn'
)

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)

print("Modification complete.")
