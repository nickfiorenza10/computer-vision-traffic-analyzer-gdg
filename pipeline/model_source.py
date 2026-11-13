"""Resolve a ``--model`` value into something YOLO can load.

Plain values pass straight through, so ``yolo26m.pt``, an absolute path or a name
Ultralytics downloads itself all behave exactly as before. A value of the form

    hf:<owner>/<repo>/<filename>

is downloaded from the Hugging Face Hub with ``huggingface_hub.hf_hub_download``
and the local cache path is what YOLO receives. ``<filename>`` may contain
sub-directories, for example ``hf:rycebag/traffic-weights/weights/yolo26m.pt``.

The token comes from ``HF_TOKEN`` when it is set, which is what a private model
repo needs; nothing is read from disk and no secret is ever logged.

Usage in a script:

    from model_source import resolve_model

    model = YOLO(resolve_model(args.model))
"""

from __future__ import annotations

import os
from dataclasses import dataclass

HF_PREFIX = "hf:"
TOKEN_ENV = "HF_TOKEN"
USAGE_HINT = "use hf:<owner>/<repo>/<filename>, for example hf:rycebag/traffic-weights/yolo26m.pt"


@dataclass(frozen=True)
class HfSpec:
    """A parsed ``hf:<owner>/<repo>/<filename>`` reference."""

    owner: str
    repo: str
    filename: str

    @property
    def repo_id(self) -> str:
        return f"{self.owner}/{self.repo}"

    def as_model_value(self) -> str:
        """The original ``hf:`` form, for messages and results files."""
        return f"{HF_PREFIX}{self.repo_id}/{self.filename}"


def parse_hf_spec(value: str) -> HfSpec | None:
    """Parse a ``hf:`` model value, or return None when it is a plain path/name.

    Raises ValueError with a plain message when the value starts with ``hf:`` but
    does not have the three required parts.
    """
    if not isinstance(value, str) or not value.startswith(HF_PREFIX):
        return None

    rest = value[len(HF_PREFIX) :]
    parts = rest.split("/")
    if len(parts) < 3:
        raise ValueError(f"Invalid --model value {value!r}: expected three parts. {USAGE_HINT}")

    owner, repo, *filename_parts = parts
    filename = "/".join(filename_parts)
    if not owner or not repo or not filename or any(part == "" for part in filename_parts):
        raise ValueError(f"Invalid --model value {value!r}: owner, repo and filename are required. {USAGE_HINT}")
    if any(segment in {".", ".."} for segment in filename_parts):
        raise ValueError(f"Invalid --model value {value!r}: '.' and '..' are not allowed. {USAGE_HINT}")
    return HfSpec(owner=owner, repo=repo, filename=filename)


def _hf_hub_download(**kwargs):
    """Imported lazily so the plain-path case does not need huggingface_hub."""
    from huggingface_hub import hf_hub_download

    return hf_hub_download(**kwargs)


def resolve_model(
    value: str,
    *,
    token: str | None = None,
    download=_hf_hub_download,
    environ=None,
) -> str:
    """Return a local path for ``value``: the value itself, or a downloaded file.

    ``token`` wins over ``HF_TOKEN``/``environ``; when neither is set no token is
    passed, which is correct for public repos. ``download`` is injectable so tests
    can run without the network.
    """
    spec = parse_hf_spec(value)
    if spec is None:
        return value

    env = os.environ if environ is None else environ
    if token is None:
        token = env.get(TOKEN_ENV) or None

    kwargs = {"repo_id": spec.repo_id, "filename": spec.filename}
    if token:
        kwargs["token"] = token

    try:
        return download(**kwargs)
    except Exception as error:  # noqa: BLE001 - re-raised with the spec in the message
        raise RuntimeError(
            f"Could not download {spec.as_model_value()} from the Hugging Face Hub: {error}"
        ) from error


def resolve_model_cli(value: str, program: str, *, download=_hf_hub_download) -> str:
    """resolve_model() for a command line: a bad value or a failed download exits
    with the message instead of a traceback."""
    try:
        return resolve_model(value, download=download)
    except (ValueError, RuntimeError) as error:
        raise SystemExit(f"{program}: {error}") from error
