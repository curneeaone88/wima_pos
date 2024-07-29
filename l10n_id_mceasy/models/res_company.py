from odoo import models, fields


class ResCompany(models.Model):
    _inherit = 'res.company'

    account_downpayment_id = fields.Many2one('account.account', string='Down payment Account', check_company=True)
    account_security_deposit_id = fields.Many2one('account.account', string='Security Deposit Account', check_company=True)