import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from dateutil import parser as date_parser

# Formats Transfermarkt serves dates in, tried in order before the day-first dateutil fallback.
DATE_FORMATS: tuple[str, ...] = ("%d/%m/%Y", "%d.%m.%Y", "%b %d, %Y", "%Y-%m-%d")
EMPTY_VALUES: frozenset[str] = frozenset({"", "-", "N/A"})
MAGNITUDES: dict[str, int] = {
    "k": 1_000,
    "m": 1_000_000,
    "mio": 1_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "mrd": 1_000_000_000,
}


def zip_lists_into_dict(list_keys: list, list_values: list) -> dict:
    """
    Create a dictionary by pairing elements from two lists.

    Args:
        list_keys (list): List of keys.
        list_values (list): List of values.

    Returns:
        dict: A dictionary created by pairing elements from the input lists.
    """
    return dict(zip(list_keys, list_values, strict=False))


def extract_from_url(tfmkt_url: str | None, element: str = "id") -> str | None:
    """
    Extract a specific element from a Transfermarkt URL using regular expressions.

    Args:
        tfmkt_url (str): The Transfermarkt URL from which to extract the element.
        element (str, optional): The element to extract (e.g., 'id', 'season_id', 'transfer_id').

    Returns:
        Optional[str]: The extracted element value or None if not found.
    """
    if not tfmkt_url:
        return None

    regex: str = (
        r"/(?P<code>[\w%-]+)"
        r"/(?P<category>[\w-]+)"
        r"/(?P<type>[\w-]+)"
        r"/(?P<id>\w+)"
        r"(/saison_id/(?P<season_id>\d{4}))?"
        r"(/transfer_id/(?P<transfer_id>\d+))?"
    )

    match = re.match(regex, urlsplit(trim(tfmkt_url)).path)
    if not match:
        return None
    return match.groupdict().get(element)


def trim(text: list | str) -> str:
    """
    Trim and clean up text by removing leading and trailing whitespace and special characters.

    Args:
        text (Union[list, str]): The text or list of text to be trimmed.

    Returns:
        str: The trimmed and cleaned text.
    """
    if isinstance(text, list):
        text = "".join(text)

    return text.strip().replace("\xa0", "")


def safe_regex(text: str | list | None, regex, group: str) -> str | None:
    """
    Safely apply a regular expression and extract a specific group from the matched text.

    Args:
        text (Optional[str]): The text to apply the regular expression to.
        regex: The regular expression pattern.
        group (str): The name of the group to extract.

    Returns:
        Optional[str]: The extracted group value or None if not found or if the input is not a string.
    """
    if not isinstance(text, (str, list)) or not text:
        return None

    try:
        groups = re.search(regex, trim(text)).groupdict()  # type: ignore[union-attr]
        return groups.get(group)
    except AttributeError:
        return None


def remove_str(text: str | None, strings_to_remove: str | list) -> str | None:
    """
    Remove specified strings from a text and return the cleaned text.

    Args:
        text (Optional[str]): The text to remove strings from.
        strings_to_remove (Union[str, list]): A string or list of strings to remove.

    Returns:
        Optional[str]: The cleaned text with specified strings removed or None if not found or if
            the input is not a string.
    """
    if not isinstance(text, str):
        return None

    if isinstance(strings_to_remove, str):
        strings_to_remove = [strings_to_remove]

    for string in strings_to_remove:
        text = text.replace(string, "")

    return trim(text)


def safe_split(text: str | None, delimiter: str) -> list | None:
    """
    Split a text using a delimiter and return a list of cleaned, trimmed values.

    Args:
        text (Optional[str]): The text to split.
        delimiter (str): The delimiter used for splitting.

    Returns:
        Optional[list]: A list of split and cleaned values or None if the input is not a string.
    """
    if not isinstance(text, str):
        return None

    return [trim(t) for t in text.split(delimiter)]


def to_camel_case(headers: list) -> list:
    """
    Convert a list of headers to camelCase format.

    Args:
        headers (list): A list of space-separated headers.

    Returns:
        list: A list of headers in camelCase format.
    """
    camel_case_headers = ["".join(word.capitalize() for word in header.split()) for header in headers]
    camel_case_headers = [header[0].lower() + header[1:] for header in camel_case_headers]

    return list(camel_case_headers)


def parse_date(value: str | date | None) -> date | None:
    """
    Parse a Transfermarkt date string, reading ambiguous numeric dates day first.

    Tries dd/mm/yyyy, dd.mm.yyyy, "Mon d, yyyy" and yyyy-mm-dd, then falls back to dateutil with
    dayfirst=True.

    Args:
        value (Optional[Union[str, date]]): The date string (or an already parsed date).

    Returns:
        Optional[date]: The parsed date, or None for empty, placeholder or unparseable values.
    """
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None

    text = trim(value)
    if text in EMPTY_VALUES:
        return None

    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue

    try:
        return date_parser.parse(text, dayfirst=True).date()
    except (ValueError, OverflowError):
        return None


def parse_int(value: str | int | None) -> int | None:
    """
    Parse a Transfermarkt number such as a market value, fee or member count.

    Currency symbols, "+", "'", whitespace and HTML tags are ignored and a leading "-" is kept. With a
    magnitude suffix (k, m, mio, b, bn, mrd) "." and "," are decimal separators ("€1.20m" -> 1200000);
    without one they are thousands separators ("170.000" -> 170000).

    Args:
        value (Optional[Union[str, int]]): The text to parse (or an already parsed int).

    Returns:
        Optional[int]: The parsed number, or None if the value holds no parseable number.
    """
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None

    text = value.lower()
    if "<" in text:
        # Values wrapped in markup (e.g. transfer fees) may carry labels; keep the first euro amount.
        match = re.search(r"-?€\s*-?[\d.,]+\s*[a-z]*", re.sub(r"<[^>]*>", " ", text))
        if not match:
            return None
        text = match.group(0)

    text = re.sub(r"[€$£+'\s]", "", text)
    match = re.fullmatch(r"(?P<sign>-?)(?P<number>\d[\d.,]*)(?P<suffix>[a-z]*)", text)
    if not match or (match["suffix"] and match["suffix"] not in MAGNITUDES):
        return None

    # With a suffix the separator is decimal ("1.20m"); without one it groups thousands ("170.000").
    number = match["number"].replace(",", ".") if match["suffix"] else re.sub(r"[.,]", "", match["number"])

    try:
        parsed = Decimal(number) * MAGNITUDES.get(match["suffix"], 1)
    except InvalidOperation:
        return None
    return int(-parsed if match["sign"] else parsed)
