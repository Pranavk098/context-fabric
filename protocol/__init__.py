"""Context Fabric — protocol package (CPP v1.0 zero-dependency schemas & canonicalization)."""

from protocol.canonical import canonicalize, packet_content_hash, sha256_hex
from protocol.models import (
    AgentOutcome,
    ContextBlock,
    ContextPacket,
    WorkflowEvent,
)

__all__ = [
    "canonicalize",
    "sha256_hex",
    "packet_content_hash",
    "ContextBlock",
    "ContextPacket",
    "WorkflowEvent",
    "AgentOutcome",
]
