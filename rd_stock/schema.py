"""Versioned, additive stock schema; imported tables belong to rd_import only."""
from sqlalchemy import (MetaData, Table, Column as C, Integer as I, Text as T,
                        ForeignKey as FK, UniqueConstraint as U, Index)
from rd_import.codec import ExactDecimal as D

metadata = MetaData()


def table(name, *columns):
    return Table('app_' + name, metadata, *columns)


versions = table('schema_versions', C('version', I, primary_key=True), C('applied_at', T, nullable=False))
settings = table('stock_settings', C('id', I, primary_key=True), C('start_date', T, nullable=False))
items = table('stock_items', C('id', I, primary_key=True), C('product_id', I, unique=True, nullable=False),
              C('display_name', T, nullable=False), C('classification', T, nullable=False),
              C('tracking', I, nullable=False), C('base_unit', T, nullable=False))
conversions = table('unit_conversions', C('item_id', FK(items.c.id), primary_key=True),
                    C('unit', T, primary_key=True), C('factor', D, nullable=False))
locations = table('locations', C('id', I, primary_key=True), C('name', T, unique=True, nullable=False))
defaults = table('stock_defaults', C('company', T, primary_key=True), C('location_id', FK(locations.c.id), nullable=False))
location_maps = table('source_locations', C('company', T, primary_key=True), C('source_name', T, primary_key=True),
                      C('location_id', FK(locations.c.id), nullable=False))
rules = table('quantity_sign_rules', C('endpoint', T, primary_key=True), C('convention', T, nullable=False))
status_policies = table('missing_status_policies', C('company', T, primary_key=True),
                        C('endpoint', T, primary_key=True), C('basis', T, nullable=False),
                        C('operator', T, nullable=False), C('confirmed_at', T, nullable=False))
mappings = table('line_mappings', C('source_key', T, primary_key=True), C('descriptor', T, nullable=False),
                 C('item_id', FK(items.c.id)), C('stock_affecting', I, nullable=False),
                 C('operator', T, nullable=False), C('confirmed_at', T, nullable=False))
openings = table('opening_stock', C('id', I, primary_key=True), C('item_id', FK(items.c.id), nullable=False),
                 C('location_id', FK(locations.c.id), nullable=False), C('quantity', D, nullable=False),
                 C('reference', T), C('notes', T), C('known_value', D), C('value_currency', T),
                 C('operator', T, nullable=False), C('created_at', T, nullable=False), U('item_id', 'location_id'))
recipes = table('recipes', C('id', I, primary_key=True), C('product_id', FK(items.c.id), nullable=False),
                C('name', T, nullable=False), C('version', I, nullable=False), C('output', D, nullable=False),
                C('unit', T, nullable=False), C('ingredients_json', T, nullable=False), C('notes', T),
                C('active', I, nullable=False), C('operator', T, nullable=False), C('created_at', T, nullable=False),
                U('product_id', 'name', 'version'))
documents = table('stock_documents', C('id', I, primary_key=True), C('token', T, unique=True, nullable=False),
                  C('kind', T, nullable=False), C('number', T, unique=True, nullable=False), C('date', T, nullable=False),
                  C('state', T, nullable=False), C('recipe_id', FK(recipes.c.id)), C('payload_json', T, nullable=False),
                  C('operator', T, nullable=False), C('created_at', T, nullable=False), C('posted_at', T),
                  C('reverses_id', FK('app_stock_documents.id'), unique=True))
entries = table('stock_entries', C('id', I, primary_key=True), C('item_id', FK(items.c.id), nullable=False),
                C('location_id', FK(locations.c.id), nullable=False), C('date', T, nullable=False),
                C('quantity', D, nullable=False), C('owner', T, nullable=False), C('document_id', FK(documents.c.id)),
                C('source_key', T), C('voucher_id', I), C('line_id', I), C('fingerprint', T),
                C('active', I, nullable=False), C('created_at', T, nullable=False), C('retired_at', T),
                C('source_snapshot', T))
issues = table('stock_issues', C('id', I, primary_key=True), C('source_key', T, nullable=False),
               C('voucher_id', I), C('line_id', I), C('code', T, nullable=False), C('detail', T, nullable=False),
               C('active', I, nullable=False), C('created_at', T, nullable=False), C('resolved_at', T))
audits = table('stock_audit', C('id', I, primary_key=True), C('action', T, nullable=False),
               C('operator', T, nullable=False), C('at', T, nullable=False), C('detail_json', T, nullable=False))
authoritative_runs = table('authoritative_stock_runs', C('id', T, primary_key=True),
    C('company', T, nullable=False), C('started_at', T, nullable=False), C('finished_at', T),
    C('status', T, nullable=False), C('source_as_of', T), C('error_code', T))
authoritative_balances = table('authoritative_stock_balances', C('id', I, primary_key=True),
    C('run_id', FK(authoritative_runs.c.id, ondelete='CASCADE'), nullable=False),
    C('company', T, nullable=False), C('item_source_id', T, nullable=False),
    C('item_name', T, nullable=False), C('location_source_id', T, nullable=False),
    C('location_name', T, nullable=False), C('quantity', D, nullable=False),
    C('unit', T, nullable=False), C('reserved_quantity', D), C('available_quantity', D),
    C('source_as_of', T), U('run_id', 'company', 'item_source_id', 'location_source_id', 'unit'))
Index('app_entries_balance', entries.c.item_id, entries.c.location_id, entries.c.active, entries.c.date)
Index('app_entries_source', entries.c.source_key, entries.c.active)
Index('app_authoritative_stock_latest', authoritative_runs.c.company,
      authoritative_runs.c.status, authoritative_runs.c.finished_at)
