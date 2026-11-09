"""Package the Lambda without Docker: uv installs Linux arm64 wheels into the asset."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import jsii
from aws_cdk import ILocalBundling

ROOT = Path(__file__).resolve().parent.parent
PYTHON_VERSION = "3.12"
PLATFORM = "aarch64-manylinux2014"  # matches Architecture.ARM_64


@jsii.implements(ILocalBundling)
class UvBundling:
    def try_bundle(self, output_dir: str, options: Any = None) -> bool:
        out = Path(output_dir)
        with tempfile.TemporaryDirectory() as tmp:
            requirements = Path(tmp) / "requirements.txt"
            subprocess.run(
                [
                    "uv",
                    "export",
                    "--frozen",
                    "--no-dev",
                    "--no-group",
                    "infra",
                    "--no-emit-project",
                    "--no-hashes",
                    "--output-file",
                    str(requirements),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--quiet",
                    "--requirements",
                    str(requirements),
                    "--target",
                    str(out),
                    "--python-platform",
                    PLATFORM,
                    "--python-version",
                    PYTHON_VERSION,
                    "--only-binary",
                    ":all:",
                ],
                cwd=ROOT,
                check=True,
            )
        shutil.copytree(
            ROOT / "src" / "cotizador_mcp",
            out / "cotizador_mcp",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            dirs_exist_ok=True,
        )
        handler = out / "run.sh"
        shutil.copy2(ROOT / "infra" / "lambda" / "run.sh", handler)
        handler.chmod(0o755)
        return True
