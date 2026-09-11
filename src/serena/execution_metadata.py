from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from mcp.types import CallToolResult, ResourceLink


@dataclass(frozen=True)
class ExecutionMedia:
    """Retrievable media or file metadata associated with one execution result."""

    media_type: Literal["image", "audio", "file"]
    name: str
    mime_type: str
    uri: str

    @classmethod
    def from_result(cls, result: object) -> "ExecutionMedia | None":
        """Extracts a persistent Serena file resource carried by a logical result."""
        link: ResourceLink | None
        if isinstance(result, ResourceLink):
            link = result
        elif isinstance(result, CallToolResult):
            link = next((block for block in result.content if isinstance(block, ResourceLink)), None)
        else:
            candidate = getattr(result, "file_link", None)
            link = candidate if isinstance(candidate, ResourceLink) else None
        if link is None:
            return None

        uri = str(link.uri)
        if not uri.startswith("serena-file://export/"):
            return None
        mime_type = str(link.mimeType or "application/octet-stream")
        if mime_type.startswith("image/"):
            media_type: Literal["image", "audio", "file"] = "image"
        elif mime_type.startswith("audio/"):
            media_type = "audio"
        else:
            media_type = "file"
        return cls(
            media_type=media_type,
            name=str(link.name or "Serena file"),
            mime_type=mime_type,
            uri=uri,
        )

    @classmethod
    def from_storage_dict(cls, payload: object) -> "ExecutionMedia | None":
        """Reconstructs media metadata from persisted execution state."""
        if not isinstance(payload, dict):
            return None
        mapping = cast(dict[str, Any], payload)
        media_type = mapping.get("type")
        uri = mapping.get("uri")
        if media_type not in {"image", "audio", "file"} or not isinstance(uri, str):
            return None
        return cls(
            media_type=cast(Literal["image", "audio", "file"], media_type),
            name=str(mapping.get("name") or "Serena file"),
            mime_type=str(mapping.get("mime_type") or "application/octet-stream"),
            uri=uri,
        )

    def public_dict(self) -> dict[str, str]:
        """Returns media metadata safe to expose through a UI transport."""
        return {"type": self.media_type, "name": self.name, "mime_type": self.mime_type}

    def storage_dict(self) -> dict[str, str]:
        """Returns complete metadata required to reopen the retained file resource."""
        return {**self.public_dict(), "uri": self.uri}


@dataclass(frozen=True)
class ExecutionResultMetadata:
    """Canonical metadata extracted from one complete logical tool result."""

    media: ExecutionMedia | None
    durable_job_id: str | None
    durable_job_label: str | None


def extract_execution_result_metadata(result: object | None) -> ExecutionResultMetadata:
    """Extracts execution metadata before central result presentation."""
    media = ExecutionMedia.from_result(result) if result is not None else None
    job_id: str | None = None
    job_label: str | None = None
    if isinstance(result, dict):
        payload = cast(dict[str, Any], result)
        candidate_id = payload.get("job_id")
        candidate_label = payload.get("label")
        if isinstance(candidate_id, str) and candidate_id:
            job_id = candidate_id
        if isinstance(candidate_label, str) and candidate_label:
            job_label = candidate_label
    return ExecutionResultMetadata(media=media, durable_job_id=job_id, durable_job_label=job_label)
