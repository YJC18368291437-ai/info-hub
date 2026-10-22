import re


def redact(text, secrets=()):
    for secret in secrets:
        if secret:
            text=text.replace(secret,'[REDACTED]')
    text=re.sub(r'(https?://[^\s?]+)\?[^\s]+',r'\1?[REDACTED]',text)
    return text
