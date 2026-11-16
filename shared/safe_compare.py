"""Constant-time equality for tokens a caller sent.

hmac.compare_digest raises TypeError for a str holding non-ASCII
characters, so a URL or header value like "å" crashed the public token
checks with a 500 instead of answering "invalid". Both sides are compared
as UTF-8 bytes; anything that is not a str (or bytes) is simply unequal.
"""

import hmac


def equal(expected, given):
    """True when both are the same str/bytes value, in constant time."""
    if isinstance(expected, str):
        expected = expected.encode("utf-8", "surrogatepass")
    if isinstance(given, str):
        given = given.encode("utf-8", "surrogatepass")
    if not isinstance(expected, bytes) or not isinstance(given, bytes):
        return False
    return hmac.compare_digest(expected, given)
