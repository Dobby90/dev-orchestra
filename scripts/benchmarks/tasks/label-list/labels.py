def parse_labels(text):
    return [part.strip() for part in text.split(",") if part.strip()]
