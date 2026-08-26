import os
import json
import shutil

count = 0
art_dir = "artifacts"
for mpn in os.listdir(art_dir):
    mpn_dir = os.path.join(art_dir, mpn)
    if os.path.isdir(mpn_dir):
        csv_path = os.path.join(mpn_dir, "v2_final_output.csv")
        llm_path = os.path.join(mpn_dir, "v2_llm_output.json")
        
        # If CSV exists, but LLM output doesn't exist (meaning it skipped LLM), it's a BAD row!
        if os.path.exists(csv_path) and not os.path.exists(llm_path):
            os.remove(csv_path)
            count += 1
            print(f"Deleted bad CSV for {mpn}")

print(f"Total cleaned: {count}")
