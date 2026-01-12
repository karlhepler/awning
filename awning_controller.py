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
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# Retry configuration for Bond API
# Retries transient network errors with exponential backoff (1s, 2s, 4s)
# Does NOT retry on HTTP 4xx errors (those raise HTTPError after raise_for_status)
BOND_RETRY_CONFIG = {
    "stop": stop_after_attempt(3),
    "wait": wait_exponential(multiplier=1, min=1, max=10),
    "retry": retry_if_exception_type(
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        )
    ),
    "reraise": True,
    "before_sleep": before_sleep_log(logger, logging.WARNING),
}


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

    def _send_action(self, action: str) -> None:
        """
        Send an action command to the Bond Bridge.

        Args:
            action: Action name (e.g., "Open", "Close", "Stop")

        Raises:
            BondAPIError: If the API request fails
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
            BondAPIError: If the API request fails
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
            BondAPIError: If the API request fails
        """
        try:
            return self._get_request(self.base_url)
        except requests.RequestException as e:
            raise BondAPIError(f"Failed to get device info: {e}") from e

    @retry(**BOND_RETRY_CONFIG)
    def _get_request(self, url: str) -> dict:
        """Make a GET request with retry logic."""
        response = requests.get(url, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    @retry(**BOND_RETRY_CONFIG)
    def _put_request(self, url: str) -> None:
        """Make a PUT request with retry logic."""
        response = requests.put(url, headers=self.headers, json={}, timeout=self.timeout)
        response.raise_for_status()

    def open(self) -> None:
        """
        Open the awning.

        Raises:
            BondAPIError: If the API request fails
        """
        self._send_action("Open")

    def close(self) -> None:
        """
        Close the awning.

        Raises:
            BondAPIError: If the API request fails
        """
        self._send_action("Close")

    def stop(self) -> None:
        """
        Stop awning movement.

        Raises:
            BondAPIError: If the API request fails
        """
        self._send_action("Stop")

    def toggle(self) -> None:
        """
        Toggle awning between open and closed.

        Raises:
            BondAPIError: If the API request fails
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
