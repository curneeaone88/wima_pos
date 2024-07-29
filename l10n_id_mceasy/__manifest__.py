{
    'name': 'McEasy Indonesian - Accounting',
    'icon': '/account/static/description/l10n.png',
    'countries': ['id'],
    'version': '1.0',
    'category': 'Accounting/Localizations/Account Charts',
    'summary' : 'McEasy Chart of Account',
    'description': """
This is Mceasy Chart of Account""",
    'author': 'Wahyu Ade Sasongko',
    'website': 'https://www.odoo.com/documentation/17.0/applications/finance/fiscal_localizations/indonesia.html',
    'depends': [
        'account',
        'base_iban',
        'base_vat'
    ],
    'data': [
        'data/account_tax_template_data.xml',
    ],
    'license': 'LGPL-3',
}
