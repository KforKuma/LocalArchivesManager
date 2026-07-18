CATALOGUE_060_FIELDS = (
    "paper_uuid",
    "uncertainty",
    "title",
    "authors",
    "year",
    "journal",
    "journal_abbrev",
    "publication_type",
    "abstract",
    "keywords",
    "manual_tags",
    "auto_tags",
    "suggested_topic",
    "topic_folder",
    "source",
    "date_added",
    "date_updated",
    "notes",
    "doi",
    "pmid",
    "arxiv_id",
)

CATALOGUE_FIELDS = (
    "paper_uuid",
    "record_origin",
    "document_expectation",
    *CATALOGUE_060_FIELDS[1:],
)

# Public name retained for one release so extensions importing the 0.5.1
# constant receive the current paper-table schema instead of a stale layout.
CATALOGUE_052_FIELDS = CATALOGUE_FIELDS
CATALOGUE_051_FIELDS = CATALOGUE_FIELDS
RECOMMENDED_FIELDS = CATALOGUE_FIELDS

RECORD_ORIGINS = {
    "pdf",
    "reference_text",
    "provider_import",
    "recovered",
    "legacy",
}
DOCUMENT_EXPECTATIONS = {"required", "optional", "unknown"}

DOCUMENT_FIELDS = (
    "document_id",
    "paper_uuid",
    "uncertainty",
    "document_type",
    "supplementary_type",
    "sequence",
    "filename",
    "relative_path",
    "extension",
    "sha256",
    "file_status",
    "source",
    "date_added",
    "date_updated",
)

LEGACY_IDENTITY_FIELDS = ("id", "record_uid")
LEGACY_PDF_FIELDS = ("pdf_status", "pdf_filename", "pdf_relative_path")

DOCUMENT_TYPES = {"main", "supplementary"}
SUPPLEMENTARY_TYPES = {
    "Supplementary",
    "Table",
    "Figure",
    "Methods",
    "Data",
    "Appendix",
    "Other",
}
MANAGED_DOCUMENT_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".csv"}

# Minimal legacy workbook signature used only by the explicit identifier
# migration. Ordinary workflows require the complete current schema.
LEGACY_CATALOGUE_REQUIRED_FIELDS = {
    "title",
    "topic_folder",
    "uncertainty",
}
PHASE1_REQUIRED_FIELDS = LEGACY_CATALOGUE_REQUIRED_FIELDS

USER_CONTROLLED_FIELDS = {"manual_tags", "topic_folder", "notes"}

MACHINE_FILLABLE_FIELDS = {
    "title",
    "authors",
    "year",
    "journal",
    "journal_abbrev",
    "doi",
    "pmid",
    "arxiv_id",
    "publication_type",
    "abstract",
    "keywords",
}

MACHINE_MAINTAINED_FIELDS = {
    "record_origin",
    "document_expectation",
    "auto_tags",
    "suggested_topic",
    "source",
    "date_added",
    "date_updated",
    "uncertainty",
}

SYSTEM_IDENTITY_FIELDS = {"paper_uuid"}

# The complete paper row is retained in the official snapshot so an explicitly
# authorized recovery can restore the same entity and UUID without inference.
SNAPSHOT_FIELDS = CATALOGUE_FIELDS
