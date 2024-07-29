from odoo import fields, models


class MailingSubscription(models.Model):
    _inherit = 'mailing.subscription'

    additional_attachments = fields.Many2many(
        comodel_name='ir.attachment'
    )
