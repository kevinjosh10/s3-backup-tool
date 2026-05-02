import os
from dotenv import load_dotenv
from botocore.config import Config as BotoConfig

# Load environment variables from .env if present
load_dotenv()

class Config:
    """
    Handles configuration loading for the S3 backup tool.
    Priority: CLI argument > Environment variable > Default
    """
    def __init__(self, cli_args=None):
        if cli_args is None:
            cli_args = {}

        # Core AWS settings
        self.bucket = cli_args.get('bucket') or os.getenv('S3_BUCKET_NAME')
        self.region = cli_args.get('region') or os.getenv('AWS_REGION') or 'us-east-1'
        self.kms_key_id = cli_args.get('kms_key') or os.getenv('KMS_KEY_ID')
        
        # S3 Metadata key for storing the MD5 hash
        self.md5_meta_key = 'file-md5'
        
        # Retry logic for intermittent network issues
        self.max_retries = int(cli_args.get('retries') or os.getenv('MAX_RETRIES', '3'))
        
        # Botocore configuration
        self.boto_config = BotoConfig(
            region_name=self.region,
            retries={
                'max_attempts': self.max_retries,
                'mode': 'standard'
            }
        )

    def validate(self):
        """Validate that all required configuration is present."""
        if not self.bucket:
            raise ValueError("Bucket name is required. Provide via CLI (--bucket) or .env (S3_BUCKET_NAME).")
