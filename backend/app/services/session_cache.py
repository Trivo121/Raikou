import os
import time
import shutil
import tempfile
import asyncio
import logging

logger = logging.getLogger(__name__)

def get_session_dir(session_id: str) -> str:
    session_root = os.environ.get("RAIKOU_SESSION_ROOT", tempfile.gettempdir())
    return os.path.join(session_root, f"raikou_session_{session_id}")

def touch_session(session_id: str):
    """
    Updates the modification time of a `.last_touched` file in the session directory.
    If the directory does not exist, it fails silently (e.g. it was evicted).
    """
    session_dir = get_session_dir(session_id)
    if not os.path.exists(session_dir):
        return
        
    touch_file = os.path.join(session_dir, ".last_touched")
    try:
        # Create or update mtime
        with open(touch_file, 'a'):
            os.utime(touch_file, None)
    except Exception as e:
        logger.warning(f"Failed to touch session {session_id}: {e}")

def sweep_stale_sessions(ttl_hours: int = 2):
    """
    Finds and deletes session directories that are inactive and finished processing.
    """
    temp_dir = os.environ.get("RAIKOU_SESSION_ROOT", tempfile.gettempdir())
    now = time.time()
    ttl_seconds = ttl_hours * 3600
    
    # Iterate over directories matching raikou_session_*
    try:
        for entry in os.listdir(temp_dir):
            if entry.startswith("raikou_session_"):
                session_dir = os.path.join(temp_dir, entry)
                if not os.path.isdir(session_dir):
                    continue
                    
                status_path = os.path.join(session_dir, "status.json")
                if os.path.exists(status_path):
                    # Still processing (status.json gets removed on completion)
                    continue
                    
                touch_file = os.path.join(session_dir, ".last_touched")
                
                # Determine last activity time
                last_active = 0
                if os.path.exists(touch_file):
                    last_active = os.path.getmtime(touch_file)
                else:
                    # Fallback to directory's mtime if no .last_touched exists yet
                    last_active = os.path.getmtime(session_dir)
                    
                if now - last_active > ttl_seconds:
                    logger.info(f"Sweeping stale session: {entry} (inactive for > {ttl_hours}h)")
                    try:
                        shutil.rmtree(session_dir, ignore_errors=True)
                    except Exception as e:
                        logger.error(f"Failed to delete {session_dir}: {e}")
    except Exception as e:
        logger.error(f"Error during sweep_stale_sessions: {e}")

async def start_cleanup_loop(interval_seconds: int = 900, ttl_hours: int = 2):
    """
    Infinite loop that sweeps stale sessions periodically (default every 15 min).
    """
    logger.info(f"Session cleanup background task started (TTL: {ttl_hours}h, Interval: {interval_seconds}s)")
    while True:
        try:
            sweep_stale_sessions(ttl_hours=ttl_hours)
        except Exception as e:
            logger.error(f"Unexpected error in start_cleanup_loop: {e}")
        await asyncio.sleep(interval_seconds)


