import os
import subprocess
import logging
from typing import List
from src.utils.config_loader import ConfigLoader

# Configure logging for this module
logger = logging.getLogger(__name__)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

ENV_SPACE_BASE_PATH = "SPACE_BASE_PATH"

class SpaceConnection:
    """
    Handles connections and file operations on Space network drives.
    Similar to NASConnection but specifically for Space drives.
    """
    
    def __init__(self, path=None):
        """
        Initialize Space connection with network authentication.
        
        Args:
            path (str, optional): Specific path to connect to, if None uses config
        """
        self.config_loader = ConfigLoader(config_dir='configs')
        self.path = path
        
        # Load configuration
        try:
            config = self.config_loader.load_config('space_disk/space_config.yaml')
            self.nas_config = config.get('nas', {})
            self.host = self.nas_config.get('host', '\\\\space')
            self.user = self.nas_config.get('user', 'space\\bsshare')
            self.password = self.nas_config.get('password', '!@#$QWERasdf')
            
            logger.info(f"Loaded Space connection config for host: {self.host}")
            
        except Exception as e:
            logger.warning(f"Could not load Space config: {e}. Using defaults.")
            self.host = '\\\\space'
            self.user = 'space\\bsshare'
            self.password = '!@#$QWERasdf'
        
        env_path = os.getenv(ENV_SPACE_BASE_PATH)
        if env_path:
            logger.info(f"Using {ENV_SPACE_BASE_PATH} override: {env_path}")
            self.path = env_path

        # Set up network connection
        self._setup_network_connection()
    
    def _setup_network_connection(self):
        """Set up network connection to Space drives using net use command."""
        try:
            # Use the specific path or default host
            target_path = self.path or self.host

            if os.name != "nt":
                if os.path.exists(target_path):
                    logger.info(f"Space path {target_path} is already accessible (non-Windows)")
                    return
                logger.error(
                    "Detected network path on non-Windows. Mount the share and set "
                    f"{ENV_SPACE_BASE_PATH} to the mounted path."
                )
                return
            
            # Check if path is already accessible
            if os.path.exists(target_path):
                logger.info(f"Space path {target_path} is already accessible")
                return
                
            # First, disconnect any existing connection to avoid conflicts
            disconnect_cmd = f'net use "{target_path}" /delete /y'
            subprocess.run(disconnect_cmd, shell=True, capture_output=True, text=True)
            
            # Then establish new connection
            connect_cmd = f'net use "{target_path}" "{self.password}" /user:"{self.user}"'
            result = subprocess.run(connect_cmd, shell=True, capture_output=True, text=True)
            
            if result.returncode != 0:
                logger.error(f"Failed to establish network connection: {result.stderr}")
                raise OSError(f"Network connection failed: {result.stderr}")
                
            logger.info(f"Network connection to {target_path} established successfully")
            
        except Exception as e:
            logger.error(f"Error setting up network connection: {e}", exc_info=True)
            # Don't raise the exception to allow fallback to existing connections
            logger.warning("Continuing without network authentication...")
    
    def test_connection(self, path: str) -> bool:
        """Test if a path is accessible."""
        try:
            return os.path.exists(path) and os.path.isdir(path)
        except Exception as e:
            logger.error(f"Error testing connection to {path}: {e}")
            return False
    
    def list_directories(self, path: str) -> List[str]:
        """List directories in the given path."""
        try:
            if not self.test_connection(path):
                logger.error(f"Cannot access path: {path}")
                return []
                
            items = os.listdir(path)
            # Return only directories
            dirs = [item for item in items if os.path.isdir(os.path.join(path, item))]
            return sorted(dirs)
            
        except Exception as e:
            logger.error(f"Error listing directories in {path}: {e}")
            return []
    
    def list_files(self, path: str, pattern: str = "*.csv") -> List[str]:
        """List files in the given path matching the pattern."""
        try:
            if not self.test_connection(path):
                logger.error(f"Cannot access path: {path}")
                return []
                
            import glob
            search_pattern = os.path.join(path, pattern)
            files = glob.glob(search_pattern)
            # Return just the filenames, not full paths
            filenames = [os.path.basename(f) for f in files]
            return sorted(filenames)
            
        except Exception as e:
            logger.error(f"Error listing files in {path}: {e}")
            return []

def ensure_space_connection(path: str) -> bool:
    """
    Utility function to ensure Space network connection is established.
    
    Args:
        path (str): The Space network path to connect to
        
    Returns:
        bool: True if connection is successful or already exists
    """
    try:
        space_conn = SpaceConnection(path)
        return space_conn.test_connection(path)
    except Exception as e:
        logger.error(f"Failed to establish Space connection: {e}")
        return False 
