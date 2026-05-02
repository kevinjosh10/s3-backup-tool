import os
import boto3
import logging
from botocore.exceptions import ClientError
from boto3.s3.transfer import TransferConfig

logger = logging.getLogger(__name__)

class S3Utils:
    def __init__(self, config):
        self.config = config
        self.s3_client = boto3.client('s3', config=config.boto_config)
        self.bucket = config.bucket
        
        # High threshold to try and keep single-part uploads for ETags, 
        # though we primarily rely on metadata now.
        self.transfer_config = TransferConfig(
            multipart_threshold=1024 * 25, # 25MB
            max_concurrency=10,
            multipart_chunksize=1024 * 25,
            use_threads=True
        )

    def get_object_md5(self, s3_key):
        """
        Fetches the MD5 hash from S3 metadata, or falls back to ETag.
        Returns: md5_string or None if not found/multipart.
        """
        try:
            response = self.s3_client.head_object(Bucket=self.bucket, Key=s3_key)
            
            # Prioritize metadata
            metadata = response.get('Metadata', {})
            if self.config.md5_meta_key in metadata:
                return metadata[self.config.md5_meta_key]
                
            # Fallback to ETag
            etag = response.get('ETag', '').strip('"')
            if '-' in etag:
                # Multipart upload ETag, not a valid MD5 to compare against
                logger.debug(f"Multipart ETag found for {s3_key}, cannot compare MD5 directly.")
                return None
                
            return etag
            
        except ClientError as e:
            if e.response['Error']['Code'] == '404':
                return None
            logger.error(f"Error fetching metadata for {s3_key}: {e}")
            raise

    def upload_file(self, local_path, s3_key, file_md5):
        """
        Uploads a file to S3 with metadata and encryption.
        """
        extra_args = {
            'Metadata': {self.config.md5_meta_key: file_md5}
        }
        
        # Apply Encryption
        if self.config.kms_key_id:
            extra_args['ServerSideEncryption'] = 'aws:kms'
            extra_args['SSEKMSKeyId'] = self.config.kms_key_id
        else:
            extra_args['ServerSideEncryption'] = 'AES256'

        try:
            self.s3_client.upload_file(
                Filename=local_path,
                Bucket=self.bucket,
                Key=s3_key,
                ExtraArgs=extra_args,
                Config=self.transfer_config
            )
            return True
        except ClientError as e:
            logger.error(f"Failed to upload {local_path} to {s3_key}: {e}")
            return False

    def copy_file(self, source_key, dest_key):
        """
        Performs a server-side copy within S3 to avoid re-uploading unchanged files.
        """
        try:
            copy_source = {'Bucket': self.bucket, 'Key': source_key}
            self.s3_client.copy_object(CopySource=copy_source, Bucket=self.bucket, Key=dest_key)
            return True
        except ClientError as e:
            logger.error(f"Failed to copy {source_key} to {dest_key}: {e}")
            return False

    def get_latest_backup_prefix(self):
        """
        Finds the most recent timestamp prefix in the bucket.
        Assumes prefixes are in YYYY-MM-DD_HH-MM-SS/ format.
        """
        try:
            paginator = self.s3_client.get_paginator('list_objects_v2')
            result = paginator.paginate(Bucket=self.bucket, Delimiter='/')
            prefixes = []
            for page in result:
                if 'CommonPrefixes' in page:
                    for prefix in page['CommonPrefixes']:
                        prefixes.append(prefix['Prefix'])
            
            if not prefixes:
                return None
            
            # Sort prefixes alphabetically, which works for YYYY-MM-DD_HH-MM-SS
            prefixes.sort()
            return prefixes[-1]
            
        except ClientError as e:
            logger.error(f"Failed to list bucket prefixes: {e}")
            return None

    def download_prefix(self, prefix, download_dir):
        """
        Downloads all objects under a given prefix using pagination.
        """
        if not os.path.exists(download_dir):
            os.makedirs(download_dir)

        paginator = self.s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=self.bucket, Prefix=prefix)

        downloaded_count = 0
        try:
            for page in pages:
                if 'Contents' not in page:
                    continue
                    
                for obj in page['Contents']:
                    s3_key = obj['Key']
                    
                    # Compute relative path from the prefix
                    rel_key = s3_key[len(prefix):].lstrip('/')
                    if not rel_key:
                        continue # Skip the folder object itself if it exists
                        
                    local_file_path = os.path.join(download_dir, os.path.normpath(rel_key))
                    
                    # Create parent directories
                    os.makedirs(os.path.dirname(local_file_path), exist_ok=True)
                    
                    logger.info(f"Downloading {s3_key} to {local_file_path}...")
                    self.s3_client.download_file(self.bucket, s3_key, local_file_path)
                    downloaded_count += 1
                    
            return downloaded_count
        except ClientError as e:
            logger.error(f"Failed to download from prefix {prefix}: {e}")
            raise

    def apply_lifecycle_policy(self):
        """
        Applies a lifecycle policy:
        - 30 days to STANDARD_IA
        - 90 days to GLACIER
        - 365 days expiration
        """
        lifecycle_rule = {
            'Rules': [
                {
                    'ID': 'BackupLifecycleRule',
                    'Filter': {'Prefix': ''},  # Apply to all objects
                    'Status': 'Enabled',
                    'Transitions': [
                        {'Days': 30, 'StorageClass': 'STANDARD_IA'},
                        {'Days': 90, 'StorageClass': 'GLACIER'}
                    ],
                    'Expiration': {'Days': 365}
                }
            ]
        }

        try:
            self.s3_client.put_bucket_lifecycle_configuration(
                Bucket=self.bucket,
                LifecycleConfiguration=lifecycle_rule
            )
            logger.info(f"Successfully applied lifecycle policy to bucket {self.bucket}")
            return True
        except ClientError as e:
            logger.error(f"Failed to apply lifecycle policy: {e}")
            return False
