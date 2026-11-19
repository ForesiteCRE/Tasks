"""Project header images: an uploaded photo, else Google Street View of the property address.

Uploads are stored next to the database (on the Railway volume) in uploads/.
Street View images are fetched server-side with GOOGLE_MAPS_API_KEY (so the key never reaches browsers)
and cached on disk per address. Without a key, the web page falls back to a map of the address.
"""
import base64
import glob
import hashlib
import logging
import os
import secrets

import httpx

from .db import DB_PATH, tx

log = logging.getLogger("photos")
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "uploads")
MAPS_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
MAX_BYTES = 8 * 1024 * 1024


def init():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with tx() as c:
        if "photo_path" not in {r[1] for r in c.execute("PRAGMA table_info(projects)")}:
            c.execute("ALTER TABLE projects ADD COLUMN photo_path TEXT")


def _kind(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def save_upload(pid: int, data_b64: str) -> str:
    data = base64.b64decode(data_b64.split(",")[-1])
    if len(data) > MAX_BYTES:
        raise ValueError("Photo is too large (8 MB max).")
    ext = _kind(data)
    if not ext:
        raise ValueError("Please upload a JPG, PNG or WebP image.")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = f"project-{pid}-{secrets.token_hex(4)}.{ext}"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(data)
    remove_upload(pid)
    with tx() as c:
        c.execute("UPDATE projects SET photo_path=? WHERE id=?", (name, pid))
    return name


def remove_upload(pid: int):
    with tx() as c:
        r = c.execute("SELECT photo_path FROM projects WHERE id=?", (pid,)).fetchone()
        c.execute("UPDATE projects SET photo_path=NULL WHERE id=?", (pid,))
    if r and r["photo_path"]:
        try:
            os.remove(os.path.join(UPLOAD_DIR, os.path.basename(r["photo_path"])))
        except FileNotFoundError:
            pass


def _decode(data_b64: str) -> tuple[bytes, str]:
    data = base64.b64decode(data_b64.split(",")[-1])
    if len(data) > MAX_BYTES:
        raise ValueError("Photo is too large (8 MB max).")
    ext = _kind(data)
    if not ext:
        raise ValueError("Please upload a JPG, PNG or WebP image.")
    return data, ext


def save_property_photo(prop_id: int, data_b64: str) -> str:
    data, ext = _decode(data_b64)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = f"property-{prop_id}-{secrets.token_hex(4)}.{ext}"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(data)
    remove_property_photo(prop_id)
    with tx() as c:
        c.execute("UPDATE properties SET photo_file=? WHERE id=?", (name, prop_id))
    return name


def remove_property_photo(prop_id: int):
    with tx() as c:
        r = c.execute("SELECT photo_file FROM properties WHERE id=?", (prop_id,)).fetchone()
        c.execute("UPDATE properties SET photo_file=NULL WHERE id=?", (prop_id,))
    if r and r["photo_file"]:
        try:
            os.remove(os.path.join(UPLOAD_DIR, os.path.basename(r["photo_file"])))
        except FileNotFoundError:
            pass


def property_photo_file(prop_id: int) -> str | None:
    with tx() as c:
        r = c.execute("SELECT photo_file FROM properties WHERE id=?", (prop_id,)).fetchone()
    if r and r["photo_file"]:
        path = os.path.join(UPLOAD_DIR, os.path.basename(r["photo_file"]))
        return path if os.path.exists(path) else None
    return None


def upload_file(pid: int) -> str | None:
    with tx() as c:
        r = c.execute("SELECT photo_path FROM projects WHERE id=?", (pid,)).fetchone()
    if r and r["photo_path"]:
        path = os.path.join(UPLOAD_DIR, os.path.basename(r["photo_path"]))
        return path if os.path.exists(path) else None
    return None


def streetview_file(address: str) -> str | None:
    """Cached Street View JPEG for an address, or None if no key / no imagery there."""
    if not (MAPS_KEY and address and address.strip()):
        return None
    h = hashlib.sha1(address.strip().lower().encode()).hexdigest()[:16]
    path = os.path.join(UPLOAD_DIR, f"sv-{h}.jpg")
    missing = path + ".none"
    if os.path.exists(path):
        return path
    if os.path.exists(missing):
        return None
    params = {"location": address, "key": MAPS_KEY, "source": "outdoor"}
    try:
        meta = httpx.get("https://maps.googleapis.com/maps/api/streetview/metadata", params=params, timeout=15).json()
        if meta.get("status") != "OK":
            open(missing, "w").close()   # remember "no imagery" so we don't ask again
            return None
        img = httpx.get("https://maps.googleapis.com/maps/api/streetview",
                        params={**params, "size": "640x320", "scale": "2", "fov": "80"}, timeout=20)
        if img.status_code != 200 or not _kind(img.content):
            log.warning("street view image failed: %s", img.status_code)
            return None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("street view error: %s", e)
        return None
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with open(path, "wb") as f:
        f.write(img.content)
    return path


def forget_streetview(address: str):
    """Address changed or retry wanted: drop cached result for it."""
    h = hashlib.sha1((address or "").strip().lower().encode()).hexdigest()[:16]
    for f in glob.glob(os.path.join(UPLOAD_DIR, f"sv-{h}.jpg*")):
        os.remove(f)
