"""Content-addressable artifact store under state/artifacts/."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from schemas import Artifact

ARTIFACTS_DIR = Path(__file__).resolve().parent / "state" / "artifacts"


class ArtifactStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or ARTIFACTS_DIR
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, artifact_id: str) -> tuple[Path, Path]:
        stem = artifact_id.removeprefix("art:")
        return self.root / f"{stem}.bin", self.root / f"{stem}.json"

    def put(
        self,
        blob: bytes,
        content_type: str,
        source: str,
        descriptor: str,
    ) -> str:
        digest = hashlib.sha256(blob).hexdigest()
        artifact_id = f"art:{digest[:16]}"
        bin_path, meta_path = self._paths(artifact_id)
        meta = Artifact(
            id=artifact_id,
            content_type=content_type,
            size_bytes=len(blob),
            source=source,
            descriptor=descriptor,
        )
        bin_path.write_bytes(blob)
        meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
        return artifact_id

    def get_bytes(self, artifact_id: str) -> bytes:
        bin_path, _ = self._paths(artifact_id)
        return bin_path.read_bytes()

    def get_meta(self, artifact_id: str) -> Artifact:
        _, meta_path = self._paths(artifact_id)
        return Artifact.model_validate_json(meta_path.read_text(encoding="utf-8"))

    def exists(self, artifact_id: str) -> bool:
        bin_path, meta_path = self._paths(artifact_id)
        return bin_path.is_file() and meta_path.is_file()
