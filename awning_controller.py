"""
Bond Bridge Awning Controller

Core domain logic for controlling an awning device through the Bond Bridge API.
This module can be used independently of the CLI interface.
"""

import logging
import os
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Retry configuration for Bond API
# Retries transient HTTP failures (5xx, 429) and connection errors with
# exponential backoff: 0s, 2s, 4s, 8s, 16s (backoff_factor=1.0, total=5).
# Bond Open/Close/Stop actions are idempotent in practice (Open-while-open
# is a no-op), so PUT is safe to retry.
# NOTE: ToggleOpen is the exception — it is non-idempotent. A retry on
# ToggleOpen after a lost-response could double-toggle the awning. ToggleOpen
# is only used by the manual CLI (awning.py), not by automation.
_BOND_RETRY_TOTAL = 5
_BOND_RETRY_STATUS_FORCELIST = [429, 500, 502, 503, 504]
_BOND_RETRY_BACKOFF_FACTOR = 1.0
_BOND_RETRY_ALLOWED_METHODS = ["GET", "PUT", "HEAD"]


class _LoggingRetry(Retry):
    """Retry subclass that logs each retry attempt at WARNING level."""

    def __init__(self, *args, _service_name="API", **kwargs):
        self._service_name = _service_name
        super().__init__(*args, **kwargs)

    def new(self, **kw):
        # Propagate _service_name through Retry.new() so it persists across
        # the retry chain (urllib3 calls new() to produce successive Retry objects).
        instance = super().new(**kw)
        instance._service_name = self._service_name
        return instance

    def increment(self, method=None, url=None, response=None, error=None, _pool=None, _stacktrace=None):
        attempt_num = len(self.history) + 1

        if response is not None:
            status = response.status
            logger.warning(
                f"{self._service_name} returned {status}, retrying "
                f"(attempt {attempt_num}/{_BOND_RETRY_TOTAL}) ..."
            )
        elif error is not None:
            logger.warning(
                f"{self._service_name} connection error ({error}), retrying "
                f"(attempt {attempt_num}/{_BOND_RETRY_TOTAL}) ..."
            )

        return super().increment(
            method=method,
            url=url,
            response=response,
            error=error,
            _pool=_pool,
            _stacktrace=_stacktrace,
        )


def _make_bond_session() -> requests.Session:
    """
    Create a requests.Session with exponential-backoff retry for the Bond API.

    Retries on 5xx server errors (including 503), 429 rate-limit, and
    connection-level errors. Includes PUT so Bond action commands (Open,
    Close, Stop) are retried — they are idempotent in practice.

    Approximate retry delays: 0s, 2s, 4s, 8s, 16s (wall-clock cap ~30s).
    """
    retry = _LoggingRetry(
        total=_BOND_RETRY_TOTAL,
        backoff_factor=_BOND_RETRY_BACKOFF_FACTOR,
        status_forcelist=_BOND_RETRY_STATUS_FORCELIST,
        allowed_methods=_BOND_RETRY_ALLOWED_METHODS,
        raise_on_status=False,  # let raise_for_status() decide after retries
        _service_name="Bond API",
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class ConfigurationError(Exception):
    """Raised when configuration is invalid or missing."""

    pass


class BondAPIError(Exception):
    """Raised when Bond Bridge API request fails."""

    pass


class BondAwningController:
    """Controller for Bond Bridge awning device."""

    def __init__(self, bond_host: str, bond_token: str, device_id: str, timeout: int = 10):
        """
        Initialize the controller.

        Args:
            bond_host: Bond Bridge hostname or IP
            bond_token: Bond API authentication token
            device_id: Device ID for the awning
            timeout: HTTP request timeout in seconds (default: 10)
        """
        self.bond_host = bond_host
        self.bond_token = bond_token
        self.device_id = device_id
        self.timeout = timeout
        self.base_url = f"http://{bond_host}/v2/devices/{device_id}"
        self.headers = {"BOND-Token": bond_token}
        self._session = _make_bond_session()
        self._session.headers.update(self.headers)

    def _send_action(self, action: str) -> None:
        """
        Send an action command to the Bond Bridge.

        Args:
            action: Action name (e.g., "Open", "Close", "Stop")

        Raises:
            BondAPIError: If the API request fails after all retries
        """
        url = f"{self.base_url}/actions/{action}"
        try:
            self._put_request(url)
        except requests.RequestException as e:
            raise BondAPIError(f"Failed to send action '{action}': {e}") from e

    def get_state(self) -> Optional[int]:
        """
        Get the current state of the awning.

        Returns:
            State value (1=open, 0=closed) or None if unavailable

        Raises:
            BondAPIError: If the API request fails after all retries
        """
        url = f"{self.base_url}/state"
        try:
            data = self._get_request(url)
            return data.get("open")
        except requests.RequestException as e:
            raise BondAPIError(f"Failed to get state: {e}") from e

    def get_info(self) -> dict:
        """
        Get device information.

        Returns:
            Device information as dictionary

        Raises:
            BondAPIError: If the API request fails after all retries
        """
        try:
            return self._get_request(self.base_url)
        except requests.RequestException as e:
            raise BondAPIError(f"Failed to get device info: {e}") from e

    def _get_request(self, url: str) -> dict:
        """Make a GET request using the retry-equipped session."""
        response = self._session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def _put_request(self, url: str) -> None:
        """Make a PUT request using the retry-equipped session."""
        response = self._session.put(url, json={}, timeout=self.timeout)
        response.raise_for_status()

    def open(self) -> None:
        """
        Open the awning.

        Raises:
            BondAPIError: If the API request fails after all retries
        """
        self._send_action("Open")

    def close(self) -> None:
        """
        Close the awning.

        Raises:
            BondAPIError: If the API request fails after all retries
        """
        self._send_action("Close")

    def stop(self) -> None:
        """
        Stop awning movement.

        Raises:
            BondAPIError: If the API request fails after all retries
        """
        self._send_action("Stop")

    def toggle(self) -> None:
        """
        Toggle awning between open and closed.

        Raises:
            BondAPIError: If the API request fails after all retries
        """
        self._send_action("ToggleOpen")


def load_config(env_file: Optional[Path] = None) -> tuple[str, str, str]:
    """
    Load configuration from environment variables.

    Args:
        env_file: Optional path to .env file. If not provided, searches current
                 working directory first, then script directory.

    Returns:
        Tuple of (bond_host, bond_token, device_id)

    Raises:
        ConfigurationError: If required environment variables are missing
    """
    # Load .env file
    if env_file:
        if env_file.exists():
            load_dotenv(env_file)
    else:
        # Search for .env in current working directory first, then script directory
        cwd_env_file = Path.cwd() / ".env"
        script_env_file = Path(__file__).parent / ".env"

        if cwd_env_file.exists():
            load_dotenv(cwd_env_file)
        elif script_env_file.exists():
            load_dotenv(script_env_file)

    # Get BOND_TOKEN (required)
    bond_token = os.getenv("BOND_TOKEN", "").strip()
    if not bond_token:
        raise ConfigurationError(
            "BOND_TOKEN environment variable is not set. "
            "Please set it in .env file or export it."
        )

    # Get BOND_HOST (required)
    bond_host = os.getenv("BOND_HOST", "").strip()
    if not bond_host:
        raise ConfigurationError(
            "BOND_HOST environment variable is not set. "
            "Set it to your Bond Bridge IP address (e.g., 192.168.1.100). "
            "Tip: Configure a DHCP reservation in your router for a stable IP."
        )

    # Get DEVICE_ID (required)
    device_id = os.getenv("DEVICE_ID", "").strip()
    if not device_id:
        raise ConfigurationError(
            "DEVICE_ID environment variable is not set. "
            "Please set it in .env file or export it."
        )

    return bond_host, bond_token, device_id


def create_controller_from_env(env_file: Optional[Path] = None) -> BondAwningController:
    """
    Create a BondAwningController from environment variables.

    Args:
        env_file: Optional path to .env file

    Returns:
        Configured BondAwningController instance

    Raises:
        ConfigurationError: If required environment variables are missing
    """
    bond_host, bond_token, device_id = load_config(env_file)
    return BondAwningController(bond_host, bond_token, device_id)
