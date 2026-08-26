import os
filepath = 'v2_pipeline/run_1000_rows.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace('def main():\n    banner(', 'def main():\n    check_internet()\n    banner(')
content = content.replace('for idx, input_row in enumerate(rows_to_run):\n        mpn', 'for idx, input_row in enumerate(rows_to_run):\n        check_internet()\n        mpn')

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(content)
