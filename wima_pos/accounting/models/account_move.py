
import calendar
from contextlib import contextmanager
from dateutil.relativedelta import relativedelta
import logging
import re

from odoo import fields, models, api, _, Command
from odoo.exceptions import UserError, ValidationError
from odoo.osv import expression
from odoo.tools import frozendict, SQL, date_utils, float_compare
from odoo.tools.misc import format_date, formatLang


_logger = logging.getLogger(__name__)


DEFERRED_DATE_MIN = '1900-01-01'
DEFERRED_DATE_MAX = '9999-12-31'


class AccountMove(models.Model):
    _inherit = "account.move"

    # used for VAT closing, containing the end date of the period this entry closes
    tax_closing_end_date = fields.Date()
    tax_report_control_error = fields.Boolean() # DEPRECATED; will be removed in master
    # technical field used to know whether to show the tax closing alert or not
    tax_closing_alert = fields.Boolean(compute='_compute_tax_closing_alert')

    # Technical field to keep the value of payment_state when switching from invoicing to accounting
    # (using invoicing_switch_threshold setting field). It allows keeping the former payment state, so that
    # we can restore it if the user misconfigured the switch date and wants to change it.
    payment_state_before_switch = fields.Char(string="Payment State Before Switch", copy=False)

    # Deferred management fields
    deferred_move_ids = fields.Many2many(
        string="Deferred Entries",
        comodel_name='account.move',
        relation='account_move_deferred_rel',
        column1='original_move_id',
        column2='deferred_move_id',
        help="The deferred entries created by this invoice",
        copy=False,
    )

    deferred_original_move_ids = fields.Many2many(
        string="Original Invoices",
        comodel_name='account.move',
        relation='account_move_deferred_rel',
        column1='deferred_move_id',
        column2='original_move_id',
        help="The original invoices that created the deferred entries",
        copy=False,
    )

    deferred_entry_type = fields.Selection(
        string="Deferred Entry Type",
        selection=[
            ('expense', 'Deferred Expense'),
            ('revenue', 'Deferred Revenue'),
        ],
        compute='_compute_deferred_entry_type',
        copy=False,
    )

    asset_id = fields.Many2one('account.asset', string='Asset', index=True, ondelete='cascade', copy=False, domain="[('company_id', '=', company_id)]")
    asset_remaining_value = fields.Monetary(string='Depreciable Value', compute='_compute_depreciation_cumulative_value')
    asset_depreciated_value = fields.Monetary(string='Cumulative Depreciation', compute='_compute_depreciation_cumulative_value')
    # true when this move is the result of the changing of value of an asset
    asset_value_change = fields.Boolean()
    #  how many days of depreciation this entry corresponds to
    asset_number_days = fields.Integer(string="Number of days", copy=False)
    asset_depreciation_beginning_date = fields.Date(string="Date of the beginning of the depreciation", copy=False) # technical field stating when the depreciation associated with this entry has begun
    depreciation_value = fields.Monetary(
        string="Depreciation",
        compute="_compute_depreciation_value", inverse="_inverse_depreciation_value", store=True,
    )

    asset_ids = fields.One2many('account.asset', string='Assets', compute="_compute_asset_ids")
    asset_id_display_name = fields.Char(compute="_compute_asset_ids")   # just a button label. That's to avoid a plethora of different buttons defined in xml
    count_asset = fields.Integer(compute="_compute_asset_ids")
    draft_asset_exists = fields.Boolean(compute="_compute_asset_ids")
    transfer_model_id = fields.Many2one('account.transfer.model', string="Originating Model")

    @api.model
    def _get_invoice_in_payment_state(self):
        # OVERRIDE to enable the 'in_payment' state on invoices.
        return 'in_payment'

    def _post(self, soft=True):
        processed_moves = self.env['account.move']
        for move in self.filtered(lambda m: m.tax_closing_end_date):
            # Generate carryover values
            report, options = move._get_report_options_from_tax_closing_entry()

            company_ids = report.get_report_company_ids(options)
            if len(company_ids) >= 2:
                # For tax units, we only do the carryover for all the companies when the last of their closing moves for the period is posted.
                # If a company has no closing move for this tax_closing_date, we consider the closing hasn't been done for it.
                closing_domains = [
                    ('company_id', 'in', company_ids),
                    ('tax_closing_end_date', '=', move.tax_closing_end_date),
                    '|', ('state', '=', 'posted'), ('id', 'in', processed_moves.ids),
                ]

                if move.fiscal_position_id:
                    closing_domains.append(('fiscal_position_id.foreign_vat', '=', move.fiscal_position_id.foreign_vat))

                posted_closings_from_unit_count = self.env['account.move'].sudo().search_count(closing_domains)

                if posted_closings_from_unit_count == len(company_ids) - 1: # -1 to exclude the company of the current move
                    report.with_context(allowed_company_ids=company_ids)._generate_carryover_external_values(options)
            else:
                report._generate_carryover_external_values(options)

            processed_moves += move

            # Post the pdf of the tax report in the chatter, and set the lock date if possible
            move._close_tax_period()

        # Deferred management
        posted = super()._post(soft)
        for move in self:
            if move._get_deferred_entries_method() == 'on_validation' and any(move.line_ids.mapped('deferred_start_date')):
                move._generate_deferred_entries()
        
        # Asset Depreciation
        # log the post of a depreciation
        posted._log_depreciation_asset()

        # look for any asset to create, in case we just posted a bill on an account
        # configured to automatically create assets
        posted.sudo()._auto_create_asset()

        return posted

    def action_post(self):
        # EXTENDS 'account' to trigger the CRON auto-reconciling the statement lines.
        res = super().action_post()
        if self.statement_line_id and not self._context.get('skip_statement_line_cron_trigger'):
            self.env.ref('wima_pos.auto_reconcile_bank_statement_line')._trigger()
        return res

    def button_draft(self):
        if any(len(deferral_move.deferred_original_move_ids) > 1 for deferral_move in self.deferred_move_ids):
            raise UserError(_("You cannot reset to draft an invoice that is grouped in deferral entry. You can create a credit note instead."))

        for move in self:
            if any(asset_id.state != 'draft' for asset_id in move.asset_ids):
                raise UserError(_('You cannot reset to draft an entry related to a posted asset'))
            # Remove any draft asset that could be linked to the account move being reset to draft
            move.asset_ids.filtered(lambda x: x.state == 'draft').unlink()

        self.deferred_move_ids._unlink_or_reverse()
        super(AccountMove, self).button_draft()
        for closing_move in self.filtered(lambda m: m.tax_closing_end_date):
            report, options = closing_move._get_report_options_from_tax_closing_entry()
            closing_months_delay = closing_move.company_id._get_tax_periodicity_months_delay()

            carryover_values = self.env['account.report.external.value'].search([
                ('carryover_origin_report_line_id', 'in', report.line_ids.ids),
                ('date', '=', options['date']['date_to']),
            ])

            carryover_impacted_period_end = fields.Date.from_string(options['date']['date_to']) + relativedelta(months=closing_months_delay)
            tax_lock_date = closing_move.company_id.tax_lock_date
            if carryover_values and tax_lock_date and tax_lock_date >= carryover_impacted_period_end:
                raise UserError(_("You cannot reset this closing entry to draft, as it would delete carryover values impacting the tax report of a "
                                  "locked period. To do this, you first need to modify you tax return lock date."))

            carryover_values.unlink()

    def button_cancel(self):
        # OVERRIDE
        res = super(AccountMove, self).button_cancel()
        self.env['account.asset'].sudo().search([('original_move_line_ids.move_id', 'in', self.ids)]).write({'active': False})
        return res

    # ============================= START - Deferred Management ====================================

    def _get_deferred_entries_method(self):
        self.ensure_one()
        if self.is_outbound():
            return self.company_id.generate_deferred_expense_entries_method
        return self.company_id.generate_deferred_revenue_entries_method

    @api.depends('deferred_original_move_ids')
    def _compute_deferred_entry_type(self):
        for move in self:
            if move.deferred_original_move_ids:
                move.deferred_entry_type = 'expense' if move.deferred_original_move_ids[0].is_outbound() else 'revenue'
            else:
                move.deferred_entry_type = False

    @api.model
    def _get_deferred_diff_dates(self, start, end):
        """
        Returns the number of months between two dates [start, end[
        The computation is done by using months of 30 days so that the deferred amount for february
        (28-29 days), march (31 days) and april (30 days) are all the same (in case of monthly computation).
        See test_deferred_management_get_diff_dates for examples.
        """
        if start > end:
            start, end = end, start
        nb_months = end.month - start.month + 12 * (end.year - start.year)
        start_day, end_day = start.day, end.day
        if start_day == calendar.monthrange(start.year, start.month)[1]:
            start_day = 30
        if end_day == calendar.monthrange(end.year, end.month)[1]:
            end_day = 30
        nb_days = end_day - start_day
        return (nb_months * 30 + nb_days) / 30

    @api.model
    def _get_deferred_period_amount(self, method, period_start, period_end, line_start, line_end, balance):
        """
        Returns the amount to defer for the given period taking into account the deferred method (day/month).
        """
        if method == 'day':
            amount_per_day = balance / (line_end - line_start).days
            return (period_end - period_start).days * amount_per_day if period_end > line_start else 0
        else:
            amount_per_month = balance / self._get_deferred_diff_dates(line_end, line_start)
            nb_months_period = self._get_deferred_diff_dates(period_end, period_start)
            return nb_months_period * amount_per_month if period_end > line_start and period_end > period_start else 0

    @api.model
    def _get_deferred_amounts_by_line(self, lines, periods):
        """
        :return: a list of dictionaries containing the deferred amounts for each line and each period
        E.g. (where period1 = (date1, date2), period2 = (date2, date3), ...)
        [
            {'account_id': 1, period_1: 100, period_2: 200},
            {'account_id': 1, period_1: 100, period_2: 200},
            {'account_id': 2, period_1: 300, period_2: 400},
        ]
        """
        values = []
        for line in lines:
            line_start = fields.Date.to_date(line['deferred_start_date'])
            line_end = fields.Date.to_date(line['deferred_end_date'])
            later_date = fields.Date.to_date(DEFERRED_DATE_MAX)
            if line_end < line_start:
                # This normally shouldn't happen, but if it does, would cause calculation errors later on.
                # To not make the reports crash, we just set both dates to the same day.
                # The user should fix the dates manually.
                line_end = line_start

            columns = {}
            for i, period in enumerate(periods):
                # periods = [Total, Before, ..., Current, ..., Later]
                # The dates to calculate the amount for the current period
                period_start = max(period[0], line_start)
                period_end = min(period[1], line_end)
                if (
                    period[1] == later_date and period[0] < line_start
                    or len(periods) <= 1
                    or i not in (1, len(periods) - 1)
                ):
                    # We are subtracting 1 day to `period_start` because the start date should be included when:
                    # - in the 'Later' period if the deferral has not started yet (line_start, line_end)
                    # - we only have one period
                    # - not in the 'Before' or 'Later' period
                    period_start -= relativedelta(days=1)
                columns[period] = self._get_deferred_period_amount(
                    self.env.company.deferred_amount_computation_method,
                    period_start, period_end,
                    line_start - relativedelta(days=1), line_end,  # -1 because we want to include the start date
                    line['balance']
                )

            values.append({
                **self.env['account.move.line']._get_deferred_amounts_by_line_values(line),
                **columns,
            })
        return values

    @api.model
    def _get_deferred_lines(self, line, deferred_account, period, ref, force_balance=None):
        """
        :return: a list of Command objects to create the deferred lines of a single given period
        """
        deferred_amounts = self._get_deferred_amounts_by_line(line, [period])[0]
        balance = deferred_amounts[period] if force_balance is None else force_balance
        return [
            Command.create(
                self.env['account.move.line']._get_deferred_lines_values(account.id, coeff * balance, ref, line.analytic_distribution, line)
            )
            for (account, coeff) in [(deferred_amounts['account_id'], 1), (deferred_account, -1)]
        ]

    def _generate_deferred_entries(self):
        """
        Generates the deferred entries for the invoice.
        """
        self.ensure_one()
        if self.is_entry():
            raise UserError(_("You cannot generate deferred entries for a miscellaneous journal entry."))
        assert not self.deferred_move_ids, "The deferred entries have already been generated for this document."
        is_deferred_expense = self.is_purchase_document()
        deferred_account = self.company_id.deferred_expense_account_id if is_deferred_expense else self.company_id.deferred_revenue_account_id
        deferred_journal = self.company_id.deferred_journal_id
        if not deferred_journal:
            raise UserError(_("Please set the deferred journal in the accounting settings."))
        if not deferred_account:
            raise UserError(_("Please set the deferred accounts in the accounting settings."))

        for line in self.line_ids.filtered(lambda l: l.deferred_start_date and l.deferred_end_date):
            periods = line._get_deferred_periods()
            if not periods:
                continue

            ref = _("Deferral of %s", line.move_id.name or '')
            # Defer the current invoice
            move_fully_deferred = self.create({
                'move_type': 'entry',
                'deferred_original_move_ids': [Command.set(line.move_id.ids)],
                'journal_id': deferred_journal.id,
                'company_id': self.company_id.id,
                'date': line.move_id.invoice_date + relativedelta(day=31),
                'auto_post': 'at_date',
                'ref': ref,
            })
            # We write the lines after creation, to make sure the `deferred_original_move_ids` is set.
            # This way we can avoid adding taxes for deferred moves.
            move_fully_deferred.write({
                'line_ids': [
                    Command.create(
                        self.env['account.move.line']._get_deferred_lines_values(account.id, coeff * line.balance, ref, line.analytic_distribution, line)
                    ) for (account, coeff) in [(line.account_id, -1), (deferred_account, 1)]
                ],
            })
            line.move_id.deferred_move_ids |= move_fully_deferred
            move_fully_deferred._post(soft=True)

            # Create the deferred entries for the periods [deferred_start_date, deferred_end_date]
            remaining_balance = line.balance
            for period_index, period in enumerate(periods):
                deferred_move = self.create({
                    'move_type': 'entry',
                    'deferred_original_move_ids': [Command.set(line.move_id.ids)],
                    'journal_id': deferred_journal.id,
                    'company_id': self.company_id.id,
                    'date': period[1],
                    'auto_post': 'at_date',
                    'ref': ref,
                })
                # For the last deferral move the balance is forced to remaining balance to avoid rounding errors
                force_balance = remaining_balance if period_index == len(periods) - 1 else None
                # Same as before, to avoid adding taxes for deferred moves.
                deferred_move.write({
                    'line_ids': self._get_deferred_lines(line, deferred_account, period, ref, force_balance=force_balance),
                })
                remaining_balance -= deferred_move.line_ids[0].balance
                line.move_id.deferred_move_ids |= deferred_move
                deferred_move._post(soft=True)

    def open_deferred_entries(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Deferred Entries"),
            'res_model': 'account.move.line',
            'domain': [('id', 'in', self.deferred_move_ids.line_ids.ids)],
            'views': [(False, 'tree'), (False, 'form')],
            'context': {
                'search_default_group_by_move': True,
                'expand': True,
            }
        }

    def open_deferred_original_entry(self):
        self.ensure_one()
        action = {
            'type': 'ir.actions.act_window',
            'name': _("Original Deferred Entries"),
            'res_model': 'account.move.line',
            'domain': [('id', 'in', self.deferred_original_move_ids.line_ids.ids)],
            'views': [(False, 'tree'), (False, 'form')],
            'context': {
                'search_default_group_by_move': True,
                'expand': True,
            }
        }
        if len(self.deferred_original_move_ids) == 1:
            action.update({
                'res_model': 'account.move',
                'res_id': self.deferred_original_move_ids[0].id,
                'views': [(False, 'form')],
            })
        return action

    # ============================= END - Deferred management ======================================

    def action_open_bank_reconciliation_widget(self):
        return self.statement_line_id._action_open_bank_reconciliation_widget(
            default_context={
                'search_default_journal_id': self.statement_line_id.journal_id.id,
                'search_default_statement_line_id': self.statement_line_id.id,
                'default_st_line_id': self.statement_line_id.id,
            }
        )

    def action_open_bank_reconciliation_widget_statement(self):
        return self.statement_line_id._action_open_bank_reconciliation_widget(
            extra_domain=[('statement_id', 'in', self.statement_id.ids)],
        )

    def action_open_business_doc(self):
        if self.statement_line_id:
            return self.action_open_bank_reconciliation_widget()
        else:
            action = super().action_open_business_doc()
            # prevent propagation of the following keys
            action['context'] = action.get('context', {}) | {
                'preferred_aml_value': None,
                'preferred_aml_currency_id': None,
            }
            return action

    def _get_mail_thread_data_attachments(self):
        res = super()._get_mail_thread_data_attachments()
        res += self.statement_line_id.statement_id.attachment_ids
        return res

    @contextmanager
    def _get_edi_creation(self):
        with super()._get_edi_creation() as move:
            previous_lines = move.invoice_line_ids
            yield move
            for line in move.invoice_line_ids - previous_lines:
                line._onchange_name_predictive()

    def refresh_tax_entry(self):
        for move in self.filtered(lambda m: m.tax_closing_end_date and m.state == 'draft'):
            report, options = move._get_report_options_from_tax_closing_entry()
            self.env['account.generic.tax.report.handler']._generate_tax_closing_entries(report, options, closing_moves=move)

    def action_open_tax_report(self):
        action = self.env["ir.actions.actions"]._for_xml_id("wima_pos.action_account_report_gt")
        if not self.tax_closing_end_date:
            raise UserError(_("You can't open a tax report from a move without a VAT closing date."))
        options = self._get_report_options_from_tax_closing_entry()[1]
        # Pass options in context and set ignore_session: true to prevent using session options
        action.update({'params': {'options': options, 'ignore_session': True}})
        return action
    
    def _close_tax_period(self):
        """ Closes tax closing entries. The tax closing activities on them will be marked done, and the next tax closing entry
        will be generated or updated (if already existing). Also, a pdf of the tax report at the time of closing
        will be posted in the chatter of each move.

        The tax lock date of each  move's company will be set to the move's date in case no other draft tax closing
        move exists for that company (whatever their foreign VAT fiscal position) before or at that date, meaning that
        all the tax closings have been performed so far.
        """
        if not self.user_has_groups('account.group_account_manager'):
            raise UserError(_('Only Billing Administrators are allowed to change lock dates!'))

        tax_closing_activity_type = self.env.ref('wima_pos.tax_closing_activity_type')

        for move in self:
            # Change lock date to end date of the period, if all other tax closing moves before this one have been treated
            open_previous_closing = self.env['account.move'].search([
                ('activity_ids.activity_type_id', '=', tax_closing_activity_type.id),
                ('company_id', '=', move.company_id.id),
                ('date', '<=', move.date),
                ('state', '=', 'draft'),
                ('id', '!=', move.id),
            ], limit=1)

            if not open_previous_closing and (not move.company_id.tax_lock_date or move.tax_closing_end_date > move.company_id.tax_lock_date):
                move.company_id.sudo().tax_lock_date = move.tax_closing_end_date

            # Add pdf report as attachment to move
            report, options = move._get_report_options_from_tax_closing_entry()

            attachments = move._get_vat_report_attachments(report, options)

            # End activity
            activity = move.activity_ids.filtered(lambda m: m.activity_type_id.id == tax_closing_activity_type.id)
            if activity:
                activity.action_done()

            # Post the message with the PDF
            subject = _(
                "Vat closing from %s to %s",
                format_date(self.env, options['date']['date_from']),
                format_date(self.env, options['date']['date_to']),
            )
            move.with_context(no_new_invoice=True).message_post(body=move.ref, subject=subject, attachments=attachments)

            # Create the recurring entry (new draft move and new activity)
            if move.fiscal_position_id.foreign_vat:
                next_closing_params = {'fiscal_positions': move.fiscal_position_id}
            else:
                next_closing_params = {'include_domestic': True}
            move.company_id._get_and_update_tax_closing_moves(move.tax_closing_end_date + relativedelta(days=1), **next_closing_params)

    def _get_report_options_from_tax_closing_entry(self):
        self.ensure_one()
        date_to = self.tax_closing_end_date
        # Take the periodicity of tax report from the company and compute the starting period date.
        delay = self.company_id._get_tax_periodicity_months_delay() - 1
        date_from = date_utils.start_of(date_to + relativedelta(months=-delay), 'month')

        # In case the company submits its report in different regions, a closing entry
        # is made for each fiscal position defining a foreign VAT.
        # We hence need to make sure to select a tax report in the right country when opening
        # the report (in case there are many, we pick the first one available; it doesn't impact the closing)
        if self.fiscal_position_id.foreign_vat:
            fpos_option = self.fiscal_position_id.id
            report_country = self.fiscal_position_id.country_id
        else:
            fpos_option = 'domestic'
            report_country = self.company_id.account_fiscal_country_id

        generic_tax_report = self.env.ref('account.generic_tax_report')
        tax_report = self.env['account.report'].search([
            ('availability_condition', '=', 'country'),
            ('country_id', '=', report_country.id),
            ('root_report_id', '=', generic_tax_report.id),
        ], limit=1)

        if not tax_report:
            tax_report = generic_tax_report

        options = {
            'date': {
                'date_from': fields.Date.to_string(date_from),
                'date_to': fields.Date.to_string(date_to),
                'filter': 'custom',
                'mode': 'range',
            },
            'fiscal_position': fpos_option,
            'tax_unit': 'company_only',
        }

        if tax_report.country_id and tax_report.filter_multi_company == 'tax_units':
            # Enforce multicompany if the closing is done for a tax unit
            candidate_tax_unit = self.company_id.account_tax_unit_ids.filtered(lambda x: x.country_id == report_country)
            if candidate_tax_unit:
                options['tax_unit'] = candidate_tax_unit.id
                company_ids = [company.id for company in candidate_tax_unit.sudo().company_ids]
            else:
                company_ids = self.env.company.ids
        else:
            company_ids = self.env.company.ids

        report_options = tax_report.with_context(allowed_company_ids=company_ids).get_options(previous_options=options)

        return tax_report, report_options

    def _get_vat_report_attachments(self, report, options):
        # Fetch pdf
        pdf_data = report.export_to_pdf(options)
        return [(pdf_data['file_name'], pdf_data['file_content'])]
    
    def _compute_tax_closing_alert(self):
        for move in self:
            move.tax_closing_alert = (
                move.state == 'posted'
                and move.tax_closing_end_date
                and move.company_id.tax_lock_date
                and move.company_id.tax_lock_date < move.tax_closing_end_date
            )

    # -------------------------------------------------------------------------
    # COMPUTE METHODS
    # -------------------------------------------------------------------------
    @api.depends('asset_id', 'depreciation_value', 'asset_id.total_depreciable_value', 'asset_id.already_depreciated_amount_import')
    def _compute_depreciation_cumulative_value(self):
        self.asset_depreciated_value = 0
        self.asset_remaining_value = 0

        # make sure to protect all the records being assigned, because the
        # assignments invoke method write() on non-protected records, which may
        # cause an infinite recursion in case method write() needs to read one
        # of these fields (like in case of a base automation)
        fields = [type(self).asset_remaining_value, type(self).asset_depreciated_value]
        with self.env.protecting(fields, self.asset_id.depreciation_move_ids):
            for asset in self.asset_id:
                depreciated = 0
                remaining = asset.total_depreciable_value - asset.already_depreciated_amount_import
                for move in asset.depreciation_move_ids.sorted(lambda mv: (mv.date, mv._origin.id)):
                    remaining -= move.depreciation_value
                    depreciated += move.depreciation_value
                    move.asset_remaining_value = remaining
                    move.asset_depreciated_value = depreciated

    @api.depends('line_ids.balance')
    def _compute_depreciation_value(self):
        for move in self:
            asset = move.asset_id or move.reversed_entry_id.asset_id  # reversed moves are created before being assigned to the asset
            if asset:
                account_internal_group = 'expense'
                asset_depreciation = sum(
                    move.line_ids.filtered(lambda l: l.account_id.internal_group == account_internal_group or l.account_id == asset.account_depreciation_expense_id).mapped('balance')
                )
                # Special case of closing entry - only disposed assets of type 'purchase' should match this condition
                if any(
                    line.account_id == asset.account_asset_id
                    and float_compare(-line.balance, asset.original_value, precision_rounding=asset.currency_id.rounding) == 0
                    for line in move.line_ids
                ):
                    account = asset.account_depreciation_id
                    asset_depreciation = (
                        asset.original_value
                        - asset.salvage_value
                        - sum(
                            move.line_ids.filtered(lambda l: l.account_id == account).mapped(
                                'debit' if asset.original_value > 0 else 'credit'
                            )
                        ) * (-1 if asset.original_value < 0 else 1)
                    )
            else:
                asset_depreciation = 0
            move.depreciation_value = asset_depreciation

    # -------------------------------------------------------------------------
    # INVERSE METHODS
    # -------------------------------------------------------------------------
    def _inverse_depreciation_value(self):
        for move in self:
            asset = move.asset_id
            amount = abs(move.depreciation_value)
            account = asset.account_depreciation_expense_id
            move.write({'line_ids': [
                Command.update(line.id, {
                    'balance': amount if line.account_id == account else -amount,
                })
                for line in move.line_ids
            ]})

    # -------------------------------------------------------------------------
    # CONSTRAINT METHODS
    # -------------------------------------------------------------------------
    @api.constrains('state', 'asset_id')
    def _constrains_check_asset_state(self):
        for move in self.filtered(lambda mv: mv.asset_id):
            asset_id = move.asset_id
            if asset_id.state == 'draft' and move.state == 'posted':
                raise ValidationError(_("You can't post an entry related to a draft asset. Please post the asset before."))
    
    def _reverse_moves(self, default_values_list=None, cancel=False):
        if default_values_list is None:
            default_values_list = [{} for _i in self]
        for move, default_values in zip(self, default_values_list):
            # Report the value of this move to the next draft move or create a new one
            if move.asset_id:
                # Recompute the status of the asset for all depreciations posted after the reversed entry

                first_draft = min(move.asset_id.depreciation_move_ids.filtered(lambda m: m.state == 'draft'), key=lambda m: m.date, default=None)
                if first_draft:
                    # If there is a draft, simply move/add the depreciation amount here
                    first_draft.depreciation_value += move.depreciation_value
                else:
                    # If there was no draft move left, create one
                    last_date = max(move.asset_id.depreciation_move_ids.mapped('date'))
                    method_period = move.asset_id.method_period

                    self.create(self._prepare_move_for_asset_depreciation({
                        'asset_id': move.asset_id,
                        'amount': move.depreciation_value,
                        'depreciation_beginning_date': last_date + (relativedelta(months=1) if method_period == "1" else relativedelta(years=1)),
                        'date': last_date + (relativedelta(months=1) if method_period == "1" else relativedelta(years=1)),
                        'asset_number_days': 0
                    }))

                msg = _('Depreciation entry %s reversed (%s)', move.name, formatLang(self.env, move.depreciation_value, currency_obj=move.company_id.currency_id))
                move.asset_id.message_post(body=msg)
                default_values['asset_id'] = move.asset_id.id
                default_values['asset_number_days'] = -move.asset_number_days
                default_values['asset_depreciation_beginning_date'] = default_values.get('date', move.date)

        return super(AccountMove, self)._reverse_moves(default_values_list, cancel)

    def _log_depreciation_asset(self):
        for move in self.filtered(lambda m: m.asset_id):
            asset = move.asset_id
            msg = _('Depreciation entry %s posted (%s)', move.name, formatLang(self.env, move.depreciation_value, currency_obj=move.company_id.currency_id))
            asset.message_post(body=msg)
    
    def _auto_create_asset(self):
        create_list = []
        invoice_list = []
        auto_validate = []
        for move in self:
            if not move.is_invoice():
                continue

            for move_line in move.line_ids:
                if (
                    move_line.account_id
                    and (move_line.account_id.can_create_asset)
                    and move_line.account_id.create_asset != "no"
                    and not (move_line.currency_id or move.currency_id).is_zero(move_line.price_total)
                    and not move_line.asset_ids
                    and not move_line.tax_line_id
                    and move_line.price_total > 0
                    and not (move.move_type in ('out_invoice', 'out_refund') and move_line.account_id.internal_group == 'asset')
                ):
                    if not move_line.name:
                        raise UserError(_('Journal Items of %(account)s should have a label in order to generate an asset', account=move_line.account_id.display_name))
                    if move_line.account_id.multiple_assets_per_line:
                        # decimal quantities are not supported, quantities are rounded to the lower int
                        units_quantity = max(1, int(move_line.quantity))
                    else:
                        units_quantity = 1
                    vals = {
                        'name': move_line.name,
                        'company_id': move_line.company_id.id,
                        'currency_id': move_line.company_currency_id.id,
                        'analytic_distribution': move_line.analytic_distribution,
                        'original_move_line_ids': [(6, False, move_line.ids)],
                        'state': 'draft',
                        'acquisition_date': move.invoice_date if not move.reversed_entry_id else move.reversed_entry_id.invoice_date,
                    }
                    model_id = move_line.account_id.asset_model
                    if model_id:
                        vals.update({
                            'model_id': model_id.id,
                        })
                    auto_validate.extend([move_line.account_id.create_asset == 'validate'] * units_quantity)
                    invoice_list.extend([move] * units_quantity)
                    for i in range(1, units_quantity + 1):
                        if units_quantity > 1:
                            vals['name'] = move_line.name + _(" (%s of %s)", i, units_quantity)
                        create_list.extend([vals.copy()])

        assets = self.env['account.asset'].with_context({}).create(create_list)
        for asset, vals, invoice, validate in zip(assets, create_list, invoice_list, auto_validate):
            if 'model_id' in vals:
                asset._onchange_model_id()
                if validate:
                    asset.validate()
            if invoice:
                asset.message_post(body=_('Asset created from invoice: %s', invoice._get_html_link()))
                asset._post_non_deductible_tax_value()
        return assets

    @api.model
    def _prepare_move_for_asset_depreciation(self, vals):
        missing_fields = {'asset_id', 'amount', 'depreciation_beginning_date', 'date', 'asset_number_days'} - set(vals)
        if missing_fields:
            raise UserError(_('Some fields are missing %s', ', '.join(missing_fields)))
        asset = vals['asset_id']
        analytic_distribution = asset.analytic_distribution
        depreciation_date = vals.get('date', fields.Date.context_today(self))
        company_currency = asset.company_id.currency_id
        current_currency = asset.currency_id
        prec = company_currency.decimal_places
        amount_currency = vals['amount']
        amount = current_currency._convert(amount_currency, company_currency, asset.company_id, depreciation_date)
        # Keep the partner on the original invoice if there is only one
        partner = asset.original_move_line_ids.mapped('partner_id')
        partner = partner[:1] if len(partner) <= 1 else self.env['res.partner']
        move_line_1 = {
            'name': asset.name,
            'partner_id': partner.id,
            'account_id': asset.account_depreciation_id.id,
            'debit': 0.0 if float_compare(amount, 0.0, precision_digits=prec) > 0 else -amount,
            'credit': amount if float_compare(amount, 0.0, precision_digits=prec) > 0 else 0.0,
            'analytic_distribution': analytic_distribution,
            'currency_id': current_currency.id,
            'amount_currency': -amount_currency,
        }
        move_line_2 = {
            'name': asset.name,
            'partner_id': partner.id,
            'account_id': asset.account_depreciation_expense_id.id,
            'credit': 0.0 if float_compare(amount, 0.0, precision_digits=prec) > 0 else -amount,
            'debit': amount if float_compare(amount, 0.0, precision_digits=prec) > 0 else 0.0,
            'analytic_distribution': analytic_distribution,
            'currency_id': current_currency.id,
            'amount_currency': amount_currency,
        }
        move_vals = {
            'partner_id': partner.id,
            'date': depreciation_date,
            'journal_id': asset.journal_id.id,
            'line_ids': [(0, 0, move_line_1), (0, 0, move_line_2)],
            'asset_id': asset.id,
            'ref': _("%s: Depreciation", asset.name),
            'asset_depreciation_beginning_date': vals['depreciation_beginning_date'],
            'asset_number_days': vals['asset_number_days'],
            'name': '/',
            'asset_value_change': vals.get('asset_value_change', False),
            'move_type': 'entry',
            'currency_id': current_currency.id,
        }
        return move_vals

    @api.depends('line_ids.asset_ids')
    def _compute_asset_ids(self):
        for record in self:
            record.asset_ids = record.line_ids.asset_ids
            record.count_asset = len(record.asset_ids)
            record.asset_id_display_name = _('Asset')
            record.draft_asset_exists = bool(record.asset_ids.filtered(lambda x: x.state == "draft"))

    def open_asset_view(self):
        return self.asset_id.open_asset(['form'])

    def action_open_asset_ids(self):
        return self.asset_ids.open_asset(['tree', 'form'])
    

class AccountMoveLine(models.Model):
    _name = "account.move.line"
    _inherit = "account.move.line"

    move_attachment_ids = fields.One2many('ir.attachment', compute='_compute_attachment')

    # Deferred management fields
    deferred_start_date = fields.Date(
        string="Start Date",
        compute='_compute_deferred_start_date', store=True, readonly=False,
        index='btree_not_null',
        copy=False,
        help="Date at which the deferred expense/revenue starts"
    )
    deferred_end_date = fields.Date(
        string="End Date",
        index='btree_not_null',
        copy=False,
        help="Date at which the deferred expense/revenue ends"
    )
    has_deferred_moves = fields.Boolean(compute='_compute_has_deferred_moves')
    asset_ids = fields.Many2many('account.asset', 'asset_move_line_rel', 'line_id', 'asset_id', string='Related Assets', copy=False)
    non_deductible_tax_value = fields.Monetary(compute='_compute_non_deductible_tax_value', currency_field='company_currency_id')

    def _order_to_sql(self, order, query, alias=None, reverse=False):
        sql_order = super()._order_to_sql(order, query, alias, reverse)
        preferred_aml_residual_value = self._context.get('preferred_aml_value')
        preferred_aml_currency_id = self._context.get('preferred_aml_currency_id')
        if preferred_aml_residual_value and preferred_aml_currency_id and order == self._order:
            currency = self.env['res.currency'].browse(preferred_aml_currency_id)
            # using round since currency.round(55.55) = 55.550000000000004
            preferred_aml_residual_value = round(preferred_aml_residual_value, currency.decimal_places)
            sql_residual_currency = self._field_to_sql(alias or self._table, 'amount_residual_currency', query)
            sql_currency = self._field_to_sql(alias or self._table, 'currency_id', query)
            return SQL(
                "ROUND(%(residual_currency)s, %(decimal_places)s) = %(value)s "
                "AND %(currency)s = %(currency_id)s DESC, %(order)s",
                residual_currency=sql_residual_currency,
                decimal_places=currency.decimal_places,
                value=preferred_aml_residual_value,
                currency=sql_currency,
                currency_id=currency.id,
                order=sql_order,
            )
        return sql_order

    def copy_data(self, default=None):
        data_list = super().copy_data(default=default)
        for line, values in zip(self, data_list):
            if 'move_reverse_cancel' in self._context:
                values['deferred_start_date'] = line.deferred_start_date
                values['deferred_end_date'] = line.deferred_end_date
        return data_list

    def write(self, vals):
        """ Prevent changing the account of a move line when there are already deferral entries.
        """
        if 'account_id' in vals:
            for line in self:
                if (
                    line.has_deferred_moves
                    and line.deferred_start_date
                    and line.deferred_end_date
                    and vals['account_id'] != line.account_id.id
                ):
                    raise UserError(_(
                        "You cannot change the account for a deferred line in %(move_name)s if it has already been deferred.",
                        move_name=line.move_id.display_name
                    ))
        return super().write(vals)

    # ============================= START - Deferred management ====================================
    def _compute_has_deferred_moves(self):
        for line in self:
            line.has_deferred_moves = line.move_id.deferred_move_ids

    def _is_compatible_account(self):
        self.ensure_one()
        return (
            self.move_id.is_purchase_document()
            and
            self.account_id.account_type in ('expense', 'expense_depreciation', 'expense_direct_cost')
        ) or (
            self.move_id.is_sale_document()
            and
            self.account_id.account_type in ('income', 'income_other')
        )

    @api.onchange('deferred_start_date')
    def _onchange_deferred_start_date(self):
        if not self._is_compatible_account():
            self.deferred_start_date = False

    @api.onchange('deferred_end_date')
    def _onchange_deferred_end_date(self):
        if not self._is_compatible_account():
            self.deferred_end_date = False

    @api.depends('deferred_end_date', 'move_id.invoice_date')
    def _compute_deferred_start_date(self):
        for line in self:
            if not line.deferred_start_date and line.move_id.invoice_date and line.deferred_end_date:
                line.deferred_start_date = line.move_id.invoice_date

    @api.constrains('deferred_start_date', 'deferred_end_date', 'account_id')
    def _check_deferred_dates(self):
        for line in self:
            if line.deferred_start_date and not line.deferred_end_date:
                raise UserError(_("You cannot create a deferred entry with a start date but no end date."))
            elif line.deferred_start_date and line.deferred_end_date and line.deferred_start_date > line.deferred_end_date:
                raise UserError(_("You cannot create a deferred entry with a start date later than the end date."))

    @api.depends('deferred_start_date', 'deferred_end_date')
    def _compute_tax_key(self):
        super()._compute_tax_key()
        for line in self:
            if line.deferred_start_date and line.deferred_end_date and line._is_compatible_account():
                line.tax_key = frozendict(
                    **line.tax_key,
                    deferred_start_date=line.deferred_start_date,
                    deferred_end_date=line.deferred_end_date
                )

    @api.depends('deferred_start_date', 'deferred_end_date')
    def _compute_all_tax(self):
        super()._compute_all_tax()
        for line in self:
            if line.deferred_start_date and line.deferred_end_date and line._is_compatible_account():
                for key in list(line.compute_all_tax.keys()):
                    rep_line = self.env['account.tax.repartition.line'].browse(key.get('tax_repartition_line_id'))
                    deferred_start_date = line.deferred_start_date if not rep_line.use_in_tax_closing else False
                    deferred_end_date = line.deferred_end_date if not rep_line.use_in_tax_closing else False
                    new_key = frozendict(**key, deferred_start_date=deferred_start_date, deferred_end_date=deferred_end_date)
                    line.compute_all_tax[new_key] = line.compute_all_tax.pop(key)

    @api.model
    def _get_deferred_ends_of_month(self, start_date, end_date):
        """
        :return: a list of dates corresponding to the end of each month between start_date and end_date.
            See test_get_ends_of_month for examples.
        """
        dates = []
        while start_date <= end_date:
            start_date = start_date + relativedelta(day=31)  # Go to end of month
            dates.append(start_date)
            start_date = start_date + relativedelta(days=1)  # Go to first day of next month
        return dates

    def _get_deferred_periods(self):
        """
        :return: a list of tuples (start_date, end_date) during which the deferred expense/revenue is spread.
            If there is only one period containing the move date, it means that we don't need to defer the
            expense/revenue since the invoice deferral and its deferred entry will be created on the same day and will
            thus cancel each other.
        """
        self.ensure_one()
        periods = [
            (max(self.deferred_start_date, date.replace(day=1)), min(date, self.deferred_end_date))
            for date in self._get_deferred_ends_of_month(self.deferred_start_date, self.deferred_end_date)
        ]
        if not periods or len(periods) == 1 and periods[0][0].replace(day=1) == self.date.replace(day=1):
            return []
        else:
            return periods

    @api.model
    def _get_deferred_amounts_by_line_values(self, line):
        return {
            'account_id': line['account_id'],
            'balance': line['balance'],
            'move_id': line['move_id'],
        }

    @api.model
    def _get_deferred_lines_values(self, account_id, balance, ref, analytic_distribution, line=None):
        return {
            'account_id': account_id,
            'balance': balance,
            'name': ref,
            'analytic_distribution': analytic_distribution,
        }

    # ============================= END - Deferred management ====================================

    def _get_computed_taxes(self):
        if self.move_id.asset_id:
            return self.tax_ids

        if self.move_id.deferred_original_move_ids:
            # If this line is part of a deferral move, do not (re)calculate its taxes automatically.
            # Doing so might unvoluntarily impact the tax report in deferral moves (if a default tax is set on the account).
            return self.tax_ids
        return super()._get_computed_taxes()

    def _compute_attachment(self):
        for record in self:
            record.move_attachment_ids = self.env['ir.attachment'].search(expression.OR(record._get_attachment_domains()))

    def action_reconcile(self):
        """ This function is called by the 'Reconcile' button of account.move.line's
        tree view. It performs reconciliation between the selected lines.
        - If the reconciliation can be done directly we do it silently
        - Else, if a write-off is required we open the wizard to let the client enter required information
        """
        wizard = self.env['account.reconcile.wizard'].with_context(
            active_model='account.move.line',
            active_ids=self.ids,
        ).new({})
        return wizard._action_open_wizard() if wizard.is_write_off_required else wizard.reconcile()

    def _get_predict_postgres_dictionary(self):
        lang = self._context.get('lang') and self._context.get('lang')[:2]
        return {'fr': 'french'}.get(lang, 'english')

    def _build_predictive_query(self, additional_domain=None):
        move_query = self.env['account.move']._where_calc([
            ('move_type', '=', self.move_id.move_type),
            ('state', '=', 'posted'),
            ('partner_id', '=', self.move_id.partner_id.id),
            ('company_id', '=', self.move_id.journal_id.company_id.id or self.env.company.id),
        ])
        move_query.order = 'account_move.invoice_date'
        move_query.limit = int(self.env["ir.config_parameter"].sudo().get_param(
            "account.bill.predict.history.limit",
            '100',
        ))
        return self.env['account.move.line']._where_calc([
            ('move_id', 'in', move_query),
            ('display_type', '=', 'product'),
        ] + (additional_domain or []))

    def _predicted_field(self, field, query=None, additional_queries=None):
        r"""Predict the most likely value based on the previous history.

        This method uses postgres tsvector in order to try to deduce a field of
        an invoice line based on the text entered into the name (description)
        field and the partner linked.
        We only limit the search on the previous 100 entries, which according
        to our tests bore the best results. However this limit parameter is
        configurable by creating a config parameter with the key:
        account.bill.predict.history.limit

        For information, the tests were executed with a dataset of 40 000 bills
        from a live database, We split the dataset in 2, removing the 5000 most
        recent entries and we tried to use this method to guess the account of
        this validation set based on the previous entries.
        The result is roughly 90% of success.

        :param field (str): the sql column that has to be predicted.
            /!\ it is injected in the query without any checks.
        :param query (osv.Query): the query object on account.move.line that is
            used to do the ranking, containing the right domain, limit, etc. If
            it is omitted, a default query is used.
        :param additional_queries (list<str>): can be used in addition to the
            default query on account.move.line to fetch data coming from other
            tables, to have starting values for instance.
            /!\ it is injected in the query without any checks.
        """
        if not self.name or not self.partner_id:
            return False

        psql_lang = self._get_predict_postgres_dictionary()
        description = self.name + ' account_move_line' # give more priority to main query than additional queries
        parsed_description = re.sub(r"[*&()|!':<>=%/~@,.;$\[\]]+", " ", description)
        parsed_description = ' | '.join(parsed_description.split())

        from_clause, where_clause, params = (query if query is not None else self._build_predictive_query()).get_sql()
        mask_from_clause, mask_where_clause, mask_params = self._build_predictive_query().get_sql()
        try:
            account_move_line = self.env.cr.mogrify(
                f"SELECT account_move_line.* FROM {mask_from_clause} WHERE {mask_where_clause}",
                mask_params,
            ).decode()
            group_by_clause = ""
            if "(" in field:  # aggregate function
                group_by_clause = "GROUP BY account_move_line.id, account_move_line.name, account_move_line.partner_id"
            self.env.cr.execute(f"""
                WITH account_move_line AS MATERIALIZED ({account_move_line}),
                source AS ({'(' + ') UNION ALL ('.join([self.env.cr.mogrify(f'''
                    SELECT {field} AS prediction,
                           setweight(to_tsvector(%%(lang)s, account_move_line.name), 'B')
                           || setweight(to_tsvector('simple', 'account_move_line'), 'A') AS document
                      FROM {from_clause}
                     WHERE {where_clause}
                  {group_by_clause}
                ''', params).decode()] + (additional_queries or [])) + ')'}
                ),

                ranking AS (
                    SELECT prediction, ts_rank(source.document, query_plain) AS rank
                      FROM source, to_tsquery(%(lang)s, %(description)s) query_plain
                     WHERE source.document @@ query_plain
                )

                SELECT prediction, MAX(rank) AS ranking, COUNT(*)
                  FROM ranking
              GROUP BY prediction
              ORDER BY ranking DESC, count DESC
            """, {
                'lang': psql_lang,
                'description': parsed_description,
            })
            result = self.env.cr.dictfetchone()
            if result:
                return result['prediction']
        except Exception:
            # In case there is an error while parsing the to_tsquery (wrong character for example)
            # We don't want to have a blocking traceback, instead return False
            _logger.exception('Error while predicting invoice line fields')
        return False

    def _predict_taxes(self):
        field = 'array_agg(account_move_line__tax_rel__tax_ids.id ORDER BY account_move_line__tax_rel__tax_ids.id)'
        query = self._build_predictive_query()
        query.left_join('account_move_line', 'id', 'account_move_line_account_tax_rel', 'account_move_line_id', 'tax_rel')
        query.left_join('account_move_line__tax_rel', 'account_tax_id', 'account_tax', 'id', 'tax_ids')
        query.add_where('account_move_line__tax_rel__tax_ids.active IS NOT FALSE')
        return self._predicted_field(field, query)

    def _predict_product(self):
        query = self._build_predictive_query(['|', ('product_id', '=', False), ('product_id.active', '=', True)])
        return self._predicted_field('account_move_line.product_id', query)

    def _predict_account(self):
        field = 'account_move_line.account_id'
        if self.move_id.is_purchase_document(True):
            excluded_group = 'income'
        else:
            excluded_group = 'expense'
        account_query = self.env['account.account']._where_calc([
            *self.env['account.account']._check_company_domain(self.move_id.company_id or self.env.company),
            ('deprecated', '=', False),
            ('internal_group', '!=', excluded_group),
        ])
        psql_lang = self._get_predict_postgres_dictionary()
        additional_queries = [self.env.cr.mogrify(*account_query.select(
            "account_account.id AS account_id",
            SQL("setweight(to_tsvector(%s, name), 'B') AS document", psql_lang),
        )).decode()]
        query = self._build_predictive_query([('account_id', 'in', account_query)])
        return self._predicted_field(field, query, additional_queries)

    @api.onchange('name')
    def _onchange_name_predictive(self):
        if (self.move_id.quick_edit_mode or self.move_id.move_type == 'in_invoice')and self.name and self.display_type == 'product':
            predict_product = int(self.env['ir.config_parameter'].sudo().get_param('account_predictive_bills.predict_product', '1'))

            if predict_product and not self.product_id and self.company_id.predict_bill_product:
                predicted_product_id = self._predict_product()
                if predicted_product_id and predicted_product_id != self.product_id.id:
                    name = self.name
                    self.product_id = predicted_product_id
                    self.name = name

            # Product may or may not have been set above, if it has been set, account and taxes are set too
            if not self.product_id:
                # Predict account.
                predicted_account_id = self._predict_account()
                if predicted_account_id and predicted_account_id != self.account_id.id:
                    self.account_id = predicted_account_id

                if not self.tax_ids:
                    # Predict taxes
                    predicted_tax_ids = self._predict_taxes()
                    if predicted_tax_ids == [None]:
                        predicted_tax_ids = []
                    if predicted_tax_ids is not False and set(predicted_tax_ids) != set(self.tax_ids.ids):
                        self.tax_ids = self.env['account.tax'].browse(predicted_tax_ids)

    def _read_group_groupby(self, groupby_spec, query):
        # enable grouping by :abs_rounded on fields, which is useful when trying
        # to match positive and negative amounts
        if ':' in groupby_spec:
            fname, method = groupby_spec.split(':')
            if fname in self and method == 'abs_rounded':  # field in self avoids possible injections
                # rounds with the used currency settings
                sql_field = self._field_to_sql(self._table, fname, query)
                currency_alias = query.left_join(self._table, 'currency_id', 'res_currency', 'id', 'currency_id')
                sql_decimal = self.env['res.currency']._field_to_sql(currency_alias, 'decimal_places', query)
                sql_group = SQL('ROUND(ABS(%s), %s)', sql_field, sql_decimal)
                return sql_group, [fname, 'currency_id']

        return super()._read_group_groupby(groupby_spec, query)

    def _read_group_having(self, having_domain, query):
        # Enable to use HAVING clause that sum rounded values depending on the
        # currency precision settings. Limitation: we only handle a having
        # clause of one element with that specific method :sum_rounded.
        if len(having_domain) == 1:
            left, operator, right = having_domain[0]
            fname, *funcs = left.split(':')
            if fname in self and funcs == ['sum_rounded']:  # fname in self avoids possible injections
                sql_field = self._field_to_sql(self._table, fname, query)
                currency_alias = query.left_join(self._table, 'currency_id', 'res_currency', 'id', 'currency_id')
                sql_decimal = self.env['res.currency']._field_to_sql(currency_alias, 'decimal_places', query)
                sql_operator = expression.SQL_OPERATORS[operator]
                sql_expr = SQL(
                    'SUM(ROUND(%s, %s)) %s %s',
                    sql_field, sql_decimal, sql_operator, right,
                )
                return sql_expr, [fname]
        return super()._read_group_having(having_domain, query)

    def turn_as_asset(self):
        ctx = self.env.context.copy()
        ctx.update({
            'default_original_move_line_ids': [(6, False, self.env.context['active_ids'])],
            'default_company_id': self.company_id.id,
        })
        if any(line.move_id.state == 'draft' for line in self):
            raise UserError(_("All the lines should be posted"))
        if any(account != self[0].account_id for account in self.mapped('account_id')):
            raise UserError(_("All the lines should be from the same account"))
        return {
            "name": _("Turn as an asset"),
            "type": "ir.actions.act_window",
            "res_model": "account.asset",
            "views": [[False, "form"]],
            "target": "current",
            "context": ctx,
        }

    @api.depends('tax_ids.invoice_repartition_line_ids')
    def _compute_non_deductible_tax_value(self):
        """ Handle the specific case of non deductible taxes,
        such as "50% Non Déductible - Frais de voiture (Prix Excl.)" in Belgium.
        """
        non_deductible_tax_ids = self.tax_ids.invoice_repartition_line_ids.filtered(
            lambda line: line.repartition_type == 'tax' and not line.use_in_tax_closing
        ).tax_id

        res = {}
        if non_deductible_tax_ids:
            domain = [('move_id', 'in', self.move_id.ids)]
            tax_details_query, tax_details_params = self._get_query_tax_details_from_domain(domain)

            self.flush_model()
            self._cr.execute(f'''
                SELECT
                    tdq.base_line_id,
                    SUM(tdq.tax_amount_currency)
                FROM ({tax_details_query}) AS tdq
                JOIN account_move_line aml ON aml.id = tdq.tax_line_id
                JOIN account_tax_repartition_line trl ON trl.id = tdq.tax_repartition_line_id
                WHERE tdq.base_line_id IN %s
                AND trl.use_in_tax_closing IS FALSE
                GROUP BY tdq.base_line_id
            ''', tax_details_params + [tuple(self.ids)])

            res = {row['base_line_id']: row['sum'] for row in self._cr.dictfetchall()}

        for record in self:
            record.non_deductible_tax_value = res.get(record._origin.id, 0.0)
