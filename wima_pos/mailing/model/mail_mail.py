import logging
import psycopg2
import smtplib
from odoo import fields, models, tools, _
from odoo.addons.base.models.ir_mail_server import MailDeliveryException
_logger = logging.getLogger(__name__)

class MailMail(models.Model):
    _inherit = ['mail.mail']

    email_bcc = fields.Char('Bcc', help='Blind Carbon copy message recipients')

    def _prepare_outgoing_list(self, recipients_follower_status=None):
        results = super(MailMail, self)._prepare_outgoing_list(recipients_follower_status)
        for r in results:
            if self.email_bcc:
                r.update({'email_bcc': tools.email_split(self.email_bcc)})
            else:
                r.update({'email_bcc': []})
    
        return results
    
    def _send(self, auto_commit=False, raise_exception=False, smtp_session=None, alias_domain_id=False):
        IrMailServer = self.env['ir.mail_server']
        # Only retrieve recipient followers of the mails if needed
        mails_with_unfollow_link = self.filtered(lambda m: m.body_html and '/mail/unfollow' in m.body_html)
        recipients_follower_status = (
            None if not mails_with_unfollow_link
            else self.env['mail.followers']._get_mail_recipients_follower_status(mails_with_unfollow_link.ids)
        )

        for mail_id in self.ids:
            success_pids = []
            failure_reason = None
            failure_type = None
            processing_pid = None
            mail = None
            try:
                mail = self.browse(mail_id)
                if mail.state != 'outgoing':
                    continue

                # Writing on the mail object may fail (e.g. lock on user) which
                # would trigger a rollback *after* actually sending the email.
                # To avoid sending twice the same email, provoke the failure earlier
                mail.write({
                    'state': 'exception',
                    'failure_reason': _('Error without exception. Probably due to sending an email without computed recipients.'),
                })
                # Update notification in a transient exception state to avoid concurrent
                # update in case an email bounces while sending all emails related to current
                # mail record.
                notifs = self.env['mail.notification'].search([
                    ('notification_type', '=', 'email'),
                    ('mail_mail_id', 'in', mail.ids),
                    ('notification_status', 'not in', ('sent', 'canceled'))
                ])
                if notifs:
                    notif_msg = _('Error without exception. Probably due to concurrent access update of notification records. Please see with an administrator.')
                    notifs.sudo().write({
                        'notification_status': 'exception',
                        'failure_type': 'unknown',
                        'failure_reason': notif_msg,
                    })
                    # `test_mail_bounce_during_send`, force immediate update to obtain the lock.
                    # see rev. 56596e5240ef920df14d99087451ce6f06ac6d36
                    notifs.flush_recordset(['notification_status', 'failure_type', 'failure_reason'])

                # protect against ill-formatted email_from when formataddr was used on an already formatted email
                emails_from = tools.email_split_and_format(mail.email_from)
                email_from = emails_from[0] if emails_from else mail.email_from

                # build an RFC2822 email.message.Message object and send it without queuing
                res = None
                # TDE note: could be great to pre-detect missing to/cc and skip sending it
                # to go directly to failed state update
                email_list = mail._prepare_outgoing_list(recipients_follower_status)

                # send each sub-email
                for email in email_list:
                    # if given, contextualize sending using alias domains
                    if alias_domain_id:
                        alias_domain = self.env['mail.alias.domain'].sudo().browse(alias_domain_id)
                        SendIrMailServer = IrMailServer.with_context(
                            domain_notifications_email=alias_domain.default_from_email,
                            domain_bounce_address=email['headers'].get('Return-Path') or alias_domain.bounce_email,
                        )
                    else:
                        SendIrMailServer = IrMailServer
                    msg = SendIrMailServer.build_email(
                        email_from=email_from,
                        email_to=email['email_to'],
                        subject=email['subject'],
                        body=email['body'],
                        body_alternative=email['body_alternative'],
                        email_cc=email['email_cc'],
                        email_bcc=email['email_bcc'],
                        reply_to=email['reply_to'],
                        attachments=email['attachments'],
                        message_id=email['message_id'],
                        references=email['references'],
                        object_id=email['object_id'],
                        subtype='html',
                        subtype_alternative='plain',
                        headers=email['headers'],
                    )
                    processing_pid = email.pop("partner_id", None)
                    try:
                        res = SendIrMailServer.send_email(
                            msg, mail_server_id=mail.mail_server_id.id, smtp_session=smtp_session)
                        if processing_pid:
                            success_pids.append(processing_pid)
                        processing_pid = None
                    except AssertionError as error:
                        if str(error) == IrMailServer.NO_VALID_RECIPIENT:
                            # if we have a list of void emails for email_list -> email missing, otherwise generic email failure
                            if not email.get('email_to') and failure_type != "mail_email_invalid":
                                failure_type = "mail_email_missing"
                            else:
                                failure_type = "mail_email_invalid"
                            # No valid recipient found for this particular
                            # mail item -> ignore error to avoid blocking
                            # delivery to next recipients, if any. If this is
                            # the only recipient, the mail will show as failed.
                            _logger.info("Ignoring invalid recipients for mail.mail %s: %s",
                                         mail.message_id, email.get('email_to'))
                        else:
                            raise
                if res:  # mail has been sent at least once, no major exception occurred
                    mail.write({'state': 'sent', 'message_id': res, 'failure_reason': False})
                    _logger.info('Mail with ID %r and Message-Id %r successfully sent', mail.id, mail.message_id)
                    # /!\ can't use mail.state here, as mail.refresh() will cause an error
                    # see revid:odo@openerp.com-20120622152536-42b2s28lvdv3odyr in 6.1
                mail._postprocess_sent_message(success_pids=success_pids, failure_type=failure_type)
            except MemoryError:
                # prevent catching transient MemoryErrors, bubble up to notify user or abort cron job
                # instead of marking the mail as failed
                _logger.exception(
                    'MemoryError while processing mail with ID %r and Msg-Id %r. Consider raising the --limit-memory-hard startup option',
                    mail.id, mail.message_id)
                # mail status will stay on ongoing since transaction will be rollback
                raise
            except (psycopg2.Error, smtplib.SMTPServerDisconnected):
                # If an error with the database or SMTP session occurs, chances are that the cursor
                # or SMTP session are unusable, causing further errors when trying to save the state.
                _logger.exception(
                    'Exception while processing mail with ID %r and Msg-Id %r.',
                    mail.id, mail.message_id)
                raise
            except Exception as e:
                if isinstance(e, AssertionError):
                    # Handle assert raised in IrMailServer to try to catch notably from-specific errors.
                    # Note that assert may raise several args, a generic error string then a specific
                    # message for logging in failure type
                    error_code = e.args[0]
                    if len(e.args) > 1 and error_code == IrMailServer.NO_VALID_FROM:
                        # log failing email in additional arguments message
                        failure_reason = tools.ustr(e.args[1])
                    else:
                        failure_reason = error_code
                    if error_code == IrMailServer.NO_VALID_FROM:
                        failure_type = "mail_from_invalid"
                    elif error_code in (IrMailServer.NO_FOUND_FROM, IrMailServer.NO_FOUND_SMTP_FROM):
                        failure_type = "mail_from_missing"
                # generic (unknown) error as fallback
                if not failure_reason:
                    failure_reason = tools.ustr(e)
                if not failure_type:
                    failure_type = "unknown"

                _logger.exception('failed sending mail (id: %s) due to %s', mail.id, failure_reason)
                mail.write({
                    "failure_reason": failure_reason,
                    "failure_type": failure_type,
                    "state": "exception",
                })
                mail._postprocess_sent_message(
                    success_pids=success_pids,
                    failure_reason=failure_reason, failure_type=failure_type
                )
                if raise_exception:
                    if isinstance(e, (AssertionError, UnicodeEncodeError)):
                        if isinstance(e, UnicodeEncodeError):
                            value = "Invalid text: %s" % e.object
                        else:
                            value = '. '.join(e.args)
                        raise MailDeliveryException(value)
                    raise

            if auto_commit is True:
                self._cr.commit()
        return True