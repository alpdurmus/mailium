import pandas as pd
import io
import re

def parse_excel_file(file_bytes: bytes, filename: str):
    """
    Parses an Excel (.xlsx, .xls) or CSV file and returns column names and rows as dictionaries.
    Auto-detects probable email column.
    """
    if filename.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(file_bytes))
    else:
        df = pd.read_excel(io.BytesIO(file_bytes))

    # Fill NaN values with empty string
    df = df.fillna("")

    # Clean column headers
    columns = [str(col).strip() for col in df.columns]
    df.columns = columns

    # Convert to list of dicts
    records = df.to_dict(orient="records")

    # Auto-detect email column
    detected_email_col = None
    for col in columns:
        col_lower = col.lower()
        if "email" in col_lower or "mail" in col_lower or "e-mail" in col_lower:
            detected_email_col = col
            break

    if not detected_email_col and len(records) > 0:
        # Inspect first row values to check for @ symbol
        for col in columns:
            val = str(records[0][col])
            if re.match(r"[^@]+@[^@]+\.[^@]+", val):
                detected_email_col = col
                break

    return {
        "columns": columns,
        "records": records,
        "total_rows": len(records),
        "detected_email_col": detected_email_col or (columns[0] if columns else "")
    }
