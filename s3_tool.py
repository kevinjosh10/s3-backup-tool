import os
import sys
import time
import argparse
import logging
from datetime import datetime
from tqdm import tqdm

from config import Config
from file_utils import scan_directory, calculate_md5
from s3_utils import S3Utils

def setup_logging(log_level, log_file=None):
    """Sets up the Python logging module."""
    handlers = [logging.StreamHandler(sys.stdout)]
    
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
        
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=handlers
    )

def retry_operation(operation, max_retries=3, delay=2, **kwargs):
    """Simple manual retry wrapper for critical operations."""
    for attempt in range(1, max_retries + 1):
        try:
            return operation(**kwargs)
        except Exception as e:
            if attempt == max_retries:
                logging.error(f"Operation failed after {max_retries} attempts: {e}")
                raise
            logging.warning(f"Attempt {attempt} failed: {e}. Retrying in {delay} seconds...")
            time.sleep(delay)

def cmd_upload(args):
    """Handles the upload subcommand."""
    config = Config(vars(args))
    config.validate()
    s3_utils = S3Utils(config)
    
    base_dir = os.path.abspath(args.dir)
    if not os.path.isdir(base_dir):
        logging.error(f"Directory not found: {base_dir}")
        sys.exit(1)
        
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    current_prefix = f"{timestamp}/"
    logging.info(f"Starting upload to prefix: {current_prefix}")
    
    # 1. Scan directory
    files_to_check = scan_directory(base_dir)
    if not files_to_check:
        logging.info("No files found to upload.")
        return

    # 2. Get previous backup for incremental check
    latest_prefix = s3_utils.get_latest_backup_prefix()
    if latest_prefix:
        logging.info(f"Found previous backup: {latest_prefix}. Will perform incremental check.")
    else:
        logging.info("No previous backup found. Performing full backup.")

    logging.info(f"Scanning {len(files_to_check)} files for changes...")
    
    stats = {'uploaded': 0, 'skipped': 0, 'failed': 0}

    for s3_key_rel, local_path in tqdm(files_to_check.items(), desc="Processing files"):
        current_s3_key = current_prefix + s3_key_rel
        
        # Calculate local MD5
        local_md5 = calculate_md5(local_path)
        if not local_md5:
            stats['failed'] += 1
            continue
            
        needs_upload = True
        
        # Incremental logic
        if latest_prefix:
            previous_s3_key = latest_prefix + s3_key_rel
            try:
                previous_md5 = retry_operation(s3_utils.get_object_md5, max_retries=config.max_retries, s3_key=previous_s3_key)
                if previous_md5 == local_md5:
                    needs_upload = False
                    if not args.dry_run:
                        # Copy unchanged file to the new timestamp prefix server-side
                        copy_success = retry_operation(
                            s3_utils.copy_file, 
                            max_retries=config.max_retries, 
                            source_key=previous_s3_key, 
                            dest_key=current_s3_key
                        )
                        if copy_success:
                            logging.debug(f"Skipping unchanged file (server-side copy): {s3_key_rel}")
                            stats['skipped'] += 1
                        else:
                            # If copy fails, fallback to upload
                            needs_upload = True
                    else:
                        logging.info(f"[DRY RUN] Would skip unchanged file: {s3_key_rel}")
                        stats['skipped'] += 1
            except Exception as e:
                # File probably doesn't exist in previous backup or error occurred
                needs_upload = True

        if needs_upload:
            if not args.dry_run:
                logging.info(f"Uploading file... {local_path} -> {current_s3_key}")
                success = retry_operation(
                    s3_utils.upload_file,
                    max_retries=config.max_retries,
                    local_path=local_path,
                    s3_key=current_s3_key,
                    file_md5=local_md5
                )
                if success:
                    stats['uploaded'] += 1
                else:
                    stats['failed'] += 1
            else:
                logging.info(f"[DRY RUN] Would upload file: {local_path}")
                stats['uploaded'] += 1

    logging.info(f"Backup completed. Uploaded: {stats['uploaded']}, Skipped (Copied): {stats['skipped']}, Failed: {stats['failed']}")

def cmd_download(args):
    """Handles the download subcommand."""
    config = Config(vars(args))
    config.validate()
    s3_utils = S3Utils(config)
    
    timestamp = args.backup
    if not timestamp.endswith('/'):
        timestamp += '/'
        
    download_dir = os.path.abspath(args.dir) if args.dir else os.path.abspath(f"./restore_{timestamp.strip('/')}")
    
    logging.info(f"Starting download of backup {timestamp} to {download_dir}")
    
    try:
        count = s3_utils.download_prefix(timestamp, download_dir)
        logging.info(f"Download completed. Recreated {count} files in {download_dir}")
    except Exception as e:
        logging.error(f"Download failed: {e}")
        sys.exit(1)

def cmd_lifecycle(args):
    """Handles the lifecycle subcommand."""
    config = Config(vars(args))
    config.validate()
    s3_utils = S3Utils(config)
    
    logging.info("Applying lifecycle policy to bucket...")
    if s3_utils.apply_lifecycle_policy():
        logging.info("Lifecycle policy applied successfully.")
    else:
        logging.error("Failed to apply lifecycle policy.")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Production-grade AWS S3 Backup Tool")
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], help="Set logging level")
    parser.add_argument('--log-file', default='logs/s3_backup.log', help="Optional file path to write logs")
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Upload Command
    upload_parser = subparsers.add_parser('upload', help="Upload a directory to S3 (incremental)")
    upload_parser.add_argument('--dir', required=True, help="Local directory to backup")
    upload_parser.add_argument('--bucket', help="S3 Bucket Name")
    upload_parser.add_argument('--region', help="AWS Region")
    upload_parser.add_argument('--kms-key', help="KMS Key ID for encryption (optional)")
    upload_parser.add_argument('--retries', type=int, help="Max retries for network operations")
    upload_parser.add_argument('--dry-run', action='store_true', help="Show what would happen without uploading")
    
    # Download Command
    download_parser = subparsers.add_parser('download', help="Download a specific backup")
    download_parser.add_argument('--backup', required=True, help="Timestamp prefix to download (e.g. 2023-10-01_12-00-00)")
    download_parser.add_argument('--dir', help="Local directory to restore to (defaults to ./restore_<timestamp>)")
    download_parser.add_argument('--bucket', help="S3 Bucket Name")
    download_parser.add_argument('--region', help="AWS Region")
    
    # Lifecycle Command
    lifecycle_parser = subparsers.add_parser('lifecycle', help="Apply S3 lifecycle policies")
    lifecycle_parser.add_argument('--bucket', help="S3 Bucket Name")
    lifecycle_parser.add_argument('--region', help="AWS Region")

    args = parser.parse_args()
    
    setup_logging(args.log_level, args.log_file)
    
    if args.command == 'upload':
        cmd_upload(args)
    elif args.command == 'download':
        cmd_download(args)
    elif args.command == 'lifecycle':
        cmd_lifecycle(args)

if __name__ == '__main__':
    main()
