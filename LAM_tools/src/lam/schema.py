RECOMMENDED_FIELDS = (
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

SYSTEM_IDENTITY_FIELDS = {"id", "record_uid"}

SNAPSHOT_FIELDS = (
    "id",
    "record_uid",
    "title",
    "topic_folder",
    "pdf_status",
    "pdf_filename",
    "pdf_relative_path",
    "uncertainty",
)

RESERVED_DIRECTORIES = {
    ".agents",
    ".codex",
    ".git",
    ".idea",
    ".library_state",
    "__pycache__",
    "build",
    "dist",
    "inbox",
    "lam_tools",
    "registered",
    "scripts",
}
