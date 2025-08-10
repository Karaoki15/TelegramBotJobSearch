import re

URL_REGEX = re.compile(
    r'((?:(?:https?|ftp):\/\/)'
    r'(?:\S+(?::\S*)?@)?'
    r'(?:(?:[1-9]\d?|1\d\d|2[01]\d|22[0-3])'
    r'(?:\.(?:1?\d{1,2}|2[0-4]\d|25[0-5])){2}'
    r'(?:\.(?:[1-9]\d?|1\d\d|2[0-4]\d|25[0-4]))|'
    r'(?:(?:[a-z\u00a1-\uffff0-9]+-?)*[a-z\u00a1-\uffff0-9]+)'
    r'(?:\.(?:[a-z\u00a1-\uffff0-9]+-?)*[a-z\u00a1-\uffff0-9]+)*'
    r'(?:\.(?:[a-z\u00a1-\uffff]{2,}))'
    r')(?::\d{2,5})?'
    r'(?:[\/?#]\S*)?)', re.IGNORECASE
)

def contains_urls(text: str) -> bool:
    if not text:
        return False
    return bool(URL_REGEX.search(text))
