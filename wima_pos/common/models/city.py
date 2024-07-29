from odoo import fields, models, api


class ResCountryCity(models.Model):
    _name = 'res.country.city'
    _description = 'Master data for City'

    name = fields.Char("City Name")
    state_id = fields.Many2one("res.country.state", string="State")
    country_id = fields.Many2one(related='state_id.country_id')


