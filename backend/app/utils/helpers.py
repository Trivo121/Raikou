def clean_filename(filename: str) -> str:
    return "".join(c for c in filename if c.isalnum() or c in "._-").strip()
