"""Business succession confirmed by the user; API company codes remain distinct.

No transition date or cross-company ItemId/PartyId correspondence was supplied.
The group code is a local reporting label, never a replacement API company code.
"""

SOURCE_COMPANIES = (
    dict(code='RDIC', business_group='RD', legal_form='proprietorship',
         predecessor_code=None, successor_code='RD', transition_date=None,
         confirmation_source='user'),
    dict(code='RD', business_group='RD', legal_form='partnership',
         predecessor_code='RDIC', successor_code=None, transition_date=None,
         confirmation_source='user'),
)

REFERENCE_STATUSES = (
    'unresolved_source_reference', 'unresolved_cross_company',
    'related_company_unresolved_reference',
)


def validate_company_code(company: str) -> str:
    """Keep source codes literal; reject malformed configuration before any fetch."""
    if not isinstance(company, str) or not company.strip() or company != company.strip():
        raise ValueError('Source company codes must be nonempty strings without surrounding whitespace')
    return company


def default_company(config: dict, endpoint: str) -> str:
    """Backward-compatible single-company setting used by the working tester."""
    return validate_company_code(config.get('endpoint_companies', {}).get(endpoint)
        or config['master_company' if endpoint.endswith('-master') else 'transaction_company'])


def companies_for(config: dict, endpoint: str) -> tuple[str, ...]:
    """Return ordered, explicit company scopes for an endpoint.

    Multi-company imports are opt-in per endpoint. Legal succession never implies
    code substitution or an automatic fetch from another company.
    """
    scopes = config.get('endpoint_company_scopes', {})
    if not isinstance(scopes, dict):
        raise ValueError('endpoint_company_scopes must be an object')
    if endpoint not in scopes:
        return (default_company(config, endpoint),)
    values = scopes[endpoint]
    if not isinstance(values, list) or not values:
        raise ValueError('Each endpoint_company_scopes entry must be a nonempty array')
    codes = tuple(validate_company_code(value) for value in values)
    if len(codes) != len(set(codes)):
        raise ValueError('Duplicate source company in endpoint_company_scopes')
    return codes


def reference_status(company, config):
    """Describe reference resolution without linking individual master records."""
    # A configured same-company scope removes the need for a cross-company
    # reference for that master type. Individual record links remain unverified.
    different = {code for endpoint in ('product-master', 'party-master')
                 if company not in companies_for(config, endpoint)
                 for code in companies_for(config, endpoint)}
    if not different:
        return 'unresolved_source_reference'
    groups = {row['code']: row['business_group'] for row in SOURCE_COMPANIES}
    if company in groups and all(groups.get(code) == groups[company] for code in different):
        return 'related_company_unresolved_reference'
    return 'unresolved_cross_company'
