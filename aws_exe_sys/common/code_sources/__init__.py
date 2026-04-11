"""Code source registry package.

Import this module to seed the built-in code sources (S3, git,
commands-only) and make them available via :func:`new_sources` /
:func:`detect_kind`. Third-party sources can be added at runtime with
:func:`register_code_source`.
"""

from aws_exe_sys.common.code_sources.base import CodeSource
from aws_exe_sys.common.code_sources.commands_only import CommandsOnlyCodeSource
from aws_exe_sys.common.code_sources.git import GitCodeSource
from aws_exe_sys.common.code_sources.registry import (
    UnknownCodeSourceError,
    detect_kind,
    list_code_sources,
    new_sources,
    register_code_source,
)
from aws_exe_sys.common.code_sources.s3 import S3CodeSource

# Seed built-in sources. Order matters: detect_kind iterates the
# returned dict in insertion order, so more specific sources (S3,
# git) come before the catch-all (commands-only).
register_code_source(S3CodeSource)
register_code_source(GitCodeSource)
register_code_source(CommandsOnlyCodeSource)

__all__ = [
    "CodeSource",
    "CommandsOnlyCodeSource",
    "GitCodeSource",
    "S3CodeSource",
    "UnknownCodeSourceError",
    "detect_kind",
    "list_code_sources",
    "new_sources",
    "register_code_source",
]
