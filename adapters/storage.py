"""Storage facade with R2 primary storage and OneDrive compatibility."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from adapters.onedrive import get_onedrive_client
from adapters.r2 import get_r2_client
from app.config import get_settings


class StorageClient:
    """Expose the legacy file methods while routing them to the configured backend.

    Downloads are URL-aware, so old OneDrive rows remain readable while Feishu
    links are migrated incrementally to the R2 public domain.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.onedrive = get_onedrive_client()
        self.r2 = get_r2_client()

    @property
    def uses_r2(self) -> bool:
        return self.settings.storage_provider.strip().casefold() == "r2"

    def download_share_link(self, url: str, dest_path: Path) -> Path:
        if self.r2.key_from_public_url(url):
            return self.r2.download_url(url, dest_path)
        return self.onedrive.download_share_link(url, dest_path)

    def get_share_item_metadata(self, url: str) -> dict:
        if self.r2.key_from_public_url(url):
            return {}
        return self.onedrive.get_share_item_metadata(url)

    def upload_and_share(
        self, local_path: Path, target_folder: str | None = None
    ) -> str:
        if not self.uses_r2:
            return self.onedrive.upload_and_share(
                local_path, target_folder=target_folder
            )
        folder = self.r2.normalize_key(target_folder or "")
        key = f"{folder.rstrip('/')}/{Path(local_path).name}" if folder else Path(local_path).name
        return self.r2.upload_file(Path(local_path), key)

    def ensure_folder(self, folder_path: str) -> None:
        if not self.uses_r2:
            self.onedrive.ensure_folder(folder_path)


@lru_cache
def get_storage_client() -> StorageClient:
    return StorageClient()
