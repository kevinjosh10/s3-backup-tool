import os
import hashlib
import logging

logger = logging.getLogger(__name__)

def calculate_md5(file_path, chunk_size=8192):
    """
    Calculate the MD5 hash of a local file.
    Reads the file in chunks to avoid memory issues with large files.
    """
    md5_hash = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                md5_hash.update(chunk)
        return md5_hash.hexdigest()
    except Exception as e:
        logger.error(f"Failed to calculate MD5 for {file_path}: {e}")
        return None

def scan_directory(base_dir):
    """
    Recursively scans a directory and returns a dictionary of:
    { 'normalized/s3/path.txt': 'absolute/local/path.txt' }
    
    Skips empty directories.
    """
    base_dir = os.path.abspath(base_dir)
    file_mapping = {}

    if not os.path.exists(base_dir):
        logger.error(f"Directory not found: {base_dir}")
        return file_mapping

    if not os.path.isdir(base_dir):
        logger.error(f"Path is not a directory: {base_dir}")
        return file_mapping

    for root, dirs, files in os.walk(base_dir):
        for file in files:
            local_path = os.path.join(root, file)
            # Calculate relative path
            rel_path = os.path.relpath(local_path, base_dir)
            # Normalize path for S3 (enforce forward slashes)
            s3_key = os.path.normpath(rel_path).replace(os.sep, '/')
            
            file_mapping[s3_key] = local_path
            
    # Note: os.walk natively skips empty directories when extracting only files.
    # Empty folders won't be represented in the file_mapping.
    
    return file_mapping
