"""One background runtime synchronization shared by startup and the USB panel.

Workers only execute bounded subprocess requests.  State, timers and subscribers
are delivered through the GTK dispatcher supplied by the caller.
"""
import logging
import threading

from ks_includes.offline_update_client import OfflineUpdateClient, RUNTIME_REASONS


class OfflineUpdateRuntime:
    def __init__(self, *, dispatch, schedule, cancel, client=None, thread_factory=threading.Thread):
        self.client = client or OfflineUpdateClient()
        self._dispatch = dispatch
        self._schedule = schedule
        self._cancel = cancel
        self._thread_factory = thread_factory
        self._lock = threading.Lock()
        self._inflight = False
        self._timer = None
        self._timer_generation = 0
        self._subscribers = {}
        self._revision = 0
        self._state = {
            'runtimeState': 'CHECKING', 'runtimeReason': '', 'runtimeVersion': '', 'changed': False,
        }

    @property
    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def helper_available(self):
        return self.client.helper_available()

    def subscribe(self, callback):
        with self._lock:
            if callback in self._subscribers:
                return
            self._subscribers[callback] = {'token': object(), 'queued': False, 'delivered': None}
        self._queue_delivery(callback)

    def unsubscribe(self, callback):
        with self._lock:
            self._subscribers.pop(callback, None)

    def _queue_delivery(self, callback):
        with self._lock:
            subscription = self._subscribers.get(callback)
            if (subscription is None or subscription['queued'] or
                    subscription['delivered'] == self._revision):
                return
            subscription['queued'] = True
            token = subscription['token']
        self._dispatch(self._deliver, callback, token)

    def _deliver(self, callback, token):
        with self._lock:
            subscription = self._subscribers.get(callback)
            if subscription is None or subscription['token'] is not token:
                return False
            subscription['queued'] = False
            if subscription['delivered'] == self._revision:
                return False
            subscription['delivered'] = self._revision
            # A queued callback carries an identity, not an outdated snapshot.
            state = dict(self._state)
        try:
            callback(state)
        except Exception:
            logging.exception('Unable to display offline update runtime status')
        return False

    def _notify(self):
        with self._lock:
            callbacks = tuple(self._subscribers)
        for callback in callbacks:
            self._queue_delivery(callback)

    def ensure(self, force=False):
        with self._lock:
            if self._inflight or self._state['runtimeState'] == 'READY':
                return False
            if self._timer is not None and not force:
                return False
            timer, self._timer = self._timer, None
            self._timer_generation += 1
            self._inflight = True
            # Retries keep the last confirmed waiting/error reason on screen.
            # CHECKING is only the initial state, not a new state every 10s.
        if timer is not None:
            self._cancel(timer)

        def worker():
            try:
                result = self.client.ensure_runtime()
            except Exception as exc:
                reason = str(exc) if str(exc) in RUNTIME_REASONS else 'RUNTIME_SETUP_FAILED'
                result = {
                    'runtimeState': 'BLOCKED', 'runtimeReason': reason,
                    'runtimeVersion': '', 'changed': False,
                }
                logging.warning('Offline update runtime setup is blocked: %s', reason)
            self._dispatch(self._complete, result)

        try:
            self._thread_factory(target=worker, daemon=True).start()
        except Exception:
            self._dispatch(self._complete, {
                'runtimeState': 'BLOCKED', 'runtimeReason': 'RUNTIME_SETUP_FAILED',
                'runtimeVersion': '', 'changed': False,
            })
        return True

    def _complete(self, result):
        with self._lock:
            state = {key: result[key] for key in (
                'runtimeState', 'runtimeReason', 'runtimeVersion', 'changed',
            )}
            changed = state != self._state
            self._state = state
            if changed:
                self._revision += 1
            self._inflight = False
            self._timer_generation += 1
            generation = self._timer_generation
        if state['runtimeState'] != 'READY':
            timer = self._schedule(10 if state['runtimeState'] == 'DEFERRED' else 60,
                                   lambda: self._retry(generation))
            with self._lock:
                keep_timer = generation == self._timer_generation and not self._inflight
                if keep_timer:
                    self._timer = timer
            if not keep_timer:
                self._cancel(timer)
        if changed:
            logging.info('Offline update runtime state: %s (%s)', state['runtimeState'], state['runtimeReason'] or '-')
            self._notify()
        return False

    def _retry(self, generation):
        with self._lock:
            if generation != self._timer_generation:
                return False
            self._timer = None
        self.ensure()
        return False


def get_offline_update_runtime(screen, glib):
    """Share one coordinator for all page openings and the startup check."""
    runtime = getattr(screen, '_offline_update_runtime', None)
    if runtime is None:
        runtime = OfflineUpdateRuntime(
            dispatch=glib.idle_add, schedule=glib.timeout_add_seconds, cancel=glib.source_remove,
        )
        screen._offline_update_runtime = runtime
    return runtime
