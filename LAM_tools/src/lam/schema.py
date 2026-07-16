RECOMMENDED_FIELDS = (
    "paper_uuid",
    "id",
    "record_uid",
    "title",
    "authors",
    "year",
    "journal",
    "journal_abbrev",
    "doi",
    "pmid",
    "publication_type",
    "abstract",
    "keywords",
    "auto_tags",
    "manual_tags",
    "suggested_topic",
    "topic_folder",
    "pdf_status",
    "pdf_filename",
    "pdf_relative_path",
    "source",
    "date_added",
    "date_updated",
    "notes",
    "uncertainty",
)

CATALOGUE_051_FIELDS = (
    "paper_uuid",
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
    "manual_tags",
    "auto_tags",
    "suggested_topic",
    "topic_folder",
    "source",
    "notes",
    "uncertainty",
    "date_added",
    "date_updated",
)

DOCUMENT_FIELDS = (
    "document_id",
    "paper_uuid",
    "document_type",
    "supplementary_type",
    "sequence",
    "filename",
    "relative_path",
    "extension",
    "sha256",
    "file_status",
    "source",
    "uncertainty",
    "date_added",
    "date_updated",
)

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

PHASE1_REQUIRED_FIELDS = {
    "id",
    "title",
    "topic_folder",
    "pdf_status",
    "pdf_filename",
    "pdf_relative_path",
    "uncertainty",
}

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
    "auto_tags",
    "suggested_topic",
    "pdf_status",
    "pdf_filename",
    "pdf_relative_path",
    "source",
    "date_added",
    "date_updated",
    "uncertainty",
}

SYSTEM_IDENTITY_FIELDS = {"id", "record_uid", "paper_uuid"}

SNAPSHOT_FIELDS = (
    "paper_uuid",
    "id",
    "record_uid",
    "title",
    "topic_folder",
    "pdf_status",
    "pdf_filename",
    "pdf_relative_path",
    "uncertainty",
)
