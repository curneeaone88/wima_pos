from odoo import models, fields, _
from odoo.exceptions import UserError
import threading


class MassMailing(models.Model):
    _inherit = "mailing.mailing"

    bcc = fields.Char(
        string='BCC',
        help='BCC Address')
    
    def action_send_mail(self, res_ids=None):
        author_id = self.env.user.partner_id.id

        for mailing in self:
            context_user = mailing.user_id or mailing.write_uid or self.env.user
            mailing = mailing.with_context(
                **self.env['res.users'].with_user(context_user).context_get()
            )
            mailing_res_ids = res_ids or mailing._get_remaining_recipients()
            if not mailing_res_ids:
                raise UserError(_('There are no recipients selected.'))

            composer_values = {
                'auto_delete': not mailing.keep_archives,
                # email-mode: keep original message for routing
                'auto_delete_keep_log': mailing.reply_to_mode == 'update',
                'author_id': author_id,
                'attachment_ids': [(4, attachment.id) for attachment in mailing.attachment_ids],
                'body': mailing._prepend_preview(mailing.body_html, mailing.preview),
                'composition_mode': 'mass_mail',
                'email_from': mailing.email_from,
                'mail_server_id': mailing.mail_server_id.id,
                'mailing_list_ids': [(4, l.id) for l in mailing.contact_list_ids],
                'mass_mailing_id': mailing.id,
                'model': mailing.mailing_model_real,
                'record_name': False,
                'reply_to_force_new': mailing.reply_to_mode == 'new',
                'subject': mailing.subject,
                'template_id': False,
            }
            if mailing.reply_to_mode == 'new':
                composer_values['reply_to'] = mailing.reply_to
            
            if mailing.bcc:
                composer_values['email_bcc'] = mailing.bcc

            composer = self.env['mail.compose.message'].with_context(
                active_ids=mailing_res_ids,
                default_composition_mode='mass_mail',
                **mailing._get_mass_mailing_context()
            ).create(composer_values)

            # auto-commit except in testing mode
            composer._action_send_mail(
                auto_commit=not getattr(threading.current_thread(), 'testing', False)
            )
            mailing.write({
                'state': 'done',
                'sent_date': fields.Datetime.now(),
                # send the KPI mail only if it's the first sending
                'kpi_mail_required': not mailing.sent_date,
            })
        return True