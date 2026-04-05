# -*- coding: utf-8 -*-

from typing import Any, Dict, Union

from requests.exceptions import RequestException

from backend.base.custom_exceptions import ClientNotWorking, CredentialInvalid
from backend.base.definitions import (BrokenClientReason, Constants,
                                      DownloadState, DownloadType)
from backend.base.helpers import Session
from backend.base.logging import LOGGER
from backend.implementations.external_clients import BaseExternalClient


def _parse_sabnzbd_size(size_str: str) -> int:
    """Convert a SABnzbd size string like '150.00 MB' or '1.23 GB' to bytes.

    Args:
        size_str (str): The SABnzbd size string.

    Returns:
        int: Size in bytes, or -1 if it cannot be parsed.
    """
    try:
        parts = size_str.strip().split()
        if len(parts) != 2:
            return -1
        value = float(parts[0])
        unit = parts[1].upper()
        multipliers = {
            'B':  1,
            'K':  1024,
            'KB': 1024,
            'M':  1024 ** 2,
            'MB': 1024 ** 2,
            'G':  1024 ** 3,
            'GB': 1024 ** 3,
            'T':  1024 ** 4,
            'TB': 1024 ** 4,
        }
        return int(value * multipliers.get(unit, 1))
    except (ValueError, AttributeError):
        return -1


def _parse_sabnzbd_speed(speed_str: str) -> int:
    """Convert a SABnzbd speed string like '1024.0 K' or '5.2 M' to bytes/sec.

    Args:
        speed_str (str): The SABnzbd speed string.

    Returns:
        int: Speed in bytes per second.
    """
    try:
        parts = speed_str.strip().split()
        if len(parts) == 1:
            # Plain number, assumed to be bytes/sec
            return int(float(parts[0]))
        value = float(parts[0])
        unit = parts[1].upper()
        multipliers = {
            'B':  1,
            'K':  1024,
            'KB': 1024,
            'M':  1024 ** 2,
            'MB': 1024 ** 2,
            'G':  1024 ** 3,
            'GB': 1024 ** 3,
        }
        return int(value * multipliers.get(unit, 1))
    except (ValueError, AttributeError):
        return 0


class SABnzbd(BaseExternalClient):
    """External download client for SABnzbd."""

    client_type = 'SABnzbd'
    download_type = DownloadType.USENET

    required_tokens = ('title', 'base_url', 'api_token')

    # Mapping from SABnzbd queue slot status strings to DownloadState
    queue_state_mapping: Dict[str, DownloadState] = {
        'Queued':     DownloadState.QUEUED_STATE,
        'Paused':     DownloadState.PAUSED_STATE,
        'Grabbing':   DownloadState.DOWNLOADING_STATE,
        'Downloading': DownloadState.DOWNLOADING_STATE,
        'Verifying':  DownloadState.DOWNLOADING_STATE,
        'Repairing':  DownloadState.DOWNLOADING_STATE,
        'Fetching':   DownloadState.DOWNLOADING_STATE,
        'Extracting': DownloadState.IMPORTING_STATE,
        'Moving':     DownloadState.IMPORTING_STATE,
        'Running':    DownloadState.IMPORTING_STATE,
        'Failed':     DownloadState.FAILED_STATE,
    }

    def __init__(self, client_id: int) -> None:
        super().__init__(client_id)
        self.ssn: Union[Session, None] = None
        return

    @staticmethod
    def _get_session() -> Session:
        """Create a plain requests session.

        Returns:
            Session: A new session.
        """
        return Session()

    @staticmethod
    def _check_auth(
        base_url: str,
        api_token: Union[str, None]
    ) -> Session:
        """Verify connectivity and API key for a SABnzbd instance.

        Args:
            base_url (str): Base URL of the SABnzbd instance.
            api_token (Union[str, None]): API key for authentication.

        Raises:
            ClientNotWorking: Cannot connect to SABnzbd or response unexpected.
            CredentialInvalid: The API key is invalid.

        Returns:
            Session: An authenticated session.
        """
        ssn = SABnzbd._get_session()

        try:
            response = ssn.get(
                f'{base_url}/api',
                params={
                    'mode':   'version',
                    'output': 'json',
                    'apikey': api_token or '',
                }
            )

        except RequestException:
            LOGGER.exception("Can't connect to SABnzbd instance: ")
            raise ClientNotWorking(BrokenClientReason.CONNECTION_ERROR)

        if not response.ok:
            LOGGER.error(
                f"Unexpected status from SABnzbd instance: {response.status_code}"
            )
            raise ClientNotWorking(BrokenClientReason.NOT_CLIENT_INSTANCE)

        try:
            data = response.json()
        except Exception:
            LOGGER.error("SABnzbd returned non-JSON response")
            raise ClientNotWorking(BrokenClientReason.FAILED_PROCESSING_RESPONSE)

        if 'error' in data:
            error_msg = str(data['error']).lower()
            if 'api key' in error_msg or 'access denied' in error_msg or 'not authorized' in error_msg:
                LOGGER.error(f"SABnzbd API key invalid: {data['error']}")
                raise CredentialInvalid
            LOGGER.error(f"SABnzbd returned error: {data['error']}")
            raise ClientNotWorking(BrokenClientReason.NOT_CLIENT_INSTANCE)

        if 'version' not in data:
            LOGGER.error("SABnzbd response missing 'version' field")
            raise ClientNotWorking(BrokenClientReason.NOT_CLIENT_INSTANCE)

        return ssn

    def add_download(
        self,
        download_link: str,
        target_folder: str,
        download_name: Union[str, None]
    ) -> str:
        """Add an NZB download to SABnzbd by URL.

        Args:
            download_link (str): The URL of the NZB file.
            target_folder (str): Unused — SABnzbd manages its own output
                folder via its configuration.
            download_name (Union[str, None]): The job name to assign in SABnzbd.
                If ``None`` the name is derived from the NZB itself.

        Raises:
            ClientNotWorking: Can't connect to client.
            CredentialInvalid: API key is invalid.

        Returns:
            str: The SABnzbd NZO ID of the queued job.
        """
        if not self.ssn:
            self.ssn = self._check_auth(self.base_url, self.api_token)

        params: Dict[str, Any] = {
            'mode':   'addurl',
            'name':   download_link,
            'cat':    Constants.NZB_CATEGORY,
            'output': 'json',
            'apikey': self.api_token or '',
        }
        if download_name:
            params['nzbname'] = download_name

        try:
            response = self.ssn.get(f'{self.base_url}/api', params=params)
            response.raise_for_status()
            data = response.json()
        except RequestException:
            LOGGER.exception("Failed to add NZB to SABnzbd: ")
            raise ClientNotWorking(BrokenClientReason.CONNECTION_ERROR)

        if not data.get('status'):
            LOGGER.error(f"SABnzbd rejected NZB addition: {data}")
            raise ClientNotWorking(BrokenClientReason.FAILED_PROCESSING_RESPONSE)

        nzo_ids = data.get('nzo_ids', [])
        if not nzo_ids:
            LOGGER.error("SABnzbd did not return an NZO ID after adding NZB")
            raise ClientNotWorking(BrokenClientReason.FAILED_PROCESSING_RESPONSE)

        nzo_id: str = nzo_ids[0]
        LOGGER.debug(f"Added NZB to SABnzbd, nzo_id={nzo_id}")
        return nzo_id

    def get_download(self, download_id: str) -> Union[Dict[str, Any], None]:
        """Get the status of an NZB job from SABnzbd.

        Checks the active queue first; if the job is not found there it checks
        the history (completed/failed jobs).

        Args:
            download_id (str): The SABnzbd NZO ID of the job.

        Raises:
            ClientNotWorking: Can't connect to client.
            CredentialInvalid: API key is invalid.

        Returns:
            Union[Dict[str, Any], None]:
                A dict with ``size`` (bytes), ``progress`` (0–100 float),
                ``speed`` (bytes/sec), ``state`` (:class:`DownloadState`), and
                optionally ``storage`` (path to completed files).
                Returns an empty dict ``{}`` if the job is not found anywhere,
                and ``None`` if SABnzbd has deleted/removed the job.
        """
        if not self.ssn:
            self.ssn = self._check_auth(self.base_url, self.api_token)

        # Check active queue first
        try:
            queue_response = self.ssn.get(
                f'{self.base_url}/api',
                params={
                    'mode':   'queue',
                    'output': 'json',
                    'apikey': self.api_token or '',
                    'search': download_id,
                }
            )
            queue_response.raise_for_status()
            queue_data = queue_response.json()
        except RequestException:
            LOGGER.exception("Failed to get SABnzbd queue: ")
            raise ClientNotWorking(BrokenClientReason.CONNECTION_ERROR)

        slots = queue_data.get('queue', {}).get('slots', [])
        for slot in slots:
            if slot.get('nzo_id') == download_id:
                status_str = slot.get('status', '')
                state = self.queue_state_mapping.get(
                    status_str,
                    DownloadState.DOWNLOADING_STATE
                )
                mb_total = float(slot.get('mb', 0) or 0)
                mb_left = float(slot.get('mbleft', 0) or 0)
                size_bytes = int(mb_total * 1024 * 1024)
                if mb_total > 0:
                    progress = round((mb_total - mb_left) / mb_total * 100, 2)
                else:
                    progress = 0.0
                speed_str = str(slot.get('speed', '0'))
                speed = _parse_sabnzbd_speed(
                    speed_str if ' ' in speed_str else f'{speed_str} B'
                )
                return {
                    'size':     size_bytes,
                    'progress': progress,
                    'speed':    speed,
                    'state':    state,
                }

        # Not in queue — check history
        try:
            hist_response = self.ssn.get(
                f'{self.base_url}/api',
                params={
                    'mode':   'history',
                    'output': 'json',
                    'apikey': self.api_token or '',
                    'search': download_id,
                }
            )
            hist_response.raise_for_status()
            hist_data = hist_response.json()
        except RequestException:
            LOGGER.exception("Failed to get SABnzbd history: ")
            raise ClientNotWorking(BrokenClientReason.CONNECTION_ERROR)

        hist_slots = hist_data.get('history', {}).get('slots', [])
        for slot in hist_slots:
            if slot.get('nzo_id') == download_id:
                hist_status = slot.get('status', '')
                if hist_status == 'Completed':
                    state = DownloadState.IMPORTING_STATE
                else:
                    state = DownloadState.FAILED_STATE

                size_bytes = _parse_sabnzbd_size(str(slot.get('size', '0 B')))
                return {
                    'size':     size_bytes,
                    'progress': 100.0,
                    'speed':    0,
                    'state':    state,
                    'storage':  slot.get('storage', ''),
                }

        # Job not in queue or history — treat as removed by SABnzbd
        return None

    def delete_download(self, download_id: str, delete_files: bool) -> None:
        """Remove a job from SABnzbd (queue or history).

        Args:
            download_id (str): The SABnzbd NZO ID of the job.
            delete_files (bool): Whether to delete the downloaded files.

        Raises:
            ClientNotWorking: Can't connect to client.
            CredentialInvalid: API key is invalid.
        """
        if not self.ssn:
            self.ssn = self._check_auth(self.base_url, self.api_token)

        del_files_value = '1' if delete_files else '0'

        # Try to delete from the active queue first
        try:
            self.ssn.get(
                f'{self.base_url}/api',
                params={
                    'mode':      'queue',
                    'name':      'delete',
                    'del_files': del_files_value,
                    'value':     download_id,
                    'output':    'json',
                    'apikey':    self.api_token or '',
                }
            )
        except RequestException:
            LOGGER.exception("Failed to delete SABnzbd queue entry: ")
            raise ClientNotWorking(BrokenClientReason.CONNECTION_ERROR)

        # Also delete from history in case the job completed
        try:
            self.ssn.get(
                f'{self.base_url}/api',
                params={
                    'mode':      'history',
                    'name':      'delete',
                    'del_files': del_files_value,
                    'value':     download_id,
                    'output':    'json',
                    'apikey':    self.api_token or '',
                }
            )
        except RequestException:
            LOGGER.exception("Failed to delete SABnzbd history entry: ")
            raise ClientNotWorking(BrokenClientReason.CONNECTION_ERROR)

        return

    @staticmethod
    def test(
        base_url: str,
        username: Union[str, None] = None,
        password: Union[str, None] = None,
        api_token: Union[str, None] = None
    ) -> None:
        """Test that a SABnzbd instance is reachable and the API key is valid.

        Args:
            base_url (str): Base URL of the SABnzbd instance.
            username (Union[str, None]): Unused (SABnzbd uses an API key).
            password (Union[str, None]): Unused (SABnzbd uses an API key).
            api_token (Union[str, None]): The SABnzbd API key.

        Raises:
            ClientNotWorking: Can't connect to SABnzbd or response unexpected.
            CredentialInvalid: The API key is invalid.
        """
        SABnzbd._check_auth(base_url, api_token)
        return
