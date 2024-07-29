from odoo import fields, models
from copy import copy

class MailComposeMessage(models.TransientModel):
    _inherit = 'mail.compose.message'

    email_bcc = fields.Char('BCC')

    def _prepare_mail_values(self, res_ids):
        lists = super(MailComposeMessage, self)._prepare_mail_values(res_ids)
        # use only for allowed models in mass mailing
        if (self.composition_mode != 'mass_mail' or
            not self.mass_mailing_id or
            not self.model_is_thread):
            return lists
        
        if len(self.mailing_list_ids) == 0:
            return lists

        for res_id, mail_values in lists.items():
            additional_attachments = self.mailing_list_ids.subscription_ids.filtered(
                lambda x: x.contact_id.id == res_id
            ).additional_attachments
            mail_values.update({
                'attachment_ids': mail_values['attachment_ids'] + [(4, attachment.id) for attachment in additional_attachments]
            })
            if self.mass_mailing_id and self.mass_mailing_id.bcc:
                mail_values.update({'email_bcc': self.mass_mailing_id.bcc})

        return lists