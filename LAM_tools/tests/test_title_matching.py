from lam.utils.title_matching import (
    filename_evidence,
    title_views,
    titles_tolerantly_equivalent,
    tolerant_title_score,
)
from lam.models import InspectionLevel
from lam.workflows.progressive_register import should_inspect_pdf


def test_greek_letters_and_latin_spelling_are_tolerant_aliases():
    assert titles_tolerantly_equivalent("γδ T cell receptor", "gamma delta T cell receptor")


def test_superscript_charge_and_ascii_charge_are_aliases():
    assert titles_tolerantly_equivalent("GZMK⁺ CD8⁺ T cells", "GZMK+ CD8+ T cells")


def test_underscore_html_and_spacing_views_are_normalized():
    left = title_views("Protein_structure &amp; function")
    right = title_views("protein structure & function")
    assert left.normalized == right.normalized


def test_hyphen_slash_colon_variants_are_highly_similar():
    assert tolerant_title_score("PD-1/PD-L1: signaling", "PD-1 PD-L1 signaling") >= 0.92


def test_charge_bearing_and_charge_free_labels_are_not_equivalent():
    assert not titles_tolerantly_equivalent("CD4+ T cells", "CD4 T cells")


def test_semantically_different_titles_are_not_merged():
    assert not titles_tolerantly_equivalent(
        "Protein structure prediction in cancer",
        "Protein structure prediction in bacteria",
    )


def test_standard_filename_exposes_title_year_and_journal():
    evidence = filename_evidence("Nature, 2025, Review - A protein design survey.pdf")
    assert evidence.title_candidate.value == "A protein design survey"
    assert evidence.title_candidate.confidence == "high"
    assert evidence.year == "2025"
    assert evidence.journal == "Nature"
    assert evidence.publication_type == "Review"


def test_progressive_inspection_decision_uses_least_expensive_next_level():
    assert should_inspect_pdf(identity_confirmed=True, content_allowed=True) == InspectionLevel.SKIP
    assert should_inspect_pdf(identity_confirmed=False, content_allowed=True) == InspectionLevel.PYPDF_TEXT
    assert should_inspect_pdf(
        identity_confirmed=False,
        content_allowed=True,
        pypdf_completed=True,
        pypdf_sufficient=False,
    ) == InspectionLevel.OCR
    assert should_inspect_pdf(
        identity_confirmed=False,
        content_allowed=True,
        pypdf_completed=True,
        pypdf_sufficient=True,
    ) == InspectionLevel.SKIP
