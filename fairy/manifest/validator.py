"""Pre-build validation with plain-language messages.

Hard failures (block archiving) are deliberately minimal — the dashcam must
not lose data over missing nice-to-haves:
  - the three required user fields (operator, goal, location)
  - at least one recorded bag
Everything else degrades to a warning rendered at mission_close.
"""

from typing import Any

from annotated_types import MaxLen

from fairy.manifest.schema import Identity, Intent

# Required briefing fields -> (section, plain-language error)
REQUIRED_USER_FIELDS = {
    "operator_name": ("identity", "I don't know who ran this mission."),
    "goal": ("intent", "I don't know what this mission was trying to do."),
    "location_name": ("intent", "I don't know where this mission happened."),
}




def _max_length(model, field: str) -> int | None:
    return next((m.max_length for m in model.model_fields[field].metadata
                 if isinstance(m, MaxLen)), None)


# Briefing fields the schema caps in length -> the cap
MAX_LENGTHS = {field: limit for model, field in
               ((Identity, "operator_name"), (Intent, "goal"))
               if (limit := _max_length(model, field)) is not None}


def length_error(field: str, value: str | None) -> str | None:
    """A plain-language error when an answer is longer than the record
    allows; None when it fits."""
    limit = MAX_LENGTHS.get(field)
    if limit is None or value is None or len(value) <= limit:
        return None
    what = field.replace("_", " ")
    return (f"The {what} can be at most {limit} characters "
            f"(this one has {len(value)}).")


def missing_user_fields(context: dict[str, Any] | None) -> list[str]:
    """Names of required briefing fields absent from mission_context."""
    context = context or {}
    missing = []
    for field, (section, _) in REQUIRED_USER_FIELDS.items():
        value = (context.get(section) or {}).get(field)
        if not isinstance(value, str) or not value.strip():
            missing.append(field)
    return missing


def validate(harvest: dict[str, Any] | None,
             context: dict[str, Any] | None) -> list[str]:
    """Plain-language errors that block archiving. Empty list = good to go."""
    errors = [REQUIRED_USER_FIELDS[f][1] for f in missing_user_fields(context)]
    for field, (section, _) in REQUIRED_USER_FIELDS.items():
        value = ((context or {}).get(section) or {}).get(field)
        error = length_error(field, value) if isinstance(value, str) else None
        if error:
            errors.append(error)
    if not (harvest or {}).get("bags"):
        errors.append("There's nothing recorded yet.")
    return errors
