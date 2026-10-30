"""err(): an expected error body for equality checks in tests.

Every error body carries `error` (as it always did) plus, when `error` is
not itself a code, a stable `code` and maybe the `field` it is about (see
shared/responses.py). `err("name is required")` equals that body with any
valid `code`/`field`; the exact codes are checked in test_error_shape.py.
"""

import re

_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


class err(dict):
    def __init__(self, error, **extra):
        super().__init__(error=error, **extra)

    def __eq__(self, other):
        if not isinstance(other, dict):
            return NotImplemented
        rest = {k: v for k, v in other.items() if k not in ("code", "field")}
        if rest != dict(self):
            return False
        if "code" in other and not _CODE.match(str(other["code"])):
            return False
        return "field" not in other or isinstance(other["field"], str)

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    __hash__ = None
