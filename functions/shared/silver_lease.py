"""Cooperative, renewed writer lease; Delta optimistic concurrency still applies."""
from contextlib import AbstractContextManager
from threading import Event, Lock, Thread

from azure.core.exceptions import HttpResponseError, ResourceExistsError


class LeaseBusy(RuntimeError):
    """Another writer currently holds the shared dataset lease."""


class LeaseLost(RuntimeError):
    """Writer must stop: lease renewal failed or lease was released."""


class BlobWriterLease(AbstractContextManager):
    """Lease one pre-created control blob for 60s, renewing every 15s.

    Checkpoints also renew synchronously. A lease does not fence an already
    in-flight Delta commit: commit conflict detection and publication checks are
    essential. No error is retried under a lost lease.
    """
    def __init__(self, blob, *, duration=60, renew_interval=15):
        if not 15 <= duration <= 60 or not 0 < renew_interval < duration / 2:
            raise ValueError('Invalid lease duration/renew interval')
        self.blob = blob
        self.duration = duration
        self.renew_interval = renew_interval
        self._lease = None
        self._lost = Event()
        self._stop = Event()
        self._mutex = Lock()
        self._thread = None

    def __enter__(self):
        try:
            self.blob.upload_blob(b'', overwrite=False)
        except ResourceExistsError:
            pass
        try:
            self._lease = self.blob.acquire_lease(lease_duration=self.duration)
        except HttpResponseError as error:
            if getattr(error, 'error_code', None) in (
                'LeaseAlreadyPresent', 'LeaseIsBreakingAndCannotBeAcquired',
            ):
                raise LeaseBusy('Silver writer lease is held') from None
            raise
        self._thread = Thread(target=self._renew_background, daemon=True)
        self._thread.start()
        return self

    def _renew_background(self):
        while not self._stop.wait(self.renew_interval):
            try:
                self.assert_held()
            except LeaseLost:
                return

    def assert_held(self):
        with self._mutex:
            if self._lost.is_set() or self._stop.is_set() or self._lease is None:
                raise LeaseLost('Silver writer lease is unavailable')
            try:
                self._lease.renew(timeout=10, connection_timeout=5, read_timeout=10, retry_total=0)
            except Exception:
                self._lost.set()
                raise LeaseLost('Silver writer lease renewal failed') from None

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.renew_interval + 1)
        with self._mutex:
            if self._lease is not None:
                try:
                    self._lease.release()
                except Exception:
                    # Expiry/release failure cannot turn an unpublished run into
                    # a success, nor replace the original failure.
                    self._lost.set()
        return False
