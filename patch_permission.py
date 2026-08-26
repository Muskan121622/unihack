import os

filepath = 'app.py'
with open(filepath, 'r', encoding='utf-8') as f:
    content = f.read()

bad_block = '''    if os.path.exists(excel_path):
        with open(excel_path, "rb") as f:
            st.download_button(
                label="? DOWNLOAD 1,000 ROWS EXCEL",
                data=f,
                file_name="final_delivery_1000_rows.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )'''

good_block = '''    if os.path.exists(excel_path):
        try:
            with open(excel_path, "rb") as f:
                file_data = f.read()
            st.download_button(
                label="? DOWNLOAD 1,000 ROWS EXCEL",
                data=file_data,
                file_name="final_delivery_1000_rows.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        except PermissionError:
            st.button("?? Close Excel to Unlock Download", disabled=True)'''

if bad_block in content:
    content = content.replace(bad_block, good_block)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched PermissionError")
else:
    print("Could not find the block to replace")
