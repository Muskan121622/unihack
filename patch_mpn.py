import re

filepath = 'v2_pipeline/run_1000_rows.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

bad_block = "art_dir = os.path.join(ARTIFACTS, mpn)"
fixed_block = "safe_mpn = __import__('re').sub(r'[\\\\/*?:\"<>|]', '_', mpn)\n        art_dir = os.path.join(ARTIFACTS, safe_mpn)"

if bad_block in content:
    content = content.replace(bad_block, fixed_block)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched artifact directory creation to handle invalid filename characters!")
else:
    print("Could not find the exact block to replace. Let me check the file.")
