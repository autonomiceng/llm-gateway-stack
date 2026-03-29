#!/usr/bin/env python3
"""Generate a .env file from .env.example with strong random secrets.

Also generates Langfuse bootstrap project API keys and related placeholders
used by LiteLLM callbacks (see README).
"""

from __future__ import annotations

import argparse
import pathlib
import re
import secrets
import sys


PLACEHOLDER_SPECS = {
    "__LITELLM_SALT_KEY__": 24,
    "__LITELLM_MASTER_KEY__": 32,
    "__POSTGRES_PASSWORD__": 24,
    "__LITELLM_DB_PASSWORD__": 24,
    "__LANGFUSE_DB_PASSWORD__": 24,
    "__NEXTAUTH_SECRET__": 32,
    "__LANGFUSE_SALT__": 24,
    "__LANGFUSE_ENCRYPTION_KEY__": 32,
    "__CLICKHOUSE_PASSWORD__": 24,
    "__MINIO_ROOT_PASSWORD__": 24,
    "__REDIS_AUTH__": 24,
    "__LANGFUSE_INIT_USER_PASSWORD__": 16,
    "__LANGFUSE_INIT_PROJECT_PUBLIC_KEY__": 16,
    "__LANGFUSE_INIT_PROJECT_SECRET_KEY__": 32,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create .env from .env.example with generated secrets."
    )
    parser.add_argument(
        "--template",
        default=".env.example",
        help="Path to template file (default: .env.example)",
    )
    parser.add_argument(
        "--output",
        default=".env",
        help="Path to output file (default: .env)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite output file if it already exists.",
    )
    return parser


def generate_values() -> dict[str, str]:
    return {
        placeholder: secrets.token_hex(nbytes)
        for placeholder, nbytes in PLACEHOLDER_SPECS.items()
    }


def render_template(template_text: str, values: dict[str, str]) -> str:
    rendered = template_text
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def assert_no_placeholders_left(rendered: str) -> None:
    leftovers = sorted(set(re.findall(r"__[A-Z0-9_]+__", rendered)))
    if leftovers:
        joined = ", ".join(leftovers)
        raise ValueError(f"Unresolved placeholders in rendered env file: {joined}")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    template_path = pathlib.Path(args.template)
    output_path = pathlib.Path(args.output)

    if not template_path.exists():
        print(f"Template not found: {template_path}", file=sys.stderr)
        return 1

    if output_path.exists() and not args.force:
        print(
            f"Refusing to overwrite existing file: {output_path}. "
            "Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    template_text = template_path.read_text()
    rendered = render_template(template_text, generate_values())
    assert_no_placeholders_left(rendered)

    output_path.write_text(rendered)
    print(f"Wrote {output_path} from {template_path}")
    print("Generated Langfuse bootstrap API keys and wired LiteLLM callback keys.")
    print("After startup, open the stack gateway at http://localhost/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
