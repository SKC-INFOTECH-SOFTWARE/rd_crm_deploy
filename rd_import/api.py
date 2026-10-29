"""Thin adapter over the working tester; only whitelisted read routes are used."""
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import test_api
from .codec import loads
from .companies import companies_for, default_company, validate_company_code

ENDPOINTS = ('product-master', 'party-master', 'pbill', 'miscpbill', 'sale',
             'salereturn', 'preturn', 'taxablecn', 'taxabledn', 'servicebill',
             'receivevoucher', 'paymentvoucher', 'journalvoucher')


def company_for(config, endpoint):
    return default_company(config, endpoint)


def validate_config(config):
    url = urlparse(config['base_url'])
    if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError('Invalid API base URL')
    if config['decryption_mode'] not in ('helper', 'local'):
        raise ValueError('Invalid decryption mode')
    if not 0 < float(config['timeout_seconds']) <= 3600:
        raise ValueError('timeout_seconds must be positive and at most 3600')
    for endpoint in ENDPOINTS:
        companies_for(config, endpoint)
    if set(config.get('endpoint_company_scopes', {})) - set(ENDPOINTS):
        raise ValueError('Unknown endpoint in endpoint_company_scopes')
    reused_number_scopes = config.get('allow_reused_voucher_numbers', {})
    if not isinstance(reused_number_scopes, dict):
        raise ValueError('allow_reused_voucher_numbers must be an object')
    for company, endpoints in reused_number_scopes.items():
        validate_company_code(company)
        if (not isinstance(endpoints, list) or not endpoints or
                any(not isinstance(endpoint, str) for endpoint in endpoints)):
            raise ValueError('Each allow_reused_voucher_numbers entry must be a nonempty endpoint array')
        if len(endpoints) != len(set(endpoints)):
            raise ValueError('Duplicate endpoint in allow_reused_voucher_numbers')
        if any(endpoint not in ENDPOINTS[2:] for endpoint in endpoints):
            raise ValueError('allow_reused_voucher_numbers supports voucher endpoints only')
    # Actual authentication material belongs in environment/.env, never JSON.
    if any(k.lower() in ('subscription_key', 'password', 'private_key_password', 'token', 'api_key') for k in config):
        raise ValueError('Move credentials out of configuration into environment variables')


def allows_reused_voucher_number(config, company, endpoint):
    """Return an explicit company/endpoint exception for a confirmed vendor defect."""
    return endpoint in config.get('allow_reused_voucher_numbers', {}).get(company, ())


def error_code(error):
    if isinstance(error, test_api.ApiError):
        match = re.match(r'HTTP (\d{3});', str(error))
        if match:
            return 'http_' + match[1]
        if str(error).startswith('Connection failed'):
            return 'connection_failed'
        return 'api_response_or_decryption_error'
    # Do not persist exceptions containing SQL parameters, names or key paths.
    return type(error).__name__


class BalanceClient:
    def __init__(self, config, retries=2, sleeper=time.sleep):
        validate_config(config)
        if not 0 <= retries <= 5:
            raise ValueError('retries must be 0..5')
        self.config, self.retries, self.sleeper = config, retries, sleeper

    def request(self, route, payload, observe):
        if route not in (*ENDPOINTS, 'decrypteddata'):
            raise ValueError('Route is not a read-only Balance route')
        for attempt in range(self.retries + 1):
            try:
                return test_api.post(self.config, route, payload, json_loads=loads,
                                     response_observer=lambda body, status: observe(route, body, status))
            except test_api.ApiError as error:
                code = error_code(error)
                transient = code in ('connection_failed', 'http_408', 'http_429',
                                     'http_500', 'http_502', 'http_503', 'http_504')
                if not transient or attempt == self.retries:
                    raise
                delay = min(2 ** attempt, 8)
                supplied = getattr(error, 'retry_after', None)
                if supplied:
                    try:
                        delay = max(delay, min(float(supplied), 300))
                    except (TypeError, ValueError):
                        try:
                            target = parsedate_to_datetime(supplied)
                            if target.tzinfo is None:
                                target = target.replace(tzinfo=timezone.utc)
                            delay = max(delay, min(max(
                                (target - datetime.now(timezone.utc)).total_seconds(), 0), 300))
                        except (TypeError, ValueError, OverflowError):
                            pass
                self.sleeper(delay)

    def fetch(self, endpoint, payload, observe, save_decrypted):
        if endpoint not in ENDPOINTS:
            raise ValueError('Unknown endpoint')
        raw = self.request(endpoint, payload, observe)
        data = test_api.unwrap(raw, json_loads=loads)
        envelope = test_api.envelope(data)
        if envelope:
            if self.config['decryption_mode'] == 'helper':
                data = self.request('decrypteddata', envelope, observe)
            else:
                data = test_api.decrypt_local(envelope, self.config, json_loads=loads)
        save_decrypted(data)
        return test_api.get_records(data, json_loads=loads)
