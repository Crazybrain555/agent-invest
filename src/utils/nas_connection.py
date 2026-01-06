import os
import io
import logging
import time
import subprocess
import pandas as pd
from typing import List
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from src.utils.config_loader import ConfigLoader

# Configure logging for this module
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Define exceptions that might occur during network file access
NETWORK_EXCEPTIONS = (FileNotFoundError, PermissionError, OSError, TimeoutError)
ENV_NAS_BASE_PATH = "NAS_BASE_PATH"

class NASConnection:
    """
    Handles connections and file operations on a NAS share accessible via standard OS paths (e.g., UNC paths on Windows).
    Includes retry logic for network instability.
    """
    def __init__(self, nas_path=None):
        """
        Initializes the NASConnection by loading configuration and setting up network connection.
        
        Args:
            nas_path (str, optional): 直接指定完整的NAS路径，如果提供则忽略配置文件中的base_path
        """
        self.config_loader = ConfigLoader(config_dir='configs')
        
        # 优先使用传入的nas_path，如果没有则从配置加载
        self.base_path = nas_path
        
        # 只有在未指定nas_path时才从配置加载
        if self.base_path is None:
            self._load_config()
        else:
            # 仍然需要用户名和密码，所以还是要加载配置
            try:
                config = self.config_loader.load_config('nas_disk/nas_config.yaml')
                self.nas_config = config.get('nas', {})
                self.host = self.nas_config.get('host')
                self.user = self.nas_config.get('user')
                self.password = self.nas_config.get('password')
            except Exception as e:
                logger.error(f"Failed to load NAS configuration: {e}", exc_info=True)
                raise
        
        # 确保base_path不为空
        if not self.base_path:
            raise ValueError("NAS path not provided and 'base_path' not found in configuration.")

        self._apply_base_path_override()

        self._setup_network_connection()
        logger.info(f"NASConnection initialized for base path: {self.base_path}")

    def _load_config(self):
        """Loads NAS configuration from the YAML file."""
        try:
            config = self.config_loader.load_config('nas_disk/nas_config.yaml')
            self.nas_config = config.get('nas', {})
            self.host = self.nas_config.get('host') # May not be needed for os ops but good for context
            
            # 检查配置中是否有base_path (向后兼容)
            self.base_path = self.nas_config.get('base_path')
            
            # 如果无base_path，则使用默认构建的路径
            if not self.base_path and self.host:
                # 构建完整UNC路径
                self.base_path = f"{self.host}"
                logger.warning(f"No base_path in config, using host as base: {self.base_path}")
                
            # Credentials might be needed if using specific SMB libraries later, but not for os access if permissions are set
            self.user = self.nas_config.get('user')
            self.password = self.nas_config.get('password')

        except Exception as e:
            logger.error(f"Failed to load NAS configuration: {e}", exc_info=True)
            raise

    def _apply_base_path_override(self):
        """Allow overriding the NAS base path via environment variable (useful for WSL/Linux mounts)."""
        env_base_path = os.getenv(ENV_NAS_BASE_PATH)
        if env_base_path:
            logger.info(f"Using {ENV_NAS_BASE_PATH} override: {env_base_path}")
            self.base_path = env_base_path

    def _setup_network_connection(self):
        """Sets up network connection to NAS using net use command."""
        try:
            if os.name != "nt":
                # On Linux/WSL, expect the NAS share to be mounted already.
                if os.path.exists(self.base_path):
                    logger.info(f"NAS path {self.base_path} is already accessible (non-Windows)")
                    return

                if self.base_path.startswith("\\\\"):
                    logger.error(
                        "Detected UNC path on non-Windows. Mount the share and set "
                        f"{ENV_NAS_BASE_PATH} to the mounted path."
                    )
                raise OSError(f"NAS path not accessible on non-Windows: {self.base_path}")

            # 检查是否已经有路径访问权限，如果有则不需要再连接
            if os.path.exists(self.base_path):
                logger.info(f"NAS path {self.base_path} is already accessible")
                return
                
            # First, disconnect any existing connection to avoid conflicts
            disconnect_cmd = f"net use {self.base_path} /delete /y"
            subprocess.run(disconnect_cmd, shell=True, capture_output=True, text=True)
            
            # Then establish new connection
            connect_cmd = f"net use {self.base_path} {self.password} /user:{self.user}"
            result = subprocess.run(connect_cmd, shell=True, capture_output=True, text=True)
            
            if result.returncode != 0:
                logger.error(f"Failed to establish network connection: {result.stderr}")
                raise OSError(f"Network connection failed: {result.stderr}")
                
            logger.info("Network connection to NAS established successfully")
            
        except Exception as e:
            logger.error(f"Error setting up network connection: {e}", exc_info=True)
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(NETWORK_EXCEPTIONS),
        before_sleep=lambda retry_state: logger.warning(
            f"Retrying NAS operation {retry_state.fn.__name__} due to {retry_state.outcome.exception()} (attempt {retry_state.attempt_number})..."
        )
    )
    def list_files(self, relative_path: str = "") -> List[str]:
        """
        Lists files in the specified relative path under the base NAS path.

        Args:
            relative_path: Path relative to the base_path defined in config. Defaults to the base_path itself.

        Returns:
            A sorted list of filenames.

        Raises:
            FileNotFoundError, PermissionError, OSError: If the path is inaccessible after retries.
        """
        full_path = os.path.join(self.base_path, relative_path)
        logger.debug(f"Listing files in NAS path: {full_path}")
        try:
            files = sorted(os.listdir(full_path))
            logger.debug(f"Found {len(files)} files in {full_path}")
            return files
        except NETWORK_EXCEPTIONS as e:
            logger.error(f"Error listing files in {full_path}: {e}")
            raise # Re-raise to trigger tenacity retry

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(NETWORK_EXCEPTIONS),
        before_sleep=lambda retry_state: logger.warning(
            f"Retrying NAS operation {retry_state.fn.__name__} due to {retry_state.outcome.exception()} (attempt {retry_state.attempt_number})..."
        )
    )
    def read_file_to_buffer(self, file_path: str) -> io.BytesIO:
        """
        Reads the content of a file from the NAS into a BytesIO buffer.
        The file_path should be relative to the base_path or an absolute path starting with the base_path.

        Args:
            file_path: The path to the file (relative to base_path or absolute).

        Returns:
            An io.BytesIO buffer containing the file content.

        Raises:
            FileNotFoundError, PermissionError, OSError: If the file cannot be read after retries.
        """
        # Construct full path if relative path is given
        if not os.path.isabs(file_path) or not file_path.startswith(self.base_path):
             # Handle potential mix of UNC/relative paths carefully
            if file_path.startswith('/') or file_path.startswith('\\'):
                 # Assume it's meant to be relative to base if not fully matching
                effective_relative_path = file_path.lstrip('/\\')
            else:
                effective_relative_path = file_path

            # Reconstruct full path using os.path.join
            full_path = os.path.join(self.base_path, effective_relative_path)

            # Normalize path separators for the current OS
            full_path = os.path.normpath(full_path)

        else:
            full_path = os.path.normpath(file_path) # Use absolute path directly

        logger.debug(f"Reading NAS file: {full_path}")
        try:
            buffer = io.BytesIO()
            with open(full_path, 'rb') as f: # Read in binary mode
                buffer.write(f.read())
            buffer.seek(0) # Reset buffer position to the beginning
            logger.debug(f"Successfully read {buffer.getbuffer().nbytes} bytes from {full_path}")
            return buffer
        except NETWORK_EXCEPTIONS as e:
            logger.error(f"Error reading file {full_path}: {e}")
            raise # Re-raise to trigger tenacity retry

    # Context manager methods are less relevant here as connection is not explicitly managed
    # but can be kept for potential future refactoring (e.g., using SMB library)
    def __enter__(self):
        # No explicit connect action needed for os-based access
        logger.debug("Entering NASConnection context.")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # No explicit close action needed
        logger.debug("Exiting NASConnection context.")
        pass # Nothing to close 
