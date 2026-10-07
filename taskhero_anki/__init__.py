"""TaskHero for Anki add-on entry point.

Copyright (C) 2026 Whetware, Inc.
SPDX-License-Identifier: AGPL-3.0-or-later
Distributed without warranty; see LICENSE for terms.
Source: https://github.com/whetware/TaskHero-Anki
"""

try:
    import aqt  # noqa: F401
except ModuleNotFoundError:
    # Core modules are intentionally importable without Anki for unit tests.
    pass
else:
    from .addon import register

    register()
