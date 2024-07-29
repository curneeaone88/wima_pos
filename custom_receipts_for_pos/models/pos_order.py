from odoo import fields, models, api

class PosOrder(models.Model):
    _inherit = 'pos.order'

    total_items = fields.Integer(string="Total Items", compute="_compute_total_items")

    @api.depends('lines.qty')
    def _compute_total_items(self):
        for order in self:
            order.total_items = sum(line.qty for line in order.lines)
