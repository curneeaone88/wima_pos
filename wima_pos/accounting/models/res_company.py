

from odoo import models, fields, api, _
from odoo.tools.misc import DEFAULT_SERVER_DATE_FORMAT
from odoo.exceptions import UserError
import itertools

from datetime import timedelta
from dateutil.relativedelta import relativedelta
import datetime as _datetime
from odoo.tools import date_utils
from odoo.tools.misc import format_date


class ResCompany(models.Model):
    _inherit = 'res.company'

    invoicing_switch_threshold = fields.Date(string="Invoicing Switch Threshold", help="Every payment and invoice before this date will receive the 'From Invoicing' status, hiding all the accounting entries related to it. Use this option after installing Accounting if you were using only Invoicing before, before importing all your actual accounting data in to Odoo.")
    predict_bill_product = fields.Boolean(string="Predict Bill Product")

    # Deferred management
    deferred_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string="Deferred Journal",
    )
    deferred_expense_account_id = fields.Many2one(
        comodel_name='account.account',
        string="Deferred Expense",
    )
    deferred_revenue_account_id = fields.Many2one(
        comodel_name='account.account',
        string="Deferred Revenue",
    )
    generate_deferred_expense_entries_method = fields.Selection(
        string="Generate Deferred Expense Entries Method",
        selection=[
            ('on_validation', 'On bill validation'),
            ('manual', 'Manually & Grouped'),
        ],
        default='on_validation',
        required=True,
    )
    generate_deferred_revenue_entries_method = fields.Selection(
        string="Generate Deferred Revenue Entries Method",
        selection=[
            ('on_validation', 'On invoice validation'),
            ('manual', 'Manually & Grouped'),
        ],
        default='on_validation',
        required=True,
    )
    deferred_amount_computation_method = fields.Selection(
        string="Deferred Amount Computation Method",
        selection=[
            ('day', 'Based on days'),
            ('month', 'Equal per month'),
        ],
        default='month',
        required=True,
    )

    gain_account_id = fields.Many2one(
        'account.account',
        domain="[('deprecated', '=', False)]",
        check_company=True,
        help="Account used to write the journal item in case of gain while selling an asset",
    )
    loss_account_id = fields.Many2one(
        'account.account',
        domain="[('deprecated', '=', False)]",
        check_company=True,
        help="Account used to write the journal item in case of loss while selling an asset",
    )

    revenue_realization_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string="Revenue Realization Journal",
    )

    reverse_entry_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string="Reverse Entry Journal",
    )


    def write(self, vals):
        old_threshold_vals = {}
        for record in self:
            old_threshold_vals[record] = record.invoicing_switch_threshold

        rslt = super(ResCompany, self).write(vals)

        for record in self:
            if 'invoicing_switch_threshold' in vals and old_threshold_vals[record] != vals['invoicing_switch_threshold']:
                self.env['account.move.line'].flush_model(['move_id', 'parent_state'])
                self.env['account.move'].flush_model(['company_id', 'date', 'state', 'payment_state', 'payment_state_before_switch'])
                if record.invoicing_switch_threshold:
                    # If a new date was set as threshold, we switch all the
                    # posted moves and payments before it to 'invoicing_legacy'.
                    # We also reset to posted all the moves and payments that
                    # were 'invoicing_legacy' and were posterior to the threshold
                    self.env.cr.execute("""
                        update account_move_line aml
                        set parent_state = 'posted'
                        from account_move move
                        where aml.move_id = move.id
                        and move.payment_state = 'invoicing_legacy'
                        and move.date >= %(switch_threshold)s
                        and move.company_id = %(company_id)s;

                        update account_move
                        set state = 'posted',
                            payment_state = payment_state_before_switch,
                            payment_state_before_switch = null
                        where payment_state = 'invoicing_legacy'
                        and date >= %(switch_threshold)s
                        and company_id = %(company_id)s;

                        update account_move_line aml
                        set parent_state = 'cancel'
                        from account_move move
                        where aml.move_id = move.id
                        and move.state = 'posted'
                        and move.date < %(switch_threshold)s
                        and move.company_id = %(company_id)s;

                        update account_move
                        set state = 'cancel',
                            payment_state_before_switch = payment_state,
                            payment_state = 'invoicing_legacy'
                        where state = 'posted'
                        and date < %(switch_threshold)s
                        and company_id = %(company_id)s;
                    """, {'company_id': record.id, 'switch_threshold': record.invoicing_switch_threshold})
                else:
                    # If the threshold date has been emptied, we re-post all the
                    # invoicing_legacy entries.
                    self.env.cr.execute("""
                        update account_move_line aml
                        set parent_state = 'posted'
                        from account_move move
                        where aml.move_id = move.id
                        and move.payment_state = 'invoicing_legacy'
                        and move.company_id = %(company_id)s;

                        update account_move
                        set state = 'posted',
                            payment_state = payment_state_before_switch,
                            payment_state_before_switch = null
                        where payment_state = 'invoicing_legacy'
                        and company_id = %(company_id)s;
                    """, {'company_id': record.id})

                self.env['account.move.line'].invalidate_model(['parent_state'])
                self.env['account.move'].invalidate_model(['state', 'payment_state', 'payment_state_before_switch'])

        return rslt

    def compute_fiscalyear_dates(self, current_date):
        """Compute the start and end dates of the fiscal year where the given 'date' belongs to.

        :param current_date: A datetime.date/datetime.datetime object.
        :return: A dictionary containing:
            * date_from
            * date_to
            * [Optionally] record: The fiscal year record.
        """
        self.ensure_one()
        date_str = current_date.strftime(DEFAULT_SERVER_DATE_FORMAT)

        # Search a fiscal year record containing the date.
        # If a record is found, then no need further computation, we get the dates range directly.
        fiscalyear = self.env['account.fiscal.year'].search([
            ('company_id', '=', self.id),
            ('date_from', '<=', date_str),
            ('date_to', '>=', date_str),
        ], limit=1)
        if fiscalyear:
            return {
                'date_from': fiscalyear.date_from,
                'date_to': fiscalyear.date_to,
                'record': fiscalyear,
            }

        date_from, date_to = date_utils.get_fiscal_year(
            current_date, day=self.fiscalyear_last_day, month=int(self.fiscalyear_last_month))

        date_from_str = date_from.strftime(DEFAULT_SERVER_DATE_FORMAT)
        date_to_str = date_to.strftime(DEFAULT_SERVER_DATE_FORMAT)

        # Search for fiscal year records reducing the delta between the date_from/date_to.
        # This case could happen if there is a gap between two fiscal year records.
        # E.g. two fiscal year records: 2017-01-01 -> 2017-02-01 and 2017-03-01 -> 2017-12-31.
        # => The period 2017-02-02 - 2017-02-30 is not covered by a fiscal year record.

        fiscalyear_from = self.env['account.fiscal.year'].search([
            ('company_id', '=', self.id),
            ('date_from', '<=', date_from_str),
            ('date_to', '>=', date_from_str),
        ], limit=1)
        if fiscalyear_from:
            date_from = fiscalyear_from.date_to + timedelta(days=1)

        fiscalyear_to = self.env['account.fiscal.year'].search([
            ('company_id', '=', self.id),
            ('date_from', '<=', date_to_str),
            ('date_to', '>=', date_to_str),
        ], limit=1)
        if fiscalyear_to:
            date_to = fiscalyear_to.date_from - timedelta(days=1)

        return {'date_from': date_from, 'date_to': date_to}

    def _get_fiscalyear_lock_statement_lines_redirect_action(self, unreconciled_statement_lines):
        # OVERRIDE account
        return self.env['account.bank.statement.line']._action_open_bank_reconciliation_widget(
            extra_domain=[('id', 'in', unreconciled_statement_lines.ids)],
            name=_('Unreconciled statements lines'),
        )


class ResCompanyInherit(models.Model):
    _inherit = "res.company"

    totals_below_sections = fields.Boolean(
        string='Add totals below sections',
        help='When ticked, totals and subtotals appear below the sections of the report.')
    account_tax_periodicity = fields.Selection([
        ('year', 'annually'),
        ('semester', 'semi-annually'),
        ('4_months', 'every 4 months'),
        ('trimester', 'quarterly'),
        ('2_months', 'every 2 months'),
        ('monthly', 'monthly')], string="Delay units", help="Periodicity", default='monthly', required=True)
    account_tax_periodicity_reminder_day = fields.Integer(string='Start from', default=7, required=True)
    account_tax_periodicity_journal_id = fields.Many2one('account.journal', string='Journal', domain=[('type', '=', 'general')], check_company=True)
    account_revaluation_journal_id = fields.Many2one('account.journal', domain=[('type', '=', 'general')], check_company=True)
    account_revaluation_expense_provision_account_id = fields.Many2one('account.account', string='Expense Provision Account', check_company=True)
    account_revaluation_income_provision_account_id = fields.Many2one('account.account', string='Income Provision Account', check_company=True)
    account_tax_unit_ids = fields.Many2many(string="Tax Units", comodel_name='account.tax.unit', help="The tax units this company belongs to.")
    account_representative_id = fields.Many2one('res.partner', string='Accounting Firm',
                                                help="Specify an Accounting Firm that will act as a representative when exporting reports.")
    account_display_representative_field = fields.Boolean(compute='_compute_account_display_representative_field')
    account_receivable_responsible = fields.Many2one('res.users', string='Account Receivable Responsible')

    @api.depends('account_fiscal_country_id.code')
    def _compute_account_display_representative_field(self):
        country_set = self._get_countries_allowing_tax_representative()
        for record in self:
            record.account_display_representative_field = record.account_fiscal_country_id.code in country_set

    def _get_countries_allowing_tax_representative(self):
        """ Returns a set containing the country codes of the countries for which
        it is possible to use a representative to submit the tax report.
        This function is a hook that needs to be overridden in localisation modules.
        """
        return set()

    def _get_default_misc_journal(self):
        """ Returns a default 'miscellanous' journal to use for
        account_tax_periodicity_journal_id field. This is useful in case a
        CoA was already installed on the company at the time the module
        is installed, so that the field is set automatically when added."""
        return self.env['account.journal'].search([
            *self.env['account.journal']._check_company_domain(self),
            ('type', '=', 'general'),
            ('show_on_dashboard', '=', True),
        ], limit=1)

    def write(self, values):
        tax_closing_update_dependencies = ('account_tax_periodicity', 'account_tax_periodicity_reminder_day', 'account_tax_periodicity_journal_id.id')
        to_update = self.env['res.company']
        for company in self:
            if company.account_tax_periodicity_journal_id:

                need_tax_closing_update = any(
                    update_dep in values and company.mapped(update_dep)[0] != values[update_dep]
                    for update_dep in tax_closing_update_dependencies
                )

                if need_tax_closing_update:
                    to_update += company

        res = super().write(values)

        for update_company in to_update:
            update_company._update_tax_closing_after_periodicity_change()

        return res

    def _update_tax_closing_after_periodicity_change(self):
        self.ensure_one()

        vat_fiscal_positions = self.env['account.fiscal.position'].search([
            ('company_id', '=', self.id),
            ('foreign_vat', '!=', False),
        ])

        self._get_and_update_tax_closing_moves(fields.Date.today(), vat_fiscal_positions, include_domestic=True)

    def _get_and_update_tax_closing_moves(self, in_period_date, fiscal_positions=None, include_domestic=False):
        """ Searches for tax closing moves. If some are missing for the provided parameters,
        they are created in draft state. Also, existing moves get updated in case of configuration changes
        (closing journal or periodicity, for example). Note the content of these moves stays untouched.

        :param in_period_date: A date within the tax closing period we want the closing for.
        :param fiscal_positions: The fiscal positions we want to generate the closing for (as a recordset).
        :param include_domestic: Whether or not the domestic closing (i.e. the one without any fiscal_position_id) must be included

        :return: The closing moves, as a recordset.
        """
        self.ensure_one()

        if not fiscal_positions:
            fiscal_positions = []

        # Compute period dates depending on the date
        period_start, period_end = self._get_tax_closing_period_boundaries(in_period_date)
        activity_deadline = period_end + relativedelta(days=self.account_tax_periodicity_reminder_day)

        # Search for an existing tax closing move
        tax_closing_activity_type = self.env.ref('wima_pos.tax_closing_activity_type', raise_if_not_found=False)
        tax_closing_activity_type_id = tax_closing_activity_type.id if tax_closing_activity_type else False

        all_closing_moves = self.env['account.move']
        for fpos in itertools.chain(fiscal_positions, [None] if include_domestic else []):

            tax_closing_move = self.env['account.move'].search([
                ('state', '=', 'draft'),
                ('company_id', '=', self.id),
                ('activity_ids.activity_type_id', '=', tax_closing_activity_type_id),
                ('tax_closing_end_date', '>=', period_start),
                ('fiscal_position_id', '=', fpos.id if fpos else None),
            ])

            # This should never happen, but can be caused by wrong manual operations
            if len(tax_closing_move) > 1:
                if fpos:
                    error = _("Multiple draft tax closing entries exist for fiscal position %s after %s. There should be at most one. \n %s")
                    params = (fpos.name, period_start, tax_closing_move.mapped('display_name'))

                else:
                    error = _("Multiple draft tax closing entries exist for your domestic region after %s. There should be at most one. \n %s")
                    params = (period_start, tax_closing_move.mapped('display_name'))

                raise UserError(error % params)

            # Compute tax closing description
            ref = self._get_tax_closing_move_description(self.account_tax_periodicity, period_start, period_end, fpos)

            # Values for update/creation of closing move
            closing_vals = {
                'company_id': self.id,# Important to specify together with the journal, for branches
                'journal_id': self.account_tax_periodicity_journal_id.id,
                'date': period_end,
                'tax_closing_end_date': period_end,
                'fiscal_position_id': fpos.id if fpos else None,
                'ref': ref,
                'name': '/', # Explicitly set a void name so that we don't set the sequence for the journal and don't consume a sequence number
            }

            if tax_closing_move:
                # Update the next activity on the existing move
                for act in tax_closing_move.activity_ids:
                    if act.activity_type_id.id == tax_closing_activity_type_id:
                        act.write({'date_deadline': activity_deadline})

                tax_closing_move.write(closing_vals)
            else:
                # Create a new, empty, tax closing move
                tax_closing_move = self.env['account.move'].create(closing_vals)

                group_account_manager = self.env.ref('account.group_account_manager')
                advisor_user = tax_closing_activity_type.default_user_id if tax_closing_activity_type else self.env['res.users']
                if advisor_user and not (self in advisor_user.company_ids and group_account_manager in advisor_user.groups_id):
                    advisor_user = self.env['res.users']

                if not advisor_user:
                    advisor_user = self.env['res.users'].search(
                        [('company_ids', 'in', self.ids), ('groups_id', 'in', group_account_manager.ids)],
                        limit=1, order="id ASC")

                self.env['mail.activity'].with_context(mail_activity_quick_update=True).create({
                    'res_id': tax_closing_move.id,
                    'res_model_id': self.env['ir.model']._get_id('account.move'),
                    'activity_type_id': tax_closing_activity_type_id,
                    'date_deadline': activity_deadline,
                    'automated': True,
                    'user_id':  advisor_user.id or self.env.user.id
                })

            all_closing_moves += tax_closing_move

        return all_closing_moves

    def _get_tax_closing_move_description(self, periodicity, period_start, period_end, fiscal_position):
        """ Returns a string description of the provided period dates, with the
        given tax periodicity.
        """
        self.ensure_one()

        foreign_vat_fpos_count = self.env['account.fiscal.position'].search_count([
            ('company_id', '=', self.id),
            ('foreign_vat', '!=', False)
        ])
        if foreign_vat_fpos_count:
            if fiscal_position:
                country_code = fiscal_position.country_id.code
                state_codes = fiscal_position.mapped('state_ids.code') if fiscal_position.state_ids else []
            else:
                # On domestic country
                country_code = self.account_fiscal_country_id.code

                # Only consider the state in case there are foreign VAT fpos on states in this country
                vat_fpos_with_state_count = self.env['account.fiscal.position'].search_count([
                    ('company_id', '=', self.id),
                    ('foreign_vat', '!=', False),
                    ('country_id', '=', self.account_fiscal_country_id.id),
                    ('state_ids', '!=', False),
                ])
                state_codes = [self.state_id.code] if vat_fpos_with_state_count else []

            if state_codes:
                region_string = " (%s - %s)" % (country_code, ', '.join(state_codes))
            else:
                region_string = " (%s)" % country_code
        else:
            # Don't add region information in case there is no foreign VAT fpos
            region_string = ''

        if periodicity == 'year':
            return _("Tax return for %s%s", period_start.year, region_string)
        elif periodicity == 'trimester':
            return _("Tax return for %s%s", format_date(self.env, period_start, date_format='qqq yyyy'), region_string)
        elif periodicity == 'monthly':
            return _("Tax return for %s%s", format_date(self.env, period_start, date_format='LLLL yyyy'), region_string)
        else:
            return _("Tax return from %s to %s%s", format_date(self.env, period_start), format_date(self.env, period_end), region_string)

    def _get_tax_closing_period_boundaries(self, date):
        """ Returns the boundaries of the tax period containing the provided date
        for this company, as a tuple (start, end).
        """
        self.ensure_one()
        period_months = self._get_tax_periodicity_months_delay()
        period_number = (date.month//period_months) + (1 if date.month % period_months != 0 else 0)
        end_date = date_utils.end_of(_datetime.date(date.year, period_number * period_months, 1), 'month')
        start_date = end_date + relativedelta(day=1, months=-period_months + 1)

        return start_date, end_date

    def _get_tax_periodicity_months_delay(self):
        """ Returns the number of months separating two tax returns with the provided periodicity
        """
        self.ensure_one()
        periodicities = {
            'year': 12,
            'semester': 6,
            '4_months': 4,
            'trimester': 3,
            '2_months': 2,
            'monthly': 1,
        }
        return periodicities[self.account_tax_periodicity]

    def  _get_branches_with_same_vat(self, accessible_only=False):
        """ Returns all companies among self and its branch hierachy (considering children and parents) that share the same VAT number
        as self. An empty VAT number is considered as being the same as the one of the closest parent with a VAT number.

        self is always returned as the first element of the resulting recordset (so that this can safely be used to restore the active company).

        Example:
        - main company ; vat = 123
            - branch 1
                - branch 1_1
            - branch 2 ; vat = 456
                - branch 2_1 ; vat = 789
                - branch 2_2

        In this example, the following VAT numbers will be considered for each company:
        - main company: 123
        - branch 1: 123
        - branch 1_1: 123
        - branch 2: 456
        - branch 2_1: 789
        - branch 2_2: 456

        :param accessible_only: whether the returned companies should exclude companies that are not in self.env.companies
        """
        self.ensure_one()

        current = self.sudo()
        same_vat_branch_ids = [current.id] # Current is always available
        current_strict_parents = current.parent_ids - current
        if accessible_only:
            candidate_branches = current.root_id._accessible_branches()
        else:
            candidate_branches = self.env['res.company'].sudo().search([('id', 'child_of', current.root_id.ids)])

        current_vat_check_set = {current.vat} if current.vat else set()
        for branch in candidate_branches - current:
            parents_vat_set = set(filter(None, (branch.parent_ids - current_strict_parents).mapped('vat')))
            if parents_vat_set == current_vat_check_set:
                # If all the branches between the active company and branch (both included) share the same VAT number as the active company,
                # we want to add the branch to the selection.
                same_vat_branch_ids.append(branch.id)

        return self.browse(same_vat_branch_ids)