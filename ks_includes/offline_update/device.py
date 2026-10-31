"""Read-only machine admission checks shared with the screen's native SWUpdate bridge."""

import json
import re
import time
import urllib.parse
import urllib.request

from .common import UpdateError


class DeviceGate:
    def __init__(self, policy):
        self.policy = policy
        self.sleep = time.sleep

    def api(self, endpoint):
        url = self.policy.get('moonrakerUrl', 'http://127.0.0.1:7125') + endpoint
        return self.http_json(url)

    @staticmethod
    def http_json(url):
        if not re.match(r'^http://127\.0\.0\.1:\d+/', url):
            raise UpdateError('NONLOCAL_HEALTH_URL')
        # Disable proxy environment: loopback checks must never reach a proxy/cloud.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=4) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise UpdateError('HEALTH_RESPONSE_TOO_LARGE')
        return json.loads(raw)

    def idle(self):
        try:
            value = self.api('/printer/objects/query?print_stats&pause_resume&webhooks'
                             '&idle_timeout&manual_probe&heaters')
            status = value['result']['status']
            if (status['webhooks']['state'] != 'ready' or
                    status['print_stats']['state'] not in ('standby', 'complete', 'cancelled', 'error') or
                    status['pause_resume']['is_paused'] is not False or
                    status['idle_timeout']['state'] not in ('Idle', 'Ready') or
                    status.get('manual_probe', {}).get('is_active', False)):
                raise UpdateError('PRINTER_NOT_IDLE')
            heaters = status['heaters']['available_heaters']
            if not isinstance(heaters, list):
                raise UpdateError('PRINTER_STATE_UNKNOWN')
            if heaters:
                query = '&'.join(urllib.parse.quote(name, safe='') for name in heaters)
                data = self.api('/printer/objects/query?' + query)['result']['status']
                for name in heaters:
                    if (data[name]['target'] != 0 or
                            data[name]['temperature'] > self.policy.get('maximumUpdateTemperature', 50)):
                        raise UpdateError('WAIT_FOR_COOLING')
        except UpdateError:
            raise
        except Exception as exc:
            raise UpdateError('PRINTER_STATE_UNKNOWN') from exc

    def validate_platform(self):
        import platform
        if platform.machine() != self.policy['device']['arch']:
            raise UpdateError('PLATFORM_MISMATCH')
