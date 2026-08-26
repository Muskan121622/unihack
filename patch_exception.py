import os

filepath = 'v2_pipeline/llm_extractor.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

bad_block = '''except Exception as e:
                print(f"    [LLM] Request exception: {e}")
                stats["error"] = str(e)
                time.sleep(5)
                break'''

fixed_block = '''except Exception as e:
                print(f"    [LLM] Request exception: {e}")
                stats["error"] = str(e)
                _rotate_bluesminds_key()
                time.sleep(2)
                continue'''

if bad_block in content:
    content = content.replace(bad_block, fixed_block)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched exception handling to rotate keys instead of giving up!")
else:
    print("Could not find the exact block to replace. Let me check the file.")
