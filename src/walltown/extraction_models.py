"""Versioned extraction records, independent of downstream classification."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExtractedPage(ExtractionModel):
    page_number: int = Field(ge=1)
    text: str | None = None
    character_count: int | None = Field(default=None, ge=0)
    extraction_status: Literal["success", "failed"] = "success"
    is_blank: bool | None = None
    requires_ocr: bool | None = None
    image_count: int | None = Field(default=None, ge=0)
    has_visible_marks: bool | None = None
    potentially_garbled: bool = False
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_text(self) -> ExtractedPage:
        if self.text is not None and self.character_count != len(self.text):
            raise ValueError("Character count must match the exact extracted text.")
        if self.extraction_status == "success" and self.text is None:
            raise ValueError("Successful pages require extracted text, including an empty string.")
        if self.text is None and self.character_count is not None:
            raise ValueError("Unknown text must have an unknown character count.")
        if self.is_blank and self.requires_ocr:
            raise ValueError("A confirmed blank page must not be marked as requiring OCR.")
        return self


class EmbeddedAttachment(ExtractionModel):
    name: str | None = None
    filename: str | None = None
    storage: Literal["embedded_files", "file_attachment_annotation"]
    page_number: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    inspection_status: Literal["success", "failed"] = "success"


class AttachmentReference(ExtractionModel):
    name: str | None = None
    page_number: int = Field(ge=1)
    source_start: int = Field(ge=0)
    source_end: int = Field(ge=0)
    raw_header_text: str
    exported_file_candidates: list[str] = Field(default_factory=list)
    embedded_attachment_candidates: list[int] = Field(default_factory=list)
    availability: Literal["not_checked", "exported_candidate", "embedded_candidate", "ambiguous_candidates", "unavailable", "name_unresolved"] = "not_checked"
    matching_basis: str | None = None
    parent_child_relationship_verified: bool = False
    relationship_evidence: list[str] = Field(default_factory=list)


class IdentityResolution(ExtractionModel):
    status: Literal["resolved", "unresolved", "ambiguous", "conflict"] = "unresolved"
    document_id: str | None = None
    original_filename: str | None = None
    source_path: str | None = None
    original_tags: list[str] | None = None
    parent_ids: list[str] | None = None
    family_ids: list[str] | None = None
    original_metadata: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    candidate_document_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ExtractedDocument(ExtractionModel):
    schema_version: Literal["1.0"] = "1.0"
    media_type: str = "application/pdf"
    document_id: str | None = None
    source_filename: str
    source_relative_path: str
    local_path: str
    original_filename: str | None = None
    source_path: str | None = None  # Original GoldFynch path, not the local export location.
    original_tags: list[str] | None = None
    parent_ids: list[str] | None = None
    family_ids: list[str] | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    page_count: int | None = Field(default=None, ge=0)
    file_size_bytes: int | None = Field(default=None, ge=0)
    pages: list[ExtractedPage] = Field(default_factory=list)
    referenced_attachments: list[AttachmentReference] = Field(default_factory=list)
    embedded_attachments: list[EmbeddedAttachment] = Field(default_factory=list)
    identity: IdentityResolution = Field(default_factory=IdentityResolution)
    extraction_status: Literal["success", "partial", "failed"] = "failed"
    extraction_warnings: list[str] = Field(default_factory=list)
    embedded_attachment_inspection_status: Literal["success", "partial", "failed", "not_attempted"] = "not_attempted"

    @model_validator(mode="after")
    def validate_pages_and_identity(self) -> ExtractedDocument:
        if self.page_count is not None and [page.page_number for page in self.pages] != list(range(1, self.page_count + 1)):
            raise ValueError("Every declared page needs one consecutively numbered result, including failed pages.")
        if self.document_id != self.identity.document_id:
            raise ValueError("Document identity fields disagree.")
        if self.identity.status != "resolved" and any(value is not None for value in (
            self.document_id, self.original_filename, self.source_path, self.original_tags, self.parent_ids, self.family_ids,
        )):
            raise ValueError("Unresolved identity metadata must remain null.")
        return self


class CopyResult(ExtractionModel):
    relative_path: str
    source_sha256: str | None = None
    destination_sha256: str | None = None
    verification_status: Literal["verified", "failed"] = "failed"
    action: str
    warnings: list[str] = Field(default_factory=list)
