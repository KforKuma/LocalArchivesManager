"""Public version identifiers for LAM's independent contracts."""

PACKAGE_VERSION = "0.6.1"
LIBRARY_SCHEMA_VERSION = "0.6.1"
JSON_SCHEMA_VERSION = "1"


def version_contract() -> dict[str, str]:
    """Return the stable public version contract used by status output."""

    return {
        "package_version": PACKAGE_VERSION,
        "library_schema_version": LIBRARY_SCHEMA_VERSION,
        "json_schema_version": JSON_SCHEMA_VERSION,
    }
