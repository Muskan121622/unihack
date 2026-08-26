import os

filepath = 'v2_pipeline/llm_extractor.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

bad_line = 'err = r.json().get("error", {}).get("message", r.text[:150]) if r.text else ""'
safe_block = '''try:
                    err = r.json().get("error", {}).get("message", r.text[:150])
                except:
                    err = r.text[:150] if hasattr(r, "text") and r.text else "No error text"'''

content = content.replace(bad_line, safe_block)

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)
