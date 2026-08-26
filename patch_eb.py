import os
import re

filepath = 'v2_pipeline/evidence_builder.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

bad_line = "d = os.path.join(artifacts_base, mpn)"
good_line = "safe_mpn = __import__('re').sub(r'[\\\\/*?:\"<>|]', '_', mpn)\n    d = os.path.join(artifacts_base, safe_mpn)"

if bad_line in content:
    content = content.replace(bad_line, good_line)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched evidence_builder.py successfully")
else:
    print("Could not find line in evidence_builder.py")
