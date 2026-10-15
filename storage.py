"""Where finished files go.

Android: a folder the user picks with the system folder picker (Storage Access
Framework). The app gets a permission for that one folder, which survives
restarts. No storage permissions are needed in the manifest.

Desktop (for testing): an ordinary folder path.
"""

from __future__ import annotations

import hashlib
import shutil
import threading
from pathlib import Path

from kivy.utils import platform

ANDROID = platform == "android"
DIR_MIME = "vnd.android.document/directory"

if ANDROID:
    from android import activity
    from jnius import autoclass

    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Intent = autoclass("android.content.Intent")
    Uri = autoclass("android.net.Uri")
    DocumentsContract = autoclass("android.provider.DocumentsContract")
    JFiles = autoclass("java.nio.file.Files")
    JFile = autoclass("java.io.File")
    PICK_CODE = 7341
    RESULT_OK = -1


def _resolver():
    return PythonActivity.mActivity.getContentResolver()


def native_lib_dir() -> str:
    """Folder where Android unpacks lib*.so files (the only place an app may run a binary from)."""
    if not ANDROID:
        return ""
    return str(PythonActivity.mActivity.getApplicationInfo().nativeLibraryDir)


def pick_folder(callback) -> None:
    """Open the system folder picker. callback(location_or_None) runs on a background thread."""
    if not ANDROID:
        raise RuntimeError("pick_folder is Android-only; the desktop build uses its own dialog")

    def on_result(request, result, intent):
        if request != PICK_CODE:
            return
        activity.unbind(on_activity_result=on_result)
        if result != RESULT_OK or intent is None or intent.getData() is None:
            callback(None)
            return
        uri = intent.getData()
        flags = Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
        try:
            _resolver().takePersistableUriPermission(uri, flags)
        except Exception:
            pass  # still usable for this session
        callback(uri.toString())

    activity.bind(on_activity_result=on_result)
    intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
    intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                    | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                    | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION)
    PythonActivity.mActivity.startActivityForResult(intent, PICK_CODE)


def describe(location: str) -> str:
    """Human-readable version of a location, e.g. 'primary:Music'."""
    if ANDROID and location.startswith("content://"):
        try:
            return str(DocumentsContract.getTreeDocumentId(Uri.parse(location)))
        except Exception:
            pass
    return location


class _Tree:
    """Small wrapper over one picked folder (a SAF document tree)."""

    def __init__(self, location: str):
        self.tree = Uri.parse(location)
        self.root_id = DocumentsContract.getTreeDocumentId(self.tree)

    def doc_uri(self, doc_id):
        return DocumentsContract.buildDocumentUriUsingTree(self.tree, doc_id)

    def children(self, parent_id):
        uri = DocumentsContract.buildChildDocumentsUriUsingTree(self.tree, parent_id)
        cursor = _resolver().query(uri, ["document_id", "_display_name", "mime_type"],
                                   None, None, None)
        rows = []
        if cursor is None:
            return rows
        try:
            while cursor.moveToNext():
                rows.append((str(cursor.getString(0)), str(cursor.getString(1)),
                             str(cursor.getString(2))))
        finally:
            cursor.close()
        return rows

    def find_or_make_dir(self, parent_id, name):
        for doc_id, display, mime in self.children(parent_id):
            if display == name and mime == DIR_MIME:
                return doc_id
        created = DocumentsContract.createDocument(
            _resolver(), self.doc_uri(parent_id), DIR_MIME, name)
        if created is None:
            raise OSError(f"Could not create the folder '{name}' in the chosen location.")
        return str(DocumentsContract.getDocumentId(created))

    def write_file(self, parent_id, name, mime, src: Path) -> str:
        for _doc_id, display, _mime in self.children(parent_id):
            if display == name:
                return name  # already there (names include the YouTube id)
        created = DocumentsContract.createDocument(_resolver(), self.doc_uri(parent_id), mime, name)
        if created is None:
            raise OSError("Android refused to create the file in the chosen folder.")
        out = _resolver().openOutputStream(created)
        if out is None:
            raise OSError("Could not open the new file for writing.")
        try:
            # Java does the copy, so no audio data passes through Python.
            JFiles.copy(JFile(str(src)).toPath(), out)
        finally:
            out.close()
        return name


class Target:
    """Receives finished audio files and remembers which tracks are already done."""

    def __init__(self, location: str, subfolder: str, state_root: Path):
        self.location = location
        self.subfolder = subfolder
        key = hashlib.sha1(f"{location}|{subfolder}".encode()).hexdigest()[:16]
        self._state = Path(state_root) / key
        self._lock = threading.Lock()
        self._tree = None
        self._parent_id = None

    def state_file(self) -> Path:
        self._state.mkdir(parents=True, exist_ok=True)
        return self._state / "downloaded_tracks.txt"

    def deliver(self, src: Path) -> str:
        if ANDROID and self.location.startswith("content://"):
            with self._lock:
                if self._tree is None:
                    self._tree = _Tree(self.location)
                    parent = self._tree.root_id
                    if self.subfolder:
                        parent = self._tree.find_or_make_dir(parent, self.subfolder)
                    self._parent_id = parent
            mime = "audio/mpeg" if src.suffix.lower() == ".mp3" else "audio/mp4"
            return self._tree.write_file(self._parent_id, src.name, mime, src)
        dest_dir = Path(self.location) / self.subfolder if self.subfolder else Path(self.location)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if not dest.exists():
            shutil.copyfile(src, dest)
        return str(dest)
