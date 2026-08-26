import csv
with open('final_delivery_5_rows.csv', 'r', encoding='utf-8') as f:
    r = csv.reader(f)
    headers = next(r)
    rows = list(r)
    
    print("================ PREVIEW OF EXTRACTED DATA ================")
    for idx, row in enumerate(rows):
        mpn = row[headers.index('Mfg_Part_Num')]
        print(f"\n--- Product {idx+1}: {mpn} ---")
        
        # Print core attributes
        core_fields = ['MANUFACTURER_NAME', 'BRAND_NAME', 'LENGTH', 'WIDTH', 'WEIGHT', 'Product Name']
        for field in core_fields:
            if field in headers:
                val = row[headers.index(field)]
                if val: print(f"  {field}: {val}")
                
        # Print dynamic attributes
        print("  Dynamic Attributes:")
        count = 0
        for i in range(1, 51):
            lbl_col = f"ATTRIBUTE_LABEL {i}"
            val_col = f"ATTRIBUTE_VALUE {i}"
            uom_col = f"ATTRIBUTE_UOM {i}"
            if lbl_col in headers and row[headers.index(lbl_col)]:
                lbl = row[headers.index(lbl_col)]
                val = row[headers.index(val_col)]
                uom = row[headers.index(uom_col)]
                print(f"    - {lbl}: {val} {uom}".strip())
                count += 1
        if count == 0:
            print("    (None extracted)")
