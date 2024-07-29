from odoo import fields, models, api
from jinja2 import Template


class WizardExternalTemplate(models.TransientModel):
    _name = 'wizard.external.template'

    @api.model
    def _selection_target_model(self):
        return [(model.model, model.name) for model in self.env['ir.model'].sudo().search([])]


    external_template_id = fields.Many2one("external.template")
    resource_ref = fields.Reference(
        string='Record',
        compute='_compute_resource_ref',
        compute_sudo=False, readonly=False,
        selection='_selection_target_model',
        store=True
    )
    preview_template = fields.Html()

    @api.depends('external_template_id')
    def _compute_resource_ref(self):
        for preview in self:
            external_template = preview.external_template_id.sudo()
            model = external_template.model
            res = self.env[model].search([], limit=1)
            preview.resource_ref = f'{model},{res.id}' if res else False

    @api.onchange("resource_ref")
    def _onchange_resource_ref(self):
        if self.resource_ref:
            self.preview_template = self.external_template_id.render_template(self.resource_ref)
        else:
            self.preview_template = self.external_template_id.content
