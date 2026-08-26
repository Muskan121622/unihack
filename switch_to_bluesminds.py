import os
import re

filepath = 'v2_pipeline/llm_extractor.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Replace globals
content = content.replace(
    'SAMBANOVA_API_KEYS = [k.strip() for k in os.getenv("SAMBANOVA_API_KEY", "").split(",") if k.strip()]',
    'BLUESMINDS_API_KEYS = [k.strip() for k in os.getenv("BLUESMINDS_API_KEY", "").split(",") if k.strip()]'
)
content = content.replace('_samba_key_idx = 0', '_bluesminds_key_idx = 0')

# 2. Replace the getter/rotator functions
content = content.replace(
    'def _get_samba_key():\n    return SAMBANOVA_API_KEYS[_samba_key_idx] if SAMBANOVA_API_KEYS else ""',
    'def _get_bluesminds_key():\n    return BLUESMINDS_API_KEYS[_bluesminds_key_idx] if BLUESMINDS_API_KEYS else ""'
)
content = content.replace(
    'def _rotate_samba_key():\n    global _samba_key_idx\n    if len(SAMBANOVA_API_KEYS) > 1:\n        _samba_key_idx = (_samba_key_idx + 1) % len(SAMBANOVA_API_KEYS)\n        print(f"    [LLM] Switched to API Key index {_samba_key_idx}")',
    'def _rotate_bluesminds_key():\n    global _bluesminds_key_idx\n    if len(BLUESMINDS_API_KEYS) > 1:\n        _bluesminds_key_idx = (_bluesminds_key_idx + 1) % len(BLUESMINDS_API_KEYS)\n        print(f"    [LLM] Switched to API Key index {_bluesminds_key_idx}")'
)

# 3. Replace Models (Bluesminds has gpt-4o, gpt-4o-mini, and custom gpt-oss-20b)
content = re.sub(
    r'_MODELS\s*=\s*\[.*?\]',
    '_MODELS = [\n    "gpt-4o",\n    "gpt-4o-mini",\n    "gpt-oss-20b"\n]',
    content,
    flags=re.DOTALL
)

# 4. Replace extract_schema function
start = content.find('def extract_schema(')
if start == -1:
    print("Could not find extract_schema")
    exit(1)
    
end = content.find('# Pre-fill: put known structured facts directly into the schema dict')

new_func = '''def extract_schema(
    mpn: str,
    brand: str,
    part_desc: str,
    ev: dict,
    chunks: list,
) -> tuple[dict, dict]:
    """Call BluesMinds LLM to fill the schema."""
    prompt = _build_prompt(mpn, brand, part_desc, ev, chunks)
    stats  = {
        "prompt_chars": len(prompt),
        "model_used":   None,
        "models_tried": [],
        "success":      False,
        "error":        None,
    }

    if not BLUESMINDS_API_KEYS:
        stats["error"] = "BLUESMINDS_API_KEY not set"
        return {}, stats

    print(f"    [LLM] Prompt {stats['prompt_chars']} chars | {len(chunks)} chunks")

    for model in _MODELS:
        stats["models_tried"].append(model)
        print(f"    [LLM] Trying {model}")
        
        max_attempts_per_model = len(BLUESMINDS_API_KEYS) + 1
        
        for attempt in range(max_attempts_per_model):
            api_key = _get_bluesminds_key()
            url = "https://api.bluesminds.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            
            payload = {
                "model":           model,
                "messages":        [
                    {"role": "system", "content": "You are a precise JSON-only product data extractor. Output ONLY valid JSON, starting with { and ending with }."},
                    {"role": "user", "content": prompt},
                ],
                "temperature":     0.0,
                "response_format": {"type": "json_object"},
            }
            
            try:
                import requests, time, json
                r = requests.post(url, headers=headers, json=payload, timeout=120)
            except Exception as e:
                print(f"    [LLM] Request exception: {e}")
                stats["error"] = str(e)
                time.sleep(5)
                break
    
            if r.status_code == 200:
                try:
                    raw = r.json()["choices"][0]["message"]["content"]
                    raw = re.sub(r"^`(?:json)?\\\s*", "", raw.strip())
                    raw = re.sub(r"\\\s*`$", "", raw)
                    result = json.loads(raw)
                    stats["model_used"] = model
                    stats["success"]    = True
                    print(f"    [LLM] Success with {model}")
                    return result, stats
                except Exception as e:
                    print(f"    [LLM] JSON parse or response error ({model}): {e}")
                    stats["error"] = f"JSON/Parse: {e}"
                    break
            elif r.status_code == 413:
                print(f"    [LLM] Payload too large for {model}")
                break
            elif r.status_code == 429:
                err_msg = r.json().get("error", {}).get("message", "") if r.text else ""
                print(f"    [LLM] 429 Rate Limit on BluesMinds: {err_msg[:100]}")
                _rotate_bluesminds_key()
                time.sleep(1)
                if attempt == len(BLUESMINDS_API_KEYS) - 1:
                    print(f"    [LLM] All keys exhausted for this attempt. Waiting 75s...")
                    time.sleep(75)
            else:
                err = r.json().get("error", {}).get("message", r.text[:150]) if r.text else ""
                print(f"    [LLM] HTTP {r.status_code}: {err}")
                stats["error"] = f"HTTP {r.status_code}: {err}"
                
                if r.status_code == 400:
                    break # Model failed to output JSON, move to next model!
                elif r.status_code in [401, 403, 500, 503]:
                    _rotate_bluesminds_key()
                    time.sleep(1)
                else:
                    time.sleep(5)
                    break

    print(f"    [LLM] All models/keys failed for {mpn}")
    return {}, stats


# ---------------------------------------------------------------------------
'''

content = content[:start] + new_func + content[end:]
with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated extract_schema to use BluesMinds API!")
