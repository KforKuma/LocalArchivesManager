# Legacy fixtures

This directory is the only allowed home for legacy Catalogue/Documents fixture
definitions or files. They must be synthetic and may be imported only by
migration or recovery tests. Ordinary workflow tests use the strict current
schema through `current_library_factory`.

