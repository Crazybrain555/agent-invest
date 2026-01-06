#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render investment memo from a template."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


def render_template(template_path: Path, context: Mapping[str, Any]) -> str:
    template = template_path.read_text(encoding="utf-8")
    return template.format(**context)


def render_investment_memo(
    *,
    template_path: Path,
    context: Mapping[str, Any],
) -> str:
    return render_template(template_path, context)
