"""Idempotent, config-driven enrichment for CRM-only client profiles."""
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
import os

from sqlalchemy import insert, select, update

from rd_import import db as source_db
from rd_import.codec import loads
from . import db, ignored_clients
from .directory import phone_digits


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = Path(os.environ.get('RD_CLIENT_PROFILE_ENRICHMENT_PATH') or
                      ROOT / 'client_profile_enrichment.json')
PRODUCTS = {'CCA', 'CCB', 'LB'}


class ClientEnrichmentError(ValueError):
    pass


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    return validate_config(loads(path.read_text(encoding='utf-8-sig')))


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict) or config.get('version') != 1:
        raise ClientEnrichmentError('Client enrichment config must use version 1.')
    if set(config.get('allowed_products') or []) != PRODUCTS:
        raise ClientEnrichmentError('Allowed products must be exactly CCA, CCB and LB.')
    source = str(config.get('source') or '').strip()
    records = config.get('records')
    if not source or not isinstance(records, list):
        raise ClientEnrichmentError('Client enrichment config needs a source and records list.')
    names = set()
    for record in records:
        if not isinstance(record, dict):
            raise ClientEnrichmentError('Every client enrichment record must be an object.')
        name = str(record.get('name') or '').strip()
        normalized = ignored_clients.normalize_name(name)
        product = str(record.get('product') or '').strip().upper()
        purchase_date = str(record.get('purchase_date') or '').strip()
        quantity = str(record.get('quantity') or '').strip()
        unit = str(record.get('unit') or '').strip().upper()
        phone = str(record.get('phone') or '').strip()
        email = str(record.get('email') or '').strip()
        record_source = str(record.get('source') or source).strip()
        if not normalized or normalized in names:
            raise ClientEnrichmentError('Client enrichment names must be non-empty and unique.')
        names.add(normalized)
        if product not in PRODUCTS:
            raise ClientEnrichmentError(f'{name} uses unsupported product {product or "(blank)"}.')
        if purchase_date:
            try:
                date.fromisoformat(purchase_date)
            except ValueError:
                raise ClientEnrichmentError(f'{name} has an invalid purchase date.') from None
        if quantity:
            try:
                amount = Decimal(quantity)
            except InvalidOperation:
                raise ClientEnrichmentError(f'{name} has an invalid quantity.') from None
            if amount <= 0 or not unit:
                raise ClientEnrichmentError(f'{name} must use a positive quantity and unit.')
        if phone and (len(phone) > 40 or len(phone_digits(phone)) < 5):
            raise ClientEnrichmentError(f'{name} has an invalid phone number.')
        if email and ('@' not in email or len(email) > 254 or email.startswith('@')):
            raise ClientEnrichmentError(f'{name} has an invalid email address.')
        if not record_source:
            raise ClientEnrichmentError(f'{name} must identify its enrichment source.')
    return config


def _has_product(value: str, product: str) -> bool:
    tokens = str(value or '').upper().replace('-', ' ').replace('/', ' ').split()
    return product in tokens


def apply_config(engine, config: dict) -> dict:
    config = validate_config(config)
    source = str(config['source']).strip()
    result = dict(profiles=0, interests_updated=0, references_updated=0,
                  contacts_created=0, contacts_updated=0)
    with db.transaction(engine) as connection:
        profiles = [dict(row) for row in connection.execute(select(db.clients)).mappings()]
        profiles_by_id = {profile['id']: profile for profile in profiles}
        ids_by_name = {}
        for profile in profiles:
            ids_by_name.setdefault(
                ignored_clients.normalize_name(profile['name']), set()).add(profile['id'])
        # A reviewed identity group may have retired the profile whose name is
        # present in the workbook. Resolve that name through its retained source
        # party links so enrichment lands on the canonical CRM profile.
        linked_names = connection.execute(select(
            source_db.parties.c.name, db.party_links.c.client_id).select_from(
                db.party_links.join(source_db.parties,
                    (db.party_links.c.company == source_db.parties.c.company) &
                    (db.party_links.c.party_id == source_db.parties.c.party_id)))).mappings()
        for row in linked_names:
            ids_by_name.setdefault(
                ignored_clients.normalize_name(row['name']), set()).add(row['client_id'])
        for record in config['records']:
            name = str(record['name']).strip()
            matches = [profiles_by_id[client_id] for client_id in sorted(
                ids_by_name.get(ignored_clients.normalize_name(name), set()))]
            if len(matches) != 1:
                raise ClientEnrichmentError(
                    f'{name} resolves to {len(matches)} CRM profiles; expected exactly one.')
            profile = matches[0]
            if ignored_clients.is_ignored(profile['name']):
                raise ClientEnrichmentError(f'{name} is also present in the ignored-client config.')
            product = str(record['product']).strip().upper()
            record_source = str(record.get('source') or source).strip()
            interest = str(profile['products_of_interest'] or '').strip()
            values = dict(
                reference_product=product,
                reference_purchase_date=str(record.get('purchase_date') or '').strip(),
                reference_purchase_quantity=str(record.get('quantity') or '').strip(),
                reference_purchase_unit=str(record.get('unit') or '').strip().upper(),
                reference_purchase_source=record_source,
                updated_at=db.now(),
            )
            if not _has_product(interest, product):
                values['products_of_interest'] = ', '.join(filter(None, (interest, product)))
                result['interests_updated'] += 1
            reference_fields = {key: values[key] for key in (
                'reference_product', 'reference_purchase_date', 'reference_purchase_quantity',
                'reference_purchase_unit', 'reference_purchase_source')}
            if any(str(profile.get(key) or '') != value for key, value in reference_fields.items()):
                result['references_updated'] += 1
            connection.execute(update(db.clients).where(db.clients.c.id == profile['id']).values(**values))
            result['profiles'] += 1

            phone = str(record.get('phone') or '').strip()
            email = str(record.get('email') or '').strip()
            if not phone and not email:
                continue
            contacts = [dict(row) for row in connection.execute(select(db.contacts).where(
                db.contacts.c.client_id == profile['id']).order_by(
                    db.contacts.c.is_primary.desc(), db.contacts.c.id)).mappings()]
            phone_missing = bool(phone) and not any(
                phone_digits(contact['phone']) == phone_digits(phone) for contact in contacts
                if contact['phone'])
            email_missing = bool(email) and not any(
                str(contact['email'] or '').strip().casefold() == email.casefold() for contact in contacts)
            primary = contacts[0] if contacts else None
            contact_values = {}
            if phone_missing and primary and not str(primary['phone'] or '').strip():
                contact_values['phone'] = phone
                phone_missing = False
            if email_missing and primary and not str(primary['email'] or '').strip():
                contact_values['email'] = email
                email_missing = False
            if contact_values:
                connection.execute(update(db.contacts).where(
                    db.contacts.c.id == primary['id']).values(**contact_values))
                result['contacts_updated'] += 1
            if phone_missing or email_missing:
                connection.execute(insert(db.contacts).values(
                    client_id=profile['id'], name='Office',
                    role='Imported from client details sheet',
                    phone=phone if phone_missing else '', whatsapp='',
                    email=email if email_missing else '', is_primary=int(not contacts)))
                result['contacts_created'] += 1
    return result


def apply(engine, path: Path = DEFAULT_CONFIG) -> dict:
    return apply_config(engine, load_config(path))
