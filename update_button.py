import os

filepath = 'app.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

bad_text = 'label="? DOWNLOAD 1,000 ROWS EXCEL"'
good_text = 'label="? Download output of given 1000 rows sample"'

if bad_text in content:
    content = content.replace(bad_text, good_text)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Button text updated successfully")
else:
    print("Could not find the button text")
