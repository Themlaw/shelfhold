"""The only place that parses XML (safe_parse_xml, ENF-SEC-014); see importlinter.ini."""

from shelfhold.infrastructure.xml.safe_parse import (
    DEFAULT_LIMITS,
    UnsafeXmlError,
    XmlLimits,
    safe_parse_xml,
)

__all__ = ["DEFAULT_LIMITS", "UnsafeXmlError", "XmlLimits", "safe_parse_xml"]
