"""Client-facing quotation and proforma-invoice workbook export."""
from datetime import datetime
from decimal import Decimal, localcontext
from io import BytesIO
import re

import xlsxwriter

from rd_import.codec import decimal


def _number(value) -> float:
    parsed = decimal(value)
    return float(parsed or Decimal(0))


def _when(value) -> datetime:
    text = str(value or '')[:19]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return datetime.now()


def _text(worksheet, row, column, value, cell_format=None) -> None:
    """Write source text as text even if it starts with an Excel formula marker."""
    worksheet.write_string(row, column, str(value or ''), cell_format)


def _product(snapshot: dict) -> str:
    if snapshot.get('kind') == 'chemical':
        recipe = snapshot.get('cost_snapshot', {}).get('recipe', {})
        name = recipe.get('recipe_name') or 'Chemical product'
        version = recipe.get('version')
        return f'{name} (recipe v{version})' if version is not None else name
    return snapshot.get('product_description') or 'Treatment plant'


def quote_filename(quote: dict, version: dict) -> str:
    client = re.sub(r'[^A-Za-z0-9]+', '-', str(quote.get('client_name') or 'client')).strip('-')
    client = client[:48] or 'client'
    return f'Quotation-Q{quote["id"]}-R{version["revision"]}-{client}.xlsx'


def build_quote_workbook(quote: dict, version: dict, snapshot: dict,
                         client: dict, buyer_gstin: str = '') -> bytes:
    """Return a two-sheet .xlsx without exposing internal costs or margins."""
    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {
        'in_memory': True,
        'strings_to_formulas': False,
        'strings_to_urls': False,
    })
    workbook.set_properties({
        'title': f'Quotation Q{quote["id"]} revision {version["revision"]}',
        'subject': 'Commercial quotation and draft proforma invoice',
        'author': 'R D Industrial Corporation',
        'company': 'R D Industrial Corporation',
        'comments': 'Generated from the frozen CRM quotation revision.',
    })
    workbook.set_calc_mode('auto')

    dark = '#173735'
    green = '#93C867'
    pale = '#EAF2F0'
    border = '#B8C9C6'
    muted = '#5F716F'
    input_fill = '#FFF2CC'
    input_blue = '#0000FF'
    linked_green = '#008000'
    currency = snapshot.get('quote_currency') or 'INR'
    money_code = '₹ #,##0.00' if currency == 'INR' else 'US$ #,##0.00'

    title = workbook.add_format({'bold': True, 'font_size': 20, 'font_color': 'white',
                                 'bg_color': dark, 'align': 'center', 'valign': 'vcenter'})
    subtitle = workbook.add_format({'bold': True, 'font_size': 14, 'font_color': dark,
                                    'bg_color': green, 'align': 'center', 'valign': 'vcenter'})
    section = workbook.add_format({'bold': True, 'font_color': 'white', 'bg_color': dark,
                                   'border': 1, 'border_color': dark})
    label = workbook.add_format({'bold': True, 'font_color': muted, 'bg_color': pale,
                                 'border': 1, 'border_color': border, 'valign': 'top'})
    value = workbook.add_format({'border': 1, 'border_color': border, 'valign': 'top'})
    wrapped = workbook.add_format({'border': 1, 'border_color': border, 'valign': 'top',
                                   'text_wrap': True})
    table_head = workbook.add_format({'bold': True, 'font_color': 'white', 'bg_color': dark,
                                      'border': 1, 'border_color': dark, 'align': 'center',
                                      'valign': 'vcenter', 'text_wrap': True})
    table_text = workbook.add_format({'border': 1, 'border_color': border, 'valign': 'top',
                                      'text_wrap': True})
    center = workbook.add_format({'border': 1, 'border_color': border, 'align': 'center'})
    number = workbook.add_format({'border': 1, 'border_color': border, 'align': 'right',
                                  'num_format': '#,##0.####'})
    money = workbook.add_format({'border': 1, 'border_color': border, 'align': 'right',
                                 'num_format': money_code})
    money_bold = workbook.add_format({'bold': True, 'border': 1, 'border_color': dark,
                                      'align': 'right', 'num_format': money_code,
                                      'top': 2, 'top_color': dark})
    total_label = workbook.add_format({'bold': True, 'align': 'right', 'border': 1,
                                       'border_color': dark, 'top': 2, 'top_color': dark})
    note = workbook.add_format({'font_color': muted, 'italic': True, 'text_wrap': True,
                                'valign': 'top'})
    input_text = workbook.add_format({'font_color': input_blue, 'bg_color': input_fill,
                                      'border': 1, 'border_color': border, 'text_wrap': True})
    input_number = workbook.add_format({'font_color': input_blue, 'bg_color': input_fill,
                                        'border': 1, 'border_color': border, 'align': 'right',
                                        'num_format': '0.00'})
    input_money = workbook.add_format({'font_color': input_blue, 'bg_color': input_fill,
                                       'border': 1, 'border_color': border, 'align': 'right',
                                       'num_format': money_code})
    linked_text = workbook.add_format({'font_color': linked_green, 'border': 1,
                                       'border_color': border, 'text_wrap': True})
    linked_number = workbook.add_format({'font_color': linked_green, 'border': 1,
                                         'border_color': border, 'align': 'right',
                                         'num_format': '#,##0.####'})
    linked_money = workbook.add_format({'font_color': linked_green, 'border': 1,
                                        'border_color': border, 'align': 'right',
                                        'num_format': money_code})
    date_format = workbook.add_format({'border': 1, 'border_color': border,
                                       'num_format': 'dd-mmm-yyyy'})
    input_date = workbook.add_format({'font_color': input_blue, 'bg_color': input_fill,
                                      'border': 1, 'border_color': border,
                                      'num_format': 'dd-mmm-yyyy'})

    quantity = decimal(snapshot.get('quantity'))
    selling_price = decimal(snapshot.get('selling_price'))
    if quantity is None or quantity <= 0 or selling_price is None or selling_price <= 0:
        workbook.close()
        raise ValueError('A resolved positive quantity and selling price are required for Excel export.')
    with localcontext() as context:
        context.prec = 80
        unit_price = selling_price / quantity
    product = _product(snapshot)
    quote_ref = f'Q-{quote["id"]:05d}-R{version["revision"]}'
    issue_date = _when(version.get('updated_at') or version.get('created_at'))
    contact = client.get('primary_contact') or {}
    address = client.get('address') or ', '.join(filter(None, [
        client.get('location'), client.get('city'), client.get('state'), client.get('country')]))
    contact_line = ' | '.join(filter(None, [contact.get('name'), contact.get('phone'),
                                             contact.get('whatsapp'), contact.get('email')]))

    quotation = workbook.add_worksheet('Quotation')
    quotation.hide_gridlines(2)
    quotation.freeze_panes(13, 0)
    quotation.set_portrait()
    quotation.fit_to_pages(1, 1)
    quotation.set_margins(0.35, 0.35, 0.45, 0.45)
    quotation.print_area('A1:F27')
    quotation.set_column('A:A', 7)
    quotation.set_column('B:B', 42)
    quotation.set_column('C:C', 14)
    quotation.set_column('D:D', 12)
    quotation.set_column('E:F', 18)
    quotation.set_row(0, 28)
    quotation.set_row(1, 28)
    quotation.merge_range('A1:F2', 'R D INDUSTRIAL CORPORATION', title)
    quotation.merge_range('A3:F3', 'COMMERCIAL QUOTATION', subtitle)
    quotation.write('A5', 'Quotation No.', label)
    _text(quotation, 4, 1, quote_ref, value)
    quotation.write('D5', 'Issue date', label)
    quotation.merge_range('E5:F5', issue_date, date_format)
    quotation.write('A6', 'Status', label)
    _text(quotation, 5, 1, version.get('status'), value)
    quotation.write('D6', 'Currency', label)
    quotation.merge_range('E6:F6', currency, value)
    quotation.merge_range('A8:F8', 'QUOTED TO', section)
    quotation.write('A9', 'Client', label)
    quotation.merge_range('B9:F9', quote.get('client_name'), value)
    quotation.write('A10', 'Address', label)
    quotation.merge_range('B10:F10', address, wrapped)
    quotation.write('A11', 'GSTIN', label)
    quotation.merge_range('B11:C11', buyer_gstin, value)
    quotation.write('D11', 'Contact', label)
    quotation.merge_range('E11:F11', contact_line, wrapped)
    quotation.set_row(9, 34)
    headers = ['S.No.', 'Description', 'HSN / SAC', 'Quantity', f'Unit price ({currency})',
               f'Amount ({currency})']
    for column, heading in enumerate(headers):
        quotation.write(12, column, heading, table_head)
    quotation.write_number('A14', 1, center)
    _text(quotation, 13, 1, product, table_text)
    _text(quotation, 13, 2, '', table_text)
    quotation.write_number('D14', _number(quantity), number)
    quotation.write_number('E14', _number(unit_price), money)
    quotation.write_formula('F14', '=ROUND(D14*E14,2)', money, _number(selling_price))
    quotation.set_row(13, 34)
    quotation.merge_range('A16:E16', 'Quoted total', total_label)
    quotation.write_formula('F16', '=SUM(F14:F14)', money_bold, _number(selling_price))
    quotation.merge_range('A18:F18', 'COMMERCIAL TERMS', section)
    terms = [('Payment terms', snapshot.get('payment_terms')),
             ('Delivery terms', snapshot.get('delivery_terms')),
             ('Destination', snapshot.get('destination')),
             ('Valid through', snapshot.get('validity_date'))]
    for row, (heading, content) in enumerate(terms, start=18):
        quotation.write(row, 0, heading, label)
        quotation.merge_range(row, 1, row, 5, str(content or ''), wrapped)
    quotation.merge_range('A24:F25',
        'Taxes, statutory levies and charges not explicitly stated above are extra as applicable. '
        'This workbook intentionally excludes internal recipe costs and gross-margin calculations.', note)
    quotation.merge_range('A27:F27', 'For R D Industrial Corporation', section)
    quotation.set_footer('&LGenerated from CRM quotation ' + quote_ref + '&RPage &P of &N')

    pi = workbook.add_worksheet('Proforma Invoice')
    pi.hide_gridlines(2)
    pi.freeze_panes(16, 0)
    pi.set_portrait()
    pi.fit_to_pages(1, 1)
    pi.set_margins(0.35, 0.35, 0.45, 0.45)
    pi.print_area('A1:F34')
    pi.set_column('A:A', 7)
    pi.set_column('B:B', 42)
    pi.set_column('C:C', 14)
    pi.set_column('D:D', 12)
    pi.set_column('E:F', 18)
    pi.set_row(0, 28)
    pi.set_row(1, 28)
    pi.merge_range('A1:F2', 'R D INDUSTRIAL CORPORATION', title)
    pi.merge_range('A3:F3', 'PROFORMA INVOICE — DRAFT', subtitle)
    pi.write('A5', 'Seller address', label)
    pi.merge_range('B5:F5', '', input_text)
    pi.write('A6', 'Seller GSTIN', label)
    pi.merge_range('B6:C6', '', input_text)
    pi.write('D6', 'PI number', label)
    pi.merge_range('E6:F6', f'PI-DRAFT-{quote_ref}', input_text)
    pi.write('A7', 'PI date', label)
    pi.write_datetime('B7', issue_date, input_date)
    pi.write('D7', 'Currency', label)
    pi.merge_range('E7:F7', currency, value)
    pi.merge_range('A9:F9', 'BILL TO / SHIP TO', section)
    pi.write('A10', 'Client', label)
    pi.merge_range('B10:F10', quote.get('client_name'), value)
    pi.write('A11', 'Address', label)
    pi.merge_range('B11:F11', address, wrapped)
    pi.write('A12', 'GSTIN', label)
    pi.merge_range('B12:C12', buyer_gstin, value if buyer_gstin else input_text)
    pi.write('D12', 'Place of supply', label)
    pi.merge_range('E12:F12', client.get('state') or '', input_text)
    pi.write('A13', 'Contact', label)
    pi.merge_range('B13:F13', contact_line, wrapped)
    for column, heading in enumerate(headers):
        pi.write(14, column, heading, table_head)
    pi.write_number('A16', 1, center)
    pi.write_formula('B16', "='Quotation'!B14", linked_text, product)
    _text(pi, 15, 2, '', input_text)
    pi.write_formula('D16', "='Quotation'!D14", linked_number, _number(quantity))
    pi.write_formula('E16', "='Quotation'!E14", linked_money, _number(unit_price))
    pi.write_formula('F16', '=ROUND(D16*E16,2)', money, _number(selling_price))
    pi.set_row(15, 34)
    pi.merge_range('A18:E18', 'Taxable value', total_label)
    pi.write_formula('F18', '=SUM(F16:F16)', money_bold, _number(selling_price))
    tax_rows = [('CGST %', 19), ('SGST %', 20), ('IGST %', 21)]
    for heading, row in tax_rows:
        pi.merge_range(row - 1, 0, row - 1, 3, '', value)
        pi.write(row - 1, 4, heading, label)
        pi.write_number(row - 1, 5, 0, input_number)
    pi.merge_range('A22:D22', '', value)
    pi.write('E22', 'Freight / other', label)
    pi.write_number('F22', 0, input_money)
    pi.merge_range('A23:D23', '', value)
    pi.write('E23', 'Tax amount', label)
    pi.write_formula('F23', '=ROUND(F18*(F19+F20+F21)/100,2)', money,
                     0)
    pi.merge_range('A24:E24', 'Grand total', total_label)
    pi.write_formula('F24', '=ROUND(F18+F22+F23,2)', money_bold, _number(selling_price))
    pi.merge_range('A26:F28',
        'Draft PI template generated from the quotation. Complete all yellow cells and confirm '
        'GST rates, place of supply, HSN/SAC, seller details and optional charges before issue. '
        'Green text is linked to the quotation sheet.', note)
    pi.merge_range('A30:F30', 'BANK / PAYMENT DETAILS', section)
    pi.write('A31', 'Bank details', label)
    pi.merge_range('B31:F32', '', input_text)
    pi.merge_range('A34:F34', 'Authorised signatory — R D Industrial Corporation', section)
    pi.set_footer('&LProforma invoice draft from ' + quote_ref + '&RPage &P of &N')

    workbook.close()
    output.seek(0)
    return output.getvalue()
