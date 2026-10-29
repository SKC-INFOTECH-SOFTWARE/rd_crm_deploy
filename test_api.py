"""Read-only Balance API explorer. Python 3.10+, no dependencies in helper mode."""
import argparse
import base64
import csv
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parent
ENDPOINTS = ['product-master', 'party-master', 'sale', 'pbill', 'salereturn',
             'preturn', 'taxablecn', 'taxabledn', 'servicebill', 'miscpbill',
             'receivevoucher', 'paymentvoucher', 'journalvoucher']

# Exact vendor messages observed in empty responses. Never use substring matching:
# an authentication/database error mentioning "no records" is still a failure.
EMPTY_RESULT_MESSAGES = (
    'no data found', 'no records found', 'no record found',
    'operation succeeded, but no records found',
)

class ApiError(Exception):
    pass

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def post(config, endpoint, payload, *, json_loads=json.loads, response_observer=None):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    key = os.environ.get('RD_SUBSCRIPTION_KEY', '')
    if key:
        header = config.get('subscription_key_header')
        if not header:
            raise ApiError('Set subscription_key_header to the vendor-confirmed header name.')
        headers[header] = key
    url = config['base_url'].rstrip('/') + '/' + endpoint
    req = Request(url, data=json.dumps(payload).encode(), headers=headers, method='POST')
    try:
        with build_opener(NoRedirect()).open(req, timeout=config['timeout_seconds']) as response:
            raw = response.read()
    except HTTPError as e:
        retry_after = e.headers.get('Retry-After') if e.headers else None
        if response_observer:
            response_observer(e.read(), e.code)
        error = ApiError(f'HTTP {e.code}; check endpoint, access credentials and company code.')
        error.status = e.code
        error.retry_after = retry_after
        raise error from None
    except (URLError, TimeoutError, OSError) as e:
        raise ApiError(f'Connection failed ({type(e).__name__}); check network/VPN and server availability.') from None
    if response_observer:
        response_observer(raw, response.status)
    try:
        return json_loads(raw)
    except (ValueError, UnicodeError):
        raise ApiError('Server returned non-JSON content.') from None

def unwrap(value, *, json_loads=json.loads):
    """Remove API wrappers, accepting only known empty-result messages."""
    for _ in range(8):
        if isinstance(value, str):
            try:
                value = json_loads(value)
                continue
            except ValueError:
                raise ApiError('Unexpected text response.') from None
        if isinstance(value, dict) and 'response' in value:
            status = str(value['response']).lower()
            message = str(value.get('errormessage') or '').strip().lower().rstrip('.')
            if status not in ('success', 'true'):
                if (status == 'false' and message in EMPTY_RESULT_MESSAGES
                        and 'data' in value and value['data'] in (None, [], {})):
                    return []
                raise ApiError('API reported failure; inspect the saved raw response.')
            if 'data' not in value:
                raise ApiError('Success wrapper is missing data.')
            value = value['data']
            continue
        return value
    raise ApiError('Too many nested response wrappers.')

def envelope(value):
    if not isinstance(value, dict):
        return None
    lower = {k.lower(): v for k, v in value.items()}
    if {'encryptedkey', 'encrypteddata', 'iv'} <= lower.keys():
        return {'encryptedKey': lower['encryptedkey'], 'encryptedData': lower['encrypteddata'], 'iv': lower['iv']}
    return None

def decrypt_local(env, config, *, json_loads=json.loads):
    try:
        from cryptography.hazmat.primitives import hashes, padding, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise ApiError('Local decryption needs: python -m pip install -r requirements-local.txt') from None
    key_path = Path(os.environ.get('RD_PRIVATE_KEY_PATH') or config['private_key_path'])
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    password = os.environ.get('RD_PRIVATE_KEY_PASSWORD')
    private = serialization.load_pem_private_key(key_path.read_bytes(), password=password.encode() if password else None)
    key = private.decrypt(base64.b64decode(env['encryptedKey'], validate=True), rsa_padding.OAEP(
        mgf=rsa_padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    if len(key) != 32:
        raise ApiError('Expected AES-256 session key.')
    decryptor = Cipher(algorithms.AES(key), modes.CBC(base64.b64decode(env['iv'], validate=True))).decryptor()
    padded = decryptor.update(base64.b64decode(env['encryptedData'], validate=True)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return json_loads((unpadder.update(padded) + unpadder.finalize()).decode('utf-8-sig'))

def get_records(value, *, json_loads=json.loads):
    value = unwrap(value, json_loads=json_loads)
    if isinstance(value, dict):
        for field in ('Header', 'Body'):
            if field in value:
                value = value[field]
                break
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ApiError('Unknown record structure; inspect decrypted.json. No records were assumed.')
    return value

def cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    # Prevent spreadsheet formulas when exported text is opened in Excel.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value

def export_csv(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows({k: cell(v) for k, v in row.items()} for row in rows)

def validate(endpoint, rows):
    issues = []
    required = {'product-master': ['ItemId', 'ItemName', 'Unit'], 'party-master': ['PartyId', 'Name'],
                'receivevoucher': ['ReceiptNo', 'Date'], 'paymentvoucher': ['ReceiptNo', 'Date'],
                'journalvoucher': ['NRVNO', 'DATE']}.get(endpoint, ['VchNo', 'Date'])
    for n, row in enumerate(rows):
        missing = [k for k in required if row.get(k) in (None, '')]
        if missing:
            issues.append({'row': n, 'issue': 'missing_fields', 'fields': missing})
        for i, item in enumerate(row.get('InventoryItems') or []):
            if not isinstance(item, dict):
                issues.append({'row': n, 'line': i, 'issue': 'invalid_inventory_line'})
                continue
            try:
                if float(item.get('Amount', 0)) == 0 and float(item.get('Qty', 0)) * float(item.get('Rate', 0)) != 0:
                    issues.append({'row': n, 'line': i, 'issue': 'zero_amount_with_nonzero_qty_rate'})
            except (TypeError, ValueError):
                issues.append({'row': n, 'line': i, 'issue': 'invalid_numeric_value'})
    return issues

def save_records(folder, db, run_id, endpoint, company, rows):
    dump(folder / 'records.json', rows)
    export_csv(folder / 'records.csv', rows)
    lines = []
    for n, row in enumerate(rows):
        for group in ('InventoryItems', 'ServiceItems', 'Payments', 'ALLLEDGERENTRIES'):
            for i, item in enumerate(row.get(group) or []):
                if isinstance(item, dict):
                    lines.append({'_record_index': n, '_line_index': i, '_group': group,
                                  '_voucher': row.get('VchNo', row.get('ReceiptNo', row.get('NRVNO'))),
                                  '_date': row.get('Date', row.get('DATE')), **item})
        db.execute('INSERT INTO records VALUES (?, ?, ?, ?, ?)',
                   (run_id, endpoint, company, n, json.dumps(row, ensure_ascii=False)))
    export_csv(folder / 'lines.csv', lines)
    db.commit()

def run(args):
    config = json.loads((ROOT / args.config).read_text(encoding='utf-8'))
    url = urlparse(config['base_url'])
    if url.scheme not in ('http', 'https') or not url.netloc or url.username or url.query or url.fragment:
        raise ApiError('base_url must be an HTTP(S) API base without credentials, query or fragment.')
    if args.list:
        print('\n'.join(ENDPOINTS))
        return 0
    if args.since:
        try:
            dt = datetime.strptime(args.since, '%Y-%m-%d')
        except ValueError:
            raise ApiError('--since must be YYYY-MM-DD.') from None
    else:
        dt = datetime.now()
    months = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
    date = f'{dt.day:02}/{months[dt.month-1]}/{dt.year}'
    selected = ENDPOINTS if args.all else [args.endpoint or 'product-master']
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8]
    output = ROOT / 'output' / run_id
    output.mkdir(parents=True)
    db = sqlite3.connect(ROOT / 'output' / 'rdic_test.sqlite3')
    db.execute('CREATE TABLE IF NOT EXISTS records (run_id TEXT, endpoint TEXT, company TEXT, row_index INTEGER, payload_json TEXT, PRIMARY KEY (run_id, endpoint, row_index))')
    db.execute('CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, summary_json TEXT)')
    print(f'Mode: {"incremental" if args.since else "full"}; decryption: {config["decryption_mode"]}')
    print('Read-only requests. Output contains confidential business data.')
    results = []
    for endpoint in selected:
        company = args.company or config.get('endpoint_companies', {}).get(endpoint) or config['master_company' if endpoint.endswith('-master') else 'transaction_company']
        payload = {'compshrtname': company, 'fetchall': 'N' if args.since else 'Y', 'lastsyncdt': date}
        folder = output / endpoint
        folder.mkdir()
        dump(folder / 'request.json', {'endpoint': endpoint, 'body': payload})
        result = {'endpoint': endpoint, 'company': company}
        start = time.monotonic()
        try:
            raw = post(config, endpoint, payload)
            dump(folder / 'raw.json', raw)
            data = unwrap(raw)
            env = envelope(data)
            if env:
                if config['decryption_mode'] == 'helper':
                    data = post(config, 'decrypteddata', env)
                elif config['decryption_mode'] == 'local':
                    data = decrypt_local(env, config)
                else:
                    raise ApiError('decryption_mode must be helper or local.')
            dump(folder / 'decrypted.json', data)
            rows = get_records(data)
            issues = validate(endpoint, rows)
            save_records(folder, db, run_id, endpoint, company, rows)
            dump(folder / 'checks.json', issues)
            result.update(status='WARN' if issues else ('OK' if rows else 'EMPTY'), records=len(rows), warnings=len(issues))
        except (ApiError, ValueError, OSError, TypeError, KeyError) as e:
            result.update(status='FAIL', error=str(e) if isinstance(e, ApiError) else f'{type(e).__name__}: check configuration, response files or private key.')
        result['seconds'] = round(time.monotonic() - start, 2)
        results.append(result)
        dump(output / 'summary.json', results)
        print(f'{endpoint:18} {company:5} {result["status"]:6} records={result.get("records", "-")} {result.get("error", "")}')
    db.execute('INSERT INTO runs VALUES (?, ?)', (run_id, json.dumps(results)))
    db.commit()
    db.close()
    print(f'\nFiles: {output}\nDatabase: {ROOT / "output" / "rdic_test.sqlite3"}')
    return int(any(r['status'] == 'FAIL' for r in results))

def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group()
    group.add_argument('--endpoint', choices=ENDPOINTS)
    group.add_argument('--all', action='store_true')
    p.add_argument('--list', action='store_true')
    p.add_argument('--since', help='Incremental fetch since YYYY-MM-DD; omitted means full fetch')
    p.add_argument('--company', help='Override documented company code for this run')
    p.add_argument('--config', default='config.json')
    try:
        return run(p.parse_args())
    except (ApiError, OSError, ValueError, KeyError) as e:
        print(f'Setup error: {e}', file=sys.stderr)
        return 2

if __name__ == '__main__':
    sys.exit(main())
