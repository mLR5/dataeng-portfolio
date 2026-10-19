"""Pure parsing rules used by the WeatherAPI Silver transformation.

Errors contain stable codes and field names, never the rejected source value.
Missing optional metrics remain null; malformed supplied metrics reject a row.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import re
from zoneinfo import ZoneInfo


_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_LOCAL_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2})?\Z")


class QualityError(ValueError):
    """A deterministic data rejection, distinct from a technical failure."""

    def __init__(self, code, field, message):
        super().__init__(message)
        self.code = code
        self.field = field


def number(value, field, *, required=False, integer=False, minimum=None, maximum=None):
    """Parse finite JSON numbers or unambiguous decimal strings without coercing bools."""
    if value is None:
        if required:
            raise QualityError("missing_field", field, "Required metric is missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise QualityError("invalid_type", field, "Metric must be numeric")
    if isinstance(value, str) and not _NUMBER.fullmatch(value):
        raise QualityError("invalid_type", field, "Metric is not an unambiguous number")
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite():
            raise ValueError
        if integer:
            if decimal != decimal.to_integral_value():
                raise QualityError("invalid_type", field, "Metric must be an integer")
            parsed = int(decimal)
        else:
            parsed = float(decimal)
            if not math.isfinite(parsed):
                raise ValueError
    except (InvalidOperation, ValueError, OverflowError) as error:
        if isinstance(error, QualityError):
            raise
        raise QualityError("invalid_type", field, "Metric must be finite") from None
    if ((minimum is not None and parsed < minimum)
            or (maximum is not None and parsed > maximum)):
        raise QualityError("out_of_range", field, "Metric is outside its allowed domain")
    return parsed


def boolean(value, field):
    """WeatherAPI encodes optional booleans as 0/1, including numeric strings."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return bool(number(value, field, integer=True, minimum=0, maximum=1))


def optional_text(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise QualityError("invalid_type", field, "Text metric must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise QualityError("invalid_type", field, "Text metric must be valid Unicode") from None
    return value


def instant(row, epoch_field, local_field, source_timezone):
    """Prefer the source epoch; resolve local fallback only when it is unambiguous.

    An epoch remains authoritative during DST: two identical wall-clock hours
    can therefore survive with distinct UTC keys. An invalid supplied epoch is
    rejected rather than silently replaced by another field.
    """
    epoch = row.get(epoch_field)
    if epoch is not None:
        value = number(epoch, epoch_field, required=True, integer=True)
        try:
            parsed = datetime.fromtimestamp(value, timezone.utc)
            # The UTC instant must also be representable in its source timezone.
            parsed.astimezone(ZoneInfo(source_timezone))
            return parsed
        except (ValueError, OverflowError, OSError):
            raise QualityError("invalid_timestamp", epoch_field, "Epoch is outside supported dates") from None
    local = row.get(local_field)
    if local is None:
        raise QualityError("missing_field", epoch_field, "Source instant is missing")
    if not isinstance(local, str) or not _LOCAL_TIME.fullmatch(local):
        raise QualityError("invalid_timestamp", local_field, "Local timestamp has an invalid format")
    try:
        wall = datetime.fromisoformat(local)
    except ValueError:
        raise QualityError("invalid_timestamp", local_field, "Local timestamp is invalid") from None
    zone = ZoneInfo(source_timezone)
    candidates = set()
    for fold in (0, 1):
        try:
            candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if candidate.astimezone(zone).replace(tzinfo=None) == wall:
                candidates.add(candidate)
        except (ValueError, OverflowError):
            raise QualityError("invalid_timestamp", local_field, "Local timestamp is outside supported dates") from None
    if not candidates:
        raise QualityError("nonexistent_local_time", local_field, "Local timestamp does not exist in this timezone")
    if len(candidates) != 1:
        raise QualityError("ambiguous_local_time", local_field, "Local timestamp needs an epoch during DST overlap")
    return candidates.pop()
