from __future__ import annotations

import pytest

from lam.directory_policy import (
    DirectoryPolicy,
    RootDirectoryKind,
    classify_root_directory,
)
from lam.exceptions import FileOperationError


def test_root_directory_classification_is_centralized(library_factory):
    root = library_factory([])
    assert classify_root_directory(root, "Inbox") == RootDirectoryKind.INBOX
    assert classify_root_directory(root, "Topics") == RootDirectoryKind.TOPICS_ROOT
    assert classify_root_directory(root, "scripts") == RootDirectoryKind.MANAGEMENT
    assert classify_root_directory(root, ".private") == RootDirectoryKind.HIDDEN
    assert classify_root_directory(root, "Unknown") == RootDirectoryKind.UNKNOWN
    assert (
        classify_root_directory(
            root, "Legacy", referenced_legacy_roots={"Legacy"}
        )
        == RootDirectoryKind.LEGACY_TOPIC_CANDIDATE
    )


def test_topic_folder_is_relative_to_topics_and_may_be_nested(library_factory):
    root = library_factory([])
    policy = DirectoryPolicy(root)
    assert policy.validate_topic_folder("IBD/Epithelial") == "IBD/Epithelial"
    assert policy.topic_path("IBD/Epithelial") == (
        root / "Topics" / "IBD" / "Epithelial"
    ).resolve()
    for unsafe in (
        "Topics/IBD",
        "../IBD",
        "C:/IBD",
        ".hidden/IBD",
        "IBD/../Other",
        "IBD/Registered",
    ):
        with pytest.raises(FileOperationError):
            policy.validate_topic_folder(unsafe)


def test_configured_reserved_root_is_reused(library_factory):
    root = library_factory([])
    policy = DirectoryPolicy(root, ("LocalManagement",))
    assert (
        policy.classify_root_directory("LocalManagement")
        == RootDirectoryKind.MANAGEMENT
    )
