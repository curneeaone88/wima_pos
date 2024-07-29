from .menu import models
# from .partner import models
# from .inventory import models
# from .inventory import wizard
# from .sales import models
from .accounting import models
from .accounting import wizard
from .accounting import controllers
# from .purchasing import models
# from .purchasing import wizard
from .efaktur import models
from .common import models
from .common import wizard
from .mailing import model
from .mailing import wizard

import logging

_logger = logging.getLogger(__name__)

def _pre_init_hook(env):
    _delete_state(env)

def _post_init_hook(env):
    sale_order_seq = env['ir.sequence'].search([
        ('code', '=', 'sale.order'),
        ('prefix', '=', 'S'),
        ('active', '=', True)
    ])
    sale_order_seq.write({
        'active': False
    })

    # default setting
    env['res.config.settings'].create({
        # 'group_stock_production_lot': True,
        # 'group_proforma_sales': True,
        'group_analytic_accounting': True,
        # 'group_stock_multi_locations': True,
        # 'group_stock_adv_location': True,
        # 'group_stock_tracking_owner': True,
        # 'group_show_purchase_receipts': True,
        # 'predict_bill_product': True,
        # 'group_uom': True,
        # 'portal_confirmation_pay': True,
        # 'auth_signup_uninvited': 'b2b',
    }).execute()

    _accounting_post_init(env)
    # _warehouse_post_init(env)
    _set_localization(env)
    env.cr.commit()


def _accounting_post_init(env):
    country_code = env.company.country_id.code
    if country_code:
        module_list = ['l10n_id_mceasy', 'product_margin']

        # SEPA zone countries will be using SEPA
        sepa_zone = env.ref('base.sepa_zone', raise_if_not_found=False)
        sepa_zone_country_codes = sepa_zone and sepa_zone.mapped('country_ids.code') or []

        if country_code in sepa_zone_country_codes:
            module_list.append('account_sepa')
            module_list.append('account_bank_statement_import_camt')
        if country_code in ('AU', 'CA', 'US'):
            module_list.append('account_reports_cash_basis')
        # The customer statement is customary in Australia and New Zealand.
        if country_code in ('AU', 'NZ'):
            module_list.append('l10n_account_customer_statements')

        module_ids = env['ir.module.module'].search([('name', 'in', module_list), ('state', '=', 'uninstalled')])
        if module_ids:
            module_ids.sudo().button_install()

    for company in env['res.company'].search([('chart_template', '!=', False)]):
        ChartTemplate = env['account.chart.template'].with_company(company)
        ChartTemplate._load_data({
            'res.company': ChartTemplate._get_account_accountant_res_company(company.chart_template),
        })
        company.write({
            'account_tax_periodicity_journal_id': env.ref('account.1_general'),
            'revenue_realization_journal_id': env.ref('wima_pos.rlz'),
            'reverse_entry_journal_id': env.ref('wima_pos.re'),
        })

# def _warehouse_post_init(env):
#     routes = env['stock.route'].search([
#         ('name', 'ilike', '1 step')
#     ])
#     for route in routes:
#         if 'Receive' in route.name:
#             route.write({
#                 'name': route.name.replace('in 1 step (stock)', '')
#             })
#         if 'Deliver' in route.name:
#             route.write({
#                 'name': route.name.replace('in 1 step (ship)', ''),
#                 'work_order_applicable': 'delivery',
#             })

def _set_localization(env):
    lang = env['res.lang'].search([
        ('iso_code', '=', 'en'),
        ('url_code', '=', 'en'),
    ])
    lang.write({
        'date_format': '%Y-%m-%d'
    })
    ## Branding
    # env.company.write({
    #     'name': '',
    #     'phone': '0811316689',
    #     'email': 'sales@mceasy.co.id',
    #     'website': 'https://www.mceasy.com',
    #     'company_details': 'Jalan Pemuda No.60-70 Sinarmas Land Plaza Lt. 8 Unit 801-803, Kecamatan Genteng 60271 - Surabaya Jawa Timur (ID) Indonesia',
    # })

def _delete_state(env):
    id_indonesia = env['res.country'].sudo().search([('code','=','ID')])
    if not id_indonesia:
        return
    id_indonesia.write({
        'address_format': '%(street)s\n%(street2)s\n%(city)s, %(state_name)s %(zip)s\n%(country_name)s'
    })
    env['res.country.state'].sudo().search([('country_id','=',id_indonesia.id)]).unlink()


def uninstall_hook(env):
    try:
        group_user = env.ref("account.group_account_user")
        group_user.write({
            'name': "Show Full Accounting Features",
            'implied_ids': [(3, env.ref('account.group_account_invoice').id)],
            'category_id': env.ref("base.module_category_hidden").id,
        })
        group_readonly = env.ref("account.group_account_readonly")
        group_readonly.write({
            'name': "Show Full Accounting Features - Readonly",
            'category_id': env.ref("base.module_category_hidden").id,
        })
    except ValueError as e:
        _logger.warning(e)

    try:
        group_manager = env.ref("account.group_account_manager")
        group_manager.write({'name': "Billing Manager",
                             'implied_ids': [(4, env.ref("account.group_account_invoice").id),
                                             (3, env.ref("account.group_account_readonly").id),
                                             (3, env.ref("account.group_account_user").id)]})
    except ValueError as e:
        _logger.warning(e)

    # make the account_accountant features disappear (magic)
    env.ref("account.group_account_user").write({'users': [(5, False, False)]})
    env.ref("account.group_account_readonly").write({'users': [(5, False, False)]})

    # this menu should always be there, as the module depends on account.
    # if it's not, there is something wrong with the db that should be investigated.
    invoicing_menu = env.ref("account.menu_finance")
    menus_to_move = [
        "account.menu_finance_receivables",
        "account.menu_finance_payables",
        "account.menu_finance_entries",
        "account.menu_finance_reports",
        "account.menu_finance_configuration",
        "account.menu_board_journal_1",
    ]
    for menu_xmlids in menus_to_move:
        try:
            env.ref(menu_xmlids).parent_id = invoicing_menu
        except ValueError as e:
            _logger.warning(e)
