

from odoo import api, fields, models, _

from odoo.exceptions import UserError

class AccountMoveLine(models.Model):
    _name = "account.move.line"
    _inherit = "account.move.line"

    expected_pay_date = fields.Date('Expected Date',
                                    help="Expected payment date as manually set through the customer statement"
                                         "(e.g: if you had the customer on the phone and want to remember the date he promised he would pay)")
    followup_line_id = fields.Many2one('account_followup.followup.line', 'Follow-up Level', copy=False)
    last_followup_date = fields.Date('Latest Follow-up', index=True, copy=False)  # TODO remove in master
    next_action_date = fields.Date('Next Action Date',  # TODO remove in master
                                   help="Date where the next action should be taken for a receivable item. Usually, "
                                        "automatically set when sending reminders through the customer statement.")
    invoice_origin = fields.Char(related='move_id.invoice_origin')


    @api.constrains('tax_ids', 'tax_tag_ids')
    def _check_taxes_on_closing_entries(self):
        for aml in self:
            if aml.move_id.tax_closing_end_date and (aml.tax_ids or aml.tax_tag_ids):
                raise UserError(_("You cannot add taxes on a tax closing move line."))
    
    @api.constrains('tax_ids')
    def _check_auto_transfer_line_ids_tax(self):
        if any(line.move_id.transfer_model_id and line.tax_ids for line in self):
            raise UserError(_("You cannot set Tax on Automatic Transfer's entries."))
