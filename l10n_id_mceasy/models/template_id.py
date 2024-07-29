# Part of Odoo. See LICENSE file for full copyright and licensing details.
from odoo import models
from odoo.addons.account.models.chart_template import template


class AccountChartTemplate(models.AbstractModel):
    _inherit = 'account.chart.template'

    @template('id')
    def _get_id_template_data(self):
        return {
            'property_account_receivable_id': 'l10n_id_mceasy_1210',
            'property_account_payable_id': 'l10n_id_mceasy_2100',
            'property_account_expense_categ_id': 'l10n_id_mceasy_5100',
            'property_account_income_categ_id': 'l10n_id_mceasy_4100',
            'property_stock_account_input_categ_id': 'l10n_id_mceasy_1499',
            'property_stock_account_output_categ_id': 'l10n_id_mceasy_1499',
            'property_stock_valuation_account_id': 'l10n_id_mceasy_1401',
            'use_anglo_saxon': 1,
            'code_digits': '4',
        }

    @template('id', 'res.company')
    def _get_id_res_company(self):
        return {
            self.env.company.id: {
                'account_fiscal_country_id': 'base.id',
                'bank_account_code_prefix': '1112',
                'cash_account_code_prefix': '1111',
                'transfer_account_code_prefix': '1999999',
                'account_downpayment_id': 'l10n_id_mceasy_2600',
                'account_security_deposit_id': 'l10n_id_mceasy_2300',
                'income_currency_exchange_account_id': 'l10n_id_mceasy_7500',
                'expense_currency_exchange_account_id': 'l10n_id_mceasy_7600',
                'account_sale_tax_id': 'tax_ST1',
                'account_purchase_tax_id': 'tax_PT1',
            },
        }
